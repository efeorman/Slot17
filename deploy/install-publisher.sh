#!/usr/bin/env bash
# Install the display publisher and the two permissions the phone's buttons use.
# Run on the Pi, after deploy/install-service.sh and after creating
# ~/.config/slot17-display.json for the service's user:
#
#     bash deploy/install-publisher.sh --dry-run        # show the plan, change nothing
#     sudo bash deploy/install-publisher.sh             # install
#     sudo bash deploy/install-publisher.sh --user pi   # run the service as 'pi'
#     sudo bash deploy/install-publisher.sh --uninstall # remove all of it
#
# The publisher starts and stops with the detector. This script starts nothing and
# never touches the receiver.
set -euo pipefail

SERVICE=tetra-display-publisher.service
UNIT_DIR=/etc/systemd/system
SUDOERS_DIR=/etc/sudoers.d
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
RUN_USER="$(stat -c '%U' "$PROJECT")"
DRY_RUN=0
UNINSTALL=0
while [ $# -gt 0 ]; do
    case "$1" in
        --user) RUN_USER="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

if ! id "$RUN_USER" >/dev/null 2>&1; then
    echo "user '$RUN_USER' does not exist" >&2
    exit 1
fi
if ! printf '%s' "$RUN_USER" | grep -Eq '^[a-z_][a-z0-9_-]*$'; then
    echo "user name '$RUN_USER' is not safe to put in a sudoers rule" >&2
    exit 1
fi
PYTHON="$(command -v python3)"
UNIT_TEXT="$(sed -e "s#__PROJECT__#$PROJECT#g" -e "s#__USER__#$RUN_USER#g" -e "s#__PYTHON__#$PYTHON#g" \
    "$PROJECT/display/$SERVICE")"
GRANTS=(60-slot17-stop 61-slot17-poweroff)

if [ "$DRY_RUN" = 1 ]; then
    echo "# would write $UNIT_DIR/$SERVICE :"
    echo "$UNIT_TEXT"
    for grant in "${GRANTS[@]}"; do
        echo
        echo "# would write $SUDOERS_DIR/$grant :"
        sed -e "s/^pi ALL=/$RUN_USER ALL=/" "$HERE/$grant.sudoers" | grep -v '^#' | grep .
    done
    exit 0
fi

if [ "$(id -u)" != 0 ]; then
    echo "installing needs root - re-run with sudo" >&2
    exit 1
fi

if [ "$UNINSTALL" = 1 ]; then
    systemctl stop "$SERVICE" 2>/dev/null || true
    systemctl disable "$SERVICE" 2>/dev/null || true
    rm -f "$UNIT_DIR/$SERVICE"
    for grant in "${GRANTS[@]}"; do rm -f "$SUDOERS_DIR/$grant"; done
    systemctl daemon-reload
    echo "removed $SERVICE and its permissions"
    exit 0
fi

CONFIG="$(getent passwd "$RUN_USER" | cut -d: -f6)/.config/slot17-display.json"
if [ ! -f "$CONFIG" ]; then
    echo "note: $CONFIG does not exist: the publisher feeds Bluetooth and the car computer's own screen only" >&2
fi

# The rules grant exactly /usr/bin/systemctl, so check that is what this machine uses.
if [ "$(readlink -f "$(command -v systemctl)")" != "/usr/bin/systemctl" ]; then
    echo "systemctl is not at /usr/bin/systemctl, so the permissions would never match" >&2
    exit 1
fi

# A malformed file in sudoers.d breaks sudo for everyone, so validate before installing.
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
for grant in "${GRANTS[@]}"; do
    sed -e "s/^pi ALL=/$RUN_USER ALL=/" "$HERE/$grant.sudoers" > "$TMP/$grant"
    /usr/sbin/visudo -c -q -f "$TMP/$grant"
done
for grant in "${GRANTS[@]}"; do
    install -o root -g root -m 0440 "$TMP/$grant" "$SUDOERS_DIR/$grant"
done

echo "$UNIT_TEXT" > "$UNIT_DIR/$SERVICE"
systemctl daemon-reload
systemctl enable "$SERVICE"
echo "installed $SERVICE (user=$RUN_USER); it starts with the detector"
