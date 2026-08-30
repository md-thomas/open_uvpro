"""CustomTkinter GUI to monitor the UV-Pro's currently-tuned channel and
decode incoming AX.25 traffic live.

Connects directly to the radio's own command protocol (via benlink, same
as radio_config_gui.py/channel_control.py) rather than tapping
kiss_bridge.py's KISS pty: the radio only allows one Bluetooth
connection at a time, and a pty slave has no "broadcast to every reader"
semantics -- a second reader there would silently steal bytes out from
under kissattach/Pat rather than seeing a copy of them. So this is its
own one-shot connection, same as kiss_bridge.py or radio_config_gui.py,
and mutually exclusive with either of those being connected -- stop the
KISS bridge first if it's running.

Each complete AX.25 frame received (reassembled from the radio's
fragmented TncDataFragment events, same reassembly kiss_bridge.py does
before writing to the KISS pty) is decoded with ax25.py (source/dest
callsigns, digipeater path, frame type, PID) and shown as a row in a
live table; selecting a row shows the full header and hex/ASCII payload
dump below it.

Run via scripts/start_channel_monitor_gui.sh (always through the venv,
not a bare python3 -- see kiss_gui.py's docstring for why).
"""

import asyncio
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import customtkinter as ctk

import ax25
import patches  # noqa: F401 (applies protocol compatibility patches on import)
from benlink.command import TncDataFragmentReceivedEvent
from gui_common import (
    MAC_RE,
    disambiguate,
    load_remembered_device,
    load_ui_state,
    make_textbox_readonly,
    pgrep,
    save_remembered_device,
    save_ui_state,
)
from radio_config import DEFAULT_DEVICE_UUID
from radio_connect import connect_rfcomm

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"

GREEN = "#2fa84f"
RED = "#c0392b"
GRAY = "#888888"

# Oldest rows are dropped past this so a busy/noisy channel can't grow
# the table (and this process's memory) without bound.
MAX_FRAMES = 500

TREE_COLUMNS = ("seq", "time", "src", "dest", "path", "type", "pid", "len", "payload")
TREE_HEADINGS = {
    "seq": "#", "time": "Time", "src": "Source", "dest": "Dest", "path": "Path",
    "type": "Type", "pid": "PID", "len": "Len", "payload": "Payload",
}
TREE_WIDTHS = {
    "seq": 40, "time": 90, "src": 90, "dest": 90, "path": 110,
    "type": 50, "pid": 55, "len": 45, "payload": 260,
}


# ---------- Persistent radio session (runs in a background asyncio loop) ----------

class AsyncLoop(threading.Thread):
    """Same pattern as radio_config_gui.py's AsyncLoop -- a background
    thread running its own asyncio event loop, so the Tkinter main loop
    is never blocked by radio I/O. Submit coroutines with .submit();
    results/exceptions come back via the returned concurrent.futures.
    Future (add a done-callback and hop back to the main thread with
    widget.after(0, ...) -- never touch Tk widgets from this thread
    directly)."""

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


