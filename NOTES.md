# Project notes / handoff

Context for picking this project up in a fresh session (e.g. on a different
machine) without re-deriving everything from scratch.

## Machine topology

The radio is paired via Bluetooth on `macbookair2` (Ubuntu). Radio MAC:
`38:D2:00:01:85:BF`. All scripts in `scripts/` must run on a machine with
Bluetooth hardware and the radio paired/trusted there.

This MAC is hardcoded as the default in `scripts/radio_config.py`, so
none of the scripts below require passing it on the command line. If
you're not sure of the address (new radio, new machine), find it with
`scripts/scan_radio.sh` (a thin wrapper around `scan_ble.py`); override
the default per-invocation with the `UV_PRO_ADDR` environment variable
rather than passing it as an argument everywhere.

## Protocol gotchas

These are the non-obvious things that took a long debugging session to
find (most are also commented inline where relevant):

- The radio's actual command/data protocol is Bluetooth **Classic RFCOMM**
  (the `SPP Dev` SDP service), not BLE GATT — despite `benlink` advertising
  BLE as the primary transport. BLE connections do complete (even resolve
  GATT services) but the traffic is bridged over BR/EDR ("GATT over
  BR/EDR"), which `bleak` handles flakily. RFCOMM is the reliable path.

- `SPP Dev`'s RFCOMM channel number **floats between sessions** (seen 1, 2,
  4) — it must be (re)discovered live, never hardcoded.
  `scripts/discover_channel.py` does this by sending a real Gaia
  `GetDeviceInfo` probe on each candidate channel and checking for a valid
  reply.

- The radio's embedded Bluetooth stack **refuses a fresh RFCOMM connect
  shortly after any disconnect** — even 60s of backoff wasn't enough to
  fix this. The working fix: never close a connection and reopen a new
  one; hand off the *same live socket* from the discovery probe straight
  into benlink's `RfcommClient` (see `patches.py`'s `PENDING_SOCKETS`
  mechanism and `radio_connect.py`).

- **"Call Audio" must be enabled in the radio's own Bluetooth menu** (a
  physical/UI setting on the radio itself, not software). Without it,
  BlueZ's automatic HFP audio-profile negotiation gets refused and tears
  down the *entire* connection, including the data channel.

- This firmware sets real values in bits that `benlink`'s protocol structs
  treat as always-zero reserved padding (`_pad` fields), and even
  `benlink`'s GitHub `main` branch doesn't perfectly match this firmware's
  field widths in every struct. `patches.py` generically relaxes every
  literal `_pad` field across all `Bitfield` subclasses at import time.

- Install `benlink` from GitHub `main`, not PyPI — the PyPI release
  (0.1.1) is stale and missing many protocol fields this firmware uses
  (see `requirements.txt`).

## Current capabilities

- `scan_radio.sh` — BLE scan, highlights the UV-Pro if it's found
- `connect_test.py` — device info, battery, GPS position (JSON on stdout)
- `radio_info.py` — battery, signal (RSSI), the actively-displayed
  channel/freq/power, dual-watch A/B channels, GPS (JSON on stdout)
- `channel_control.py` — list/get/edit channel memories (name, freq,
  modulation, bandwidth, tone, power/scan flags), set dual-watch A/B
  channel pointers
- `kiss_bridge.py` — bridges the radio's built-in TNC to a real KISS pty,
  so the Linux AX.25 stack (and Pat) can use the radio as a packet TNC
  over Bluetooth. See "Pat / Winlink over Bluetooth" below.
- `start_radio.sh` / `start_pat.sh` / `stop_radio.sh` / `stop_pat.sh` —
  one-command startup/shutdown for the above (no `screen`, no manual
  multi-terminal juggling); see below.
- `install_sudo_rules.sh` — one-time setup for passwordless `kissattach`
  attach/detach; see "Passwordless sudo" below.
