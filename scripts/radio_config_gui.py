"""CustomTkinter GUI for actual UV-Pro radio control: browse and edit
channel memories, set the dual-watch A/B pointers, and view live radio
status (battery/signal/current channel/GPS).

This is a different tool from kiss_gui.py, which only manages the
Linux-side Bluetooth/KISS-bridge connection lifecycle and never talks
the radio's own command protocol. This GUI *does* talk that protocol
directly (via benlink, same as channel_control.py/radio_info.py), so it
needs the radio's one RFCOMM connection for itself -- kiss_bridge.py
must not be running at the same time. Unlike the one-shot CLI scripts,
it holds that connection open for as long as you're connected here
(in a background asyncio event loop thread), so browsing/editing
multiple channels doesn't pay a reconnect cost -- and doesn't risk the
radio's "won't reconnect right after a disconnect" quirk -- for every
single action.

Run via scripts/start_radio_config_gui.sh (always through the venv, not
a bare python3 -- see kiss_gui.py's docstring for why).
"""

import asyncio
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import customtkinter as ctk

import patches  # noqa: F401 (applies protocol compatibility patches on import)
from gui_common import (
    MAC_RE,
    disambiguate,
    format_info,
    load_remembered_device,
    load_ui_state,
    make_textbox_readonly,
    pgrep,
    save_remembered_device,
    save_ui_state,
)
from radio_config import DEFAULT_DEVICE_UUID
from radio_connect import connect_rfcomm
from radio_info import channel_at, channel_summary

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"

GREEN = "#2fa84f"
RED = "#c0392b"
GRAY = "#888888"

# (key, kind, choices) -- kind is one of "str", "float", "choice", "tone", "bool".
# Matches channel_control.py's editable-field set exactly.
CHANNEL_FIELDS = [
    ("name", "str", None),
    ("tx_freq", "float", None),
    ("rx_freq", "float", None),
    ("tx_mod", "choice", ["AM", "FM", "DMR"]),
    ("rx_mod", "choice", ["AM", "FM", "DMR"]),
    ("bandwidth", "choice", ["NARROW", "WIDE"]),
    ("tx_sub_audio", "tone", None),
    ("rx_sub_audio", "tone", None),
]
CHANNEL_BOOL_FIELDS = [
    "scan", "talk_around", "pre_de_emph_bypass", "sign", "tx_disable",
    "fixed_freq", "fixed_bandwidth", "fixed_tx_power", "mute",
]
# tx_at_max_power/tx_at_med_power are handled as one "TX Power" combobox
# (Low/Medium/High) instead of two separate checkboxes -- only one should
# ever be true at a time, so a single control is both clearer and can't
# represent the invalid high+medium-both-true state.
TX_POWER_CHOICES = ["low", "medium", "high"]
DUAL_WATCH_CHOICES = ["OFF", "A", "B"]

TREE_COLUMNS = ("id", "name", "tx_freq", "rx_freq", "mod", "bw", "power", "scan")
TREE_HEADINGS = {
    "id": "ID", "name": "Name", "tx_freq": "TX Freq", "rx_freq": "RX Freq",
    "mod": "Mod", "bw": "BW", "power": "Power", "scan": "Scan",
}
TREE_WIDTHS = {
    "id": 40, "name": 130, "tx_freq": 85, "rx_freq": 85,
    "mod": 50, "bw": 65, "power": 65, "scan": 50,
}


def _power_of(channel: dict) -> str:
    if channel["tx_at_max_power"]:
        return "high"
    if channel["tx_at_med_power"]:
        return "medium"
    return "low"


# ---------- Persistent radio session (runs in a background asyncio loop) ----------

class AsyncLoop(threading.Thread):
    """A background thread running its own asyncio event loop, so the
    Tkinter main loop is never blocked by radio I/O. Submit coroutines
    with .submit(); results/exceptions come back via the returned
    concurrent.futures.Future (add a done-callback and hop back to the
    main thread with widget.after(0, ...) -- never touch Tk widgets from
    this thread directly)."""

    def __init__(self):
        super().__init__(daemon=True)
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()

    def run(self):
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()

    def start_and_wait(self):
        self.start()
        self._ready.wait()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)


