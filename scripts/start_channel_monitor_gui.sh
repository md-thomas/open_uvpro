#!/bin/bash
# Launch the channel monitor GUI (live AX.25 traffic decode).
#
# Usage:
#   scripts/start_channel_monitor_gui.sh
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

exec .venv/bin/python scripts/channel_monitor_gui.py
