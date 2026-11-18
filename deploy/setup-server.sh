#!/usr/bin/env bash
# Set up the display server: the web page and API the phone and the car computer use.
#
#     bash setup.sh server            install, or update while keeping the keys
#     bash setup.sh server --show     print the links and keys again
#
# Options:
#     --yes               accept the default answer to every question
#     --port PORT         port to listen on (default 5732)
#     --address ADDRESS   this server's address as the phone sees it (default: detected)
#
# It installs into /opt/slot17-display and runs as its own user, slot17, through the
# system service slot17-display.service. Needs Python 3.10 or newer, and nothing else.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/.." && pwd)"
# shellcheck source=deploy/common.sh
. "$HERE/common.sh"

PORT=5732; ADDRESS=""; SHOW=0
while [ $# -gt 0 ]; do
    case "$1" in
        --yes) YES=1 ;;
        --port) PORT="$2"; shift ;;
        --address) ADDRESS="$2"; shift ;;
        --show) SHOW=1 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (see bash setup.sh server --help)" ;;
    esac
    shift
done
case "$PORT" in ''|*[!0-9]*) die "the port must be a number" ;; esac
TARGET=/opt/slot17-display
SERVICE=slot17-display.service
CONFIG="$TARGET/config.json"

detect_address() {
    if [ -n "$ADDRESS" ]; then printf '%s' "$ADDRESS"; return; fi
    local found=""
    if command -v curl >/dev/null; then
        found="$(curl -fsS -m 5 https://api.ipify.org 2>/dev/null || true)"
    fi
    [ -n "$found" ] || found="$(hostname -I 2>/dev/null | awk '{print $1}')"
    printf '%s' "${found:-YOUR-SERVER-ADDRESS}"
}

show() {
    local address port
    address="$(detect_address)"
    port="$(sudo python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("port", 5732))' "$CONFIG")"
    sudo python3 - "$CONFIG" "$address" "$port" <<'EOF'
import json, sys
cfg = json.load(open(sys.argv[1]))
base = f"http://{sys.argv[2]}:{sys.argv[3]}"
print(f"""
  1. Your private link, for the phone app and the web page. Treat it like a password:
     anyone with it can see the data, stop the detector and power off the car computer.

       {base}/?key={cfg['viewer_key']}

  2. For the car computer's setup (bash setup.sh car):

       Server address:      {sys.argv[2]}:{sys.argv[3]}
       Car computer key:    {cfg['pi_token']}

  3. Optional, for a phone shortcut that answers "on" or "off":

       {base}/api/pi-on?token={cfg.get('pi_status_key', '(none; set pi_status_key in config.json)')}
""")
EOF
    # The private link as a QR code, so the iPhone camera can take it instead of typing it.
    if command -v qrencode >/dev/null; then
        echo "  The private link as a QR code. The iPhone camera opens it as the web page. For the app,"
        echo "  touch and hold the link the camera shows, copy it, and paste it in Settings, Connection:"
        echo
        sudo python3 -c 'import json,sys; c=json.load(open(sys.argv[1])); print("http://%s:%s/?key=%s" % (sys.argv[2], sys.argv[3], c["viewer_key"]))' \
            "$CONFIG" "$address" "$port" | qrencode -t ANSIUTF8 -m 2
        echo "  Anyone who can see this code can use the link: don't share screenshots of it."
    fi
    echo "  Show this again with: bash setup.sh server --show"
}

need_debian
need_normal_user

if [ "$SHOW" = 1 ]; then
    sudo test -f "$CONFIG" || die "the server isn't installed yet. Run: bash setup.sh server"
    show
    exit 0
fi

cat <<EOF

${BOLD}Setting up the Slot17 display server${RESET} in $TARGET, on port $PORT.
EOF

apt_install python3 curl qrencode
python_ok || die "Python 3.10 or newer is needed. This system has $(python3 --version 2>&1)."

step "Installing the server"
if ! id slot17 >/dev/null 2>&1; then
    sudo useradd --system --home-dir "$TARGET" --no-create-home --shell /usr/sbin/nologin slot17
    ok "created the system user slot17"
fi
sudo install -d -o slot17 -g slot17 -m 0750 "$TARGET"
for file in server.py page.html drive_report.py icon-180.png icon-512.png; do
    sudo install -o root -g root -m 0644 "$PROJECT/display/$file" "$TARGET/$file"
done
# The app settings are yours to change, so an update never overwrites them.
if ! sudo test -f "$TARGET/app-config.json"; then
    sudo install -o slot17 -g slot17 -m 0644 "$PROJECT/display/app-config.json" "$TARGET/app-config.json"
fi
sudo install -d -o slot17 -g slot17 -m 0750 "$TARGET/logs" "$TARGET/sounds"
if sudo test -f "$CONFIG"; then
    ok "keeping the existing keys in $CONFIG"
else
    sudo -u slot17 python3 "$TARGET/server.py" --init --port "$PORT" >/dev/null
    sudo chmod 0600 "$CONFIG"
    ok "created new random keys in $CONFIG (only the slot17 user can read it)"
fi

sudo tee /etc/systemd/system/$SERVICE >/dev/null <<EOF
# Slot17 display server. Written by deploy/setup-server.sh.
[Unit]
Description=Slot17 display server
After=network-online.target
Wants=network-online.target

[Service]
User=slot17
Group=slot17
WorkingDirectory=$TARGET
ExecStart=$(command -v python3) $TARGET/server.py
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=always
RestartSec=3
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=$TARGET

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null 2>&1
sudo systemctl restart "$SERVICE"

step "Checking it answers"
for _ in $(seq 1 30); do
    if [ "$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" || true)" = 200 ]; then
        ok "the server answers on port $PORT"
        break
    fi
    sleep 0.5
done
[ "$(curl -s -m 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/healthz" || true)" = 200 ] \
    || { sudo journalctl -u "$SERVICE" -n 20 --no-pager; die "the server didn't start; the lines above say why."; }

step "Firewall"
if command -v ufw >/dev/null && sudo ufw status 2>/dev/null | grep -q "Status: active"; then
    sudo ufw allow "$PORT/tcp" >/dev/null
    ok "opened port $PORT in ufw"
else
    note "if this server has a firewall, or your hosting provider has one, allow incoming TCP on port $PORT."
fi

step "Finished. Keep these safe:"
show
cat <<EOF
  Status:   sudo systemctl status $SERVICE
  Log:      sudo journalctl -u $SERVICE -f
  Settings for the app (sounds, levels): $TARGET/app-config.json
EOF
