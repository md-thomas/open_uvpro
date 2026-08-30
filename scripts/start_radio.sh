#!/bin/bash
# Start the UV-Pro Bluetooth<->KISS bridge and attach it to the kernel
# AX.25 stack, without needing multiple terminals or `screen`.
#
# Usage:
#   scripts/start_radio.sh [device_uuid]
#
# device_uuid defaults to radio_config.py's DEFAULT_DEVICE_UUID (override
# via the UV_PRO_ADDR env var) if omitted -- run scripts/scan_radio.sh
# first if you don't already know the radio's address.
#
# Runs kiss_bridge.py in the background (log + pid file under ~/.cache),
# waits for it to create the KISS pty, then runs `sudo kissattach` in the
# foreground so you can enter your password once. kissattach daemonizes
# on success, so this script exits leaving both processes running.
#
# Stop the bridge later with:
#   kill -INT "$(cat ~/.cache/uvpro-bridge.pid)"
# (plain SIGINT, not -9 -- see NOTES.md on why an abrupt kill can leave
# ax0 stuck at the kernel level). Stop kissattach with a plain
# `sudo kill <pid>` (find it via `pgrep -af kissattach`).

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Leave DEVICE_UUID unset when no arg is given so kiss_bridge.py falls
# back to radio_config.py's DEFAULT_DEVICE_UUID itself -- one source of
# truth for the address instead of duplicating it here.
DEVICE_UUID="${1:-}"
PTY_PATH="$HOME/.cache/uvpro-kisstnc"
LOG_PATH="$HOME/.cache/uvpro-bridge.log"
PID_PATH="$HOME/.cache/uvpro-bridge.pid"

if pgrep -f "kiss_bridge.py" >/dev/null 2>&1; then
    echo "kiss_bridge.py is already running:"
    pgrep -af "kiss_bridge.py"
    echo "Stop it first with: kill -INT \$(cat $PID_PATH)"
    exit 1
fi

if pgrep -af kissattach >/dev/null 2>&1; then
    echo "A kissattach process is already running from a previous session:"
    pgrep -af kissattach
    echo "Kill it first (needs sudo), then re-run this script:"
    echo "  sudo pkill kissattach"
    exit 1
fi

echo "Starting kiss_bridge.py in the background (log: $LOG_PATH)..."
cd "$PROJECT_DIR"
BRIDGE_ARGS=()
[ -n "$DEVICE_UUID" ] && BRIDGE_ARGS+=("$DEVICE_UUID")
nohup .venv/bin/python scripts/kiss_bridge.py "${BRIDGE_ARGS[@]}" >"$LOG_PATH" 2>&1 &
echo $! >"$PID_PATH"
echo "Bridge pid $(cat "$PID_PATH")"

echo "Waiting for KISS pty at $PTY_PATH..."
for _ in $(seq 1 20); do
    [ -L "$PTY_PATH" ] && break
    sleep 0.5
done
if [ ! -L "$PTY_PATH" ]; then
    echo "Pty never appeared -- check the log:"
    tail -n 20 "$LOG_PATH"
    exit 1
fi
echo "Pty ready: $PTY_PATH -> $(readlink "$PTY_PATH")"

echo "Attaching AX.25 (will prompt for your sudo password)..."
sudo kissattach "$PTY_PATH" wl2k

echo
echo "Done. Bridge log: tail -f $LOG_PATH"
echo "Interface: $(ip -brief link show ax0 2>/dev/null || echo 'ax0 not up yet')"
