#!/bin/bash
# Launch kiss_bridge.py and kissattach together in a detached `screen`
# session, so both survive after this script returns and you don't have
# to juggle separate terminals.
#
# Usage:
#   scripts/start_bridge.sh [device_uuid]
#
# Then:
#   screen -r uvpro-tnc     # attach (Ctrl-A D to detach without killing it)
#
# The "kissattach" window runs `sudo kissattach ...` and will sit at a
# password prompt until you attach and type it once.
set -euo pipefail

DEVICE_UUID="${1:-38:D2:00:01:85:BF}"
SESSION=uvpro-tnc
PTY_PATH="$HOME/.cache/uvpro-kisstnc"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if screen -list 2>/dev/null | grep -q "\.${SESSION}[[:space:]]"; then
    echo "screen session '$SESSION' is already running."
    echo "Attach with: screen -r $SESSION"
    exit 0
fi

# kissattach daemonizes on success and keeps running independently of the
# window that launched it -- a stale one from an earlier session holds the
# 'wl2k' port lock and makes the next attach fail with "already in use".
if pgrep -af kissattach >/dev/null 2>&1; then
    echo "A kissattach process is already running from a previous session:"
    pgrep -af kissattach
    echo "Kill it first (needs sudo), then re-run this script:"
    echo "  sudo pkill kissattach"
    exit 1
fi

screen -dmS "$SESSION" -t bridge bash -c \
    "cd '$PROJECT_DIR' && source .venv/bin/activate && python scripts/kiss_bridge.py '$DEVICE_UUID'; echo; echo '[bridge exited]'; exec bash"

# Give the bridge a moment to create the pty before kissattach tries to open it.
sleep 3

screen -S "$SESSION" -X screen -t kissattach bash -c \
    "echo 'Run: sudo kissattach $PTY_PATH wl2k'; sudo kissattach '$PTY_PATH' wl2k; echo; echo '[kissattach exited]'; exec bash"

echo "Started screen session '$SESSION' with windows: bridge, kissattach"
echo "Attach with:  screen -r $SESSION"
echo "  Ctrl-A D          detach (leaves it running)"
echo "  Ctrl-A N / Ctrl-A P   next/previous window"
echo "  Ctrl-A \"           list windows"
