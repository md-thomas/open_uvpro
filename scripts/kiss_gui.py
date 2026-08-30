"""CustomTkinter GUI for the Linux-side KISS/AX.25 bridge to the UV-Pro:
scan for the Bluetooth device, and start/stop the KISS bridge (+
kissattach) and Pat's Winlink web UI.

This is connection-lifecycle control, not general UV-Pro radio control
(channel editing etc. -- see channel_control.py for that) -- it's a
thin wrapper around the existing scripts (scan_ble.py, start_radio.sh,
stop_radio.sh, start_pat.sh, stop_pat.sh) rather than a reimplementation,
so behavior stays in sync with the command-line tools, and process
status is read live from the OS (pgrep) rather than tracked internally
-- so it stays correct even if something was started/stopped outside
the GUI.

kissattach needs sudo, and a GUI subprocess has no controlling terminal
to prompt on. If you fill in the "sudo password" field, it's passed to
start_radio.sh/stop_radio.sh via the SUDO_PASS environment variable for
that one subprocess call only (never written to disk or logged); leave
it blank to fall back to whatever terminal launched this GUI prompting
you there instead (works if you started kiss_gui.py from an interactive
shell).

Usage:
    python scripts/kiss_gui.py
"""

import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import customtkinter as ctk

from gui_common import (
    MAC_RE,
    bt_connected,
    disambiguate as _disambiguate,
    format_info as _format_info,
    load_remembered_device as _load_remembered_device,
    make_textbox_readonly,
    pgrep,
    save_remembered_device as _save_remembered_device,
)
from radio_config import DEFAULT_DEVICE_UUID

PROJECT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = PROJECT_DIR / "scripts"
BRIDGE_LOG_PATH = Path.home() / ".cache" / "uvpro-bridge.log"
PAT_URL = "http://localhost:8080"

GREEN = "#2fa84f"
RED = "#c0392b"
GRAY = "#888888"


class RadioGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("UV-Pro KISS Bridge Control")
        self.geometry("640x780")
        self.minsize(560, 640)

        ctk.set_appearance_mode("system")

        self._scan_map: dict[str, str] = {}
        self._busy = False
        self._status_check_in_flight = False
        self._device_connected = False

        # Vertically resizable panels: a PanedWindow gives each section a
        # drag handle (sash) on its bottom edge instead of fixed heights.
        self.paned = tk.PanedWindow(
            self, orient="vertical", sashrelief="raised", sashwidth=6,
            bg=self.cget("bg"), bd=0,
        )
        self.paned.pack(fill="both", expand=True, padx=10, pady=10)

        self._build_device_frame()
        self._build_bridge_frame()
        self._build_pat_frame()
        self._build_log_frame()

        self._refresh_status()

    # ---------- Device selection ----------

    def _build_device_frame(self):
        frame = ctk.CTkFrame(self.paned)
        self.paned.add(frame, minsize=180, stretch="never")
        frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            frame, text="Radio device", font=ctk.CTkFont(weight="bold")
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(10, 0))

        # Remembered (or default) device is kept as a fallback address for
        # actions (Connect, Start Bridge, Refresh Info) even while the
        # combobox shows blank -- see _maybe_show_only_device for when it
        # does/doesn't start blank.
        remembered = _load_remembered_device() or (DEFAULT_DEVICE_UUID, DEFAULT_DEVICE_UUID)
        self._remembered = remembered
        label, addr = remembered
        self._scan_map = {label: addr}

        # Plain ttk.Combobox, not CTkComboBox: the latter is a custom
        # Canvas-drawn widget that has shown real redraw bugs (vanishing
        # entirely, leaving blank space) in this GNOME/Wayland (XWayland)
        # session after unrelated widget updates elsewhere in the window.
        # ttk.Combobox is a native, mature Tk widget -- less visually
        # matched to the CTk theme, but doesn't have that failure mode.
        # Starts showing the remembered device directly: at this point
        # there's only ever one candidate (no scan has run yet), so
        # there's no ambiguity to hide by blanking it -- see
        # _maybe_show_only_device.
        self.device_var = ctk.StringVar(value=label)
        self.device_combo = ttk.Combobox(
            frame, textvariable=self.device_var, values=[label], width=30,
        )
        # sticky="w" (not "ew"): don't stretch to fill column 0's width.
        # Column 0 also holds conn_status, a CTkLabel whose text changes on
        # every status poll -- each CTk canvas redraw very slightly
        # perturbs its measured width, which (with sticky="ew" tying this
        # widget's rendered size to that same column) fed a slow width
        # oscillation that eventually left this widget unmapped. A fixed
        # width here breaks that feedback loop regardless of the label.
        self.device_combo.grid(row=1, column=0, padx=10, pady=10, sticky="w")
        self.device_combo.bind("<<ComboboxSelected>>", lambda e: self._on_device_selected())
        self.device_combo.bind("<Return>", lambda e: self._on_device_selected())

        self.connect_button = ctk.CTkButton(
            frame, text="Connect", command=self._on_connect_toggle, width=100
        )
        self.connect_button.grid(row=1, column=1, padx=(0, 10), pady=10)

        self.scan_button = ctk.CTkButton(
            frame, text="Scan for devices", command=self._on_scan, width=140
        )
        self.scan_button.grid(row=1, column=2, padx=(0, 10), pady=10)

        self.conn_status = ctk.CTkLabel(frame, text="Bluetooth: unknown", text_color=GRAY)
        self.conn_status.grid(row=2, column=0, padx=10, pady=(0, 10), sticky="w")

        self.scan_status = ctk.CTkLabel(
            frame, text="Checking connection...", text_color=GRAY,
        )
        self.scan_status.grid(row=2, column=1, columnspan=2, padx=10, pady=(0, 10), sticky="w")

        separator = ctk.CTkFrame(frame, height=2, fg_color=GRAY)
        separator.grid(row=3, column=0, columnspan=3, sticky="ew", padx=10, pady=(0, 10))

        self.refresh_info_btn = ctk.CTkButton(
            frame, text="Refresh Info", command=self._on_refresh_info, width=140
        )
        self.refresh_info_btn.grid(row=4, column=0, padx=10, pady=(0, 5), sticky="w")

        # wraplength on both: an unwrapped long line (e.g. a full
        # exception message) forces this column to demand far more width
        # than the window has, and that renegotiation was what actually
        # broke device_combo's layout -- see _info_done/_on_refresh_info,
        # which now also keep this label's own text short regardless.
        self.info_note = ctk.CTkLabel(frame, text="", text_color=GRAY, wraplength=400, justify="left")
        self.info_note.grid(row=4, column=1, columnspan=2, padx=10, pady=(0, 5), sticky="w")

        self.info_label = ctk.CTkLabel(
            frame,
            text="No info yet -- click Refresh Info. Requires the bridge to be"
                 " stopped (the radio only allows one connection at a time).",
            justify="left", anchor="w", text_color=GRAY, wraplength=560,
        )
        self.info_label.grid(row=5, column=0, columnspan=3, padx=10, pady=(0, 10), sticky="w")

    def _on_scan(self):
        self.scan_button.configure(state="disabled")
        self.scan_status.configure(text="Scanning (10s)...")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        devices: list[tuple[str, str]] = []
        error = None
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "scan_ble.py")],
                capture_output=True,
                text=True,
                timeout=30,
            )
            for line in result.stdout.splitlines():
                m = MAC_RE.match(line.strip())
                if m:
                    addr, name = m.group(1), m.group(2)
                    label = name if name and name != "(unknown name)" else addr
                    devices.append((label, addr))
        except Exception as e:
            error = str(e)
        self.after(0, lambda: self._scan_done(_disambiguate(devices), error))

    def _scan_done(self, devices, error):
        self.scan_button.configure(state="normal")
        if error:
            self.scan_status.configure(text=f"Scan failed: {error}", text_color=RED)
            return
        if not devices:
            self.scan_status.configure(text="No devices found.", text_color=RED)
            return

        labels = [label for label, _ in devices]
        self._scan_map = dict(devices)
        if self._remembered:
            r_label, r_addr = self._remembered
            if r_label not in self._scan_map:
                # Keep the remembered device pickable even if this scan
                # didn't find it -- e.g. it's already connected (as
                # classic BT), so it may not be BLE-advertising right now.
                self._scan_map[r_label] = r_addr
                labels.append(r_label)
        self.device_combo.configure(values=labels)
        # Deliberately don't auto-pick or auto-remember a result when
        # there's more than one (not even a "uv-pro" name match) -- if
        # the actual radio didn't show up (e.g. it's already connected
        # and so isn't advertising, as happened during testing),
        # auto-selecting would silently pick some other nearby device
        # instead. Wait for an explicit choice. But if there's only one
        # candidate at all, there's no ambiguity to wait out.
        self._maybe_show_only_device(labels)
        self.scan_status.configure(
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
            # Field is blank (not yet confirmed connected) -- fall back to
            # the remembered device so Connect/Start Bridge/Refresh Info
            # still work without forcing a rescan.
            return self._remembered[1]
        return value

    def _label_for_address(self, addr: str) -> str:
        for label, a in self._scan_map.items():
            if a == addr:
                return label
        if self._remembered and self._remembered[1] == addr:
            return self._remembered[0]
        return addr

    def _on_device_selected(self, _value=None):
        value = self.device_var.get().strip()
        if value:
            addr = self._selected_address()
            if MAC_RE.match(addr):
                _save_remembered_device(value, addr)
                self._remembered = (value, addr)
        self._update_status_once()

    def _on_connect_toggle(self):
        addr = self._selected_address()
        if not MAC_RE.match(addr):
            self._log("No valid device selected -- scan first.\n")
            return
        action = "disconnect" if self._device_connected else "connect"
        self.connect_button.configure(state="disabled")
        self._log(f"$ bluetoothctl {action} {addr}\n")
        threading.Thread(target=self._bt_action_worker, args=(addr, action), daemon=True).start()

    def _bt_action_worker(self, addr: str, action: str):
        try:
            result = subprocess.run(
                ["bluetoothctl", action, addr],
                capture_output=True, text=True, timeout=15,
            )
            output = (result.stdout + result.stderr).strip()
            self.after(0, lambda: self._log(output + "\n"))
        except Exception as e:
            self.after(0, lambda: self._log(f"{action.capitalize()} failed: {e}\n"))
        finally:
            self.after(0, lambda: self.connect_button.configure(state="normal"))
            self.after(0, self._update_status_once)

    # ---------- Bridge / kissattach ----------

    def _build_bridge_frame(self):
        frame = ctk.CTkFrame(self.paned)
        self.paned.add(frame, minsize=140, stretch="never")
        frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            frame, text="Bluetooth <-> KISS bridge", font=ctk.CTkFont(weight="bold")
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(10, 5))

        self.bridge_status = ctk.CTkLabel(frame, text="Bridge: unknown", text_color=GRAY)
        self.bridge_status.grid(row=1, column=0, sticky="w", padx=10)
        self.kissattach_status = ctk.CTkLabel(frame, text="AX.25 (ax0): unknown", text_color=GRAY)
        self.kissattach_status.grid(row=2, column=0, sticky="w", padx=10, pady=(0, 5))

        ctk.CTkLabel(frame, text="sudo password (kissattach only, optional):").grid(
            row=3, column=0, sticky="w", padx=10
        )
        self.sudo_pass_entry = ctk.CTkEntry(frame, show="*", width=200)
        self.sudo_pass_entry.grid(row=3, column=1, sticky="w", padx=(0, 10), pady=5)

        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.grid(row=4, column=0, columnspan=3, sticky="w", padx=5, pady=(0, 10))
        self.start_radio_btn = ctk.CTkButton(
            btn_frame, text="Start Bridge", command=self._on_start_radio
        )
        self.start_radio_btn.pack(side="left", padx=5)
        self.stop_radio_btn = ctk.CTkButton(
            btn_frame, text="Stop Bridge", command=self._on_stop_radio, fg_color=RED
        )
        self.stop_radio_btn.pack(side="left", padx=5)

    def _on_start_radio(self):
        addr = self._selected_address()
        if not MAC_RE.match(addr):
            self._log(f"Not a valid device address: {addr!r}\n")
            return
        args = [str(SCRIPTS_DIR / "start_radio.sh"), addr]
        self._run_script_async(args, sudo_password=self.sudo_pass_entry.get())

    def _on_stop_radio(self):
        args = [str(SCRIPTS_DIR / "stop_radio.sh")]
        self._run_script_async(args, sudo_password=self.sudo_pass_entry.get())

    # ---------- Pat ----------

    def _build_pat_frame(self):
        frame = ctk.CTkFrame(self.paned)
        self.paned.add(frame, minsize=100, stretch="never")

        ctk.CTkLabel(
            frame, text="Pat (Winlink web UI)", font=ctk.CTkFont(weight="bold")
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=10, pady=(10, 5))

        self.pat_status = ctk.CTkLabel(frame, text="Pat: unknown", text_color=GRAY)
        self.pat_status.grid(row=1, column=0, sticky="w", padx=10, pady=(0, 10))

        btn_frame = ctk.CTkFrame(frame, fg_color="transparent")
        btn_frame.grid(row=2, column=0, columnspan=3, sticky="w", padx=5, pady=(0, 10))
        self.start_pat_btn = ctk.CTkButton(
            btn_frame, text="Start Pat", command=self._on_start_pat
        )
        self.start_pat_btn.pack(side="left", padx=5)
        self.stop_pat_btn = ctk.CTkButton(
            btn_frame, text="Stop Pat", command=self._on_stop_pat, fg_color=RED
        )
        self.stop_pat_btn.pack(side="left", padx=5)
        self.open_pat_btn = ctk.CTkButton(
            btn_frame, text="Open Web UI", command=self._on_open_pat
        )
        self.open_pat_btn.pack(side="left", padx=5)

    def _on_start_pat(self):
        # Not routed through _run_script_async: start_pat.sh execs `pat
        # http`, a server that runs until killed. Waiting on it (like the
        # other actions do) would hold _busy = True -- and every button
        # disabled, including Stop Pat itself -- for as long as Pat keeps
        # running, which is indefinitely. Fire-and-forget instead; the
        # status poll picks up "Pat: running" once it's up.
        args = [str(SCRIPTS_DIR / "start_pat.sh")]
        self._log(f"$ {Path(args[0]).name} (starts a server that runs until stopped)\n")
        try:
            subprocess.Popen(
                args,
                cwd=str(PROJECT_DIR),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as e:
            self._log(f"Error: {e}\n")
        self.after(1000, self._update_status_once)

    def _on_stop_pat(self):
        self._run_script_async([str(SCRIPTS_DIR / "stop_pat.sh")])

    def _on_open_pat(self):
        import webbrowser

        webbrowser.open(PAT_URL)

    # ---------- Radio info ----------

    def _on_refresh_info(self):
        if pgrep(r"kiss_bridge\.py"):
            self.info_note.configure(text="Stop the bridge first.", text_color=RED)
            self._log("Refresh Info: the radio allows only one connection -- stop the bridge first.\n")
            return
        addr = self._selected_address()
        if not MAC_RE.match(addr):
            self.info_note.configure(text="No valid device selected.", text_color=RED)
            return
        self.refresh_info_btn.configure(state="disabled")
        self.info_note.configure(text="Querying radio...", text_color=GRAY)
        threading.Thread(target=self._refresh_info_worker, args=(addr,), daemon=True).start()

    def _refresh_info_worker(self, addr: str):
        data, error = None, None
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "radio_info.py"), addr],
                capture_output=True, text=True, timeout=40,
            )
            if result.returncode != 0:
                stderr_lines = result.stderr.strip().splitlines()
                error = stderr_lines[-1] if stderr_lines else "radio_info.py failed"
            else:
                data = json.loads(result.stdout)
        except Exception as e:
            error = str(e)
        self.after(0, lambda: self._info_done(data, error))

    def _info_done(self, data, error):
        self.refresh_info_btn.configure(state="normal")
        if error:
            self.info_note.configure(text="Failed -- see log for details.", text_color=RED)
            self._log(f"Refresh Info failed: {error}\n")
            return
        self.info_note.configure(text="")
        self.info_label.configure(text=_format_info(data), text_color=("black", "white"))

    # ---------- Log ----------

    def _build_log_frame(self):
        frame = ctk.CTkFrame(self.paned)
        self.paned.add(frame, minsize=80, stretch="always")

        ctk.CTkLabel(frame, text="Log", font=ctk.CTkFont(weight="bold")).pack(
            anchor="w", padx=10, pady=(10, 0)
        )
        self.log_box = ctk.CTkTextbox(frame, wrap="word")
        self.log_box.pack(fill="both", expand=True, padx=10, pady=10)
        make_textbox_readonly(self.log_box)

    def _log(self, text: str):
        self.log_box.insert("end", text)
        self.log_box.see("end")

    # ---------- Running scripts without blocking the UI ----------

    def _set_busy(self, busy: bool):
        self._busy = busy
        state = "disabled" if busy else "normal"
        for btn in (
            self.start_radio_btn, self.stop_radio_btn,
            self.start_pat_btn, self.stop_pat_btn, self.scan_button,
        ):
            btn.configure(state=state)

    def _run_script_async(self, args: list[str], sudo_password: str = ""):
        if self._busy:
            return
        self._set_busy(True)
        self._log(f"$ {' '.join(Path(a).name for a in args)}\n")
        threading.Thread(
            target=self._run_script_worker, args=(args, sudo_password), daemon=True
        ).start()

    def _run_script_worker(self, args: list[str], sudo_password: str):
        env = os.environ.copy()
        if sudo_password:
            env["SUDO_PASS"] = sudo_password
        try:
            proc = subprocess.Popen(
                args,
                cwd=str(PROJECT_DIR),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            for line in proc.stdout:
                self.after(0, lambda line=line: self._log(line))
            proc.wait()
        except Exception as e:
            self.after(0, lambda: self._log(f"Error: {e}\n"))
        finally:
            sudo_password = ""  # noqa: F841 (best-effort local scrub)
            self.after(0, lambda: (self._set_busy(False), self._update_status_once()))

    # ---------- Status polling ----------
    #
    # _refresh_status() is the one self-rescheduling periodic loop (started
    # once from __init__); every other call site wants an immediate,
    # one-off status update instead, so it calls _update_status_once()
    # directly -- calling _refresh_status() from those would start a second
    # concurrent polling chain that never stops.

    def _refresh_status(self):
        self._update_status_once()
        self.after(2000, self._refresh_status)

    def _update_status_once(self):
        # pgrep and `bluetoothctl info` are blocking subprocess calls --
        # bluetoothctl in particular can stall for seconds if BlueZ is
        # still busy right after a scan, so this must never run on the
        # main thread (it would freeze/glitch the whole window). Skip if
        # a check is already in flight rather than piling up threads.
        if self._status_check_in_flight:
            return
        self._status_check_in_flight = True
        addr = self._selected_address()
        label = self._label_for_address(addr)
        threading.Thread(target=self._status_worker, args=(addr, label), daemon=True).start()

    def _status_worker(self, addr: str, label: str):
        bridge_up = bool(pgrep(r"kiss_bridge\.py"))
        kissattach_up = bool(pgrep("kissattach"))
        pat_up = bool(pgrep("pat http"))
        connected = bt_connected(addr) if MAC_RE.match(addr) else False
        self.after(0, lambda: self._apply_status(bridge_up, kissattach_up, pat_up, connected, label))

    def _apply_status(
        self, bridge_up: bool, kissattach_up: bool, pat_up: bool, connected: bool, label: str,
    ):
        self._status_check_in_flight = False
        # Only fill the device field in once we've actually confirmed a
        # connection -- never overwrite something the user is mid-typing,
        # and never auto-blank a field they set deliberately (e.g. via a
        # scan) just because a poll caught it momentarily disconnected.
        if connected and not self.device_var.get().strip() and label:
            self.device_var.set(label)
        if connected != self._device_connected:
            self._device_connected = connected
            self.connect_button.configure(text="Disconnect" if connected else "Connect")
        self._set_label(
            self.conn_status,
            f"Bluetooth: {'connected' if connected else 'not connected'}",
            GREEN if connected else RED,
        )
        self._set_label(
            self.bridge_status,
            f"Bridge: {'running' if bridge_up else 'stopped'}",
            GREEN if bridge_up else RED,
        )
        self._set_label(
            self.kissattach_status,
            f"AX.25 (ax0): {'attached' if kissattach_up else 'not attached'}",
            GREEN if kissattach_up else RED,
        )
        self._set_label(
            self.pat_status,
            f"Pat: {'running' if pat_up else 'stopped'}",
            GREEN if pat_up else RED,
        )

    @staticmethod
    def _set_label(widget, text: str, color: str):
        # Skip .configure() entirely when nothing changed -- called every
        # 2s by the status poll, and a no-op CTkLabel reconfigure still
        # triggers a canvas redraw whose measured width can jitter by a
        # pixel, which (for widgets sharing a grid column with weight=1)
        # was feeding a slow width-oscillation bug. See device_combo.
        if widget.cget("text") != text or widget.cget("text_color") != color:
            widget.configure(text=text, text_color=color)


if __name__ == "__main__":
    app = RadioGUI()
    app.mainloop()
