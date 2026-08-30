#!/bin/bash
# Stop the UV-Pro Bluetooth<->KISS bridge and kissattach started by
# scripts/start_radio.sh (or run manually).
#
# Usage:
#   scripts/stop_radio.sh
#
# If the SUDO_PASS env var is set, it's piped to `sudo -S` instead of
# prompting on the terminal -- used by kiss_gui.py. Not persisted anywhere;
# leave it unset for normal interactive use.

set -uo pipefail

PID_PATH="$HOME/.cache/uvpro-bridge.pid"

# --- kiss_bridge.py: SIGINT, never -9 (see NOTES.md on why an abrupt
# kill can leave ax0 stuck at the kernel level) ---
BRIDGE_PID=""
[ -f "$PID_PATH" ] && BRIDGE_PID="$(cat "$PID_PATH")"

if [ -z "$BRIDGE_PID" ] || ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
    # Not started via start_radio.sh (no valid pid file) -- search by name.
    BRIDGE_PID="$(pgrep -f 'kiss_bridge\.py' | head -n1 || true)"
fi

if [ -n "$BRIDGE_PID" ] && kill -0 "$BRIDGE_PID" 2>/dev/null; then
    echo "Stopping kiss_bridge.py (pid $BRIDGE_PID, SIGINT)..."
    kill -INT "$BRIDGE_PID"
    for _ in $(seq 1 10); do
        kill -0 "$BRIDGE_PID" 2>/dev/null || break
        sleep 0.5
    done
    if kill -0 "$BRIDGE_PID" 2>/dev/null; then
        echo "kiss_bridge.py (pid $BRIDGE_PID) did not exit after SIGINT."
        echo "Check ~/.cache/uvpro-bridge.log before considering kill -9."
    else
        echo "kiss_bridge.py stopped."
        rm -f "$PID_PATH"
    fi
else
    echo "kiss_bridge.py is not running."
fi

# --- kissattach: needs sudo, plain SIGTERM (default pkill signal). Uses
# `pkill -f kissattach` (matched by name) rather than `kill <pid>` so the
# passwordless-sudo rule from install_sudo_rules.sh can be scoped to this
# exact command instead of granting blanket kill-any-pid rights. ---
if pgrep -f kissattach >/dev/null 2>&1; then
    echo "Stopping kissattach (needs sudo)..."
    if [ -n "${SUDO_PASS:-}" ]; then
        printf '%s\n' "$SUDO_PASS" | sudo -S pkill -f kissattach
    else
        sudo pkill -f kissattach
    fi
    sleep 1
    if pgrep -af kissattach >/dev/null 2>&1; then
        echo "kissattach is still running -- sudo pkill may have failed (wrong password?)."
    else
        echo "kissattach stopped."
    fi
else
    echo "kissattach is not running."
fi

# --- ax0 sanity check: orphaned interface is a known stuck-kernel bug ---
if ip link show ax0 >/dev/null 2>&1 && ! pgrep -af kissattach >/dev/null 2>&1; then
    echo
    echo "Warning: ax0 still exists with no kissattach process owning it."
    echo "This is the known stuck-ax0 bug in NOTES.md. Confirm with:"
    echo "  sudo rmmod mkiss"
    echo "If that reports 'in use', a reboot is the only reliable fix."
fi
