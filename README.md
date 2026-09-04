# open_uvpro

Interface for the BTech UV-Pro handheld ham radio, supporting connections over
Bluetooth and USB to send and receive data.

Built on [benlink](https://github.com/khusmann/benlink) for Bluetooth
communication with Benshi-based radios (BTech UV-Pro, Vero VR-N76,
RadioOddity GA5WB).

## Testing 
This has been tested on: 
Ubuntu 25.10 6.17.0-41-generic
Python 3.13.7
BTech UV Pro FW: 0.9.2-3

## Install/Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

The radio's Bluetooth address defaults to the one set in
`scripts/radio_config.py`, so you don't need to pass it to any script
below. If you're connecting a different radio, find its address with
`scripts/scan_radio.sh` and either edit that default or set the
`UV_PRO_ADDR` environment variable.

## Usage

```bash
scripts/scan_radio.sh                  # confirm the radio is powered on and in range
python scripts/connect_test.py rfcomm  # device info, battery, GPS (JSON)
python scripts/channel_control.py list # list channel memories
python scripts/radio_info.py           # battery/signal/current channel/dual-watch/GPS snapshot (JSON)
```

### KISS bridge control GUI

A CustomTkinter desktop app for the Linux-side connection lifecycle:
scan for and remember the radio's Bluetooth device, connect/disconnect,
start/stop the packet bridge and Pat, and view live radio info -- all
from one window. This is *not* general UV-Pro control (channel editing
etc.) -- that's `channel_control.py` above.

```bash
scripts/start_kiss_gui.sh
```

That script always launches through the venv rather than a bare
`python3` -- `kiss_gui.py` shells out to other scripts (e.g. the scan)
using `sys.executable`, so a system Python without `bleak`/`benlink`
installed would fail those actions.

### Radio configuration GUI

A second, separate CustomTkinter app for actual UV-Pro control: browse
and edit the 30 channel memories (name, frequency, mode, bandwidth,
tone, TX power, scan flag, ...), enable/configure dual-watch A/B, and
view live radio status. Unlike `kiss_gui.py`, this one talks the
radio's own command protocol directly, so it needs the radio's one
Bluetooth connection for itself -- stop the KISS bridge first
(`scripts/stop_radio.sh`) if it's running.

```bash
scripts/start_radio_config_gui.sh
```

Remembers its window size, panel layout, and last-used tab between runs.

### Channel monitor GUI

A third CustomTkinter app: connects directly to the radio and decodes
incoming AX.25 traffic live (source/destination callsigns, digipeater
path, frame type, protocol ID, payload) into a scrolling table, with a
detail view (full header + hex/ASCII dump) for whichever frame is
selected. Like the radio configuration GUI, it needs the radio's one
Bluetooth connection for itself -- stop the KISS bridge first if it's
running.

```bash
scripts/start_channel_monitor_gui.sh
```

### Packet radio / Winlink (Pat) over Bluetooth

```bash
scripts/start_radio.sh   # starts the Bluetooth<->KISS bridge, attaches AX.25
scripts/start_pat.sh     # starts Pat's web UI at http://localhost:8080
scripts/stop_radio.sh    # stops both
scripts/stop_pat.sh
```

`sudo` is needed to attach/detach the AX.25 interface. Run
`sudo scripts/install_sudo_rules.sh` once to stop being prompted every
time (narrowly scoped to just that; see `NOTES.md`) -- or enter your
password in the GUI's password field / the `SUDO_PASS` env var each time
instead, which still works either way.

See `NOTES.md` for one-time AX.25/Pat configuration and known issues.

## License

Apache License 2.0 -- see `LICENSE` and `NOTICE`.
