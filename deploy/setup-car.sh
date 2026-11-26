#!/usr/bin/env bash
# Set up the car computer: packages, checks, the detector service and, if you want,
# the link to the display server and phone notifications.
#
#     bash setup.sh car                  asks a few questions
#     bash setup.sh car --check          only report what is installed; changes nothing
#
# Options, mainly for automatic installs:
#     --yes                 accept the default answer to every question
#     --server ADDRESS      display server, for example 203.0.113.5 or http://203.0.113.5:5732
#     --pi-key KEY          the car computer's key, printed by the server setup
#     --hotspot NAME        your phone's hotspot; the password is asked, or read from
#                           SLOT17_HOTSPOT_PASSWORD (never put it on the command line)
#     --no-hotspot          don't ask about the phone's hotspot
#     --updates / --no-updates      install the Slot17 team's signed updates by themselves, or not
#     --no-display          don't connect to a display server
#     --bluetooth / --no-bluetooth  send alerts to the app over Bluetooth too, or not
#     --bluetooth-name NAME         the name phones show for this car computer (default Slot17)
#     --notify / --no-notify        set up phone notifications through Pushover, or not
#     --receiver KIND       airspy (Airspy Mini), rsp1b or rsp1a (SDRplay), rtlsdr2 (two RTL-SDRs);
#                           without it: the one set up before, else the one found on USB
#     --accept-sdrplay-licence  install SDRplay's API without asking (for someone who has read its licence)
#     --screen / --no-screen        show the display page full screen on a screen attached to this computer,
#                                   at every power-up, or switch that off (deploy/install-screen.sh)
#     --calibrate / --no-calibrate  measure this receiver's frequency error, or not
#     --start / --no-start          start the detector at the end, or not
#     --no-autostart        don't start the detector at every power-up
#     --no-services         install packages and run the checks only (for testing)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
# shellcheck source=deploy/common.sh
. "$HERE/common.sh"

SERVER=""; PI_KEY=""; HOTSPOT=""; HOTSPOT_STEP=ask; UPDATES=ask; DISPLAY_LINK=ask; NOTIFY=ask; CALIBRATE=ask; START=ask
BLUETOOTH=ask; BT_NAME=""; SCREEN=keep
RECEIVER=""; ACCEPT_SDRPLAY=0
AUTOSTART=1; NO_SERVICES=0; CHECK=0
while [ $# -gt 0 ]; do
    case "$1" in
        --yes) YES=1 ;;
        --server) SERVER="$2"; shift ;;
        --pi-key) PI_KEY="$2"; shift ;;
        --hotspot) HOTSPOT="$2"; HOTSPOT_STEP=yes; shift ;;
        --no-hotspot) HOTSPOT_STEP=no ;;
        --updates) UPDATES=yes ;;
        --no-updates) UPDATES=no ;;
        --no-display) DISPLAY_LINK=no ;;
        --bluetooth) BLUETOOTH=yes ;;
        --no-bluetooth) BLUETOOTH=no ;;
        --bluetooth-name) BT_NAME="$2"; [ "$BLUETOOTH" = no ] || BLUETOOTH=yes; shift ;;
        --screen) SCREEN=yes ;;
        --no-screen) SCREEN=no ;;
        --notify) NOTIFY=yes ;;
        --no-notify) NOTIFY=no ;;
        --receiver) RECEIVER="$2"; shift ;;
        --accept-sdrplay-licence|--accept-sdrplay-license) ACCEPT_SDRPLAY=1 ;;
        --calibrate) CALIBRATE=yes ;;
        --no-calibrate) CALIBRATE=no ;;
        --start) START=yes ;;
        --no-start) START=no ;;
        --no-autostart) AUTOSTART=0 ;;
        --no-services) NO_SERVICES=1 ;;
        --check) CHECK=1 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (see bash setup.sh car --help)" ;;
    esac
    shift
