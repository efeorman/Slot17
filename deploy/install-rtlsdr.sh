#!/usr/bin/env bash
# Install what a pair of RTL-SDRs needs (receiver rtlsdr2), from the computer's own package list.
#
#     bash deploy/install-rtlsdr.sh            install, and keep the television driver off the sticks
#     bash deploy/install-rtlsdr.sh --check    what is installed and plugged in; changes nothing
#
# What it does:
#   - apt: librtlsdr0, the library p0/rtlsdr_ctypes.py loads (librtlsdr.so.0), and rtl-sdr, its tools (rtl_test,
#     rtl_eeprom). Debian 13 has 2.0.2, which knows the RTL-SDR Blog V4 too. The package's udev rules give the
#     sticks to the plugdev group; the detector's service runs with that group (SupplementaryGroups=plugdev),
#     and this user is added to it for setup's own calibration capture.
#   - /etc/modprobe.d/slot17-rtlsdr.conf: Linux's DVB-T television drivers would otherwise take each stick at
#     power-up. librtlsdr detaches them when it opens a stick, so this is belt and braces, and it is written only
#     when nothing on this computer blacklists them yet.
#   - RTL-SDR Blog's own driver, built from source at a fixed commit into /usr/local, when a stick needs it: a V4L
#     (R828S tuner, sold since the V4 ended), or a V4 (R828D) with a librtlsdr older than 2.0.0 (Debian 12 has
#     0.6.0). An older driver takes the V4L for an R820T and may hear nothing, and tunes the V4 with the wrong
#     reference. The detector loads that build when it is there (p0/rtlsdr_ctypes.py); the system's own package
#     stays. Needs internet, once.
#   - checks: the library loads, and how many sticks are plugged in, with their serials, read from the USB bus
#     without opening anything. Two sticks with one serial get a hint: rtl_eeprom changes a serial, but that is
#     writing to the stick, so this script only says how.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/common.sh
. "$HERE/common.sh"

# The real places; scripts/test_install_rtlsdr.py points these at a sandbox.
BLACKLIST="${SLOT17_RTLSDR_BLACKLIST:-/etc/modprobe.d/slot17-rtlsdr.conf}"
MODPROBE_DIRS="${SLOT17_MODPROBE_DIRS:-/etc/modprobe.d /run/modprobe.d /lib/modprobe.d /usr/lib/modprobe.d}"
USB_SYSFS="${SLOT17_USB_SYSFS:-/sys/bus/usb/devices}"
LIBRARY="${SLOT17_RTLSDR_LIB:-librtlsdr.so.0}"
BLOG_URL="https://github.com/rtlsdrblog/rtl-sdr-blog"
BLOG_TAG="V1.4.0"
BLOG_COMMIT="aed0ea19f3a273370a13c9009b96313c75d54c7b"
BLOG_PREFIX="${SLOT17_RTLSDR_BLOG_PREFIX:-/usr/local}"
BLOG_MARK="$BLOG_PREFIX/share/slot17/rtl-sdr-blog.commit"
CHECK=0
while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK=1 ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "unknown option: $1 (see bash deploy/install-rtlsdr.sh --help)" ;;
    esac
    shift
done

