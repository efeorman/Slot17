#!/usr/bin/env bash
# Build the ready-made Slot17 image: Raspberry Pi OS Lite (64-bit) with Slot17's packages and this
# release on it, SSH on, and a first-start service that finishes the setup from slot17.txt.
#
#     sudo bash scripts/build_image.sh dist/slot17.tar.gz dist/install.sh VERSION OUTDIR
#
# Runs on an x86 Linux machine (GitHub Actions) with qemu-user-static, parted, e2fsprogs, xz-utils and
# curl. The base image comes from raspberrypi.com and is checked against its published SHA-256. Before
# packing, Slot17's own offline checks run inside the image, under ARM emulation.
set -euo pipefail
TARBALL="$(realpath "$1")"; INSTALLER="$(realpath "$2")"; VERSION="$3"; OUT="$(realpath -m "$4")"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE_URL="${SLOT17_BASE_IMAGE:-https://downloads.raspberrypi.com/raspios_lite_arm64_latest}"
GROW_MB=1536
work="$(mktemp -d)"; mnt="$work/root"
loop=""
cleanup() {
    set +e
    for m in "$mnt/boot/firmware" "$mnt/dev/pts" "$mnt/dev" "$mnt/proc" "$mnt/sys" "$mnt"; do mountpoint -q "$m" && umount -l "$m"; done
    [ -n "$loop" ] && losetup -d "$loop"
    rm -rf "$work"
}
trap cleanup EXIT
[ "$(id -u)" = 0 ] || { echo "run this with sudo" >&2; exit 1; }

echo "== Fetching Raspberry Pi OS Lite (64-bit)"
real="$(curl -fsSIL -o /dev/null -w '%{url_effective}' "$BASE_URL")"
echo "   $real"
curl -fsSL "$real" -o "$work/os.img.xz"
want="$(curl -fsSL "$real.sha256" | awk '{print $1}')"
got="$(sha256sum "$work/os.img.xz" | awk '{print $1}')"
[ -n "$want" ] && [ "$want" = "$got" ] || { echo "checksum does not match ($got, expected $want)" >&2; exit 1; }
echo "   checksum ok"
xz -d "$work/os.img.xz"
img="$work/os.img"

echo "== Making room for Slot17"
truncate -s "+${GROW_MB}M" "$img"
parted -s "$img" resizepart 2 100%
loop="$(losetup -Pf --show "$img")"
e2fsck -fy "${loop}p2" >/dev/null || true
resize2fs "${loop}p2" >/dev/null
mkdir -p "$mnt"
mount "${loop}p2" "$mnt"
mount "${loop}p1" "$mnt/boot/firmware"
mount -t proc proc "$mnt/proc"; mount -t sysfs sys "$mnt/sys"
mount --bind /dev "$mnt/dev"; mount --bind /dev/pts "$mnt/dev/pts"
cp /etc/resolv.conf "$mnt/etc/resolv.conf.slot17"
mount --bind "$mnt/etc/resolv.conf.slot17" "$mnt/etc/resolv.conf" 2>/dev/null || cp /etc/resolv.conf "$mnt/etc/resolv.conf"

echo "== Installing Slot17's packages (under ARM emulation, a few minutes)"
chroot "$mnt" /usr/bin/env DEBIAN_FRONTEND=noninteractive bash -euo pipefail -c '
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 python3-numpy python3-scipy python3-psutil python3-cryptography usbutils airspy libairspy0 python-is-python3 curl >/dev/null
apt-get clean
'

echo "== Adding this release and the first-start setup"
install -d -m 755 "$mnt/opt/slot17"
install -m 644 "$TARBALL" "$mnt/opt/slot17/slot17.tar.gz"
install -m 755 "$INSTALLER" "$mnt/opt/slot17/install.sh"
install -m 755 "$HERE/deploy/image/firstboot.sh" "$mnt/opt/slot17/firstboot.sh"
install -m 755 "$HERE/deploy/image/firstboot_config.py" "$mnt/opt/slot17/firstboot_config.py"
install -m 644 "$HERE/deploy/image/slot17-firstboot.service" "$mnt/etc/systemd/system/slot17-firstboot.service"
echo "$VERSION" > "$mnt/opt/slot17/VERSION"
install -m 644 "$HERE/deploy/image/slot17.txt" "$mnt/boot/firmware/slot17.txt"
chroot "$mnt" systemctl enable slot17-firstboot.service ssh >/dev/null 2>&1
touch "$mnt/boot/firmware/ssh"   # SSH on from the first start, on top of Imager's own setting

