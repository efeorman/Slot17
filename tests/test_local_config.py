"""This machine's own calibration and channel files take precedence over the shipped examples."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
import analyze
import detect
import paths


class LocalConfig(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.here = Path(self.folder.name)
        (self.here / "calibration.json").write_text(json.dumps({"median_ppm": 4.0}))
        (self.here / "downlink_channels.json").write_text(json.dumps({"channels": [60, 90]}))
        # Point the module at a temporary folder. Replacing os.path.abspath instead recurses on Linux,
        # where realpath calls abspath.
        self.patch = patch.object(analyze, "__file__", str(self.here / "analyze.py"))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_the_shipped_example_is_used_until_this_machine_is_calibrated(self):
        self.assertEqual(analyze.load_calibration(), 4.0)
        self.assertEqual(detect.load_downlink_channels(str(Path(analyze.config_file("downlink_channels.json")))), [60, 90])

    def test_a_local_measurement_wins(self):
        (self.here / "calibration.local.json").write_text(json.dumps({"median_ppm": -1.5}))
        (self.here / "downlink_channels.local.json").write_text(json.dumps({"channels": [7]}))
        self.assertEqual(analyze.load_calibration(), -1.5)
        self.assertEqual(Path(analyze.config_file("downlink_channels.json")).name, "downlink_channels.local.json")
        self.assertEqual(detect.load_downlink_channels(analyze.config_file("downlink_channels.json")), [7])

    def test_a_damaged_local_file_gives_no_correction_rather_than_a_crash(self):
        (self.here / "calibration.local.json").write_text("not json")
        self.assertEqual(analyze.load_calibration(), 0.0)


class Tools(unittest.TestCase):
    def test_airspy_rx_falls_back_to_the_system_copy_off_windows(self):
        with patch.object(paths.sys, "platform", "linux"), \
                patch.object(paths.os.path, "isfile", return_value=False), \
                patch.object(paths.shutil, "which", return_value="/usr/bin/airspy_rx"):
            self.assertEqual(paths._tool("airspy_rx"), "/usr/bin/airspy_rx")

    def test_a_bundled_copy_is_kept_on_windows(self):
        with patch.object(paths.sys, "platform", "win32"):
            self.assertEqual(paths._tool("airspy_rx"), paths.os.path.join(paths.TOOLS_BIN, "airspy_rx" + paths._EXE))


if __name__ == "__main__":
    unittest.main()