done
RUN_USER="$(id -un)"
SERVICE=tetra-detector.service
DISPLAY_CONFIG="$HOME/.config/slot17-display.json"
BT_SETTINGS="$PROJECT/p0/bluetooth_config.py"

airspy_present() { command -v lsusb >/dev/null && lsusb -d 1d50:60a1 >/dev/null 2>&1; }
RECEIVER_PY="$PROJECT/p0/receiver.py"
configured_receiver() {
    python3 -B "$RECEIVER_PY" --show 2>/dev/null | python3 -c 'import json, sys; d = json.load(sys.stdin); print(d["kind"] if d["set"] else "")' 2>/dev/null || true
}
receiver_name() { python3 -B -c 'import sys; sys.path.insert(0, sys.argv[1]); import receiver; print(receiver.name(sys.argv[2]))' "$PROJECT/p0" "$1" 2>/dev/null || echo "$1"; }
# The set-up receiver is plugged in: the Airspy as always; any other kind by its USB ID, every device it needs.
receiver_present() {
    local kind="${RECEIVER:-$(configured_receiver)}"
    if [ -z "$kind" ] || [ "$kind" = airspy ]; then airspy_present; return; fi
    command -v lsusb >/dev/null && python3 -B "$RECEIVER_PY" --present "$kind" >/dev/null 2>&1
}

report() {
    step "What is installed"
    printf '  %-22s %s\n' "System" "$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}") ($(uname -m))"
    printf '  %-22s %s\n' "Computer" "$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)"
    printf '  %-22s %s MB\n' "Memory" "$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
    printf '  %-22s %s GB free\n' "Storage" "$(df -Pk "$PROJECT" | awk 'NR==2 {print int($4/1048576)}')"
    printf '  %-22s %s\n' "Python" "$(python3 --version 2>&1 || echo missing)"
    printf '  %-22s %s\n' "NumPy and SciPy" "$(python3 -c 'import numpy, scipy; print(numpy.__version__, scipy.__version__)' 2>/dev/null || echo missing)"
    if [ -z "$(configured_receiver)" ] || [ "$(configured_receiver)" = airspy ]; then
        printf '  %-22s %s\n' "Airspy plugged in" "$(airspy_present && echo yes || echo 'no, or lsusb is missing')"
    else
        printf '  %-22s %s\n' "Receiver" "$(receiver_name "$(configured_receiver)"), $(receiver_present && echo 'plugged in' || echo 'not found on USB')"
    fi
    printf '  %-22s %s\n' "Detector service" "$(systemctl is-enabled $SERVICE 2>/dev/null || echo 'not installed'), $(systemctl is-active $SERVICE 2>/dev/null || true)"
    printf '  %-22s %s\n' "Display link" "$([ -f "$DISPLAY_CONFIG" ] && echo configured || echo 'not configured')"
    printf '  %-22s %s\n' "Bluetooth" "$(python3 -B "$BT_SETTINGS" --show 2>/dev/null | python3 -c '
import json, sys
s = json.load(sys.stdin)
print("on, shown as " + s["name"] if s["on"] else "off")' 2>/dev/null || echo unknown)"
    printf '  %-22s %s\n' "Notifications" "$([ -f "$HOME/.config/tetra-pushover.json" ] && echo configured || echo 'not configured')"
    printf '  %-22s %s\n' "Own calibration" "$([ -f "$PROJECT/p0/calibration.local.json" ] && echo yes || echo 'no, using the shipped example')"
}

if [ "$CHECK" = 1 ]; then
    report
    exit 0
fi

need_debian
need_normal_user
if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    die "the detector is running. Stop it first with: sudo systemctl stop $SERVICE"
fi

cat <<EOF

${BOLD}Setting up the Slot17 car computer${RESET} in $PROJECT, for user $RUN_USER.
This takes a few minutes. Nothing here opens the receiver unless you agree to calibrate it.
EOF