loads() { python3 -c 'import ctypes, sys; ctypes.CDLL(sys.argv[1])' "$LIBRARY" >/dev/null 2>&1; }
# modprobe reads only the *.conf files directly in these folders (modprobe.d(5)): a backup such as
# x.conf.bak or x.conf.dpkg-old holds nothing (until 24 November 2026 every file counted; found in review).
# A file in an earlier folder of the list hides one of the same name in a later folder, as /etc's hides
# /lib's (the second review found that missing too).
blacklisted() {
    local d f seen=" "
    # shellcheck disable=SC2086  # MODPROBE_DIRS is a list of folders, in modprobe's order
    for d in $MODPROBE_DIRS; do
        for f in "$d"/*.conf; do
            [ -f "$f" ] || continue
            case "$seen" in *" ${f##*/} "*) continue ;; esac
            seen="$seen${f##*/} "
            grep -qsE '^\s*blacklist\s+dvb_usb_rtl28xxu\b' "$f" && return 0
        done
    done
    return 1
}
# One line per RTL-SDR on the USB bus: its serial (or ?), from sysfs, so nothing is opened.
sticks() {
    local d v p
    for d in "$USB_SYSFS"/*; do
        [ -f "$d/idVendor" ] && [ -f "$d/idProduct" ] || continue
        v="$(cat "$d/idVendor")"; p="$(cat "$d/idProduct")"
        if [ "$v" = 0bda ] && { [ "$p" = 2838 ] || [ "$p" = 2832 ]; }; then
            cat "$d/serial" 2>/dev/null || echo "?"
        fi
    done
}
# One line per RTL-SDR: its USB maker and product strings, from sysfs ("RTLSDRBlog|Blog V4L").
models() {
    local d v p
    for d in "$USB_SYSFS"/*; do
        [ -f "$d/idVendor" ] && [ -f "$d/idProduct" ] || continue
        v="$(cat "$d/idVendor")"; p="$(cat "$d/idProduct")"
        if [ "$v" = 0bda ] && { [ "$p" = 2838 ] || [ "$p" = 2832 ]; }; then
            printf '%s|%s\n' "$(cat "$d/manufacturer" 2>/dev/null)" "$(cat "$d/product" 2>/dev/null)"
        fi
    done
}
blog_built() { [ "$(cat "$BLOG_MARK" 2>/dev/null)" = "$BLOG_COMMIT" ]; }
# Why these sticks need RTL-SDR Blog's own driver (printed), or nothing (status 1).
blog_needed() {
    local m version
    m="$(models)"
    if printf '%s\n' "$m" | grep -q '^RTLSDRBlog|Blog V4L'; then echo "an RTL-SDR Blog V4L"; return 0; fi
    if printf '%s\n' "$m" | grep -q '^RTLSDRBlog|Blog V4'; then
        version="$(dpkg-query -W -f='${Version}' librtlsdr0 2>/dev/null || true)"
        if [ -n "$version" ] && dpkg --compare-versions "$version" ge 2.0.0; then return 1; fi
        echo "an RTL-SDR Blog V4 with librtlsdr ${version:-not installed}"; return 0
    fi
    return 1
}
build_blog() {
    local why="$1" tmp
    echo "  $why needs RTL-SDR Blog's own driver: building $BLOG_TAG from source (a few minutes)."
    apt_install git cmake build-essential pkg-config libusb-1.0-0-dev
    tmp="$(mktemp -d)"
    git clone --quiet "$BLOG_URL" "$tmp/src" \
        || die "couldn't download RTL-SDR Blog's driver ($BLOG_URL). Check the internet connection, then run this again."
    git -C "$tmp/src" checkout --quiet "$BLOG_COMMIT" 2>/dev/null \
        || die "RTL-SDR Blog's driver has no commit $BLOG_COMMIT ($BLOG_TAG); nothing was built"
    [ "$(git -C "$tmp/src" rev-parse HEAD)" = "$BLOG_COMMIT" ] \
        || die "the download isn't the version Slot17 was checked with; nothing was built"
    { cmake -S "$tmp/src" -B "$tmp/build" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$BLOG_PREFIX" \
          -DDETACH_KERNEL_DRIVER=ON -DINSTALL_UDEV_RULES=OFF \
      && make -C "$tmp/build" -j"$(nproc 2>/dev/null || echo 2)"; } >"$tmp/build.log" 2>&1 \
        || { tail -20 "$tmp/build.log"; die "building RTL-SDR Blog's driver failed (above)"; }
    sudo make -C "$tmp/build" install >>"$tmp/build.log" 2>&1 || { tail -20 "$tmp/build.log"; die "installing it failed"; }
    sudo ldconfig 2>/dev/null || true
    sudo mkdir -p "$(dirname "$BLOG_MARK")"
    echo "$BLOG_COMMIT" | sudo tee "$BLOG_MARK" >/dev/null
    rm -rf "$tmp"
    ok "RTL-SDR Blog's driver $BLOG_TAG built into $BLOG_PREFIX; the detector uses it"
}
report_sticks() {
    local serials count
    serials="$(sticks)"
    count="$(printf '%s' "$serials" | grep -c . || true)"
    case "$count" in
        2) ok "two RTL-SDRs plugged in (serials $(printf '%s' "$serials" | tr '\n' ' '| sed 's/ $//'))" ;;
        0) note "no RTL-SDR plugged in yet: plug both in (a powered hub, or the computer's own ports)" ;;
        1) note "one RTL-SDR plugged in; the pair needs two" ;;
        *) note "$count RTL-SDRs plugged in; Slot17 uses exactly two, so unplug the others" ;;
    esac
    if [ "$count" -ge 2 ] && [ "$(printf '%s\n' "$serials" | sort | uniq -d | grep -c .)" -gt 0 ]; then
        note "two sticks share a serial, so which is A can change between power-ups. Up to 3 ppm apart they share"
        note "one correction; further apart the detector won't start. Then change one: plug in only that one and run: rtl_eeprom -s 00000002"
        note "(it writes to the stick, so it is yours to do), then unplug it and plug both back in."
    fi
}

if [ "$CHECK" = 1 ]; then
    loads && echo "library: $LIBRARY loads" || echo "library: not installed"
    blacklisted && echo "television driver: blacklisted" || echo "television driver: not blacklisted"
    lsmod 2>/dev/null | grep -q '^dvb_usb_rtl28xxu' && echo "television driver: loaded now" || true
    report_sticks
    if why="$(blog_needed)"; then
        blog_built && echo "RTL-SDR Blog driver: $BLOG_TAG built, for $why" \
            || echo "RTL-SDR Blog driver: needed for $why, not built (run this without --check)"
    fi
    loads
    exit
fi

step "Drivers for the RTL-SDR pair"
apt_install librtlsdr0 rtl-sdr
loads || die "$LIBRARY doesn't load after installing librtlsdr0: try sudo ldconfig"
ok "librtlsdr $(dpkg-query -W -f='${Version}' librtlsdr0 2>/dev/null || echo '?') installed"
if why="$(blog_needed)"; then
    if blog_built; then ok "RTL-SDR Blog's driver $BLOG_TAG is already built, for $why"; else build_blog "$why"; fi
fi

if blacklisted; then
    ok "the DVB-T television driver is already kept off the sticks"
else
    printf '%s\n' "# Slot17 (deploy/install-rtlsdr.sh): keep Linux's DVB-T television drivers off the RTL-SDR sticks." \
        "# librtlsdr detaches them anyway when it opens a stick; this keeps them from taking it at power-up." \
        "blacklist dvb_usb_rtl28xxu" "blacklist rtl2832" "blacklist rtl2832_sdr" "blacklist rtl2830" \
        | sudo tee "$BLACKLIST" >/dev/null
    ok "the DVB-T television driver is kept off the sticks ($BLACKLIST)"
fi
if lsmod 2>/dev/null | grep -q '^dvb_usb_rtl28xxu'; then
    note "the television driver is loaded right now; librtlsdr detaches it when the detector opens the sticks, and it stays off after the next power-up"
fi

# The package's rules apply to sticks plugged in from now on; apply them to the ones already there.
sudo udevadm control --reload-rules 2>/dev/null || true
for product in 2838 2832; do
    sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=0bda --attr-match="idProduct=$product" 2>/dev/null || true
done
if id -nG "$USER" | grep -qw plugdev; then
    ok "$USER may open the sticks (group plugdev)"
else
    sudo usermod -aG plugdev "$USER"
    note "$USER added to the plugdev group, to open the sticks; it applies from the next login (the detector's service has it already)"
fi
report_sticks
