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
#     --no-display          don't connect to a display server
#     --notify / --no-notify        set up phone notifications through Pushover, or not
#     --calibrate / --no-calibrate  measure this receiver's frequency error, or not
#     --start / --no-start          start the detector at the end, or not
#     --no-autostart        don't start the detector at every power-up
#     --no-services         install packages and run the checks only (for testing)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
# shellcheck source=deploy/common.sh
. "$HERE/common.sh"

SERVER=""; PI_KEY=""; DISPLAY_LINK=ask; NOTIFY=ask; CALIBRATE=ask; START=ask
AUTOSTART=1; NO_SERVICES=0; CHECK=0
while [ $# -gt 0 ]; do
    case "$1" in
        --yes) YES=1 ;;
        --server) SERVER="$2"; shift ;;
        --pi-key) PI_KEY="$2"; shift ;;
        --no-display) DISPLAY_LINK=no ;;
        --notify) NOTIFY=yes ;;
        --no-notify) NOTIFY=no ;;
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

airspy_present() { command -v lsusb >/dev/null && lsusb -d 1d50:60a1 >/dev/null 2>&1; }

report() {
    step "What is installed"
    printf '  %-22s %s\n' "System" "$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-unknown}") ($(uname -m))"
    printf '  %-22s %s\n' "Computer" "$(tr -d '\0' </proc/device-tree/model 2>/dev/null || echo unknown)"
    printf '  %-22s %s MB\n' "Memory" "$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
    printf '  %-22s %s GB free\n' "Storage" "$(df -Pk "$PROJECT" | awk 'NR==2 {print int($4/1048576)}')"
    printf '  %-22s %s\n' "Python" "$(python3 --version 2>&1 || echo missing)"
    printf '  %-22s %s\n' "NumPy and SciPy" "$(python3 -c 'import numpy, scipy; print(numpy.__version__, scipy.__version__)' 2>/dev/null || echo missing)"
    printf '  %-22s %s\n' "Airspy plugged in" "$(airspy_present && echo yes || echo 'no, or lsusb is missing')"
    printf '  %-22s %s\n' "Detector service" "$(systemctl is-enabled $SERVICE 2>/dev/null || echo 'not installed'), $(systemctl is-active $SERVICE 2>/dev/null || true)"
    printf '  %-22s %s\n' "Display link" "$([ -f "$DISPLAY_CONFIG" ] && echo configured || echo 'not configured')"
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
packages=(python3 python3-numpy python3-scipy python3-psutil usbutils)
[ "$NO_SERVICES" = 1 ] || packages+=(airspy libairspy0)
apt_install "${packages[@]}"
# Lets you type "python" instead of "python3". Optional: not every system has it.
sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python-is-python3 >/dev/null 2>&1 \
    && ok "the 'python' command works" || note "'python' isn't available here; use 'python3' instead"
python_ok || die "Python 3.10 or newer is needed. This system has $(python3 --version 2>&1). Use a newer system, such as Raspberry Pi OS based on Debian 12 or 13."

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

# --- 6. display server --------------------------------------------------------
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
        case "$SERVER" in http://*|https://*) ;; *) SERVER="http://$SERVER" ;; esac
        case "${SERVER#*://}" in *:*) ;; *) SERVER="$SERVER:5732" ;; esac
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

# --- 7. notifications ---------------------------------------------------------
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

# --- 8. calibration -----------------------------------------------------------
step "Calibrating the receiver (recommended once)"
if ! airspy_present; then
    note "no Airspy found on USB, so this is skipped. Plug it in and run: bash setup.sh car --calibrate --no-display --no-notify"
    CALIBRATE=no
fi
if [ "$CALIBRATE" = ask ]; then
    echo "  Every receiver's frequency is slightly off. This listens to the base stations' own signals"
    echo "  (390-395 MHz) for a few seconds to measure it. The antenna must be connected."
    ask_yes "Calibrate now?" y && CALIBRATE=yes || CALIBRATE=no
fi
if [ "$CALIBRATE" = yes ]; then
    if python3 -B "$PROJECT/p0/capture.py" --freq 392.5 --secs 4 --rate 3 --lna 12 --mixer 12 --vga 12 \
            --tag downlink-calibration >/dev/null 2>&1 \
       && (cd "$PROJECT/p0" && python3 -B calibrate.py 2>&1 | tail -4); then
        ok "calibrated; saved in p0/calibration.local.json"
    else
        note "calibration didn't work. Common reasons: no TETRA base stations in 390-395 MHz where you are, or the antenna isn't connected. The detector still runs, using the example calibration. If the receiver stops responding, unplug it and plug it back in."
    fi
fi

# --- 9. start -----------------------------------------------------------------
step "Starting the detector"
if [ "$START" = ask ]; then
    if airspy_present; then
        ask_yes "Start it now?" y && START=yes || START=no
    else
        START=no
        note "not started, because no Airspy is plugged in. It starts by itself at the next power-up."
    fi
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
