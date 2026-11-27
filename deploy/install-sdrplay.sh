#!/usr/bin/env bash
# Install SDRplay's API for an RSP1B or RSP1A, with SDRplay's own installer.
#
#     bash deploy/install-sdrplay.sh                    shows SDRplay's licence and asks, as SDRplay's installer does
#     bash deploy/install-sdrplay.sh --accept-licence   unattended, for someone who has read the licence (below)
#     bash deploy/install-sdrplay.sh --check            installed and running? Changes nothing
#     bash deploy/install-sdrplay.sh --download-only DIR    download, check and unpack into DIR, then stop: nothing
#                                                           of SDRplay's runs or is installed (for testing)
#
# SDRplay's licence lets nobody pass the API on, so it is downloaded here, from sdrplay.com, and checked against
# the SHA-256 of the one version Slot17 was tested with (p0/sdrplay_ctypes.py refuses any other) before any of it
# runs. SDRplay's installer then puts:
#   - the library in /usr/local/lib (libsdrplay_api.so.3.15) and its headers in /usr/local/include;
#   - its service in /opt/sdrplay_api, run as root by systemd as sdrplay.service, started at every power-up;
#   - USB rules for SDRplay receivers in /etc/udev/rules.d/66-sdrplay.rules, and their names in the USB name list.
# The licence is SDRplay's: by installing, the person accepts it. Read it first at
#   https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-3.15.2.run (it is in the package, sdrplay_license.txt)
# Its section 6 says the API may send details of the receiver to SDRplay's server.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/common.sh
. "$HERE/common.sh"

VERSION=3.15.2
URL="https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-${VERSION}.run"
SHA256=3a97ca764263bbe76fb0f2220e6408942357e8864c19e1408a6d6987af382fe3
LIB=/usr/local/lib/libsdrplay_api.so.3.15
ACCEPT=0; CHECK=0; ONLY_TO=""
while [ $# -gt 0 ]; do
    case "$1" in
        --accept-licence|--accept-license) ACCEPT=1 ;;
        --check) CHECK=1 ;;
        --download-only) ONLY_TO="${2:?--download-only needs a folder}"; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (see bash deploy/install-sdrplay.sh --help)" ;;
    esac
    shift
done

# At power-up the detector waits for SDRplay's service, and that service, which carries the receiver's USB
# traffic, is never starved by the detector's own threads (found in review, 26 November 2026). Drop-ins, so neither
# unit file is edited; written again on every run.
SYSTEMD_DIR="${SLOT17_SYSTEMD_DIR:-/etc/systemd/system}"
order_services() {
    sudo mkdir -p "$SYSTEMD_DIR/tetra-detector.service.d" "$SYSTEMD_DIR/sdrplay.service.d"
    printf '[Unit]\nAfter=sdrplay.service\nWants=sdrplay.service\n' \
        | sudo tee "$SYSTEMD_DIR/tetra-detector.service.d/slot17-sdrplay.conf" >/dev/null
    printf '[Service]\nNice=-10\n' | sudo tee "$SYSTEMD_DIR/sdrplay.service.d/slot17-priority.conf" >/dev/null
    sudo systemctl daemon-reload >/dev/null 2>&1 || true
}
running() { systemctl is-active --quiet sdrplay.service 2>/dev/null; }
enabled() { systemctl is-enabled --quiet sdrplay.service 2>/dev/null; }
loads() { python3 -c 'import ctypes; ctypes.CDLL("libsdrplay_api.so.3")' >/dev/null 2>&1; }

if [ "$CHECK" = 1 ]; then
    [ -f "$LIB" ] && echo "library: $LIB" || echo "library: not installed"
    running && echo "service: running" || echo "service: not running"
    enabled && echo "service: starts at power-up" || echo "service: not enabled for power-up"
    [ -f "$LIB" ] && running && enabled && loads
    exit
fi

