#!/bin/bash
# Launch the radio configuration GUI (channel editing, dual-watch,
# live status) -- not to be confused with start_kiss_gui.sh, which
# controls the Linux-side Bluetooth/KISS bridge instead.
#
# Usage:
#   scripts/start_radio_config_gui.sh
#
# Requires kiss_bridge.py to not be running (the radio allows only one
# connection at a time) -- stop it first with scripts/stop_radio.sh if
# it's up.
#
# Always goes through the venv (not a bare `python3`): the GUI shells
# out to scan_ble.py using sys.executable, so a system Python without
# bleak/benlink installed would fail that.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

exec .venv/bin/python scripts/radio_config_gui.py
