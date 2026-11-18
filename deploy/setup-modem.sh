#!/usr/bin/env bash
# Optional: internet from a ZTE MF833N 4G USB stick instead of a phone hotspot.
# Run it on the car computer, with the stick plugged in and another internet
# connection (wired, Wi-Fi or the hotspot) still working.
#
#     bash setup.sh modem            install
#     bash setup.sh modem --check    only report what is installed; changes nothing
#     bash setup.sh modem --remove   take it out again
#
# What it installs, and why, is in docs/operations/MOBILE-MODEM.md. In short:
# someone else's USB fix that the stick needs on a Raspberry Pi 5, and a small
# check of ours that keeps the stick's data session up and ranks its route after
# a wired network and before Wi-Fi.
#
# A stick that Linux recognises as a network interface by itself needs none of this.
#
# Options:
#     --yes     accept the default answer to every question
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/common.sh
. "$HERE/common.sh"

# The USB fix is not ours. This is the exact version that was read and tested;
# a newer one is not fetched until someone has checked it and changed this line.
FIX_URL="https://github.com/hamacherengineering/zte-mf79n-pi5-fix.git"
FIX_COMMIT="1e75ba93b5ed76390c436a3aa50df1677c8775fb"
FIX_DIR="$HOME/zte-mf79n-pi5-fix"
LIB=/usr/local/lib/slot17
DETECTOR=tetra-detector.service

MODE=install
while [ $# -gt 0 ]; do
    case "$1" in
        --yes) YES=1 ;;
        --check) MODE=check ;;
        --remove) MODE=remove ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (see bash setup.sh modem --help)" ;;
    esac
    shift
done

stick_id() { lsusb 2>/dev/null | sed -n 's/.*ID \(19d2:[0-9a-f]*\).*/\1/p' | head -1; }
# The fix's modules for the running kernel. (dkms itself is not on a normal user's PATH.)
fix_built() { ls "/lib/modules/$(uname -r)/updates/dkms/"zte_setconfig_verify.ko* >/dev/null 2>&1; }
stick_interface() { nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null | sed -n 's/^zte-mf79n://p' | head -1; }

report() {
    step "4G stick"
    local id iface
    id="$(stick_id)"; iface="$(stick_interface)"
    printf '  %-22s %s\n' "Stick on USB" "${id:-none found}"
    printf '  %-22s %s\n' "USB fix" "$(fix_built && echo 'installed' || echo 'not installed')"
    printf '  %-22s %s\n' "Session check" "$(systemctl is-enabled slot17-modem.timer 2>/dev/null || echo 'not installed'), $(systemctl is-active slot17-modem.timer 2>/dev/null || true)"
    printf '  %-22s %s\n' "Network interface" "${iface:-none}"
    if [ -n "$iface" ]; then
        printf '  %-22s %s\n' "Route" "$(ip route show default dev "$iface" 2>/dev/null | head -1 || true)"
    fi
    printf '  %-22s\n' "Default routes, best first:"
    ip route show default 2>/dev/null \
        | awk '{m=0; for (i=1; i<NF; i++) if ($i=="metric") m=$(i+1); print m, $0}' \
        | sort -n | cut -d' ' -f2- | sed 's/^/      /' || true
}

if [ "$MODE" = check ]; then
    report
    exit 0
fi

need_debian
need_normal_user

if [ "$MODE" = remove ]; then
    step "Removing the session check"
    sudo systemctl disable --now slot17-modem.timer >/dev/null 2>&1 || true
    sudo rm -f /etc/systemd/system/slot17-modem.service /etc/systemd/system/slot17-modem.timer "$LIB/modem_link.py"
    sudo systemctl daemon-reload
    ok "removed"
    step "Removing the USB fix"
    if [ -f "$FIX_DIR/uninstall.sh" ]; then
        (cd "$FIX_DIR" && sudo bash uninstall.sh) || die "the fix's own uninstaller stopped; its message is above."
        ok "removed. Restart the computer to clear its module from memory. Remember the receiver: power the computer off and on rather than rebooting with the Airspy plugged in."
    else
        note "$FIX_DIR is not there, so the USB fix was left alone. Its uninstaller needs that folder."
    fi
    exit 0
fi

cat <<EOF

${BOLD}Setting up the ZTE MF833N 4G stick${RESET} on this car computer.
Keep your present internet connection until this has finished.
EOF

