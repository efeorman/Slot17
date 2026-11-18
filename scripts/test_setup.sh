#!/usr/bin/env bash
# End-to-end test of the installer and both setups, on a disposable Linux machine with systemd.
# It installs real services and permission rules, so never run it on a machine you use.
#
#     bash scripts/test_setup.sh full          release installer, server, car computer, uninstall
#     bash scripts/test_setup.sh distro IMAGE  packages and checks inside a Docker image
#
# Needs passwordless sudo. Used by .github/workflows/linux-setup.yml and codemagic.yaml.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
pass() { printf '\033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '\033[31mFAIL\033[0m %s\n' "$*" >&2; exit 1; }
check() { local what="$1"; shift; if "$@"; then pass "$what"; else fail "$what"; fi; }
config() { sudo python3 -c 'import json, sys; print(json.load(open("/opt/slot17-display/config.json"))[sys.argv[1]])' "$1"; }
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

distro() {
    local image="$1"
    docker run --rm -v "$ROOT:/src:ro" "$image" bash -euc '
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq sudo ca-certificates >/dev/null
        useradd -m tester
        echo "tester ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/tester
        cp -r /src /home/tester/Slot17
        chown -R tester /home/tester/Slot17
        su - tester -c "cd ~/Slot17 && bash setup.sh car --yes --no-services"
        su - tester -c "cd ~/Slot17 && python -B -m unittest display/test_display.py 2>&1 | tail -3"
    '
    pass "packages, offline checks and display tests on $image"
}

full() {
    bash scripts/make_release.sh HEAD
    local tarball="$ROOT/dist/slot17.tar.gz" server_copy="$HOME/server-copy"

    echo "--- server, installed through install.sh"
    SLOT17_TARBALL="$tarball" SLOT17_DIR="$server_copy" bash dist/install.sh server --yes --address 127.0.0.1
    bash "$server_copy/setup.sh" server --show
    check "server service is running" systemctl is-active --quiet slot17-display.service
    check "health answers 200" test "$(code http://127.0.0.1:5732/healthz)" = 200
    check "data needs a key" test "$(code http://127.0.0.1:5732/api/state)" = 401
    local key; key="$(config viewer_key)"
    check "private link sets the cookie" test "$(code "http://127.0.0.1:5732/?key=$key")" = 303
    check "page opens with the cookie" test "$(code -b "s17=$key" http://127.0.0.1:5732/)" = 200
    check "keys are private to the service user" test "$(sudo stat -c '%a %U' /opt/slot17-display/config.json)" = "600 slot17"
    sudo sha256sum /opt/slot17-display/config.json > /tmp/slot17-keys-before

    echo "--- car computer, installed through install.sh"
    SLOT17_TARBALL="$tarball" bash dist/install.sh car --yes --server 127.0.0.1 --pi-key "$(config pi_token)" \
        --no-calibrate --no-notify --no-start
    bash "$HOME/Slot17/setup.sh" car --check
    check "the python command works" python --version
    check "detector starts at power-up" test "$(systemctl is-enabled tetra-detector.service)" = enabled
    check "publisher follows the detector" test "$(systemctl is-enabled tetra-display-publisher.service)" = enabled
    check "display settings are private" test "$(stat -c '%a' "$HOME/.config/slot17-display.json")" = 600
    # Only our two files: a full "visudo -c" also judges the machine's own sudo setup.
    sudo visudo -c -f /etc/sudoers.d/60-slot17-stop || true
    sudo visudo -c -f /etc/sudoers.d/61-slot17-poweroff || true
    sudo visudo -c || true
    check "stop rule is valid" sudo visudo -c -q -f /etc/sudoers.d/60-slot17-stop
    check "power-off rule is valid" sudo visudo -c -q -f /etc/sudoers.d/61-slot17-poweroff
    check "rules are owned by root and read-only" test "$(sudo stat -c '%a %U' /etc/sudoers.d/60-slot17-stop /etc/sudoers.d/61-slot17-poweroff | sort -u)" = "440 root"
    local me; me="$(id -un)"
    check "stop permission is exactly one command" sudo grep -qx "$me ALL=(root) NOPASSWD: /usr/bin/systemctl --no-block stop tetra-detector.service" /etc/sudoers.d/60-slot17-stop
    check "power-off permission is exactly one command" sudo grep -qx "$me ALL=(root) NOPASSWD: /usr/bin/systemctl --no-block poweroff" /etc/sudoers.d/61-slot17-poweroff
    check "service files are valid" systemd-analyze verify /etc/systemd/system/tetra-detector.service /etc/systemd/system/tetra-display-publisher.service
    check "logs are kept across power cuts" test -f /etc/systemd/journald.conf.d/60-slot17-persistent.conf

    echo "--- the car computer reaches the server"
    python "$HOME/Slot17/display/display_publisher.py" --once
    check "the on/off shortcut says on" test "$(curl -s "http://127.0.0.1:5732/api/pi-on?token=$(config pi_status_key)")" = on
    check "the phone sees the car computer's state" bash -c "curl -s -b 's17=$key' http://127.0.0.1:5732/api/state | python -c 'import json, sys; assert json.load(sys.stdin)[\"seq\"] >= 1'"

    echo "--- without a receiver the detector fails once and does not retry"
    if sudo systemctl start tetra-detector.service; then fail "the detector started without a receiver"; fi
    sleep 15
    check "no automatic restart" test "$(systemctl show tetra-detector.service -p NRestarts --value)" = 0
    check "detector is not running" bash -c '! systemctl is-active --quiet tetra-detector.service'
    check "publisher is not running" bash -c '! systemctl is-active --quiet tetra-display-publisher.service'
    journalctl -u tetra-detector.service --no-pager | grep -E 'offline_preflight_pass|Ran [0-9]+ tests|airspy|receiver' | tail -6 || true

    echo "--- running both installers again"
    SLOT17_TARBALL="$tarball" SLOT17_DIR="$server_copy" bash dist/install.sh server --yes --address 127.0.0.1
    check "an update keeps the server's keys" sudo sha256sum -c --quiet /tmp/slot17-keys-before
    SLOT17_TARBALL="$tarball" bash dist/install.sh car --yes --no-calibrate --no-notify --no-start
    check "an update keeps the display settings" test -f "$HOME/.config/slot17-display.json"

    echo "--- uninstall"
    sudo bash "$HOME/Slot17/deploy/install-publisher.sh" --uninstall
    sudo bash "$HOME/Slot17/deploy/install-service.sh" --uninstall
    check "permissions removed" test ! -e /etc/sudoers.d/60-slot17-stop
    check "detector service removed" test ! -e /etc/systemd/system/tetra-detector.service
    echo "ALL PASSED"
}

case "${1:-}" in
    full) full ;;
    distro) distro "${2:?give a Docker image, for example debian:13}" ;;
    *) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
