#!/usr/bin/env bash
# The car computer's own screen: the display page full screen at every power-up, on a screen attached to
# the car computer (an HDMI touchscreen in a Pi case, for example), served by the car computer itself with
# no server or internet in between. Run on the car computer:
#
#     bash deploy/install-screen.sh --dry-run        # show the plan, change nothing
#     sudo bash deploy/install-screen.sh             # install and switch the screen on
#     sudo bash deploy/install-screen.sh --off       # switch it off; it stays off after a power-up
#     sudo bash deploy/install-screen.sh --on        # switch it back on
#     sudo bash deploy/install-screen.sh --uninstall # remove what this installed (packages stay)
#
# Always: slot17-screen.service (p0/screen.py, the page for this computer only, on 127.0.0.1:5733).
# Then, for the browser:
#   - Raspberry Pi OS Lite (no desktop): cage, which shows one program full screen, with Chromium in kiosk
#     mode on the first console (slot17-kiosk.service).
#   - A desktop (a display manager such as lightdm runs): Chromium in kiosk mode started with the desktop,
#     from ~/.config/autostart/slot17-screen.desktop, so the desktop itself is left as it is.
# Drive mode or the cockpit is chosen on the screen and kept. Nothing here starts or touches the receiver.
# The browser runs at low priority: the detector comes first.
set -euo pipefail

UNIT_DIR=/etc/systemd/system
PAM_FILE=/etc/pam.d/slot17-kiosk
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
RUN_USER="$(stat -c '%U' "$PROJECT")"
ACTION=install
DRY_RUN=0
while [ $# -gt 0 ]; do
    case "$1" in
        --user) RUN_USER="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        --off) ACTION=off; shift ;;
        --on) ACTION=on; shift ;;
        --uninstall) ACTION=uninstall; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

if ! id "$RUN_USER" >/dev/null 2>&1 || ! printf '%s' "$RUN_USER" | grep -Eq '^[a-z_][a-z0-9_-]*$'; then
    echo "user '$RUN_USER' does not exist or is not a plain user name" >&2
    exit 1
fi
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
RUN_UID="$(id -u "$RUN_USER")"
AUTOSTART="$RUN_HOME/.config/autostart/slot17-screen.desktop"
PYTHON="$(command -v python3)"
DESKTOP=0
if systemctl is-enabled --quiet display-manager.service 2>/dev/null; then DESKTOP=1; fi
if [ "$DESKTOP" = 1 ]; then PACKAGES=(chromium); else PACKAGES=(cage chromium); fi

unit_text() {
    sed -e "s#__PROJECT__#$PROJECT#g" -e "s#__USER__#$RUN_USER#g" -e "s#__PYTHON__#$PYTHON#g" "$HERE/$1"
}
autostart_text() {
    printf '%s\n' "[Desktop Entry]" "Type=Application" "Name=Slot17 screen" \
        "Comment=The Slot17 display page full screen (deploy/install-screen.sh --off removes this)" \
        "Exec=nice -n 10 $PROJECT/deploy/kiosk-browser.sh" "X-GNOME-Autostart-enabled=true"
}
browser_now() {         # on a running desktop: open it now, as the user, in their session
    if pgrep -u "$RUN_USER" -x labwc >/dev/null || pgrep -u "$RUN_USER" -x wayfire >/dev/null; then
        sudo -u "$RUN_USER" env XDG_RUNTIME_DIR="/run/user/$RUN_UID" WAYLAND_DISPLAY=wayland-0 \
            setsid -f nice -n 10 "$PROJECT/deploy/kiosk-browser.sh" >/dev/null 2>&1 < /dev/null
        echo "opened on the running desktop"
    fi
}
browser_stop() {
    pkill -u "$RUN_USER" -f 'slot17-kiosk' 2>/dev/null || true
}
switch_on() {
    if [ "$DESKTOP" = 1 ]; then
        install -d -o "$RUN_USER" -g "$RUN_USER" "$RUN_HOME/.config/autostart"
        autostart_text > "$AUTOSTART"
        chown "$RUN_USER:$RUN_USER" "$AUTOSTART"
        systemctl enable --now slot17-screen.service
        browser_now
    else
        systemctl enable --now slot17-screen.service slot17-kiosk.service
    fi
}
switch_off() {
    rm -f "$AUTOSTART"
    systemctl disable --now slot17-kiosk.service 2>/dev/null || true
    browser_stop
    systemctl disable --now slot17-screen.service 2>/dev/null || true
}

if [ "$DRY_RUN" = 1 ]; then
    echo "# action: $ACTION, for user $RUN_USER, $([ "$DESKTOP" = 1 ] && echo 'on a desktop' || echo 'without a desktop')"
    if [ "$ACTION" = install ]; then
        echo "# would install: ${PACKAGES[*]}"
        echo; echo "# would write $UNIT_DIR/slot17-screen.service :"; unit_text slot17-screen.service
        if [ "$DESKTOP" = 1 ]; then
            echo; echo "# would write $AUTOSTART :"; autostart_text
        else
            echo; echo "# would add $RUN_USER to the groups video, render, input and write $PAM_FILE"
            echo; echo "# would write $UNIT_DIR/slot17-kiosk.service :"; unit_text slot17-kiosk.service
        fi
    fi
    exit 0
fi

if [ "$(id -u)" != 0 ]; then
    echo "this needs root - re-run with sudo" >&2
    exit 1
fi

case "$ACTION" in
    off)
        switch_off
        echo "the screen is off; it stays off after a power-up (--on switches it back)"
        exit 0 ;;
    on)
        switch_on
        echo "the screen is on"
        exit 0 ;;
    uninstall)
        switch_off
        rm -f "$UNIT_DIR/slot17-kiosk.service" "$UNIT_DIR/slot17-screen.service" "$PAM_FILE"
        systemctl daemon-reload
        echo "removed the screen's services and autostart entry (the packages stay installed)"
        exit 0 ;;
esac

echo "installing ${PACKAGES[*]} if missing (Chromium is about 250 MB)"
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${PACKAGES[@]}" >/dev/null
chmod 755 "$HERE/kiosk-browser.sh"
unit_text slot17-screen.service > "$UNIT_DIR/slot17-screen.service.tmp"
mv "$UNIT_DIR/slot17-screen.service.tmp" "$UNIT_DIR/slot17-screen.service"
if [ "$DESKTOP" = 0 ]; then
    for group in video render input; do
        getent group "$group" >/dev/null && usermod -aG "$group" "$RUN_USER"
    done
    install -m 644 "$HERE/slot17-kiosk.pam" "$PAM_FILE"
    unit_text slot17-kiosk.service > "$UNIT_DIR/slot17-kiosk.service.tmp"
    mv "$UNIT_DIR/slot17-kiosk.service.tmp" "$UNIT_DIR/slot17-kiosk.service"
    systemctl set-default graphical.target >/dev/null
fi
chmod 644 "$UNIT_DIR"/slot17-*.service
systemctl daemon-reload
switch_on
echo "the screen is on: the display page full screen, now and at every power-up"
echo "switch it off with: sudo bash $HERE/install-screen.sh --off"
