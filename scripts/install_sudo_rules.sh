#!/bin/bash
# One-time setup: let the UV-Pro radio scripts (start_radio.sh,
# stop_radio.sh, gui.py) attach/detach the KISS TNC without a password
# prompt every time, by installing a narrowly-scoped NOPASSWD sudoers
# rule for exactly two commands:
#
#   /usr/sbin/kissattach          (attaching the pty as an AX.25 device)
#   /usr/bin/pkill -f kissattach  (stopping it -- see stop_radio.sh)
#
# Deliberately NOT a blanket `sudo kill` or full NOPASSWD grant -- that
# would let any process ask sudo to kill arbitrary PIDs (including
# security-critical root processes) without a password, which is a real
# privilege-escalation-adjacent risk. This only ever runs those two exact
# commands.
#
# Installed as its own file under /etc/sudoers.d/, not appended to
# /etc/sudoers directly: sudoers evaluates rules last-match-wins
# regardless of specificity, and Ubuntu's default /etc/sudoers processes
# /etc/sudoers.d/* (via `#includedir`) after its own rules -- so a rule
# added straight to /etc/sudoers can silently be overridden by a later,
# more general line already there (e.g. a plain `ALL=(ALL:ALL) ALL`),
# exactly what happened when this was tried by hand. A dedicated
# sudoers.d file is guaranteed to be evaluated last and win.
#
# The password field in gui.py (and the SUDO_PASS env var in
# start_radio.sh/stop_radio.sh) still works either way -- this just makes
# it optional instead of required.
#
# Usage:
#   sudo scripts/install_sudo_rules.sh [username]
# (username defaults to whichever user invoked sudo)

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this with sudo: sudo scripts/install_sudo_rules.sh" >&2
    exit 1
fi

TARGET_USER="${1:-${SUDO_USER:-}}"
if [ -z "$TARGET_USER" ]; then
    echo "Couldn't determine which user to grant this to." >&2
    echo "Usage: sudo scripts/install_sudo_rules.sh <username>" >&2
    exit 1
fi

if ! id "$TARGET_USER" >/dev/null 2>&1; then
    echo "No such user: $TARGET_USER" >&2
    exit 1
fi

KISSATTACH_PATH="$(command -v kissattach || echo /usr/sbin/kissattach)"
PKILL_PATH="$(command -v pkill || echo /usr/bin/pkill)"

RULES_FILE="/etc/sudoers.d/uvpro-radio"
TMP_FILE="$(mktemp)"
trap 'rm -f "$TMP_FILE"' EXIT

cat > "$TMP_FILE" <<EOF
# Managed by open_ht/scripts/install_sudo_rules.sh -- do not hand-edit,
# re-run that script instead. Lets $TARGET_USER attach/detach the UV-Pro's
# KISS TNC without a password prompt. See that script for why this is a
# separate sudoers.d file and why pkill (not plain kill) is used.
$TARGET_USER ALL=(root) NOPASSWD: $KISSATTACH_PATH
$TARGET_USER ALL=(root) NOPASSWD: $PKILL_PATH -f kissattach
EOF

# Validate before installing anywhere near /etc/sudoers -- a syntax error
# in a live sudoers file can lock out sudo entirely.
if ! visudo -c -f "$TMP_FILE"; then
    echo "Generated sudoers rule failed validation -- not installing." >&2
    exit 1
fi

install -o root -g root -m 0440 "$TMP_FILE" "$RULES_FILE"
echo "Installed $RULES_FILE for user $TARGET_USER."

echo
echo "Verifying..."
if sudo -n -l -U "$TARGET_USER" 2>/dev/null | grep -q "$KISSATTACH_PATH"; then
    echo "OK: sudo -l confirms the rule is active for $TARGET_USER."
else
    echo "Warning: couldn't confirm via 'sudo -l -U $TARGET_USER'."
fi
