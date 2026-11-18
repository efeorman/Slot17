#!/usr/bin/env bash
# Install the start-up notification as a systemd service, and (optionally) store
# the Pushover credentials for the user the service runs as.
#
#     sudo ./deploy/install-notify.sh                       # install + enable
#     sudo ./deploy/install-notify.sh --ask --device iphone
#     sudo ./deploy/install-notify.sh --dry-run             # show the unit only
#     sudo ./deploy/install-notify.sh --uninstall
#
# The credentials are written to the RUN USER's ~/.config/tetra-pushover.json
# with mode 0600 - not into the project tree, so they are never copied around by
# a code sync. They are never echoed back.
set -euo pipefail

SERVICE=tetra-boot-notify.service
UNIT_DIR=/etc/systemd/system
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"

RUN_USER="$(stat -c '%U' "$PROJECT")"
DRY_RUN=0
UNINSTALL=0
ASK=0
TOKEN=""
USERKEY=""
DEVICE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --user-account) RUN_USER="$2"; shift 2 ;;
        --token) TOKEN="$2"; shift 2 ;;
        --user) USERKEY="$2"; shift 2 ;;
        --ask) ASK=1; shift ;;
        --device) DEVICE="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ "$UNINSTALL" = 1 ]; then
    systemctl stop "$SERVICE" 2>/dev/null || true
    systemctl disable "$SERVICE" 2>/dev/null || true
    rm -f "$UNIT_DIR/$SERVICE"
    systemctl daemon-reload
    echo "removed $SERVICE  (credentials left in place)"
    exit 0
fi

if [ -x "$PROJECT/.venv/bin/python" ]; then
    PYTHON="$PROJECT/.venv/bin/python"
else
    PYTHON="$(command -v python3)"
fi
[ -n "$PYTHON" ] || { echo "no python3 found" >&2; exit 1; }
id "$RUN_USER" >/dev/null 2>&1 || { echo "user '$RUN_USER' does not exist" >&2; exit 1; }

UNIT_TEXT="$(sed \
    -e "s#__PROJECT__#$PROJECT#g" \
    -e "s#__USER__#$RUN_USER#g" \
    -e "s#__PYTHON__#$PYTHON#g" \
    "$HERE/$SERVICE")"

if [ "$DRY_RUN" = 1 ]; then
    echo "# would write $UNIT_DIR/$SERVICE :"
    echo "$UNIT_TEXT"
    exit 0
fi

[ "$(id -u)" = 0 ] || { echo "installing needs root - re-run with sudo" >&2; exit 1; }

# Credentials, if supplied. Written as the run user, 0600, outside the repo. --ask keeps them off
# every command line; passed as --token and --user, they are visible to other accounts while this runs.
if [ "$ASK" = 1 ]; then
    read -rsp "Pushover token: " TOKEN; echo
    read -rsp "Pushover user key: " USERKEY; echo
fi
if [ -n "$TOKEN" ] && [ -n "$USERKEY" ]; then
    ARGS=(--configure --credentials-stdin)
    [ -n "$DEVICE" ] && ARGS+=(--device "$DEVICE")
    printf '%s\n%s\n' "$TOKEN" "$USERKEY" | sudo -u "$RUN_USER" "$PYTHON" "$PROJECT/p0/notify.py" "${ARGS[@]}"
elif ! sudo -u "$RUN_USER" "$PYTHON" -c "
import sys; sys.path.insert(0, '$PROJECT/p0')
import notify; sys.exit(0 if notify.configured() else 1)"; then
    echo "WARNING: no Pushover credentials for '$RUN_USER'." >&2
    echo "  run: sudo ./deploy/install-notify.sh --ask --device iphone" >&2
    echo "  (installing the unit anyway; it will do nothing until configured)" >&2
fi

echo "$UNIT_TEXT" > "$UNIT_DIR/$SERVICE"
systemctl daemon-reload
systemctl enable "$SERVICE"

echo "installed and enabled $SERVICE (user=$RUN_USER, python=$PYTHON)"
echo "  it runs at boot, once the network is up."
echo "  test it now:    sudo systemctl start $SERVICE && journalctl -u $SERVICE -n 20"
echo "  check config:   sudo -u $RUN_USER $PYTHON $PROJECT/p0/notify.py --status"
