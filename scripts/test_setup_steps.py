"""deploy/setup-car.sh's steps, run for real in bash with stand-ins for the receiver, sudo and Python: how often one
setup opens the receiver, which capture calibration measures, and when the receiver choice is saved.

Found in review on 24 November 2026:
  * accepting both default answers calibrated (one opening of the Airspy) and then started the detector (a second
    one) in the same power-up, which the project's rules forbid for a board that can stop responding at a second
    start;
  * the receiver choice was saved before its driver was in, and calibration measured the newest captures,
    whatever made them (tested here since the second review by running the steps, not by reading their text:
    those tests sat in tests/, where the updater's copy has no deploy/ folder, and made every release fail).
The steps are cut out of the real script by their headings and run with the script's own shell options. Needs
bash (Git Bash on Windows); skipped without it.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "deploy" / "setup-car.sh"


def find_bash():
    if os.name == "nt":                      # not System32's bash.exe, which is WSL's
        for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if Path(candidate).is_file():
                return candidate
        return None
    return shutil.which("bash")


BASH = find_bash()
STAND_INS = r'''
step() { :; }
ok() { echo "ok: $*"; }
note() { echo "note: $*"; }
die() { echo "stopped: $*"; exit 1; }
ask_yes() { [ "$2" = y ]; }                       # every question answered with its default
ask_text() { echo ""; }
airspy_present() { [ "$PLUGGED" = 1 ]; }
receiver_present() { [ "$PLUGGED" = 1 ]; }
receiver_name() { echo "the $1"; }
configured_receiver() { echo ""; }
apt_install() { echo "apt_install $*" >> "$LOG"; }
lsusb() { :; }
sudo() { echo "sudo $*" >> "$LOG"; return 0; }
journalctl() { case "$*" in *"-b -u"*) [ -n "${JOURNAL_THIS_BOOT:-}" ] && echo "$JOURNAL_THIS_BOOT" ;; esac; return 0; }
systemctl() { [ "$1" = show ] && echo "${ACTIVE_SINCE_BOOT:-0}"; return 0; }   # the service's last start, this boot
bash() { echo "bash $*" >> "$LOG"; return "${DRIVER_RC:-0}"; }
python3() {
    echo "python3 $*" >> "$LOG"
    case "$*" in
        *"import paths"*) echo "$IQ" ;;                                   # where captures go
        *capture.py*)                                                     # a capture, named with its tag
            local tag="" prev=""
            for a in "$@"; do [ "$prev" = --tag ] && tag="$a"; prev="$a"; done
            : > "$IQ/20261124-120000_${tag}_392.5000MHz_6Msps.iq" ;;
    esac
    return 0
}
'''


def section(first, until):
    """setup-car.sh's text from the heading `first` up to `until`, with the script's own `set` line before it."""
    text = SETUP.read_text(encoding="utf-8")
    options = re.search(r"^set -[a-z]+.*$", text, re.M).group(0)
    start = text.index(first)
    return options + "\n" + STAND_INS + text[start:text.index(until, start)]


@unittest.skipUnless(BASH, "needs bash")
class Steps(unittest.TestCase):
    def run_section(self, first, until, **env):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "p0").mkdir()
            (t / "iq").mkdir()
            old = t / "iq" / "20261101-010000_downlink-calibration_392.5000MHz_6Msps.iq"   # another receiver's
            old.write_bytes(b"")
            log = t / "calls.log"
            log.write_text("")
            script = t / "steps.sh"
            script.write_text(section(first, until), encoding="utf-8", newline="\n")
            full = dict(os.environ, LOG=log.as_posix(), IQ=(t / "iq").as_posix(), PROJECT=t.as_posix(),
                        HERE=(t / "deploy").as_posix(), SERVICE="tetra-detector.service", CALIBRATE="ask",
                        START="ask", AUTOSTART="1", RECEIVER="airspy", YES="1", PLUGGED="1", NO_SERVICES="0",
                        ACCEPT_SDRPLAY="0", RECEIVER_PY=(t / "p0" / "receiver.py").as_posix())
            full.update({k: str(v) for k, v in env.items()})
            done = subprocess.run([BASH, script.as_posix()], env=full, capture_output=True, text=True, timeout=60)
            calls = log.read_text().splitlines()
        return done.returncode, calls, done.stdout + done.stderr

    def calibrate_and_start(self, **env):
        rc, calls, out = self.run_section("# --- 11. calibration", 'step "Finished"', **env)
        captures = [c for c in calls if "capture.py" in c]
        starts = [c for c in calls if c.startswith("sudo systemctl start")]
        return captures, starts, calls, out


class OneOpeningPerSetup(Steps):
    def test_the_default_answers_open_the_receiver_once(self):
        captures, starts, _, out = self.calibrate_and_start()
        self.assertEqual((len(captures), len(starts)), (1, 0), out)
        self.assertIn("starts by itself at the next power-up", out)

    def test_calibrating_and_an_explicit_start_still_open_it_once(self):
        captures, starts, _, out = self.calibrate_and_start(CALIBRATE="yes", START="yes")
        self.assertEqual((len(captures), len(starts)), (1, 0), out)

    def test_without_calibration_it_is_started(self):
        captures, starts, _, out = self.calibrate_and_start(CALIBRATE="no")
        self.assertEqual((len(captures), len(starts)), (0, 1), out)

    def test_without_autostart_the_note_says_how_to_start_it(self):
        captures, starts, _, out = self.calibrate_and_start(AUTOSTART="0")
        self.assertEqual(len(starts), 0, out)
        self.assertIn("sudo systemctl start tetra-detector.service", out)

    def test_no_receiver_plugged_in_opens_nothing(self):
        captures, starts, _, out = self.calibrate_and_start(PLUGGED="0")
        self.assertEqual((len(captures), len(starts)), (0, 0), out)

    # Found in review on 26 November 2026: the setup runs only with the detector stopped, so a re-run in the same
    # power-up (an update, Bluetooth or the screen switched later) came after an opening, and its defaults calibrated
    # or started the receiver again.
    def test_after_the_detector_ran_this_power_up_the_defaults_open_nothing(self):
        captures, starts, _, out = self.calibrate_and_start(ACTIVE_SINCE_BOOT="81234567")
        self.assertEqual((len(captures), len(starts)), (0, 0), out)
        self.assertIn("already used the receiver in this power-up", out)
        self.assertIn("starts by itself at the next power-up", out)

    def test_a_detector_without_autostart_is_seen_in_this_boots_journal(self):
        """systemd forgets a stopped unit nothing wants, so its activation time reads 0 again."""
        captures, starts, _, out = self.calibrate_and_start(JOURNAL_THIS_BOOT="READY: first analysis window completed")
        self.assertEqual((len(captures), len(starts)), (0, 0), out)

    def test_after_it_ran_an_explicit_calibration_is_done_and_nothing_more(self):
        captures, starts, _, out = self.calibrate_and_start(ACTIVE_SINCE_BOOT="81234567", CALIBRATE="yes", START="yes")
        self.assertEqual((len(captures), len(starts)), (1, 0), out)
        self.assertIn("if it stops responding", out)

    def test_after_it_ran_an_explicit_start_waits_for_the_next_power_up(self):
        captures, starts, _, out = self.calibrate_and_start(ACTIVE_SINCE_BOOT="81234567", CALIBRATE="no", START="yes")
        self.assertEqual((len(captures), len(starts)), (0, 0), out)

    def test_a_start_asked_for_still_needs_the_whole_receiver(self):
        """First boot asks to start when any known receiver is on USB; one stick of a pair isn't the receiver."""
        captures, starts, _, out = self.calibrate_and_start(CALIBRATE="no", START="yes", PLUGGED="0",
                                                            RECEIVER="rtlsdr2")
        self.assertEqual(len(starts), 0, out)
        self.assertIn("isn't all plugged in", out)