# --- 1. packages ------------------------------------------------------------
packages=(python3 python3-numpy python3-scipy python3-psutil python3-cryptography usbutils)
apt_install "${packages[@]}"
# Lets you type "python" instead of "python3". Optional: not every system has it.
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python-is-python3 >/dev/null 2>&1 \
    && ok "the 'python' command works" || note "'python' isn't available here; use 'python3' instead"
python_ok || die "Python 3.10 or newer is needed. This system has $(python3 --version 2>&1). Use a newer system, such as Raspberry Pi OS based on Debian 12 or 13."

# --- 1b. the receiver, and only its driver -------------------------------------
if [ "$NO_SERVICES" != 1 ]; then
    step "The receiver"
    if [ -z "$RECEIVER" ]; then
        RECEIVER="$(configured_receiver)"
        [ -z "$RECEIVER" ] || ok "set up before: $(receiver_name "$RECEIVER") (choose another with --receiver)"
    fi
    if [ -z "$RECEIVER" ]; then
        mapfile -t seen < <(python3 -B "$RECEIVER_PY" --usb "$(lsusb 2>/dev/null || true)" 2>/dev/null || true)
        if [ "${#seen[@]}" -eq 1 ]; then
            RECEIVER="${seen[0]}"
            ok "found on USB: $(receiver_name "$RECEIVER")"
        elif [ "${#seen[@]}" -eq 0 ]; then
            RECEIVER=airspy
            note "no receiver found on USB, so this sets up for the Airspy Mini. Using another? Plug it in and run the setup again, or add --receiver KIND."
        elif [ "$YES" = 1 ] || [ ! -t 0 ]; then
            RECEIVER="${seen[0]}"
            note "more than one receiver is plugged in; using $(receiver_name "$RECEIVER"). Choose with --receiver KIND."
        else
            echo "  More than one receiver is plugged in. Which one should the detector use?"
            for k in "${seen[@]}"; do echo "    $k  ($(receiver_name "$k"))"; done
            RECEIVER="$(ask_text "Type one of: ${seen[*]}")"
        fi
    fi
    # The choice is saved only once its driver is in: a licence declined or an install that fails leaves the
    # receiver set up before (and its tuned gain) as it was (until 24 November 2026 it was saved first).
    case "$RECEIVER" in
        airspy|rsp1b|rsp1a|rtlsdr2) ;;
        *) die "unknown receiver '$RECEIVER': choose airspy, rsp1b, rsp1a or rtlsdr2" ;;
    esac
    case "$RECEIVER" in
        airspy)
            apt_install airspy libairspy0 ;;
        rsp1b|rsp1a)
            echo "  The $(receiver_name "$RECEIVER") needs SDRplay's own driver, the SDRplay API. It is SDRplay's software, under"
            echo "  SDRplay's licence, which the installer shows you; it runs a service of SDRplay's as root."
            if [ "$ACCEPT_SDRPLAY" = 1 ]; then
                bash "$HERE/install-sdrplay.sh" --accept-licence
            elif [ "$YES" = 1 ] || [ ! -t 0 ]; then
                die "the SDRplay API needs your agreement to SDRplay's licence: run this in a terminal, or add --accept-sdrplay-licence once you have read it"
            else
                bash "$HERE/install-sdrplay.sh"
            fi ;;
        rtlsdr2)
            bash "$HERE/install-rtlsdr.sh" ;;
    esac
    python3 -B "$RECEIVER_PY" --set "$RECEIVER" >/dev/null || die "the receiver choice could not be saved ($RECEIVER)"
    ok "the detector uses the $(receiver_name "$RECEIVER")"
fi

# --- 2. this machine ----------------------------------------------------------
step "Checking this machine"
mem_mb="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
free_gb="$(df -Pk "$PROJECT" | awk 'NR==2 {print int($4/1048576)}')"
model="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo "$(uname -m) computer")"
ok "$model, ${mem_mb} MB memory, ${free_gb} GB free"
case "$model" in
    *"Raspberry Pi 5"*) ;;
    *) note "only the Raspberry Pi 5 has been tested. Other computers may work if they keep up; the setup's checks and the first start will tell you." ;;
