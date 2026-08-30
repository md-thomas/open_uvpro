#!/bin/bash
# Start Pat's Winlink web UI. Run scripts/start_radio.sh first so the
# 'wl2k' AX.25 port exists for Pat to connect through.
#
# Usage:
#   scripts/start_pat.sh

set -euo pipefail

if ! ip link show ax0 >/dev/null 2>&1; then
    echo "Warning: ax0 interface not found -- did scripts/start_radio.sh run successfully?"
fi

echo "Starting Pat web UI at http://localhost:8080 ..."
exec pat http