class CalibrationOfItsOwnCapture(Steps):
    def test_calibration_measures_the_capture_it_just_made_and_no_other(self):
        captures, starts, calls, out = self.calibrate_and_start(CALIBRATE="yes", START="no", RECEIVER="rsp1b")
        self.assertEqual(len(captures), 1, out)
        self.assertIn("--receiver rsp1b", captures[0])
        tag = captures[0].split("--tag ")[1].split()[0]
        self.assertTrue(tag.startswith("downlink-calibration-"), tag)
        measured = [c for c in calls if "calibrate.py" in c]
        self.assertEqual(len(measured), 1, out)
        files = measured[0].split("calibrate.py", 1)[1].split()
        self.assertEqual(len(files), 1, measured[0])
        self.assertIn(f"_{tag}_", files[0])
        self.assertNotIn("20261101-010000", measured[0])


class ReceiverChoice(Steps):
    def choose(self, receiver, **env):
        return self.run_section("# --- 1b. the receiver", "# --- 2. this machine", RECEIVER=receiver, **env)

    def test_the_choice_is_saved_only_after_its_driver_is_in(self):
        for receiver, driver in (("rtlsdr2", "install-rtlsdr.sh"), ("rsp1b", "install-sdrplay.sh --accept-licence")):
            with self.subTest(receiver=receiver):
                rc, calls, out = self.choose(receiver, ACCEPT_SDRPLAY="1")
                self.assertEqual(rc, 0, out)
                saved = [i for i, c in enumerate(calls) if f"--set {receiver}" in c]
                installed = [i for i, c in enumerate(calls) if driver in c]
                self.assertEqual((len(saved), len(installed)), (1, 1), calls)
                self.assertLess(installed[0], saved[0])

    def test_a_driver_that_fails_leaves_the_choice_as_it_was(self):
        for receiver in ("rtlsdr2", "rsp1b"):
            with self.subTest(receiver=receiver):
                rc, calls, out = self.choose(receiver, ACCEPT_SDRPLAY="1", DRIVER_RC="1")
                self.assertNotEqual(rc, 0, out)
                self.assertFalse([c for c in calls if "--set" in c], calls)

    def test_the_sdrplay_licence_is_never_accepted_for_you(self):
        rc, calls, out = self.choose("rsp1b")                   # unattended, licence not accepted
        self.assertNotEqual(rc, 0, out)
        self.assertIn("needs your agreement to SDRplay's licence", out)
        self.assertFalse([c for c in calls if "install-sdrplay" in c or "--set" in c], calls)

    def test_an_unknown_receiver_is_refused_before_anything_is_installed(self):
        rc, calls, out = self.choose("hackrf")
        self.assertNotEqual(rc, 0, out)
        self.assertEqual([c for c in calls if c.startswith(("bash", "apt_install")) or "--set" in c], [])


if __name__ == "__main__":
    sys.exit(unittest.main())