esac
[ "$mem_mb" -ge 1800 ] || note "under 2 GB of memory. The detector itself uses about 0.6 GB, so this may be tight."
[ "$free_gb" -ge 32 ] || note "only ${free_gb} GB free. Recordings grow by several GB per hour of driving, and recording pauses below 5 GB."

# --- 3. offline checks --------------------------------------------------------
step "Running the offline checks"
echo "  About a minute. These never open the receiver."
log="$(mktemp)"
if python3 -B "$PROJECT/p0/preflight.py" --offline --tests >"$log" 2>&1; then
    ok "$(grep -E '^Ran [0-9]+ tests' "$log" | head -1), all passed"
else
    echo "  What failed:"
    grep -E '^(ERROR|FAIL):|^FAILED|^Ran [0-9]' "$log" | head -20 | sed 's/^/    /' || true
    python3 "$HERE/show_failures.py" "$log" || true
    die "the checks failed (full output: $log). Nothing was installed as a service."
fi
rm -f "$log"

if [ "$NO_SERVICES" = 1 ]; then
    step "Done"
    echo "  Packages and checks only, as asked with --no-services."
    exit 0
fi

# --- 4. logs that survive power cuts -----------------------------------------
step "Keeping logs across power cuts"
sudo mkdir -p /etc/systemd/journald.conf.d
sudo install -m 0644 "$HERE/60-slot17-persistent.conf" /etc/systemd/journald.conf.d/60-slot17-persistent.conf
sudo systemctl restart systemd-journald
ok "system logs are kept, up to 256 MB"

# --- 5. detector service ------------------------------------------------------
step "Installing the detector service"
if [ "$AUTOSTART" = 1 ]; then
    echo "  It starts by itself every time this computer powers up, which is what you want in a car."
    sudo bash "$HERE/install-service.sh" --user "$RUN_USER" --enable >/dev/null
else
    sudo bash "$HERE/install-service.sh" --user "$RUN_USER" >/dev/null
    sudo systemctl disable "$SERVICE" >/dev/null 2>&1 || true
fi
ok "$SERVICE installed$([ "$AUTOSTART" = 1 ] && echo ', starts at power-up')"

# --- 6. the phone's hotspot ---------------------------------------------------
step "Your phone's hotspot"
if [ "$HOTSPOT_STEP" = ask ]; then
    if [ -f /etc/NetworkManager/system-connections/slot17-hotspot.nmconnection ]; then
        HOTSPOT_STEP=no; ok "already saved; run the setup with --hotspot NAME to change it"
    elif [ "$YES" = 1 ] || ! command -v nmcli >/dev/null; then
        HOTSPOT_STEP=no
        command -v nmcli >/dev/null || note "NetworkManager isn't here; add the hotspot with this system's own Wi-Fi settings."
    else
        echo "  In the car, this computer goes online through your phone's hotspot. Save it now and it joins"
        echo "  by itself every drive. On an iPhone: Settings > Personal Hotspot, with Maximise Compatibility on."
        ask_yes "Save your phone's hotspot as a Wi-Fi network?" y && HOTSPOT_STEP=yes || HOTSPOT_STEP=no
    fi
fi
if [ "$HOTSPOT_STEP" = yes ]; then
    [ -n "$HOTSPOT" ] || HOTSPOT="$(ask_text "Hotspot name (on an iPhone: Settings > General > About > Name):")"
    password="${SLOT17_HOTSPOT_PASSWORD:-}"
    if [ -z "$password" ] && [ -t 0 ] && [ "$YES" != 1 ]; then
        read -r -s -p "  Hotspot password (not shown): " password; echo
    fi
    if [ -n "$HOTSPOT" ] && [ -n "$password" ]; then
        if printf '%s' "$password" | sudo bash "$HERE/hotspot.sh" "$HOTSPOT" >/dev/null; then
            ok "saved \"$HOTSPOT\"; this computer joins it whenever it's in range"
        else
            note "not saved (see the reason above). Add it later with: sudo nmtui"
        fi
    else
        note "skipped: both the name and the password are needed. Run the setup again to add it."
    fi
    unset password
