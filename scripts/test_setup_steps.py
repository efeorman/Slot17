"""deploy/setup-car.sh's calibration and start steps, run for real in bash with stand-ins for the receiver, sudo and
Python: how many times does one setup open the receiver?

Found in review on 24 November 2026: accepting both default answers calibrated (one opening of the Airspy) and then
started the detector (a second one) in the same power-up, which the project's rules forbid for a board that can
stop responding at a second start. The steps are cut out of the real script by their headings, so a change to them
is tested as it is written. Needs bash (Git Bash on Windows); skipped without it.
"""
import os
from pathlib import Path
import shutil
import subprocess
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
set -uo pipefail
step() { :; }
ok() { echo "ok: $*"; }
note() { echo "note: $*"; }
die() { echo "stopped: $*"; exit 1; }
ask_yes() { [ "$2" = y ]; }                       # every question answered with its default
airspy_present() { [ "$PLUGGED" = 1 ]; }
receiver_present() { [ "$PLUGGED" = 1 ]; }
python3() { echo "python3 $*" >> "$LOG"; return 0; }
sudo() { echo "sudo $*" >> "$LOG"; return 0; }
journalctl() { :; }
'''


def steps():
    """Setup's text from its calibration step to its end message."""
    text = SETUP.read_text(encoding="utf-8")
    first = text.index("# --- 11. calibration")
    return text[first:text.index('step "Finished"', first)]


@unittest.skipUnless(BASH, "needs bash")
class OneOpeningPerSetup(unittest.TestCase):
    def run_steps(self, calibrate, start, plugged=True, autostart=1):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "p0").mkdir()
            log = Path(tmp) / "calls.log"
            log.write_text("")
            script = Path(tmp) / "steps.sh"
            script.write_text(STAND_INS + steps(), encoding="utf-8", newline="\n")
            env = dict(os.environ, LOG=log.as_posix(), PLUGGED=str(int(plugged)), PROJECT=Path(tmp).as_posix(),
                       SERVICE="tetra-detector.service", CALIBRATE=calibrate, START=start, AUTOSTART=str(autostart),
                       RECEIVER="airspy", YES="1")
            done = subprocess.run([BASH, script.as_posix()], env=env, capture_output=True, text=True, timeout=60)
            calls = log.read_text().splitlines()
        captures = [c for c in calls if "capture.py" in c]
        starts = [c for c in calls if c.startswith("sudo systemctl start")]
        return captures, starts, done.stdout + done.stderr

    def test_the_default_answers_open_the_receiver_once(self):
        captures, starts, out = self.run_steps("ask", "ask")
        self.assertEqual((len(captures), len(starts)), (1, 0), out)
        self.assertIn("starts by itself at the next power-up", out)

    def test_calibrating_and_an_explicit_start_still_open_it_once(self):
        captures, starts, out = self.run_steps("yes", "yes")
        self.assertEqual((len(captures), len(starts)), (1, 0), out)

    def test_without_calibration_it_is_started(self):
        captures, starts, out = self.run_steps("no", "ask")
        self.assertEqual((len(captures), len(starts)), (0, 1), out)

    def test_without_autostart_the_note_says_how_to_start_it(self):
        captures, starts, out = self.run_steps("ask", "ask", autostart=0)
        self.assertEqual(len(starts), 0, out)
        self.assertIn("sudo systemctl start tetra-detector.service", out)

    def test_no_receiver_plugged_in_opens_nothing(self):
        captures, starts, out = self.run_steps("ask", "ask", plugged=False)
        self.assertEqual((len(captures), len(starts)), (0, 0), out)


if __name__ == "__main__":
    unittest.main()
