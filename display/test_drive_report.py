"""The drive report: made from the phone's drive data and the car computer's logs, saved beside them."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import drive_report  # noqa: E402
import server as srv  # noqa: E402


def drive_data():
    return {"kind": "slot17-drive-data", "app": "1.4.8 (14)", "from": "2026-11-14T19:20:00Z", "until": "2026-11-14T20:00:00Z",
            "alerts": [{"id": "s/w1-ch119", "channel": 119, "strength": 46.5, "age": 0.8, "at": "2026-11-14T19:30:00Z",
                        "reason": "", "sound": "chime"},
                       {"id": "s/w2-ch119", "channel": 119, "strength": 20.0, "age": 12.5, "at": "2026-11-14T19:31:00Z",
                        "reason": "below"},
                       {"id": "s/w3-ch170", "channel": 170, "strength": 22.0, "age": 1.1, "at": "2026-11-14T19:40:00Z",
                        "reason": "", "sound": "call"}],
            "sightings": [{"seenAt": "2026-11-14T19:30:30Z", "label": "ambulance", "note": "blue lights"}],
            "track": [{"at": "2026-11-14T19:30:00Z", "speedKmh": 90}, {"at": "2026-11-14T19:45:00Z", "speedKmh": 0}]}


def pi_log(summary_utc, seconds, matches):
    summary = {"utc": summary_utc, "seconds": seconds, "windows": 4000, "window_s": 0.25, "confirmed_hits": matches,
               "weak_matches": 2, "call_matches": 3, "dropped_samples": 0, "unchecked_hits": 10, "whole_band_windows": 4,
               "rf_health": {"median_floor_db": 86.4}, "degraded_reasons": []}
    return ("Slot17 Pi log.\n\n===== summary.json =====\n" + json.dumps(summary) + "\n\n===== journal =====\n"
            "[100.0] raspberry python3[1]: link: 30 pushes in 60 s, 1 failed, median 52 ms, slowest 140 ms, via default 30\n"
            "[120.0] raspberry python3[2]: modem: link up (LTE, 3 bars, RSRP -104 dBm, RSRQ -11 dB, SNR 2.0 dB, cell a/1); "
            "round trip to 203.0.113.5 48 ms (worst 90 ms) [regular report]\n"
            "[150.0] raspberry python3[2]: modem: no data session (ppp_connecting; LTE, 1 bars)\n")


class Report(unittest.TestCase):
    def folder(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        (d / "app-20261114-200100Z-00000001.log").write_text(json.dumps(drive_data()))
        drive_end = 1794686400.0                     # 2026-11-14T20:00:00Z
        (d / "pi-20261114-195500Z-00000002.log").write_text(pi_log(drive_end - 300, 2000, 41))
        # A session started at home after the drive must not be mistaken for it.
        (d / "pi-20261114-200800Z-00000003.log").write_text(pi_log(drive_end + 480, 200, 0))
        return d

    def test_the_report_says_what_was_measured(self):
        text = drive_report.write_report(self.folder(), "app-20261114-200100Z-00000001.log")
        self.assertIn("3 matches on 2 channels", text)
        self.assertIn("1 call, 1 chime", text)
        self.assertIn("1 over 10 s", text)
        self.assertIn("ambulance: 1 sound(s) in the minute before", text)
        self.assertIn("top speed 90 km/h", text)
        self.assertIn("41 matches", text, "the drive's own session, not the one at home after it")
        self.assertIn("3 in a call pattern", text)
        self.assertIn("30 pushes, 1 failed", text)
        self.assertIn("round trip median 48 ms, signal RSRP -104 to -104 dBm; 1 checks without a data session", text)
        self.assertIn("not who, not how far", text)

    def test_anything_that_is_not_drive_data_makes_no_report(self):
        d = self.folder()
        (d / "app-20261114-200200Z-00000004.log").write_text("2026-11-14T20:00:00Z app started")
        self.assertIsNone(drive_report.write_report(d, "app-20261114-200200Z-00000004.log"))
        self.assertIsNone(drive_report.write_report(d, "missing.log"))

    def test_the_server_saves_a_report_after_drive_data_and_lists_it(self):
        d = self.folder()
        name = srv.save_log(d, "app", json.dumps(drive_data()).encode())
        srv.make_drive_report(d, name).join(10)
        reports = [r["name"] for r in srv.list_logs(d) if r["name"].startswith("report-")]
        self.assertEqual(len(reports), 1)
        self.assertTrue(srv.LOG_NAME.match(reports[0]))
        self.assertIn("RECOGNISED TETRA", (d / reports[0]).read_text())

    def test_a_broken_report_never_breaks_the_upload(self):
        d = self.folder()
        (d / "app-20261114-200300Z-00000005.log").write_text(json.dumps(dict(drive_data(), alerts="nonsense")))
        thread = srv.make_drive_report(d, "app-20261114-200300Z-00000005.log")
        thread.join(10)
        self.assertFalse(thread.is_alive())
        time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