fi

# --- 7. updates by themselves --------------------------------------------------
step "Updates by themselves"
KEY_FILE="$HOME/.config/slot17-release-key.pub"
TEAM_KEY="$(tr -d '[:space:]' <"$HERE/slot17-release-key.pub")"
if [ -f "$KEY_FILE" ] && [ "$(tr -d '[:space:]' <"$KEY_FILE")" != "$TEAM_KEY" ]; then
    ok "this car computer trusts its own release key; leaving it as it is"
elif [ "$UPDATES" = no ]; then
    rm -f "$KEY_FILE"
    note "off. Update by running the install line again when the app says there's a new version."
else
    if [ "$UPDATES" = ask ] && [ ! -f "$KEY_FILE" ]; then
        echo "  The car computer can install new Slot17 versions by itself, at a start, from your server."
        echo "  It only takes a version signed by the Slot17 team, and tests it before it replaces anything."
        ask_yes "Install updates by themselves (recommended)?" y && UPDATES=yes || UPDATES=no
    fi
    if [ "$UPDATES" = no ]; then
        note "off. Update by running the install line again when the app says there's a new version."
    else
        mkdir -p "$(dirname "$KEY_FILE")"
        printf '%s\n' "$TEAM_KEY" > "$KEY_FILE"
        ok "on: signed updates from your server install at the next start"
    fi
fi

# --- 8. display server --------------------------------------------------------
step "Connecting to your display server"
if [ "$DISPLAY_LINK" = ask ]; then
    if [ -n "$SERVER" ] || [ -f "$DISPLAY_CONFIG" ]; then
        DISPLAY_LINK=yes
    elif [ "$YES" = 1 ]; then
        DISPLAY_LINK=no
    else
        echo "  The phone app and web page get their data from a display server. Set that up first"
        echo "  with: bash setup.sh server (on the server). It prints an address and a key for this step."
        ask_yes "Connect this car computer to a display server now?" y && DISPLAY_LINK=yes || DISPLAY_LINK=no
    fi