class MonitorSession:
    """Holds one RadioController connection open, reassembling incoming
    TNC data fragments into complete AX.25 frames and handing them to
    the GUI thread via a thread-safe queue -- add_event_handler's
    callback runs on the AsyncLoop thread, and Tk widgets may only be
    touched from the main thread.

    All async methods must run on the AsyncLoop thread (via .submit())."""

    def __init__(self):
        self._cm = None
        self.radio = None
        self._watchdog: asyncio.Task | None = None
        self.frame_queue: queue.Queue[tuple[float, bytes]] = queue.Queue()
        self.disconnect_queue: queue.Queue[BaseException] = queue.Queue()

    @property
    def connected(self) -> bool:
        return self.radio is not None

    async def connect(self, addr: str) -> None:
        if self.radio is not None:
            raise RuntimeError("Already connected")
        cm = connect_rfcomm(addr, "auto")
        radio = await cm.__aenter__()
        self._cm = cm
        self.radio = radio

        if not radio.settings.kiss_en:
            await radio.set_settings(kiss_en=True)

        reassembly = bytearray()

        def on_event(event):
            nonlocal reassembly
            if isinstance(event, TncDataFragmentReceivedEvent):
                reassembly += event.tnc_data_fragment.data
                if event.tnc_data_fragment.is_final_fragment:
                    self.frame_queue.put_nowait((time.time(), bytes(reassembly)))
                    reassembly = bytearray()

        radio.add_event_handler(on_event)

        # The background read loop can die silently (e.g. a Gaia-protocol
        # desync on real off-air traffic -- see NOTES.md) without raising
        # anywhere visible here. Watch its task directly, same as
        # kiss_bridge.py does, so a dead connection is reported instead of
        # this GUI just going quiet and looking like an idle channel.
        listen_task = radio._conn._link._client._st.listen_task

        async def _watchdog():
            try:
                await listen_task
                exc: BaseException = ConnectionError("radio link closed")
            except asyncio.CancelledError:
                return
            except Exception as e:
                exc = e
            self.disconnect_queue.put_nowait(exc)

        self._watchdog = asyncio.ensure_future(_watchdog())

    async def disconnect(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None
        if self._cm is None:
            return
        cm, self._cm = self._cm, None
        self.radio = None
        await cm.__aexit__(None, None, None)


# ---------- GUI ----------

class ChannelMonitorGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("UV-Pro Channel Monitor")
        self._ui_state = load_ui_state("channel_monitor_gui")
        self.geometry(self._ui_state.get("geometry", "900x780"))
        self.minsize(720, 600)

        ctk.set_appearance_mode("system")

        self._remembered = load_remembered_device() or (DEFAULT_DEVICE_UUID, DEFAULT_DEVICE_UUID)
        self._scan_map: dict[str, str] = {self._remembered[0]: self._remembered[1]}
        self._connected = False
        self._seq = 0
        self._frames: dict[str, ax25.Frame | None] = {}
        self._raw_by_iid: dict[str, bytes] = {}

        self._session = MonitorSession()
        self._loop = AsyncLoop()
        self._loop.start_and_wait()

        self.paned = tk.PanedWindow(
            self, orient="vertical", sashrelief="raised", sashwidth=6,
            bg=self.cget("bg"), bd=0,
        )
        self.paned.pack(fill="both", expand=True, padx=10, pady=10)

        self._build_device_frame()
        self._build_table_frame()
        self._build_detail_frame()
        self._build_log_frame()

        self._restore_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_queues()

    # ---------- Async plumbing ----------

    def _submit(self, coro, on_success=None, on_error=None):
        fut = self._loop.submit(coro)

        def _done(f):
            try:
                result = f.result()
            except Exception as e:
                if on_error:
                    self.after(0, lambda: on_error(e))
                else:
                    self.after(0, lambda: self._log(f"Error: {e}\n"))
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

    # ---------- Layout persistence ----------

    def _save_layout(self):
        state = {
            "geometry": self.geometry(),
            "sashes": [list(self.paned.sash_coord(i)) for i in range(len(self.paned.panes()) - 1)],
        }
        save_ui_state("channel_monitor_gui", state)

    def _restore_layout(self):
        state = self._ui_state
        if not state:
            return
        # Sash positions only mean anything once the window has its real
        # on-screen size (set via geometry() in __init__, applied here).
        self.update_idletasks()
        for i, coord in enumerate(state.get("sashes", [])):
            try:
                self.paned.sash_place(i, coord[0], coord[1])
            except Exception:
                pass

    # ---------- Device selection / connection ----------

    def _build_device_frame(self):
        frame = ctk.CTkFrame(self.paned)
        self.paned.add(frame, minsize=110, stretch="never")
        frame.grid_columnconfigure(0, weight=1)

        label, addr = self._remembered
        self.device_var = ctk.StringVar(value=label)
        self.device_combo = ttk.Combobox(
            frame, textvariable=self.device_var, values=[label], width=30,
        )
        self.device_combo.grid(row=0, column=0, padx=10, pady=10, sticky="w")
        self.device_combo.bind("<<ComboboxSelected>>", lambda e: self._on_device_selected())

        self.connect_button = ctk.CTkButton(
            frame, text="Connect", command=self._on_connect_toggle, width=100
        )
        self.connect_button.grid(row=0, column=1, padx=(0, 10), pady=10)

        self.scan_button = ctk.CTkButton(
            frame, text="Scan for devices", command=self._on_scan, width=140
        )
        self.scan_button.grid(row=0, column=2, padx=(0, 10), pady=10)

        self.conn_status = ctk.CTkLabel(frame, text="Not connected.", text_color=GRAY)
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
        self.conn_status.configure(text="Connected -- monitoring.", text_color=GREEN)
        self._log("Connected. Monitoring channel for AX.25 traffic.\n")

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

    def _on_disconnected(self, note: str = "Disconnected."):
        self._set_connected_ui(False)
        self.conn_status.configure(text=note, text_color=GRAY)
        self._log(note + "\n")

    def _set_connected_ui(self, connected: bool):
        self._connected = connected
        self.connect_button.configure(
            text="Disconnect" if connected else "Connect", state="normal",
        )

    # ---------- Frame table ----------

    def _build_table_frame(self):
        frame = ctk.CTkFrame(self.paned)
        self.paned.add(frame, minsize=220, stretch="always")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        toolbar = ctk.CTkFrame(frame, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 0))
        ctk.CTkLabel(toolbar, text="Traffic", font=ctk.CTkFont(weight="bold")).pack(side="left")
        self.count_label = ctk.CTkLabel(toolbar, text="0 frames", text_color=GRAY)
        self.count_label.pack(side="left", padx=(10, 0))
        self.autoscroll_var = tk.BooleanVar(value=True)
        ctk.CTkCheckBox(toolbar, text="Autoscroll", variable=self.autoscroll_var).pack(
            side="right", padx=(10, 0)
        )
        ctk.CTkButton(toolbar, text="Clear", command=self._on_clear, width=80).pack(side="right")

        tree_frame = ctk.CTkFrame(frame, fg_color="transparent")
        tree_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
        tree_frame.grid_columnconfigure(0, weight=1)
        tree_frame.grid_rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(tree_frame, columns=TREE_COLUMNS, show="headings")
        for col in TREE_COLUMNS:
            self.tree.heading(col, text=TREE_HEADINGS[col])
            self.tree.column(col, width=TREE_WIDTHS[col], anchor="w", stretch=(col == "payload"))
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

    def _on_clear(self):
        self.tree.delete(*self.tree.get_children())
        self._frames.clear()
        self._raw_by_iid.clear()
        self._seq = 0
        self.count_label.configure(text="0 frames")
        self.detail_box.delete("1.0", "end")

    def _insert_frame(self, ts: float, raw: bytes):
        self._seq += 1
        iid = str(self._seq)
        try:
            frame = ax25.decode(raw)
        except ax25.FrameError as e:
            frame = None
            row = (self._seq, _fmt_time(ts), "-", "-", "-", "?", "", len(raw), f"(undecodable: {e})")
        else:
            row = (
                self._seq, _fmt_time(ts), str(frame.source), str(frame.dest), frame.path,
                frame.subtype, (f"0x{frame.pid:02X}" if frame.pid is not None else ""),
                len(raw), frame.payload_ascii[:80],
            )
        self._frames[iid] = frame
        self._raw_by_iid[iid] = raw
        self.tree.insert("", "end", iid=iid, values=row)

        while len(self._frames) > MAX_FRAMES:
            oldest = self.tree.get_children()[0]
            self.tree.delete(oldest)
            del self._frames[oldest]
            del self._raw_by_iid[oldest]

        self.count_label.configure(text=f"{len(self._frames)} frames")
        if self.autoscroll_var.get():
            self.tree.see(iid)

    def _on_tree_select(self, event=None):
        sel = self.tree.selection()
        if not sel:
            return
        iid = sel[0]
        frame = self._frames.get(iid)
        raw = self._raw_by_iid.get(iid, b"")
        self.detail_box.delete("1.0", "end")
        self.detail_box.insert("1.0", _format_detail(frame, raw))

    # ---------- Frame detail ----------

    def _build_detail_frame(self):
        frame = ctk.CTkFrame(self.paned)
        self.paned.add(frame, minsize=140, stretch="never")
        ctk.CTkLabel(frame, text="Selected frame", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=10, pady=(10, 0)
        )
        self.detail_box = ctk.CTkTextbox(frame, wrap="none", font=("monospace", 12))
        self.detail_box.pack(fill="both", expand=True, padx=10, pady=10)
        make_textbox_readonly(self.detail_box)

    # ---------- Log ----------

    def _build_log_frame(self):
        frame = ctk.CTkFrame(self.paned)
        self.paned.add(frame, minsize=80, stretch="always")
        ctk.CTkLabel(frame, text="Log", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=10, pady=(10, 0)
        )
        self.log_box = ctk.CTkTextbox(frame, wrap="word", height=80)
        self.log_box.pack(fill="both", expand=True, padx=10, pady=10)
        make_textbox_readonly(self.log_box)

    def _log(self, text: str):
        self.log_box.insert("end", text)
        self.log_box.see("end")

    # ---------- Queue polling (asyncio thread -> Tk main thread handoff) ----------

    def _poll_queues(self):
        # Bounded per tick: a burst of traffic must not stall the Tk
        # event loop by draining an unbounded queue in one go.
        for _ in range(100):
            try:
                ts, raw = self._session.frame_queue.get_nowait()
            except queue.Empty:
                break
            self._insert_frame(ts, raw)

        try:
            exc = self._session.disconnect_queue.get_nowait()
        except queue.Empty:
            pass
        else:
            self._session.radio = None
            self._on_disconnected(f"Connection lost: {exc}")

        self.after(150, self._poll_queues)


def _fmt_time(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts)) + f".{int(ts * 1000) % 1000:03d}"


def _format_detail(frame: "ax25.Frame | None", raw: bytes) -> str:
    if frame is None:
        return f"Could not decode as AX.25 ({len(raw)} bytes).\n\nHex:\n{raw.hex(' ')}"
    extra = f"  P/F={frame.poll_final}"
    if frame.frame_type == "I":
        extra = f"  N(S)={frame.ns} N(R)={frame.nr}" + extra
    elif frame.frame_type == "S":
        extra = f"  N(R)={frame.nr}" + extra
    lines = [
        f"Source: {frame.source}",
        f"Dest:   {frame.dest}",
        f"Path:   {frame.path or '(none)'}",
        f"Type:   {frame.subtype}{extra}",
    ]
    if frame.pid is not None:
        lines.append(f"PID:    0x{frame.pid:02X} ({frame.pid_name})")
    lines.append(f"\nPayload ({len(frame.payload)} bytes):")
    lines.append(frame.payload_ascii)
    lines.append("")
    lines.append(frame.payload_hex)
    return "\n".join(lines)


if __name__ == "__main__":
    app = ChannelMonitorGUI()
    app.mainloop()
