#!/usr/bin/env bash
# Install (or update) the TETRA detector as a systemd service that starts at
# boot. Run on the Raspberry Pi, from anywhere:
#
#     sudo ./deploy/install-service.sh                 # install only, keep stopped
#     sudo ./deploy/install-service.sh --start         # install + start this boot
#     sudo ./deploy/install-service.sh --enable --start # production autostart
#     sudo ./deploy/install-service.sh --user pi       # run the service as 'pi'
#     sudo ./deploy/install-service.sh --dry-run       # show the unit, change nothing
#     sudo ./deploy/install-service.sh --uninstall     # stop, disable, remove
#
# It fills the __TOKENS__ in tetra-detector.service with this machine's real
# paths, so nothing is hard-coded. Idempotent: re-run it after editing options.
set -euo pipefail

SERVICE=tetra-detector.service
UNIT_DIR=/etc/systemd/system
# Project root = parent of this script's directory (deploy/..).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"

# The user the service runs as: default to the owner of the project tree, which
# on a normal Pi is the login user, not root.
RUN_USER="$(stat -c '%U' "$PROJECT")"
DRY_RUN=0
UNINSTALL=0
START=0
ENABLE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --user) RUN_USER="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        --start) START=1; shift ;;
        --enable) ENABLE=1; shift ;;
        --install-only) shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ "$UNINSTALL" = 1 ]; then
    systemctl stop "$SERVICE" 2>/dev/null || true
    systemctl disable "$SERVICE" 2>/dev/null || true
    rm -f "$UNIT_DIR/$SERVICE"
    systemctl daemon-reload
    echo "removed $SERVICE"
    exit 0
fi

# Prefer the project's venv python if present, else the system python3.
if [ -x "$PROJECT/.venv/bin/python" ]; then
    PYTHON="$PROJECT/.venv/bin/python"
else
    PYTHON="$(command -v python3)"
fi

if [ -z "$PYTHON" ]; then
    echo "no python3 found" >&2
    exit 1
fi
if ! id "$RUN_USER" >/dev/null 2>&1; then
    echo "user '$RUN_USER' does not exist" >&2
    exit 1
fi

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

if [ "$(id -u)" != 0 ]; then
    echo "installing needs root - re-run with sudo" >&2
    exit 1
fi

# Make sure the run-user can reach the Airspy without root. libairspy's udev
# rules assign it to plugdev; add the user if a group exists.
if getent group plugdev >/dev/null; then
    usermod -aG plugdev "$RUN_USER" || true
fi

if systemctl is-active --quiet "$SERVICE" && [ "$START" != 1 ]; then
    echo "detector is running; stop it before an install-only update" >&2
    exit 1
fi
runuser -u "$RUN_USER" -- "$PYTHON" "$PROJECT/p0/preflight.py" --offline

echo "$UNIT_TEXT" > "$UNIT_DIR/$SERVICE"
if [ -L "$PROJECT/logs" ]; then
    echo "$PROJECT/logs is a symbolic link; refusing to change its owner as root" >&2
    exit 1
fi
mkdir -p "$PROJECT/logs"
chown -h "$RUN_USER" "$PROJECT/logs" 2>/dev/null || true
systemctl daemon-reload
if [ "$ENABLE" = 1 ]; then systemctl enable "$SERVICE"; fi
if [ "$START" = 1 ]; then systemctl restart "$SERVICE"; fi

echo "installed $SERVICE (user=$RUN_USER, python=$PYTHON, start=$START, enable=$ENABLE)"
echo "  follow it:   journalctl -u $SERVICE -f"
echo "  status:      systemctl status $SERVICE"
echo "  the log:     $PROJECT/logs/supervise.log"