fi
if [ "$DISPLAY_LINK" = yes ]; then
    if [ -z "$SERVER" ] && [ -f "$DISPLAY_CONFIG" ] && [ -z "$PI_KEY" ]; then
        ok "already connected; keeping $DISPLAY_CONFIG"
    else
        [ -n "$SERVER" ] || SERVER="$(ask_text "Server address (for example 203.0.113.5):")"
        [ -n "$PI_KEY" ] || PI_KEY="$(ask_text "Car computer key (pi_token):")"
        [ -n "$SERVER" ] && [ -n "$PI_KEY" ] || die "both the server address and the key are needed. Run the setup again when you have them."
        # A hosted server (name.slot17.cloud) is HTTPS on the normal port; your own server is
        # plain HTTP on 5732 unless you give a scheme or a port.
        case "$SERVER" in http://*|https://*) ;; *.slot17.cloud|*.slot17.cloud/) SERVER="https://$SERVER" ;; *) SERVER="http://$SERVER" ;; esac
        case "$SERVER" in
            https://*) ;;
            *) case "${SERVER#*://}" in *:*) ;; *) SERVER="$SERVER:5732" ;; esac ;;
        esac
        SERVER="${SERVER%/}"
        [ "${#PI_KEY}" -ge 32 ] || die "that key is too short. Copy the whole pi_token the server setup printed."
        mkdir -p "$(dirname "$DISPLAY_CONFIG")"
        ( umask 077; SLOT17_URL="$SERVER" SLOT17_TOKEN="$PI_KEY" python3 -c '
import json, os, sys
json.dump({"url": os.environ["SLOT17_URL"], "token": os.environ["SLOT17_TOKEN"]}, open(sys.argv[1], "w"))
' "$DISPLAY_CONFIG" )
        ok "saved $DISPLAY_CONFIG (only you can read it)"
        if command -v curl >/dev/null && curl -s -m 5 -o /dev/null "$SERVER/healthz"; then
            ok "the server answers at $SERVER"
        else
            note "the server didn't answer at $SERVER right now. Check the address and its firewall; the car computer keeps trying while it runs."
        fi
    fi
    sudo bash "$HERE/install-publisher.sh" --user "$RUN_USER" >/dev/null
    ok "publisher installed; it runs whenever the detector runs. The phone can stop the detector and power off this computer, nothing else."
else
    note "skipped. You can connect later by running the setup again."
fi

# --- 9. Bluetooth -------------------------------------------------------------
step "Bluetooth to your phone"
if [ "$BLUETOOTH" != no ] && ! python3 -B "$PROJECT/p0/bluetooth_link.py" adapter 2>/dev/null | grep -q '"present": true'; then
    note "no Bluetooth adapter found (or BlueZ isn't running), so this is skipped."
    BLUETOOTH=none
fi
if { [ "$BLUETOOTH" = ask ] || [ "$BLUETOOTH" = yes ]; } && [ "$DISPLAY_LINK" != yes ]; then
    note "skipped: the app gets this car computer's Bluetooth key through the display server, so connect that first."
    BLUETOOTH=none
fi
if [ "$BLUETOOTH" = ask ]; then
    echo "  Besides the display server, the car computer can talk to the Slot17 app straight over"
    echo "  Bluetooth: faster, and the app then works in the car without internet. It is visible to"
    echo "  Bluetooth scanners nearby, and a phone nearby can read what it sends (signed, not encrypted)."
    ask_yes "Send alerts over Bluetooth too?" y && BLUETOOTH=yes || BLUETOOTH=no
fi
if [ "$BLUETOOTH" = yes ]; then
    current="$(python3 -B "$BT_SETTINGS" --show | python3 -c 'import json, sys; print(json.load(sys.stdin)["name"])')"
    if [ -z "$BT_NAME" ]; then
        echo "  Phones show this car computer under a name: up to 20 letters, digits, spaces, '.', '-' or '_'."
        BT_NAME="$(ask_text "Name (Enter for $current):")"
        BT_NAME="${BT_NAME:-$current}"
    fi
    if ! python3 -B "$BT_SETTINGS" --on --name "$BT_NAME" >/dev/null; then
        note "\"$BT_NAME\" can't be used as a name; using $current. Rename it in the app or with --bluetooth-name."
        BT_NAME="$current"
        python3 -B "$BT_SETTINGS" --on --name "$BT_NAME" >/dev/null
    fi
    # BlueZ as a GATT client made phones ask to pair on every connection; Slot17 needs it only as a server.
    if [ -f /etc/bluetooth/main.conf ]; then
        if said="$(sudo python3 -B "$BT_SETTINGS" --gatt-client-off)"; then
            case "$said" in
                already*) ;;
                *) sudo systemctl restart bluetooth.service && ok "phones connect without being asked to pair: BlueZ's GATT client $said" ;;
            esac
        else
            note "could not change /etc/bluetooth/main.conf, so phones may be asked to pair at every connection"
        fi
    fi
    python3 -B "$PROJECT/p0/bluetooth_link.py" adapter --name "$BT_NAME" >/dev/null 2>&1 \
        || note "the adapter keeps its old name until the detector starts; then it takes \"$BT_NAME\"."
    ok "on, shown as \"$BT_NAME\". In the app: Settings, Bluetooth to the car computer, Set up."
elif [ "$BLUETOOTH" = no ]; then
    if [ -f "$HOME/.config/slot17-bluetooth.json" ]; then
        python3 -B "$BT_SETTINGS" --off >/dev/null && ok "switched off"
    else
        note "skipped. You can switch it on later in the app, or with bash setup.sh car --bluetooth."
    fi
