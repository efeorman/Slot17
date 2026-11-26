#!/usr/bin/env python3
"""deploy/install-rtlsdr.sh in a sandbox: sudo, apt-get, udevadm, usermod and friends are stand-ins that only
write down how they were called, the USB bus is a folder, and the blacklist goes to a temporary file. Nothing on
this computer is installed or changed. Linux only (it runs the script with bash).

    python3 scripts/test_install_rtlsdr.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "install-rtlsdr.sh"
SHIMS = {
    # sudo runs the command itself (env, tee and the stand-ins below), as root would.
    "sudo": 'echo "sudo $*" >>"$LOG"; case "$1" in -n|-v) exit 0 ;; esac; exec "$@"',
    "apt-get": 'echo "apt-get $*" >>"$LOG"',
    "udevadm": 'echo "udevadm $*" >>"$LOG"',
    "usermod": 'echo "usermod $*" >>"$LOG"',
    "rtl_eeprom": 'echo "rtl_eeprom $*" >>"$LOG"',
    "dpkg-query": 'echo 2.0.2-2+b1',
    "lsmod": 'printf "%s\\n" "Module Size Used by" "$FAKE_LSMOD"',
    "id": 'if [ "$1" = -nG ]; then echo "$FAKE_GROUPS"; else exec /usr/bin/id "$@"; fi',
}


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("bash"), "runs the script with bash on Linux")
class InstallRtlsdr(unittest.TestCase):
    def run_script(self, *args, sticks=(("0bda", "2838", "00000002"), ("0bda", "2838", "00000001")),
                   groups="pi adm plugdev", lsmod="", existing_blacklist=False, library="libc.so.6", etc_files=None):
        tmp = Path(tempfile.mkdtemp(dir=self.tmp.name))      # a sandbox of its own for every run
        bin_dir, usb, modprobe = tmp / "bin", tmp / "usb", tmp / "modprobe.d"
        for d in (bin_dir, usb, modprobe):
            d.mkdir(exist_ok=True)
        for name, body in SHIMS.items():
            p = bin_dir / name
            p.write_text("#!/bin/sh\n" + body + "\n")
            p.chmod(0o755)
        for k, (vid, pid, serial) in enumerate(sticks):
            dev = usb / f"1-{k + 1}"
            dev.mkdir()
            (dev / "idVendor").write_text(vid + "\n")
            (dev / "idProduct").write_text(pid + "\n")
            (dev / "serial").write_text(serial + "\n")
        hub = usb / "usb1"                            # a hub: other IDs, no serial
        hub.mkdir()
        (hub / "idVendor").write_text("1d6b\n")
        (hub / "idProduct").write_text("0003\n")
        (usb / "1-1:1.0").mkdir(exist_ok=True)        # an interface: no IDs at all
        if existing_blacklist:
            name = existing_blacklist if isinstance(existing_blacklist, str) else "blacklist-rtl.conf"
            (modprobe / name).write_text("blacklist dvb_usb_rtl28xxu\n")
        dirs = str(modprobe)
        if etc_files:                                 # a folder before it, as /etc/modprobe.d is before /lib's
            etc = tmp / "etc-modprobe.d"
            etc.mkdir()
            for name, text in etc_files.items():
                (etc / name).write_text(text)
            dirs = f"{etc} {modprobe}"
        self.blacklist = modprobe / "slot17-rtlsdr.conf"
        self.log = tmp / "calls.log"
        self.log.write_text("")
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", LOG=str(self.log), FAKE_GROUPS=groups,
                   FAKE_LSMOD=lsmod, SLOT17_RTLSDR_BLACKLIST=str(self.blacklist), SLOT17_MODPROBE_DIRS=dirs,
                   SLOT17_USB_SYSFS=str(usb), SLOT17_RTLSDR_LIB=library, USER="pi")
        r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=60)
        return r.returncode, r.stdout + r.stderr, self.log.read_text().splitlines()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_fresh_install(self):
        code, out, calls = self.run_script(groups="pi adm")
        self.assertEqual(code, 0, out)
        self.assertIn("sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq librtlsdr0 rtl-sdr", calls)
        text = self.blacklist.read_text()
        for module in ("dvb_usb_rtl28xxu", "rtl2832", "rtl2832_sdr", "rtl2830"):
            self.assertIn(f"blacklist {module}\n", text)
        triggers = [c for c in calls if c.startswith("udevadm trigger")]
        self.assertEqual(len(triggers), 2)
        self.assertTrue(all("--attr-match=idVendor=0bda" in c for c in triggers))
        self.assertEqual({c.split("idProduct=")[1] for c in triggers}, {"2838", "2832"})
        self.assertIn("usermod -aG plugdev pi", calls)
        self.assertIn("librtlsdr 2.0.2-2+b1 installed", out)
        self.assertIn("two RTL-SDRs plugged in (serials 00000002 00000001)", out)
        self.assertFalse(any(c.startswith("rtl_eeprom") for c in calls))

    def test_an_existing_blacklist_and_group_are_left_alone(self):
        code, out, calls = self.run_script(existing_blacklist=True)
        self.assertEqual(code, 0, out)
        self.assertFalse(self.blacklist.exists())
        self.assertIn("already kept off the sticks", out)
        self.assertFalse(any(c.startswith("usermod") for c in calls))

    def test_a_backup_file_is_not_a_blacklist(self):
        """modprobe reads only *.conf (modprobe.d(5)); until 24 November 2026 a backup counted (found in review)."""
        for backup in ("blacklist-rtl.conf.bak", "blacklist-rtl.conf.dpkg-old"):
            code, out, calls = self.run_script(existing_blacklist=backup)
            self.assertEqual(code, 0, out)
            self.assertTrue(self.blacklist.exists(), backup)
            self.assertIn("blacklist dvb_usb_rtl28xxu\n", self.blacklist.read_text())

    def test_a_blacklist_hidden_by_a_same_named_file_is_not_one(self):
        """modprobe.d(5): /etc's x.conf hides /lib's x.conf (found in the second review, 24 November 2026)."""
        code, out, calls = self.run_script(existing_blacklist=True,
                                           etc_files={"blacklist-rtl.conf": "# this machine's own, nothing about rtl\n"})
        self.assertEqual(code, 0, out)
        self.assertTrue(self.blacklist.exists())
        self.assertIn("blacklist dvb_usb_rtl28xxu\n", self.blacklist.read_text())

    def test_the_same_serial_twice_gets_a_hint_and_nothing_is_written_to_a_stick(self):
        code, out, calls = self.run_script(sticks=(("0bda", "2838", "00000001"), ("0bda", "2832", "00000001")))
        self.assertEqual(code, 0, out)
        self.assertIn("two sticks share a serial", out)
        self.assertIn("rtl_eeprom -s 00000002", out)
        self.assertFalse(any(c.startswith("rtl_eeprom") for c in calls))

    def test_the_number_of_sticks_is_said(self):
        for sticks, words in (((("0bda", "2838", "00000001"),), "one RTL-SDR plugged in; the pair needs two"),
                              ((), "no RTL-SDR plugged in yet"),
                              (tuple(("0bda", "2838", f"0000000{k}") for k in range(3)), "3 RTL-SDRs plugged in"),
                              ((("1d50", "60a1", "x"), ("0bda", "8153", "y")), "no RTL-SDR plugged in yet")):
            with self.subTest(words=words):
                code, out, _ = self.run_script(sticks=sticks)
                self.assertEqual(code, 0, out)
                self.assertIn(words, out)

    def test_a_loaded_television_driver_is_named(self):
        code, out, _ = self.run_script(lsmod="dvb_usb_rtl28xxu 61440 0")
        self.assertIn("the television driver is loaded right now", out)

    def test_check_changes_nothing(self):
        code, out, calls = self.run_script("--check", library="/no/such/librtlsdr.so.0")
        self.assertEqual(code, 1)
        self.assertIn("library: not installed", out)
        self.assertIn("television driver: not blacklisted", out)
        self.assertEqual(calls, [])
        self.assertFalse(self.blacklist.exists())
        code, out, calls = self.run_script("--check")
        self.assertEqual(code, 0, out)
        self.assertEqual(calls, [])

    def test_a_library_that_still_does_not_load_stops_the_setup(self):
        code, out, _ = self.run_script(library="/no/such/librtlsdr.so.0")
        self.assertEqual(code, 1)
        self.assertIn("doesn't load after installing librtlsdr0", out)


if __name__ == "__main__":
    unittest.main()