echo "== Slot17's offline checks inside the image (ARM emulation, slow)"
# Emulation runs about ten times slower than a Pi 5. The detector has a real-time deadline (90 % of each
# window) and skips what it can't finish in time, so under emulation the checks that send bursts through
# the live pipeline find nothing, and the update check's 120 s limit runs out. Those few are left out here
# only: the Pi runs the full suite, them included, on its first start, before it opens the receiver
# (no test record yet, so --tests-if-changed runs everything). Every other check runs here.
cat >"$mnt/tmp/slot17-image-checks.py" <<'PY'
import json, subprocess, sys, unittest
REALTIME = (
    "test_own_burst.GeneratedSignal.test_a_real_burst_beside_a_broadband_spike_is_inspected_first_and_matched",
    "test_overlap.OverlappingWindows.test_every_burst_is_recognised_exactly_once_at_its_time",
    "test_overlap.OverlappingWindows.test_the_old_windows_lost_the_burst_on_their_edge",
    "test_update.RealChecks.test_the_real_offline_checks_refuse_a_release_that_breaks_the_detector",
)
pre = subprocess.run([sys.executable, "-B", "p0/preflight.py", "--offline"], capture_output=True, text=True)
out = pre.stdout
result = json.loads(out[out.index("{"):])
if not result["offline_preflight_pass"]:
    print(out, pre.stderr[-3000:]); sys.exit("offline checks failed: %s" % result["errors"])
sys.path.insert(0, "tests")
def flat(suite):
    for t in suite:
        yield from (flat(t) if isinstance(t, unittest.TestSuite) else [t])
tests = list(flat(unittest.defaultTestLoader.discover("tests")))
kept = unittest.TestSuite(t for t in tests if t.id() not in REALTIME)
left_out = len(tests) - kept.countTestCases()
print(f"{len(tests)} tests, {left_out} left out here (real-time speed; the Pi runs them at first start)")
if left_out != len(REALTIME):
    sys.exit("the list of real-time checks no longer matches the tests: update build_image.sh")
ok = unittest.TextTestRunner(verbosity=1).run(kept).wasSuccessful()
sys.exit(0 if ok else "regression tests failed")
PY
chroot "$mnt" bash -euo pipefail -c '
d="$(mktemp -d)"; tar -xzf /opt/slot17/slot17.tar.gz -C "$d"
cd "$d/slot17" && python3 -B /tmp/slot17-image-checks.py >"$d/checks.log" 2>&1 || {
    grep -E "^(FAIL|ERROR):|^Traceback|Error|^Ran [0-9]|failed|no longer matches" -A 4 "$d/checks.log" | head -120
    exit 1; }
grep -E "left out here|^Ran [0-9]+ tests" "$d/checks.log"; rm -rf "$d"
'
rm -f "$mnt/tmp/slot17-image-checks.py"

echo "== Packing"
umount -l "$mnt/etc/resolv.conf" 2>/dev/null || true
rm -f "$mnt/etc/resolv.conf.slot17"
for m in "$mnt/boot/firmware" "$mnt/dev/pts" "$mnt/dev" "$mnt/proc" "$mnt/sys" "$mnt"; do umount -l "$m"; done
losetup -d "$loop"; loop=""
mkdir -p "$OUT"
name="slot17-$VERSION-raspios-lite-arm64.img"
mv "$img" "$OUT/$name"
xz -T0 -6 "$OUT/$name"
(cd "$OUT" && sha256sum "$name.xz" > "$name.xz.sha256")
ls -l "$OUT"