fi

# --- 10. notifications --------------------------------------------------------
step "Phone notifications (optional)"
if [ "$NOTIFY" = ask ]; then
    if [ "$YES" = 1 ]; then NOTIFY=no
    else
        echo "  Pushover (pushover.net, a small one-off fee) can send faults to your phone, such as"
        echo "  'receiver hears nothing'. You need its app token and your user key."
        ask_yes "Set up Pushover notifications now?" n && NOTIFY=yes || NOTIFY=no
    fi
fi
if [ "$NOTIFY" = yes ]; then
    sudo bash "$HERE/install-notify.sh" --user-account "$RUN_USER" --ask
else
    note "skipped"
fi

# --- 10b. the car computer's own screen (only when asked) ----------------------
if [ "$SCREEN" = yes ]; then
    step "The car computer's own screen"
    # The screen's data comes from the publisher, which is installed with the display server connection (found in
    # review, 26 November 2026: a screen set up without one opened and showed no data, with no word why).
    if [ "$DISPLAY_LINK" != yes ] && [ ! -f "$DISPLAY_CONFIG" ]; then
        note "the screen shows the detector's data once this car computer is connected to a display server: the part that feeds the screen comes with that connection. Run the setup again with --server ADDRESS --pi-key KEY (from step 2 of the README)."
    fi
    sudo bash "$HERE/install-screen.sh" --user "$RUN_USER"
elif [ "$SCREEN" = no ] && [ -f /etc/systemd/system/slot17-kiosk.service ]; then
    step "The car computer's own screen"
    sudo bash "$HERE/install-screen.sh" --off
fi

# --- 11. calibration ----------------------------------------------------------
step "Calibrating the receiver (recommended once)"
# Did the detector already use the receiver in this power-up? systemd keeps the monotonic time the service last
# became active, 0 until it has run once since boot. The setup only runs with the detector stopped, so a re-run
# (an update, Bluetooth or the screen switched later) came after an opening; until 26 November 2026 its default
# answers calibrated or started it, a second opening (found in review).
# systemd forgets the time of a stopped unit that nothing wants (a detector without autostart), so this boot's
# journal of the service counts too.
RAN_THIS_BOOT=0
case "$(systemctl show "$SERVICE" -p ActiveEnterTimestampMonotonic --value 2>/dev/null || true)" in
    ""|0) ;;
    *) RAN_THIS_BOOT=1 ;;
esac
if [ "$RAN_THIS_BOOT" = 0 ] && [ -n "$(journalctl -b -u "$SERVICE" -q -n 1 --no-pager 2>/dev/null || true)" ]; then
    RAN_THIS_BOOT=1
fi
if [ "$RAN_THIS_BOOT" = 1 ] && [ "$CALIBRATE" = ask ]; then
    CALIBRATE=no
    note "not calibrating: the detector has already used the receiver in this power-up, and a second opening can make it stop responding. To calibrate anyway, run the setup with --calibrate."
fi
if ! receiver_present; then
    note "no $(receiver_name "${RECEIVER:-airspy}") found on USB, so this is skipped. Plug it in and run: bash setup.sh car --calibrate --no-display --no-notify"
    CALIBRATE=no
fi
if [ "$CALIBRATE" = ask ]; then
    echo "  Every receiver's frequency is slightly off. This listens to the base stations' own signals"
    echo "  (390-395 MHz) for a few seconds to measure it. The antenna must be connected."
    ask_yes "Calibrate now?" y && CALIBRATE=yes || CALIBRATE=no