step "The SDRplay API ${VERSION}, for the RSP1B and RSP1A"
if [ -z "$ONLY_TO" ] && [ -f "$LIB" ] && running && loads; then
    # Running now is not starting at the next power-up (the second review, 24 November 2026, found this path
    # still took a disabled service as installed).
    if ! enabled; then
        sudo systemctl enable sdrplay.service >/dev/null 2>&1 && enabled \
            || die "SDRplay's service runs but can't be enabled for power-up: systemctl status sdrplay"
    fi
    order_services
    ok "already installed; its service runs and starts at every power-up, before the detector"
    exit 0
fi
arch="$(dpkg --print-architecture 2>/dev/null || uname -m)"
case "$arch" in
    arm64|amd64|armhf) ;;
    *) die "SDRplay's installer has no version for this computer ($arch)" ;;
esac
# libusb for the service; 'file', which SDRplay's installer calls to describe the system.
[ -n "$ONLY_TO" ] || apt_install curl ca-certificates libusb-1.0-0 file

if [ -n "$ONLY_TO" ]; then
    mkdir -p "$ONLY_TO"
    tmp="$(cd "$ONLY_TO" && pwd)"
else
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
fi
echo "  Downloading it from sdrplay.com ($(basename "$URL"), about 0.5 MB)."
curl -fsSL --retry 3 --max-time 120 -o "$tmp/api.run" "$URL" \
    || die "couldn't download $URL. Check the internet connection, then run this again."
if ! echo "$SHA256  $tmp/api.run" | sha256sum -c --quiet - >/dev/null 2>&1; then
    die "the download is not the version Slot17 was tested with (its SHA-256 differs), so none of it was run."
fi
ok "downloaded and checked (SHA-256 ${SHA256:0:16}...)"
# Makeself's own option: unpack only. SDRplay's installer is then run by hand, below.
sh "$tmp/api.run" --noexec --target "$tmp/api" >/dev/null
cd "$tmp/api"
[ -f install_lib.sh ] && [ -f sdrplay_license.txt ] || die "the package is not laid out as expected; nothing was installed"
if [ -n "$ONLY_TO" ]; then
    [ -f "$arch/libsdrplay_api.so.3.15" ] && [ -f "$arch/sdrplay_apiService" ] \
        || die "the package has no $arch files"
    ok "unpacked into $tmp/api for $arch; nothing of SDRplay's was run or installed"
    exit 0
fi

if [ "$ACCEPT" = 1 ]; then
    echo "  Installing with SDRplay's licence accepted (--accept-licence). Its text: $HOME/sdrplay_license.txt"
    # SDRplay's installer asks three things: RETURN to see the licence, y to accept it, y for its default folders.
    printf '\ny\ny\n' | bash ./install_lib.sh >"$tmp/install.log" 2>&1 \
        || { tail -20 "$tmp/install.log"; die "SDRplay's installer failed (above)"; }
else
    [ -t 0 ] || die "SDRplay's licence has to be accepted by you: run this in a terminal, or with --accept-licence once you have read it"
    echo "  SDRplay's installer shows its licence next and asks you to accept it; then it asks to use its"
    echo "  default folders: answer y."
    bash ./install_lib.sh
fi

[ -f "$LIB" ] || die "SDRplay's installer finished, but $LIB isn't there"
# Enabled, not only running: the detector needs it at every power-up (until 24 November 2026 a failed enable was
# passed over and "starts at every power-up" rested on it running now; found in review).
sudo systemctl enable sdrplay.service >/dev/null 2>&1 || die "SDRplay's service can't be enabled: systemctl status sdrplay"
systemctl is-enabled --quiet sdrplay.service || die "SDRplay's service isn't enabled for power-up: systemctl status sdrplay"
sudo systemctl start sdrplay.service || true
sleep 2
running || die "the SDRplay service doesn't run: journalctl -u sdrplay -n 30 --no-pager"
loads || die "the SDRplay library doesn't load: try sudo ldconfig"
order_services
ok "installed; its service runs as sdrplay.service and starts at every power-up, before the detector"
note "SDRplay's installer asks for a restart for its USB rules: the next power-up does that."
