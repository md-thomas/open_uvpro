# open_ht

Interface for the BTech UV-Pro handheld ham radio, supporting connections over
Bluetooth and USB to send and receive data.

Built on [benlink](https://github.com/khusmann/benlink) for Bluetooth
communication with Benshi-based radios (BTech UV-Pro, Vero VR-N76,
RadioOddity GA5WB).

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
scripts/scan_radio.sh                 # confirm the radio is powered on and in range
python scripts/connect_test.py rfcomm # device info, battery, GPS (JSON)
python scripts/channel_control.py list # list channel memories
```

### Packet radio / Winlink (Pat) over Bluetooth

```bash
scripts/start_radio.sh   # starts the Bluetooth<->KISS bridge, attaches AX.25
scripts/start_pat.sh     # starts Pat's web UI at http://localhost:8080
```

See `NOTES.md` for one-time AX.25/Pat configuration and known issues.