fi
RECEIVER_OPENED=0
OPENED_BY="the detector has already used the receiver in this power-up"
[ "$RAN_THIS_BOOT" = 1 ] && RECEIVER_OPENED=1
if [ "$CALIBRATE" = yes ]; then
    [ "$RAN_THIS_BOOT" = 1 ] && note "the detector has already used the receiver in this power-up: if it stops responding, unplug it, plug it back in and power-cycle the computer."
    RECEIVER_OPENED=1       # whether the capture worked or not, the receiver has been opened once this power-up
    OPENED_BY="the calibration has just used the receiver"
    # A tag of its own, so calibration measures exactly this capture, never an older one from another receiver
    # (until 24 November 2026 it measured the newest files, whatever made them).
    CAL_TAG="downlink-calibration-$(date +%s)-$$"
    CAL_DIR="$(python3 -B -c 'import sys; sys.path.insert(0, sys.argv[1]); import paths; print(paths.IQ_DIR)' "$PROJECT/p0")"
    if python3 -B "$PROJECT/p0/capture.py" --freq 392.5 --secs 4 --rate 3 --lna 12 --mixer 12 --vga 12 \
            --receiver "${RECEIVER:-airspy}" --tag "$CAL_TAG" >/dev/null 2>&1 \
       && CAL_FILES=("$CAL_DIR"/*_"$CAL_TAG"_*.iq) && [ -f "${CAL_FILES[0]}" ] \
       && (cd "$PROJECT/p0" && python3 -B calibrate.py "${CAL_FILES[@]}" 2>&1 | tail -4); then
        ok "calibrated; saved in p0/calibration.local.json"
    else
        note "calibration didn't work. Common reasons: no TETRA base stations in 390-395 MHz where you are, or the antenna isn't connected. The detector still runs, using the example calibration. If the receiver stops responding, unplug it and plug it back in."
    fi
fi

# --- 12. start ----------------------------------------------------------------
step "Starting the detector"
# One opening of the receiver per power-up: an Airspy can stop responding when it is started a second time
# (README, Limits). Until 24 November 2026 the two default answers calibrated and then started it (found in
# review); now a calibration leaves the start to the next power-up, even when --start was given.
if [ "$RECEIVER_OPENED" = 1 ] && [ "$START" != no ]; then
    START=no
    note "not started now: $OPENED_BY, and a second start in the same power-up can make it stop responding. $([ "$AUTOSTART" = 1 ] && echo "It starts by itself at the next power-up." || echo "After the next power-up, start it with: sudo systemctl start $SERVICE")"
fi
if [ "$START" = ask ]; then
    if receiver_present; then
        ask_yes "Start it now?" y && START=yes || START=no
    else
        START=no
        note "not started, because no $(receiver_name "${RECEIVER:-airspy}") is plugged in. It starts by itself at the next power-up."
    fi
elif [ "$START" = yes ] && ! receiver_present; then
    # First boot asks to start when any known receiver is on USB; one stick of a pair isn't the receiver chosen.
    START=no
    note "not started: the $(receiver_name "${RECEIVER:-airspy}") isn't all plugged in (both sticks, for the RTL-SDR pair). It starts by itself at the next power-up."
fi
if [ "$START" = yes ]; then
    echo "  This waits for the first completed analysis, up to 90 seconds."
    if sudo systemctl start "$SERVICE"; then
        ok "the detector is running"
        python3 -B "$PROJECT/p0/status.py" --heartbeat /run/tetra/watch.heartbeat 2>/dev/null | head -6 || true
    else
        journalctl -u "$SERVICE" -n 15 --no-pager 2>/dev/null | tail -15 || true
        die "the detector didn't start; the lines above say why. Don't keep retrying: if the receiver stopped responding, unplug it, plug it back in, and run: sudo systemctl start $SERVICE"
    fi
fi

step "Finished"
cat <<EOF
  Check it any time:     python $PROJECT/p0/status.py --heartbeat /run/tetra/watch.heartbeat
  Follow its log:        journalctl -u $SERVICE -f
  Stop it:               sudo systemctl stop $SERVICE
  See this setup again:  bash $PROJECT/setup.sh car --check
EOF
