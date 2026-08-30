#!/bin/bash
# Launch the KISS bridge control GUI.
#
# Usage:
#   scripts/start_kiss_gui.sh
#
# Always goes through the venv (not a bare `python3`): kiss_gui.py shells
# out to other scripts (e.g. the scan) using sys.executable, so a system
# Python without bleak/benlink installed would fail those actions.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

exec .venv/bin/python scripts/kiss_gui.py
