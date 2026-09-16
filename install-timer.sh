#!/usr/bin/env bash
#
# install-timer.sh — put the morning paper on a schedule.
#
# Installs the systemd user units, turns the timer on, and — this is the part
# that is easy to miss — enables lingering.
#
# Lingering is what lets a user service run when the user is not logged in. A
# laptop that boots to a login screen and sits there has no user session, so
# without it the timer is simply not running, and the failure is invisible: the
# unit is enabled, `systemctl --user status` looks right from an SSH session, and
# no paper arrives. `loginctl enable-linger` is one command and easy to forget.
#
# Re-runnable. Copies the units over any previous version, reloads, restarts.
#
# Usage:
#     ./install-timer.sh
#     ./install-timer.sh --remove

set -euo pipefail

cd "$(dirname "$0")"

UNIT_DIR="$HOME/.config/systemd/user"
UNITS=(todays-brief.service todays-brief.timer)

if [[ "${1:-}" == "--remove" ]]; then
    systemctl --user disable --now todays-brief.timer 2>/dev/null || true
    for unit in "${UNITS[@]}"; do rm -f "$UNIT_DIR/$unit"; done
    systemctl --user daemon-reload
    echo "Removed. The pipeline and the data are untouched."
    exit 0
fi

if ! command -v systemctl >/dev/null; then
    echo "No systemctl on this system. Use cron instead — but note it will not" >&2
    echo "fire while the machine is suspended." >&2
    exit 1
fi

mkdir -p "$UNIT_DIR"
for unit in "${UNITS[@]}"; do
    cp "systemd/$unit" "$UNIT_DIR/$unit"
done

systemctl --user daemon-reload
systemctl --user enable --now todays-brief.timer

# Without this the timer only runs while a session is open.
if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
    if loginctl enable-linger "$USER" 2>/dev/null; then
        echo "Enabled lingering, so the timer runs without you logged in."
    else
        echo "Could not enable lingering. If no paper arrives, run:" >&2
        echo "    sudo loginctl enable-linger $USER" >&2
    fi
fi

echo
echo "Installed. Next run:"
systemctl --user list-timers todays-brief.timer --no-pager | sed 's/^/  /'
echo
echo "  Run it now      : ./deliver.sh"
echo "  Watch it happen : journalctl --user -u todays-brief -f"
echo "  Turn it off     : ./install-timer.sh --remove"