- `kiss_gui.py` (launch via `start_kiss_gui.sh`) — CustomTkinter desktop
  app for the connection lifecycle (device scan/remember/connect,
  bridge/Pat start-stop, live radio info) -- not general UV-Pro control
  (that's `channel_control.py` above); see "GUI" below.
- `radio_config_gui.py` (launch via `start_radio_config_gui.sh`) —
  CustomTkinter desktop app for actual UV-Pro control: browse/edit
  channel memories, dual-watch A/B (including enabling it, not just
  which channels A/B point to), live status. See "Radio configuration
  GUI" below.
- `gui_common.py` — shared helpers for all three GUIs (remembered-device
  and window-layout persistence, BLE scan-line parsing, process/
  Bluetooth status checks) so they don't diverge and stay in sync on the
  same remembered device.
- `channel_monitor_gui.py` (launch via `start_channel_monitor_gui.sh`) —
  CustomTkinter desktop app that connects directly to the radio and
  decodes incoming AX.25 traffic live (source/dest callsigns, digipeater
  path, frame type, PID, payload) into a scrolling table; see "Channel
  monitor GUI" below.
- `ax25.py` — minimal AX.25 v2.0/2.2 (mod-8) frame decoder used by
  `channel_monitor_gui.py`: address fields (callsign/SSID/digipeater
  path), I/S/U frame type, PID. Not a full stack (no state machine, no
  mod-128 extended sequence numbers) -- decode-only, for display.

Note: the radio's actively-displayed channel is hardware-dial-controlled
and not remotely settable in this protocol.

## Pat / Winlink over Bluetooth

The radio's TNC data doesn't come out as raw KISS bytes -- it rides
inside the same benlink command protocol as everything else (fragmented
`HT_SEND_DATA`/`DATA_RXD` messages, ≤50 bytes/fragment, gated by the
`kiss_en` setting -- already `True` on this radio, so `kiss_bridge.py`
leaves it alone). `kiss_bridge.py` does the translation: it opens a pty,
speaks real KISS framing (FEND/FESC escaping) on it, and
fragments/reassembles across the radio connection underneath.

Pat's config (`~/.config/pat/config.json`) is already set up for this:
`ax25.engine: "linux"`, `ax25_linux.port: "wl2k"` -- i.e. it expects a
kernel AX.25 interface reachable through an axports entry named `wl2k`.

One-time setup:

```
sudo apt install ax25-tools ax25-apps
echo "wl2k    K0MDT-1 1200    255     2       UV-Pro Bluetooth TNC" | sudo tee -a /etc/ax25/axports
```

(Confirmed done as of 2026-08-30: `ax25-tools`/`ax25-apps` installed
(`kissattach`, `ax25d` present in `/usr/sbin`), `axports` has the `wl2k`
entry above.)

Per session (or just use `kiss_gui.py` -- see "GUI" below):

```
scripts/start_radio.sh                # backgrounds kiss_bridge.py, then
                                       # runs `sudo kissattach` (prompts once
                                       # unless install_sudo_rules.sh has run)
scripts/start_pat.sh                  # runs `pat http` in the foreground
scripts/stop_radio.sh                 # stops both kiss_bridge.py and kissattach
scripts/stop_pat.sh
```

`start_radio.sh` runs `kiss_bridge.py` in the background (log at
`~/.cache/uvpro-bridge.log`, pid at `~/.cache/uvpro-bridge.pid`), waits
for the KISS pty to appear, then runs `sudo kissattach` in the
foreground so you can enter your password once; `kissattach` daemonizes
on success so the script exits leaving both processes running.
`stop_radio.sh` reverses this (SIGINT to the bridge, `sudo pkill -f
kissattach`). Then `scripts/start_pat.sh` starts Pat's web UI at
`http://localhost:8080`, or connect directly with
`pat connect ax25:///SOME-CALL`.

The pty symlink deliberately defaults under `~/.cache`, not `/tmp`:
`kissattach` runs as root via sudo, and the kernel's `fs.protected_symlinks`
hardening (on by default on Ubuntu) refuses to let root follow a symlink
owned by another user sitting in a sticky world-writable directory like
`/tmp` -- fails with `open: Permission denied` even though the file
permissions look fine. `~/.cache` isn't sticky/world-writable, so the
restriction never triggers. (Hit this live on 2026-08-29 with the /tmp
default before switching it.)

`start_radio.sh` also checks for a stale `kiss_bridge.py` or
`kissattach` from an earlier session first and refuses to start over
it -- a stale `kissattach` holds the `wl2k` port lock and makes a fresh
attach fail with "already in use". Kill it with `sudo pkill kissattach`
(or `sudo kill <pid>` from `pgrep -af kissattach`) before retrying.

**Known failure mode, hit live on 2026-08-29: `ax0` can get permanently
stuck at the kernel level.** After enough kill/restart/crash cycles of
the bridge (don't know the precise trigger -- suspect an abrupt kill of
`kiss_bridge.py`, or of `kissattach` itself, without it getting a clean
SIGTERM to detach), `ax0` ends up orphaned: no `kissattach` process
owns it (`ps` shows nothing), but the interface object persists
(`ip link show ax0` still reports it, same ifindex, forever). Symptoms:
a fresh `sudo kissattach ~/.cache/uvpro-kisstnc wl2k` prints only the
usual benign `tty_speed: tcgetattr: Inappropriate ioctl for device`
warning and silently does nothing -- no success line
(`AX.25 port wl2k bound to device ax0`), no new process, `ax0` unchanged.
Tried and confirmed NOT sufficient: `sudo ip link delete ax0` (fails,
`RTNETLINK answers: Operation not supported` -- AX.25 devices don't
support netlink delete), `sudo ip link set ax0 down` (succeeds, but
doesn't release the underlying line-discipline attachment). Confirmed
root cause: `sudo rmmod mkiss` / `rmmod ax25` both fail with
`Module ... is in use` -- a genuine stuck kernel refcount, not just a
missing userspace process. At that point the only reliable fix is a
reboot; there's no live process left to signal and no forced-removal
path that's worth the risk for what it's unlikely to fix. If this
happens again, don't burn time on `ip link`/`rmmod` troubleshooting --
confirm with `rmmod mkiss` once (fast, harmless check) and go straight
to reboot if it reports "in use".

Prevention (unconfirmed, but worth trying next time): always let
`kissattach` receive a real `SIGTERM` (`sudo kill <pid>`, not `-9`) before
starting a new one, and always stop `kiss_bridge.py` with a plain
`kill -INT` (which it handles for clean shutdown) rather than `-9`,
even when it seems unresponsive -- give it a few seconds first.

The bridge honors KISS TXDELAY/TXTAIL parameter frames (maps to the
radio's `kiss_tx_delay`/`kiss_tx_tail` settings, currently 0/0) if
`kissattach`/`kissparms` sends them; if on-air packets come out garbled,
that's the first thing to check.

**Known bug, live on 2026-08-29 (not yet fixed, only mitigated):** the
underlying Gaia-protocol read loop (`listen()` in `patches.py`, wrapping
benlink's frame parser) can desync and die with `ValueError: error in
field 'start' of 'GaiaFrame': expected b'\xff', got b'\xc0'`. It dies
*silently* -- as an unretrieved background task exception, not something
`kiss_bridge.py`'s own code sees -- so every subsequent
`send_tnc_data_fragment` call just hangs forever awaiting a reply that
will never arrive. This is almost certainly what caused the first
"Dial timeout" against N4SER-10: the kernel AX.25 stack successfully
wrote SABM retries to the pty (confirmed via `ip -s link show ax0`), but
the bridge was silently deadlocked on an earlier send and never actually
forwarded anything over Bluetooth. Root cause of the desync itself is
still unknown (seen once right after two concurrent `channel_control.py`
probes hit the radio while the bridge already held its one command
connection open -- maybe related, unconfirmed). Mitigation in place:
`send_tnc_data_fragment` calls are now wrapped in `asyncio.wait_for(...,
timeout=10)`, and the outer connection loop reconnects on any exception
-- so a desync now costs a stall and a fresh RFCOMM connection instead of
a silent, permanent hang.

**Root cause identified (still 2026-08-29):** it's not TX-related. Added
raw-chunk logging to `patches.py`'s `listen()` (prints the offending
bytes on any parse error) and caught it live: the crash fires on
*inbound* real off-air packet traffic, unrelated to anything we send.
The captured chunk was a genuine APRS position report the radio received
over RF (decoded cleanly to `...2754.60NS08159.53W_...Mulberry,
Florida...`), delivered as a `DATA_RXD`/`EventType=12` (`DATA_TXD`, no
dispatch case in benlink's `event_notification_disc`) notification --
and critically, the raw bytes were themselves `0xC0`-delimited (KISS
framing) *inside* the Gaia notification payload. benlink's length-field
arithmetic for that message evidently doesn't account for this
firmware's real-packet framing, desyncing the byte stream for
everything after it. This is a real, fairly frequent trigger in this
area (ambient packet/APRS chatter), not an edge case -- expect the
bridge to reconnect periodically during normal use, not just during our
own connect attempts. `RECONNECT_BACKOFF_SECONDS` is 15 (not 5) to avoid
hammering the radio's BT stack's own "won't reconnect right after a
disconnect" quirk.

**Actually fixed, 2026-08-29:** `GaiaFrame.from_bitstream_batch` already
has a built-in resync mode (`consume_errors=True` -- drop one byte and
keep scanning for the next valid frame instead of raising) that benlink
just doesn't use by default. `patches.py` now replaces
`RfcommCommandLink.connect` to pass `consume_errors=True`. Verified with
a standalone unit test (corrupt-prefix + valid-frame bytes -> the valid
frame still recovers) before relying on it live. This turns a bad
message into a silent single-byte skip instead of killing the whole
RFCOMM connection -- much cheaper than the reconnect-based mitigation,
and (important) means a real reply arriving right when ambient traffic
also triggers the bug no longer risks getting lost to a torn-down
connection. The reconnect-loop mitigation (wait_for timeout, proactive
listen-task-death detection, 15s backoff) is kept as a second line of
defense for whatever this doesn't catch.

If packet connects still stall mysteriously, check the bridge's log for
`TX:`/`RX:` lines (confirms frames reaching the radio) and `[DEBUG]
parse error on raw chunk: ...` (confirms this bug firing again).

Confirmed on 2026-08-29: channel 13 (index 12) is 144.950 MHz FM, no
tone, WIDE -- matches N4SER-10's published frequency (Sarasota, FL;
matches this radio's GPS fix), and the SABM frame the bridge sent
decoded correctly to a real AX.25 connect request addressed to N4SER.

Also on 2026-08-29: with the fixes above (proactive listen-task-death
detection especially) plus APRS decoding disabled on the radio itself
(user turned it off mid-session, removing the main trigger for the
desync bug), the bridge went fully stable -- 4 clean SABM
retransmissions over a full ~45s AX.25 retry cycle, all sent and acked,
zero crashes. Software chain (Pat -> kernel AX.25 -> bridge -> BT ->
radio) confirmed working end-to-end. Still no reply heard from N4SER-10
in that run, so `kiss_bridge.py` now also defaults `kiss_tx_delay` to
300ms / `kiss_tx_tail` to 50ms on connect if they're 0 (this radio's
default) -- 0ms TXDELAY risks keying up and sending data before the
PA/transmitter stabilizes, clipping the start of every packet. Not yet
confirmed whether that gets an actual reply -- if not, next things to
check are physical (does the radio visibly key up / TX on send?
antenna? is N4SER-10 actually up right now?), not the bridge.

## Passwordless sudo for kissattach

`kissattach`/detaching it need root. `scripts/install_sudo_rules.sh`
(run once as `sudo scripts/install_sudo_rules.sh`) sets this up so you
stop being prompted every time.

It installs a dedicated `/etc/sudoers.d/uvpro-radio` file rather than
appending to `/etc/sudoers` directly -- **sudoers evaluates rules
last-match-wins, regardless of specificity.** A `NOPASSWD:
/usr/sbin/kissattach` line added by hand straight into `/etc/sudoers`
got silently overridden by a later, more general `ALL=(ALL:ALL) ALL`
line already in that file, so it looked "installed" (`sudo -l` listed
it) but never actually applied (`sudo -n kissattach` still demanded a
password). Ubuntu's default `/etc/sudoers` processes `/etc/sudoers.d/*`
via `#includedir` *after* its own rules, so a dedicated file there is
guaranteed to be evaluated last and win, sidestepping the ordering issue
entirely. Confirmed live on 2026-08-30.

The rule is scoped to exactly two commands -- `kissattach` and `pkill -f
kissattach` -- not a blanket NOPASSWD or plain `kill`. Granting
passwordless `kill` on arbitrary PIDs would let anything ask sudo to
kill security-critical root processes without a password; `pkill -f
kissattach` only ever matches that one process by name. `stop_radio.sh`
uses `pkill`, not `kill <pid>`, to match.

The GUI's sudo password field (and `SUDO_PASS` for the CLI scripts)
still works regardless of whether this is installed -- it's a
convenience, not a requirement.

## GUI (kiss_gui.py)

CustomTkinter desktop wrapper around the scripts above -- connection
lifecycle only (device scan/remember/connect, bridge/Pat start-stop,
live radio info), not general UV-Pro control -- named `kiss_gui.py`
(not just `gui.py`) for that reason. Persists the remembered device to
`~/.cache/uvpro-gui-device.json`. Launch via `scripts/start_kiss_gui.sh`,
which always goes through the venv rather than a bare `python3` -- the
app launches other scripts (e.g. the scan) via `sys.executable`, and a
system Python without this project's venv packages will fail those.

Gotchas hit building it (2026-08-30), for if similar symptoms show up
again:

- **A `subprocess.Popen` launched from the GUI must set `stdin=
  subprocess.DEVNULL`.** Without it, the child inherits the GUI's own
  terminal stdin -- if a launched script's `sudo` call ever needs a
  password (no `SUDO_PASS`, passwordless sudo not active), it silently
  blocks waiting for input on a terminal the user isn't looking at.
  Since button state only resets in a `finally` block after the
  subprocess exits, this froze *every* button (including Stop) forever,
  not just the one clicked.

- **Never wait on a subprocess that's a long-running server.**
  `start_pat.sh` execs `pat http`, which runs until killed; the action
  handler that launches it must fire-and-forget (no `.wait()`), or the
  "busy" flag blocking other buttons never clears. `start_radio.sh` is
  fine to wait on since it backgrounds `kiss_bridge.py` itself and
  returns quickly.

- **A `ttk`/`CTk` widget sharing a grid column with a `CTkLabel` whose
  text changes periodically can get unmapped/destroyed.** Each
  `CTkLabel.configure(text=...)` triggers a canvas redraw that can very
  slightly perturb its measured width; with `sticky="ew"` tying a
  neighbor's rendered size to that same column (`grid_columnconfigure(...,
  weight=1)`), this fed a slow width-oscillation feedback loop that
  eventually broke the widget. Confirmed by binding `<Configure>`/
  `<Unmap>`/`<Destroy>` and dumping a stack trace on each -- no
  application code was calling `.destroy()`; it was Tk's own geometry
  manager reacting to the churn. Fix: `sticky="w"` (fixed width, doesn't
  stretch) on the affected widget, and skip `.configure()` entirely when
  the target text/color hasn't actually changed (see `_set_label` in
  `kiss_gui.py`). Also keep any variable-length text (e.g. a raw exception
  message) short and/or `wraplength`-wrapped -- an unwrapped long line
  forces its column far wider than the window and was a second way to
  trigger the same underlying instability.

- `bluetoothctl info <addr>` can report `Connected: yes` for a classic
  BT link that's actually gone stale/stuck -- seen as RFCOMM channel
  discovery failing ("No RFCOMM channel ... responded") well beyond the
  brief post-disconnect refusal window described above. Cycling it
  (`bluetoothctl disconnect <addr>` then `connect <addr>`) resolved it
  immediately when this happened. Worth trying before assuming
  something's broken in software.

Device-selection/remembered-device logic (blank-until-connected combo,
scan/disambiguate, `_selected_address` fallback) and status-label/log
helpers now live in `gui_common.py` and are shared with
`radio_config_gui.py` below -- fix bugs there once, not twice.

## Radio configuration GUI (radio_config_gui.py)

A second, separate GUI from `kiss_gui.py`: actual UV-Pro control
(channel list/edit, dual-watch, live status) via the radio's own
command protocol (same as `channel_control.py`/`radio_info.py`), not
the Linux-side bridge lifecycle. Needs the radio's one RFCOMM connection
for itself -- refuses to connect while `kiss_bridge.py` is running.

**Persistent connection, unlike the one-shot CLI scripts.** Reconnecting
per action would be slow and risks the "won't reconnect right after a
disconnect" quirk (see Protocol gotchas). Instead it runs a background
`asyncio` event loop in its own thread (`AsyncLoop`), holds one
`RadioController` connection open in `RadioSession` for as long as
you're connected, and dispatches each action as
`asyncio.run_coroutine_threadsafe(coro, loop)` with a done-callback that
hops back to the Tkinter thread via `self.after(0, ...)` -- never touch
a Tk widget from the loop thread directly. Verified live against the
real radio (connect, list 30 channels, edit a channel field, dual-watch
enable/disable, all cleanly reversible) before wiring up the GUI itself.

`settings.double_channel` (int, not `channels.channel_a`/`channel_b`)
is what actually turns dual-watch on/off and picks the active slot --
0=OFF, 1=A, 2=B (`benlink.protocol.ChannelType`). `channel_a`/
`channel_b` only pick which channel each slot points to; easy to miss
since channel_control.py's CLI only ever exposed those two, not
`double_channel` itself.

**Layout:** a `CTkTabview` with "Connection" (device, status,
dual-watch) and "Channels" (channel list + edit form) tabs -- too much
content to show usefully all at once otherwise. Each tab holds its own
inner `tk.PanedWindow` so its sections are independently resizable; Log
sits outside the tabs in an outer `PanedWindow` (with the tabview as the
other pane) so it stays visible regardless of which tab is active.
Every section also has a collapse toggle (▼/▶ button in its header) that
hides its body and shrinks the pane to just the header row via
`paneconfigure(minsize=..., height=...)`.

Window geometry, every pane's sash position, each section's
collapsed/expanded state, and the active tab are saved to
`~/.cache/uvpro-gui-layout.json` (via `gui_common.save_ui_state`/
`load_ui_state`, keyed by app name so this and `kiss_gui.py` can share
the file) on window close and restored on next launch. Restoring sash
positions only works once the window has its real on-screen size, so
restore order matters: apply saved geometry first, build all panes,
apply saved collapse states (each is just a toggle call), *then*
`update_idletasks()`, *then* `sash_place()` -- doing this in the wrong
order silently no-ops the sash restore.

Editable channel fields are laid out in a 3-column grid (not one long
vertical list) via a small `_place_field` helper computing
`row, col = i // 3, i % 3`. TX power is presented as one Low/Medium/High
combobox instead of the underlying `tx_at_max_power`/`tx_at_med_power`
booleans (only one should ever be true; a single control can't represent
the invalid both-true state) -- handled specially in
`_load_channel_into_form`/`_collect_changes` since it doesn't map to a
single channel key. `tx_sub_audio`/`rx_sub_audio` (CTCSS tone) editing
only supports a plain float-Hz-or-none value, matching
`channel_control.py`'s own simplification; a DCS-coded tone loads as a
disabled, read-only field rather than risking corrupting it.

Log panels in both GUIs are click-and-drag selectable/Ctrl+C-copyable
now (`gui_common.make_textbox_readonly`) -- `state="disabled"` on the
underlying `tkinter.Text` was the tempting way to make a log read-only,
but it inconsistently blocks mouse selection and copy too, not just
typing (Tk-version/platform dependent). The working pattern: leave
`state="normal"` always, and bind `<Key>` to swallow keystrokes that
would edit content while letting Control-combos and navigation keys
through.

## Channel monitor GUI (channel_monitor_gui.py)

A third GUI, for passively watching AX.25 traffic on the radio's
currently-tuned channel: connects directly to the radio's own command
protocol (same `RadioController`/`connect_rfcomm` as
`radio_config_gui.py`), so it needs the radio's one RFCOMM connection
for itself -- refuses to connect while `kiss_bridge.py` is running, same
as `radio_config_gui.py`.

**Deliberately doesn't tap `kiss_bridge.py`'s KISS pty.** A pty slave
has no "broadcast to every reader" semantics -- if this GUI opened the
same slave kissattach has open, the kernel would split incoming bytes
between the two readers essentially at random, silently corrupting both
kissattach's AX.25 stack traffic and whatever this GUI displays. A fully
separate connection avoids that at the cost of the usual one-connection
limit (can't monitor while the bridge/Pat are running).

Reassembles the radio's fragmented `TncDataFragment` events into
complete AX.25 frames -- identical logic to `kiss_bridge.py`'s
reassembly, just decoded and displayed instead of KISS-encoded onto a
pty. Also enables `kiss_en` on connect if it was off, same as
`kiss_bridge.py`, since fragments don't arrive at all otherwise.

Same silent-death risk as `kiss_bridge.py`'s background read loop (see
Protocol gotchas) applies here too, so `MonitorSession.connect()` starts
a watchdog task awaiting the same internal `listen_task` directly and
reports a "Connection lost" status instead of the GUI just going quiet
and looking like an idle channel.

Decoding is `ax25.py`'s job, kept separate from the GUI: address fields
(6 left-shifted ASCII chars + an SSID/flags byte, repeated for
dest/source/up to 8 digipeaters until the address-extension bit is set),
then a 1-byte control field (mod-8 only -- this radio's TNC doesn't do
extended mod-128 sequencing) identifying I/S/U frame type, then a PID
byte for I-frames and UI frames only. `*` after a digipeater callsign
means its H-bit ("has been repeated") is set -- standard monitor
convention (e.g. `axlisten`). Malformed/non-AX.25 frames (line noise,
corruption) are still listed (undecodable, raw hex shown) rather than
dropped, so the table reflects everything the radio actually handed up.

The frame table is capped at 500 rows (oldest dropped first) so a
busy/noisy channel can't grow memory unbounded during a long monitoring
session.

## Not yet built

- Nothing tracked right now.
