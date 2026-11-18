"""`setup.sh doctor` with a pretend machine: the right verdict per part, and it never changes anything."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import doctor  # noqa: E402

GOOD_HB = {"windows": 100, "samples_in": 6000000, "windows_started": 101, "ready": True, "degraded": False}


class Machine(doctor.Probe):
    """A healthy car computer unless a test changes it. Records every command, to prove it only looks."""

    def __init__(self, **change):
        self.commands = []
        self.files = {
            str(doctor.HEARTBEAT): [json.dumps(GOOD_HB), json.dumps(dict(GOOD_HB, windows=104, samples_in=6600000))],
            str(doctor.DISPLAY_CONFIG): json.dumps({"url": "http://203.0.113.5:5732/api/push", "token": "x" * 40}),
            str(doctor.PUSHOVER_CONFIG): "{}",
            str(doctor.RELEASE_KEY): "key",
            str(doctor.ROOT / "p0/calibration.local.json"): "{}",
            str(doctor.SERVICE_FILE): "ExecStartPre=python preflight.py --offline --tests-if-changed",
            str(doctor.ROOT / "VERSION"): "1.2.9\n",
        }
        self.answers = {
            "lsusb": (0, "Bus 003 Device 002: ID 1d50:60a1 Airspy"),
            "systemctl is-enabled": (0, "enabled"),
            "systemctl is-active": (0, "active"),
            "ip": (0, "default via 172.20.10.1 dev wlan0 proto dhcp"),
            "timedatectl": (0, "yes"),
            "vcgencmd": (0, "throttled=0x0"),
        }
        self.http = (200, None)
        self.free = 300.0
        for key, value in change.items():
            setattr(self, key, value)

    def run(self, *cmd, timeout=5):
        self.commands.append(cmd)
        key = " ".join(cmd[:2]) if cmd[0] == "systemctl" else cmd[0]
        if cmd[0] == sys.executable:
            return 0, "2.2.0 1.14.0"
        return self.answers.get(key, (None, ""))

    def read(self, path):
        value = self.files.get(str(path))
        if isinstance(value, list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    def exists(self, path):
        return str(path) in self.files

    def free_gb(self, path):
        return self.free

    def get(self, url, timeout=6):
        return self.http

    def wait(self, seconds):
        pass


def verdicts(machine, role="car"):
    """One run: exit status, {part: level}, and the printed text."""
    seen = []
    real = doctor.diagnose

    def capture(p, r):
        seen.extend(real(p, r))
        return seen
    doctor.diagnose = capture
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            status = doctor.main([role], probe=machine)
    finally:
        doctor.diagnose = real
    return status, {name: level for name, level, _ in seen}, out.getvalue()


class DoctorTests(unittest.TestCase):
    def test_a_healthy_car_computer(self):
        status, rows, text = verdicts(Machine())
        self.assertEqual(status, 0, text)
        self.assertIn("No problems found", text)
        self.assertNotIn(doctor.PROBLEM, rows.values())

    def test_it_only_looks(self):
        m = Machine()
        verdicts(m)
        allowed = {("systemctl", "is-enabled"), ("systemctl", "is-active")}
        for cmd in m.commands:
            if cmd[0] == "systemctl":
                self.assertIn(cmd[:2], allowed)
            self.assertNotIn(cmd[0], ("airspy_info", "airspy_rx", "sudo", "reboot", "kill"))

    def test_no_receiver_is_a_problem(self):
        m = Machine()
        m.answers["lsusb"] = (1, "")
        self.assertEqual(verdicts(m)[1]["Receiver"], doctor.PROBLEM)

    def test_a_stuck_heartbeat_is_a_problem(self):
        m = Machine()
        m.files[str(doctor.HEARTBEAT)] = json.dumps(GOOD_HB)
        self.assertEqual(verdicts(m)[1]["Detector"], doctor.PROBLEM)

    def test_degraded_is_named_and_not_a_restart(self):
        m = Machine()
        bad = dict(GOOD_HB, degraded=True, degraded_reasons=[{"fault": "receiver input very low"}])
        m.files[str(doctor.HEARTBEAT)] = [json.dumps(bad), json.dumps(dict(bad, windows=104))]
        status, rows, text = verdicts(m)
        self.assertEqual(rows["Detector"], doctor.NOTE)
        self.assertIn("receiver input very low", text)
        self.assertIn("Restarting does not help", text)

    def test_unreachable_server_and_no_network(self):
        m = Machine(http=(None, "timed out"))
        m.answers["ip"] = (0, "")
        status, rows, text = verdicts(m)
        self.assertEqual(status, 1)
        self.assertEqual(rows["Display server"], doctor.PROBLEM)
        self.assertEqual(rows["Internet"], doctor.PROBLEM)
        self.assertIn("firewall", text)

    def test_power_disk_and_old_service_file(self):
        m = Machine(free=3.0)
        m.answers["vcgencmd"] = (0, "throttled=0x50005")
        m.files[str(doctor.SERVICE_FILE)] = "ExecStartPre=python preflight.py --offline --tests --require-idle"
        status, rows, text = verdicts(m)
        self.assertEqual(rows["Power"], doctor.PROBLEM)
        self.assertEqual(rows["Storage"], doctor.PROBLEM)
        self.assertEqual(rows["Start-up checks"], doctor.NOTE)

    def test_pushover_switched_off_is_reported(self):
        m = Machine()
        m.files[str(doctor.PUSHOVER_SWITCH)] = json.dumps({"pushover": False})
        self.assertIn("switched off in the app", verdicts(m)[2])

    def test_no_keys_in_the_output(self):
        text = verdicts(Machine())[2]
        self.assertNotIn("x" * 40, text)

    def test_server_role(self):
        m = Machine()
        m.files[str(doctor.SERVER_CONFIG)] = json.dumps({"port": 5732, "viewer_key": "v" * 30, "pi_token": "p" * 40})
        status, rows, text = verdicts(m, "server")
        self.assertEqual(status, 0, text)
        self.assertNotIn("v" * 30, text)
        self.assertNotIn("p" * 40, text)

    def test_the_version_is_what_runs_not_the_stale_version_file(self):
        """Found on the car computer, 18 Nov: signed updates never bring VERSION, so it said "unknown"."""
        m = Machine()
        del m.files[str(doctor.ROOT / "VERSION")]
        self.assertIn("software unknown", verdicts(m)[2])
        m = Machine()
        m.files[str(doctor.ROOT / "VERSION")] = "1.2.1\n"
        m.files[str(doctor.ROOT / "p0/version.py")] = 'RELEASE = "x"\nSOFTWARE = "1.2.9"\n'
        self.assertIn("software 1.2.9", verdicts(m)[2])

    def test_calibration_says_what_the_file_says(self):
        """Found on the car computer: its own measurement is in calibration.json, so "shipped example" was wrong."""
        m = Machine()
        del m.files[str(doctor.ROOT / "p0/calibration.local.json")]
        m.files[str(doctor.ROOT / "p0/calibration.json")] = json.dumps(
            {"measured_utc": "2026-11-03T23:30:00+00:00", "median_ppm": 4.09})
        text = verdicts(m)[2]
        self.assertIn("+4.09 ppm, measured 2026-11-03", text)
        self.assertNotIn("shipped example", text)


if __name__ == "__main__":
    unittest.main()