# --- 1. is this the right machine and stick? ----------------------------------
step "Checking this machine"
model="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)"
case "$model" in
    *"Raspberry Pi 5"*) ok "$model" ;;
    *) die "the USB fix is for the Raspberry Pi 5 only, and this is: $model. On another computer the stick may simply work; plug it in and look at: ip -br addr" ;;
esac
systemctl is-active --quiet NetworkManager.service || die "this needs NetworkManager, which Raspberry Pi OS uses by default."
id="$(stick_id)"
case "$id" in
    19d2:1225|19d2:1403|19d2:1405) ok "stick found on USB ($id)" ;;
    "") note "no ZTE stick found on USB. Installing anyway; plug it in afterwards." ;;
    *) die "found a ZTE device ($id) that the fix does not cover. Only 19d2:1225, 19d2:1403 and 19d2:1405 are." ;;
esac
# Building the fix is heavy work. A second run, with the fix already built, is light.
if ! fix_built && systemctl is-active --quiet "$DETECTOR" 2>/dev/null; then
    die "the detector is running, and building the fix would disturb it. Stop it first with: sudo systemctl stop $DETECTOR"
fi

# --- 2. the USB fix -----------------------------------------------------------
step "Fetching the USB fix"
command -v git >/dev/null || apt_install git
if [ -d "$FIX_DIR/.git" ]; then
    git -C "$FIX_DIR" fetch -q origin || note "could not reach GitHub; using the copy already here"
else
    [ ! -e "$FIX_DIR" ] || die "$FIX_DIR exists but is not a Git checkout. Move it away and run this again."
    git clone -q "$FIX_URL" "$FIX_DIR" || die "could not download the fix. Is the internet connection working?"
fi
git -C "$FIX_DIR" -c advice.detachedHead=false checkout -q "$FIX_COMMIT" \
    || die "the tested version ($FIX_COMMIT) is not in the download."
[ "$(git -C "$FIX_DIR" rev-parse HEAD)" = "$FIX_COMMIT" ] || die "the download is not the tested version."
[ -z "$(git -C "$FIX_DIR" status --porcelain)" ] || die "$FIX_DIR has local changes. Move it away and run this again."
ok "the tested version, ${FIX_COMMIT:0:7}, is in $FIX_DIR (keep this folder: removing the fix needs it)"

step "Installing the USB fix"
echo "  Its own installer checks the kernel, builds two small modules and starts the stick."
(cd "$FIX_DIR" && sudo bash install.sh --check) || die "the fix says this system is not supported; its message is above."
(cd "$FIX_DIR" && sudo bash install.sh) || die "the fix's installer stopped; its message is above."
ok "USB fix installed"

# --- 3. our session check -----------------------------------------------------
step "Installing the session check"
sudo install -d "$LIB"
sudo install -m 0755 "$HERE/modem_link.py" "$LIB/modem_link.py"
sudo install -m 0644 "$HERE/slot17-modem.service" /etc/systemd/system/slot17-modem.service
sudo install -m 0644 "$HERE/slot17-modem.timer" /etc/systemd/system/slot17-modem.timer
sudo systemctl daemon-reload
sudo systemctl enable --now slot17-modem.timer >/dev/null 2>&1
ok "it checks the stick every 30 seconds"

# --- 4. does it work? ---------------------------------------------------------
step "Waiting for the stick"
if [ -z "$(stick_id)" ]; then
    note "no stick is plugged in, so there is nothing to wait for."
else
    echo "  Up to two minutes: the stick has to start and open its data session."
    iface=""; works=0
    for _ in $(seq 1 24); do
        iface="$(stick_interface)"
        if [ -n "$iface" ] && command -v curl >/dev/null \
           && curl -s -m 8 -4 --interface "$iface" -o /dev/null https://www.google.com/generate_204; then
            works=1; break
        fi
        sleep 5
    done
    if [ "$works" = 1 ]; then
        ok "internet works over the stick ($iface)"
    elif [ -n "$iface" ]; then
        note "the stick is connected ($iface) but has no internet yet. Check the SIM, its PIN and its data allowance on the stick's page, http://192.168.0.1, then look at: journalctl -u slot17-modem.service -b"
    else
        note "the stick did not come up. Unplug it, plug it back in, wait a minute and run: bash setup.sh modem --check. The fix's log: journalctl -u zte-mf79n.service -b"
    fi
fi

report
step "Finished"
cat <<EOF
  The order is now: wired network, then the stick, then Wi-Fi such as your phone's hotspot.
  The fix asks for one restart to confirm it comes up by itself. In the car that is the next power-up.
  See it again:   bash setup.sh modem --check
  Remove it:      bash setup.sh modem --remove
EOF
