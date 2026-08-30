#!/bin/bash
# Stop Pat's Winlink web UI started by scripts/start_pat.sh.
#
# Usage:
#   scripts/stop_pat.sh

set -uo pipefail

PIDS="$(pgrep -f 'pat http' || true)"
if [ -z "$PIDS" ]; then
    echo "pat http is not running."
    exit 0
fi

echo "Stopping pat http (pid(s) $PIDS)..."
kill $PIDS
for _ in $(seq 1 10); do
    pgrep -f 'pat http' >/dev/null 2>&1 || break
    sleep 0.5
done
if pgrep -af 'pat http' >/dev/null 2>&1; then
    echo "pat http did not exit -- if it stays stuck, force it with: kill -9 $PIDS"
else
    echo "pat http stopped."
fi