class RadioSession:
    """Holds one RadioController connection open across multiple calls.
    All methods must run on the AsyncLoop thread (via .submit())."""

    def __init__(self):
        self._cm = None
        self.radio = None

    @property
    def connected(self) -> bool:
        return self.radio is not None

    async def connect(self, addr: str) -> None:
        if self.radio is not None:
            raise RuntimeError("Already connected")
        cm = connect_rfcomm(addr, "auto")
        self.radio = await cm.__aenter__()
        self._cm = cm

    async def disconnect(self) -> None:
        if self._cm is None:
            return
        cm, self._cm = self._cm, None
        self.radio = None
        await cm.__aexit__(None, None, None)

    async def list_channels(self) -> list[dict]:
        return [c.model_dump(mode="json") for c in self.radio.channels]

    async def edit_channel(self, channel_id: int, **kwargs) -> dict:
        await self.radio.set_channel(channel_id, **kwargs)
        return self.radio.channels[channel_id].model_dump(mode="json")

    async def set_watch_channel(self, slot: str, channel_id: int) -> None:
        await self.radio.set_settings(**{f"channel_{slot.lower()}": channel_id})

    async def set_dual_watch_active(self, slot: str) -> None:
        # double_channel: 0=OFF, 1=A, 2=B (benlink.protocol.ChannelType).
        # This is what actually turns dual-watch on/off; channel_a/
        # channel_b (set_watch_channel above) only pick which channel
        # each slot points to.
        value = {"OFF": 0, "A": 1, "B": 2}[slot]
        await self.radio.set_settings(double_channel=value)

    async def get_status(self) -> dict:
        status = self.radio.status
        settings = self.radio.settings
        channels = self.radio.channels
        result = {
            "device_info": self.radio.device_info.model_dump(mode="json"),
            "battery_percent": await self.radio.battery_level_as_percentage(),
            "signal_strength_rssi": status.rssi,
            "gps": None,
            "current_channel": channel_summary(channel_at(channels, status.curr_ch_id)),
            "dual_watch": {
                "active_slot": status.double_channel,
                "channel_a": channel_summary(channel_at(channels, settings.channel_a)),
                "channel_b": channel_summary(channel_at(channels, settings.channel_b)),
            },
        }
        if status.is_gps_locked:
            result["gps"] = (await self.radio.position()).model_dump(mode="json")
        return result


# ---------- GUI ----------

class RadioConfigGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("UV-Pro Radio Configuration")
        self._ui_state = load_ui_state("radio_config_gui")
        self.geometry(self._ui_state.get("geometry", "780x900"))
        self.minsize(680, 640)

        ctk.set_appearance_mode("system")

        self._scan_map: dict[str, str] = {}
        self._remembered = load_remembered_device() or (DEFAULT_DEVICE_UUID, DEFAULT_DEVICE_UUID)
        self._scan_map = {self._remembered[0]: self._remembered[1]}
        self._connected = False
        self._channels_by_id: dict[int, dict] = {}
        self._loaded_channel: dict | None = None
        self._field_vars: dict[str, tk.Variable] = {}
        self._field_kind: dict[str, str] = {}
        self._field_widgets: dict[str, object] = {}
        self._panes: list[dict] = []  # registered by _make_pane, for layout save/restore

        self._session = RadioSession()
        self._loop = AsyncLoop()
        self._loop.start_and_wait()

        # Outer layout: a Connection/Channels tab pair (too much content to
        # show usefully all at once) plus a Log panel that stays visible
        # regardless of which tab is active, in an outer vertical
        # PanedWindow so Log can still be resized/collapsed against the
        # tabview. Each tab holds its own inner PanedWindow so its
        # sections are independently resizable/collapsible too.
        self.outer_paned = tk.PanedWindow(
            self, orient="vertical", sashrelief="raised", sashwidth=6,
            bg=self.cget("bg"), bd=0,
        )
        self.outer_paned.pack(fill="both", expand=True, padx=10, pady=10)

        self.tabview = ctk.CTkTabview(self.outer_paned)
        self.outer_paned.add(self.tabview, minsize=300, stretch="always")
        connection_tab = self.tabview.add("Connection")
        channels_tab = self.tabview.add("Channels")

        self.connection_paned = tk.PanedWindow(
            connection_tab, orient="vertical", sashrelief="raised", sashwidth=6,
            bg=self.cget("bg"), bd=0,
        )
        self.connection_paned.pack(fill="both", expand=True)

        self.channels_paned = tk.PanedWindow(
            channels_tab, orient="vertical", sashrelief="raised", sashwidth=6,
            bg=self.cget("bg"), bd=0,
        )
        self.channels_paned.pack(fill="both", expand=True)

        self._build_device_frame()
        self._build_status_frame()
        self._build_dual_watch_frame()
        self._build_channel_list_frame()
        self._build_channel_edit_frame()
        self._build_log_frame()

        self._set_connected_ui(False)
        self._restore_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- Async plumbing ----------

    def _submit(self, coro, on_success=None, on_error=None):
        fut = self._loop.submit(coro)

        def _done(f):
            try:
                result = f.result()
            except Exception as e:
                # `e` is unbound again the moment this except block exits
                # (Python implicitly deletes it) -- rebind to a plain local
                # so the lambda below, which only runs on the next mainloop
                # tick via self.after(), has something to close over.
                err = e
                if on_error:
                    self.after(0, lambda: on_error(err))
                else:
                    self.after(0, lambda: self._log(f"Error: {err}\n"))
                return
            if on_success:
                self.after(0, lambda: on_success(result))

        fut.add_done_callback(_done)

    def _on_close(self):
        self._save_layout()
        if self._connected:
            try:
                self._loop.submit(self._session.disconnect()).result(timeout=5)
            except Exception:
                pass
        self._loop.stop()
        self.destroy()

    # ---------- Collapsible panes ----------

    def _make_pane(
        self, paned: tk.PanedWindow, title: str, minsize: int,
        stretch: str = "never", key: str | None = None,
    ):
        """Add a pane to `paned` (one of self.outer_paned/connection_paned/
        channels_paned) with a header row (collapse toggle + bold title --
        pack more controls into the returned header with
        side="left"/"right") and a body frame for the section's actual
        content (grid or pack into the returned body). Collapsing hides
        the body and shrinks the pane to just the header row; expanding
        restores it to `minsize`.

        `key` is the identity used for layout persistence (_save_layout/
        _restore_layout key collapsed-state by it) and defaults to
        `title` -- pass an explicit, distinct `key` if two panes end up
        with the same displayed title (e.g. across different tabs), so
        their saved collapse states don't collide."""
        pane = ctk.CTkFrame(paned)
        paned.add(pane, minsize=minsize, stretch=stretch)

        header = ctk.CTkFrame(pane, fg_color="transparent")
        header.pack(fill="x", padx=10, pady=(10, 0))

        toggle_btn = ctk.CTkButton(header, text="▼", width=24, command=lambda: None)
        toggle_btn.pack(side="left", padx=(0, 6))
        ctk.CTkLabel(header, text=title, font=ctk.CTkFont(weight="bold")).pack(side="left")

        body = ctk.CTkFrame(pane, fg_color="transparent")
        body.pack(fill="both", expand=True)

        toggle_btn.configure(
            command=lambda: self._toggle_pane(paned, pane, body, toggle_btn, minsize)
        )
        self._panes.append({
            "paned": paned, "pane": pane, "body": body, "toggle_btn": toggle_btn,
            "title": key or title, "minsize": minsize,
        })
        return header, body

    def _toggle_pane(self, paned: tk.PanedWindow, pane, body, toggle_btn, expanded_minsize: int):
        if body.winfo_ismapped():
            body.pack_forget()
            toggle_btn.configure(text="▶")
            pane.update_idletasks()
            collapsed_height = pane.winfo_reqheight()
            paned.paneconfigure(pane, minsize=collapsed_height, height=collapsed_height)
        else:
            body.pack(fill="both", expand=True)
            toggle_btn.configure(text="▼")
            paned.paneconfigure(pane, minsize=expanded_minsize)

    # ---------- Layout persistence ----------

    def _named_paned_windows(self):
        return (
            ("outer", self.outer_paned),
            ("connection", self.connection_paned),
            ("channels", self.channels_paned),
        )

    def _save_layout(self):
        state = {
            "geometry": self.geometry(),
            "active_tab": self.tabview.get(),
            "collapsed": {
                entry["title"]: not entry["body"].winfo_ismapped()
                for entry in self._panes
            },
            "sashes": {
                name: [list(paned.sash_coord(i)) for i in range(len(paned.panes()) - 1)]
                for name, paned in self._named_paned_windows()
            },
        }
        save_ui_state("radio_config_gui", state)

    def _restore_layout(self):
        state = self._ui_state
        if not state:
            return

        collapsed = state.get("collapsed", {})
        for entry in self._panes:
            if collapsed.get(entry["title"]):
                self._toggle_pane(
                    entry["paned"], entry["pane"], entry["body"],
                    entry["toggle_btn"], entry["minsize"],
                )

        # Sash positions only mean anything once the window has its real
        # on-screen size (set via geometry() in __init__, applied here).
        self.update_idletasks()
        sashes = state.get("sashes", {})
        for name, paned in self._named_paned_windows():
            for i, coord in enumerate(sashes.get(name, [])):
                try:
                    paned.sash_place(i, coord[0], coord[1])
                except Exception:
                    pass

        active_tab = state.get("active_tab")
        if active_tab:
            try:
                self.tabview.set(active_tab)
            except Exception:
                pass

    # ---------- Device selection / connection ----------

    def _build_device_frame(self):
        header, body = self._make_pane(self.connection_paned, "Radio device", minsize=110)
        body.grid_columnconfigure(0, weight=1)

        label, addr = self._remembered
        # Starts showing the remembered device directly: at this point
        # there's only ever one candidate (no scan has run yet), so
        # there's no ambiguity to hide by blanking it -- see
        # _maybe_show_only_device.
        self.device_var = ctk.StringVar(value=label)
        self.device_combo = ttk.Combobox(
            body, textvariable=self.device_var, values=[label], width=30,
        )
        self.device_combo.grid(row=0, column=0, padx=10, pady=10, sticky="w")
        self.device_combo.bind("<<ComboboxSelected>>", lambda e: self._on_device_selected())

        self.connect_button = ctk.CTkButton(
            body, text="Connect", command=self._on_connect_toggle, width=100
        )
        self.connect_button.grid(row=0, column=1, padx=(0, 10), pady=10)

        self.scan_button = ctk.CTkButton(
            body, text="Scan for devices", command=self._on_scan, width=140
        )
        self.scan_button.grid(row=0, column=2, padx=(0, 10), pady=10)

        self.conn_status = ctk.CTkLabel(body, text="Not connected.", text_color=GRAY)
        self.conn_status.grid(row=1, column=0, columnspan=3, padx=10, pady=(0, 10), sticky="w")

    def _on_scan(self):
        self.scan_button.configure(state="disabled")
        self.conn_status.configure(text="Scanning (10s)...", text_color=GRAY)
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        devices: list[tuple[str, str]] = []
        error = None
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "scan_ble.py")],
                capture_output=True, text=True, timeout=30,
            )
            for line in result.stdout.splitlines():
                m = MAC_RE.match(line.strip())
                if m:
                    addr, name = m.group(1), m.group(2)
                    label = name if name and name != "(unknown name)" else addr
                    devices.append((label, addr))
        except Exception as e:
            error = str(e)
        self.after(0, lambda: self._scan_done(disambiguate(devices), error))

    def _scan_done(self, devices, error):
        self.scan_button.configure(state="normal")
        if error:
            self.conn_status.configure(text=f"Scan failed: {error}", text_color=RED)
            return
        if not devices:
            self.conn_status.configure(text="No devices found.", text_color=RED)
            return
        labels = [label for label, _ in devices]
        self._scan_map = dict(devices)
        if self._remembered[0] not in self._scan_map:
            self._scan_map[self._remembered[0]] = self._remembered[1]
            labels.append(self._remembered[0])
        self.device_combo.configure(values=labels)
        self._maybe_show_only_device(labels)
        self.conn_status.configure(
            text=f"Found {len(devices)} device(s) -- pick one from the list.",
            text_color=GREEN,
        )

    def _maybe_show_only_device(self, labels: list[str]):
        # Only auto-fill when there's exactly one candidate at all -- no
        # ambiguity to wait out. Never overrides an existing non-blank
        # selection (e.g. one the user already picked/typed).
        if len(labels) == 1 and not self.device_var.get().strip():
            self.device_combo.set(labels[0])
            self._on_device_selected()

    def _selected_address(self) -> str:
        value = self.device_var.get().strip()
        if value in self._scan_map:
            return self._scan_map[value]
        m = MAC_RE.match(value)
        if m:
            return m.group(1)
        if not value and self._remembered:
            return self._remembered[1]
        return value

    def _on_device_selected(self):
        value = self.device_var.get().strip()
        if value:
            addr = self._selected_address()
            if MAC_RE.match(addr):
                save_remembered_device(value, addr)
                self._remembered = (value, addr)

    def _on_connect_toggle(self):
        if self._connected:
            self._on_disconnect()
        else:
            self._on_connect()

    def _on_connect(self):
        addr = self._selected_address()
        if not MAC_RE.match(addr):
            self._log("No valid device selected -- scan first.\n")
            return
        if pgrep(r"kiss_bridge\.py"):
            self._log(
                "Stop the KISS bridge first (scripts/stop_radio.sh or kiss_gui.py) --"
                " the radio only allows one connection at a time.\n"
            )
            return
        self.connect_button.configure(state="disabled")
        self.conn_status.configure(text="Connecting...", text_color=GRAY)
        self._log(f"Connecting to {addr}...\n")
        self._submit(
            self._session.connect(addr),
            on_success=lambda _: self._on_connected(),
            on_error=self._on_connect_failed,
        )

    def _on_connected(self):
        self._set_connected_ui(True)
        self.conn_status.configure(text="Connected.", text_color=GREEN)
        self._log("Connected.\n")
        self._on_refresh_status()

    def _on_connect_failed(self, error):
        self.connect_button.configure(state="normal")
        self.conn_status.configure(text=f"Connect failed: {error}", text_color=RED)
        self._log(f"Connect failed: {error}\n")

    def _on_disconnect(self):
        self.connect_button.configure(state="disabled")
        self._submit(
            self._session.disconnect(),
            on_success=lambda _: self._on_disconnected(),
            on_error=self._on_connect_failed,
        )

    def _on_disconnected(self):
        self._set_connected_ui(False)
        self.conn_status.configure(text="Disconnected.", text_color=GRAY)
        self._log("Disconnected.\n")

    def _set_connected_ui(self, connected: bool):
        self._connected = connected
        self.connect_button.configure(
            text="Disconnect" if connected else "Connect", state="normal",
        )
        state = "normal" if connected else "disabled"
        for widget in (
            self.load_channels_btn, self.save_channel_btn,
            self.set_watch_a_btn, self.set_watch_b_btn,
            self.set_dual_watch_active_btn, self.refresh_status_btn,
        ):
            widget.configure(state=state)

    # ---------- Channel list ----------

    def _build_channel_list_frame(self):
        header, body = self._make_pane(self.channels_paned, "Channels", minsize=150, stretch="always")
        self.load_channels_btn = ctk.CTkButton(
            header, text="Load Channels", command=self._on_load_channels, width=130
        )
        self.load_channels_btn.pack(side="right")

        tree_frame = ctk.CTkFrame(body, fg_color="transparent")
        tree_frame.pack(fill="both", expand=True, padx=10, pady=10)

        self.tree = ttk.Treeview(
            tree_frame, columns=TREE_COLUMNS, show="headings", height=6,
        )
        for col in TREE_COLUMNS:
            self.tree.heading(col, text=TREE_HEADINGS[col])
            self.tree.column(col, width=TREE_WIDTHS[col], anchor="w")
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

    def _on_load_channels(self):
        self.load_channels_btn.configure(state="disabled")
        self._log("Loading channels...\n")
        self._submit(
            self._session.list_channels(),
            on_success=self._on_channels_loaded,
            on_error=self._on_load_channels_failed,
        )

    def _on_channels_loaded(self, channels: list[dict]):
        self.load_channels_btn.configure(state="normal")
        self._channels_by_id = {c["channel_id"]: c for c in channels}
        self.tree.delete(*self.tree.get_children())
        for c in channels:
            self.tree.insert(
                "", "end", iid=str(c["channel_id"]),
                values=(
                    c["channel_id"], c["name"], f"{c['tx_freq']:.4f}",
                    f"{c['rx_freq']:.4f}", c["tx_mod"], c["bandwidth"],
                    _power_of(c), "Y" if c["scan"] else "",
                ),
            )
        if channels:
            max_id = max(c["channel_id"] for c in channels)
            self.watch_a_spinbox.configure(to=max_id)
            self.watch_b_spinbox.configure(to=max_id)
        self._log(f"Loaded {len(channels)} channels.\n")

    def _on_load_channels_failed(self, error):
        self.load_channels_btn.configure(state="normal")
        self._log(f"Load channels failed: {error}\n")

    def _on_tree_select(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        channel = self._channels_by_id.get(int(sel[0]))
        if channel:
            self._load_channel_into_form(channel)

    # ---------- Channel edit form ----------

    def _build_channel_edit_frame(self):
        header, body = self._make_pane(self.channels_paned, "Edit channel", minsize=340)
        self.edit_channel_label = ctk.CTkLabel(header, text="(none selected)", text_color=GRAY)
        self.edit_channel_label.pack(side="left", padx=10)
        self.save_channel_btn = ctk.CTkButton(
            header, text="Save Channel", command=self._on_save_channel, width=130
        )
        self.save_channel_btn.pack(side="right")

        fields_frame = ctk.CTkFrame(body, fg_color="transparent")
        fields_frame.pack(fill="x", padx=10, pady=10)

        # 3 columns of (label, widget) pairs -- wraps CHANNEL_FIELDS plus
        # the synthetic tx_power field (below) into a compact grid instead
        # of one long vertical list.
        FIELD_COLS = 3

        def _place_field(index: int, key: str, label_widget, value_widget):
            row, col = index // FIELD_COLS, index % FIELD_COLS
            label_widget.grid(row=row, column=col * 2, sticky="w", padx=(0, 8), pady=3)
            value_widget.grid(row=row, column=col * 2 + 1, sticky="w", padx=(0, 16), pady=3)

        for i, (key, kind, choices) in enumerate(CHANNEL_FIELDS):
            if kind == "choice":
                var = tk.StringVar()
                widget = ttk.Combobox(
                    fields_frame, textvariable=var, values=choices,
                    state="readonly", width=9,
                )
            else:
                var = tk.StringVar()
                widget = ctk.CTkEntry(fields_frame, textvariable=var, width=110)
            _place_field(i, key, ctk.CTkLabel(fields_frame, text=key), widget)
            self._field_vars[key] = var
            self._field_kind[key] = kind
            self._field_widgets[key] = widget

        # TX power: a single Low/Medium/High combobox instead of the two
        # underlying tx_at_max_power/tx_at_med_power booleans -- handled
        # explicitly (not via the generic per-field loop above) since it
        # doesn't map to one channel key. See _load_channel_into_form and
        # _collect_changes. Placed as the next slot in the same grid.
        self.tx_power_var = tk.StringVar()
        tx_power_widget = ttk.Combobox(
            fields_frame, textvariable=self.tx_power_var, values=TX_POWER_CHOICES,
            state="readonly", width=9,
        )
        _place_field(
            len(CHANNEL_FIELDS), "tx_power",
            ctk.CTkLabel(fields_frame, text="tx_power"), tx_power_widget,
        )

        bool_frame = ctk.CTkFrame(body, fg_color="transparent")
        bool_frame.pack(fill="x", padx=10, pady=(0, 10))
        for i, key in enumerate(CHANNEL_BOOL_FIELDS):
            var = tk.BooleanVar()
            widget = ctk.CTkCheckBox(bool_frame, text=key, variable=var)
            widget.grid(row=i // 3, column=i % 3, sticky="w", padx=10, pady=4)
            self._field_vars[key] = var
            self._field_kind[key] = "bool"
            self._field_widgets[key] = widget

    def _load_channel_into_form(self, channel: dict):
        self._loaded_channel = channel
        self.edit_channel_label.configure(
            text=f"Channel {channel['channel_id']}: {channel['name']}", text_color=GRAY,
        )
        for key, kind in self._field_kind.items():
            var = self._field_vars[key]
            value = channel.get(key)
            widget = self._field_widgets[key]
            if kind == "bool":
                var.set(bool(value))
            elif kind == "tone":
                if isinstance(value, dict):
                    var.set("DCS (read-only, edit via channel_control.py)")
                    widget.configure(state="disabled")
                else:
                    widget.configure(state="normal")
                    var.set("none" if value is None else str(value))
            else:
                var.set("" if value is None else str(value))
        self.tx_power_var.set(_power_of(channel))

    def _collect_changes(self) -> dict:
        if self._loaded_channel is None:
            raise RuntimeError("No channel loaded -- select one from the list first.")
        changes = {}

        new_power = self.tx_power_var.get()
        if new_power != _power_of(self._loaded_channel):
            changes["tx_at_max_power"] = new_power == "high"
            changes["tx_at_med_power"] = new_power == "medium"

        for key, kind in self._field_kind.items():
            original = self._loaded_channel.get(key)
            var = self._field_vars[key]
            if kind == "bool":
                new = bool(var.get())
                if new != bool(original):
                    changes[key] = new
            elif kind == "float":
                raw = var.get().strip()
                try:
                    new = float(raw)
                except ValueError:
                    raise ValueError(f"{key} must be a number, got {raw!r}")
                if new != original:
                    changes[key] = new
            elif kind == "tone":
                if isinstance(original, dict):
                    continue  # DCS -- left alone, not editable here
                raw = var.get().strip()
                new = None if raw.lower() in ("", "none") else float(raw)
                if new != original:
                    changes[key] = new
            else:  # str, choice
                new = var.get()
                if new != original:
                    changes[key] = new
        return changes

    def _on_save_channel(self):
        try:
            changes = self._collect_changes()
        except Exception as e:
            self._log(f"Save failed: {e}\n")
            return
        if not changes:
            self._log("No changes to save.\n")
            return
        channel_id = self._loaded_channel["channel_id"]
        self.save_channel_btn.configure(state="disabled")
        self._log(f"Saving channel {channel_id}: {changes}\n")
        self._submit(
            self._session.edit_channel(channel_id, **changes),
            on_success=self._on_channel_saved,
            on_error=self._on_save_channel_failed,
        )

    def _on_channel_saved(self, channel: dict):
        self.save_channel_btn.configure(state="normal")
        self._channels_by_id[channel["channel_id"]] = channel
        self.tree.item(
            str(channel["channel_id"]),
            values=(
                channel["channel_id"], channel["name"], f"{channel['tx_freq']:.4f}",
                f"{channel['rx_freq']:.4f}", channel["tx_mod"], channel["bandwidth"],
                _power_of(channel), "Y" if channel["scan"] else "",
            ),
        )
        self._load_channel_into_form(channel)
        self._log(f"Saved channel {channel['channel_id']}.\n")

    def _on_save_channel_failed(self, error):
        self.save_channel_btn.configure(state="normal")
        self._log(f"Save failed: {error}\n")

    # ---------- Dual-watch ----------

    def _build_dual_watch_frame(self):
        header, body = self._make_pane(
            self.connection_paned, "Channels", minsize=90, key="Dual-watch (A/B)",
        )

        ctk.CTkLabel(body, text="Dual Channel:").grid(row=0, column=0, sticky="w", padx=10, pady=5)
        self.dual_watch_active_var = tk.StringVar(value="OFF")
        ttk.Combobox(
            body, textvariable=self.dual_watch_active_var, values=DUAL_WATCH_CHOICES,
            state="readonly", width=6,
        ).grid(row=0, column=1, sticky="w", padx=(0, 10), pady=5)
        self.set_dual_watch_active_btn = ctk.CTkButton(
            body, text="Apply", width=80, command=self._on_set_dual_watch_active,
        )
        self.set_dual_watch_active_btn.grid(row=0, column=2, padx=(0, 10), pady=5)
        ctk.CTkLabel(
            body, text="(OFF disables dual-watch entirely)", text_color=GRAY,
        ).grid(row=0, column=3, columnspan=3, sticky="w", padx=(0, 10))

        # Spinbox (not a plain entry) so A/B can be dialed up/down instead
        # of typed; `to` defaults to this radio's channel count and is
        # tightened once the real count is known (_on_channels_loaded).
        ctk.CTkLabel(body, text="Channel A ID:").grid(row=1, column=0, sticky="w", padx=10)
        self.watch_a_var = tk.StringVar()
        self.watch_a_spinbox = tk.Spinbox(
            body, from_=0, to=29, textvariable=self.watch_a_var, width=5,
        )
        self.watch_a_spinbox.grid(row=1, column=1, sticky="w", padx=(0, 10), pady=(5, 10))
        self.set_watch_a_btn = ctk.CTkButton(
            body, text="Set A", width=80,
            command=lambda: self._on_set_watch_channel("A", self.watch_a_var),
        )
        self.set_watch_a_btn.grid(row=1, column=2, padx=(0, 20), pady=(5, 10))

        ctk.CTkLabel(body, text="Channel B ID:").grid(row=1, column=3, sticky="w")
        self.watch_b_var = tk.StringVar()
        self.watch_b_spinbox = tk.Spinbox(
            body, from_=0, to=29, textvariable=self.watch_b_var, width=5,
        )
        self.watch_b_spinbox.grid(row=1, column=4, sticky="w", padx=(0, 10), pady=(5, 10))
        self.set_watch_b_btn = ctk.CTkButton(
            body, text="Set B", width=80,
            command=lambda: self._on_set_watch_channel("B", self.watch_b_var),
        )
        self.set_watch_b_btn.grid(row=1, column=5, padx=(0, 10), pady=(5, 10))

    def _on_set_dual_watch_active(self):
        slot = self.dual_watch_active_var.get()
        self.set_dual_watch_active_btn.configure(state="disabled")
        self._log(f"Setting dual-watch active slot to {slot}...\n")
        self._submit(
            self._session.set_dual_watch_active(slot),
            on_success=lambda _: self._on_dual_watch_active_set(slot),
            on_error=self._on_dual_watch_active_failed,
        )

    def _on_dual_watch_active_set(self, slot: str):
        self.set_dual_watch_active_btn.configure(state="normal")
        self._log(f"Dual-watch active slot set to {slot}.\n")

    def _on_dual_watch_active_failed(self, error):
        self.set_dual_watch_active_btn.configure(state="normal")
        self._log(f"Set dual-watch active slot failed: {error}\n")

    def _on_set_watch_channel(self, slot: str, var: tk.StringVar):
        raw = var.get().strip()
        try:
            channel_id = int(raw)
        except ValueError:
            self._log(f"Channel {slot} ID must be an integer, got {raw!r}\n")
            return
        self._log(f"Setting dual-watch {slot} to channel {channel_id}...\n")
        self._submit(
            self._session.set_watch_channel(slot, channel_id),
            on_success=lambda _: self._log(f"Dual-watch {slot} set to channel {channel_id}.\n"),
        )

    # ---------- Status ----------

    def _build_status_frame(self):
        header, body = self._make_pane(
            self.connection_paned, "Radio status", minsize=140, stretch="always",
        )
        self.refresh_status_btn = ctk.CTkButton(
            header, text="Refresh Status", command=self._on_refresh_status, width=140
        )
        self.refresh_status_btn.pack(side="right")

        self.status_label = ctk.CTkLabel(
            body, text="Not connected.", justify="left", anchor="w",
            text_color=GRAY, wraplength=680,
        )
        self.status_label.pack(fill="x", padx=10, pady=10, anchor="w")

    def _on_refresh_status(self):
        self.refresh_status_btn.configure(state="disabled")
        self._submit(
            self._session.get_status(),
            on_success=self._on_status_done,
            on_error=self._on_status_failed,
        )

    def _on_status_done(self, data: dict):
        self.refresh_status_btn.configure(state="normal")
        self.status_label.configure(text=format_info(data), text_color=("black", "white"))

        # Keep the dual-watch controls in sync with the radio's actual
        # current settings, not just whatever was last typed/left over.
        dw = data["dual_watch"]
        self.dual_watch_active_var.set(dw["active_slot"])
        if dw["channel_a"]:
            self.watch_a_var.set(str(dw["channel_a"]["channel_id"]))
        if dw["channel_b"]:
            self.watch_b_var.set(str(dw["channel_b"]["channel_id"]))

    def _on_status_failed(self, error):
        self.refresh_status_btn.configure(state="normal")
        self._log(f"Refresh status failed: {error}\n")

    # ---------- Log ----------

    def _build_log_frame(self):
        _header, body = self._make_pane(self.outer_paned, "Log", minsize=60, stretch="always")
        self.log_box = ctk.CTkTextbox(body, wrap="word", height=100)
        self.log_box.pack(fill="both", expand=True, padx=10, pady=10)
        make_textbox_readonly(self.log_box)

    def _log(self, text: str):
        self.log_box.insert("end", text)
        self.log_box.see("end")


if __name__ == "__main__":
    app = RadioConfigGUI()
    app.mainloop()
