"""1.4.0: the support package. What goes in, what never does, and the drive, window and size choices.

Uses a made-up car computer in a temporary folder; no command runs (a stand-in answers them).
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
import support  # noqa: E402

START = 1795200000.0          # the drive's corrected start
SECRET = "Bearer abcdefabcdefabcdefabcdef"


def answer(cmd, timeout=20):
    if cmd[:2] == ["journalctl", "--list-boots"]:
        return "IDX BOOT ID FIRST ENTRY LAST ENTRY\n -1 aaa x y\n  0 bbb x y\n"
    if cmd[0] == "journalctl":
        return ("[1.0] raspberry python3[9]:   candidate ch154 383.86 MHz 31 dB\n"
                f"[2.0] raspberry python3[9]: push failed: Authorization: {SECRET}\n[3.0] raspberry kernel: hello\n")
    return f"{cmd[0]} answered\n"


class SupportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.saved = support.ROOT, support.CAPTURES, support.HOME
        support.ROOT, support.CAPTURES, support.HOME = base, base / "captures", base / "home"
        (base / "home/.config").mkdir(parents=True)
        (base / "home/.config/slot17-display.json").write_text(json.dumps({"url": "http://x", "token": "t" * 40}))
        (base / "VERSION").write_text("1.4.0\n")
        self.session = "20261119-171847-301483Z_watch_382.5MHz"
        d = support.CAPTURES / self.session
        d.mkdir(parents=True)
        (d / "session-time.json").write_text(json.dumps({"corrected_start_utc": START}))
        (d / "summary.json").write_text(json.dumps({"seconds": 3600, "confirmed_hits": 4, "dropped_samples": 1310720,
                                                    "startup_dropped_samples": 1048576, "whole_band_windows": 12,
                                                    "degraded_reasons": [{"fault": "receiver input very low"}]}))
        lines = [{"event_type": "clock_step", "delta_s": 100.0, "utc": START - 90},      # the clock was 100 s behind
                 {"event_type": "candidate", "classification": "energy_only", "utc": START + 60},
                 {"event_type": "candidate", "classification": "tetra_training_match", "utc": START + 60,
                  "note": f"token={SECRET}"}]
        lines = [{"event_type": "host_health", "utc": START - 100 + 5}] + lines        # before the step: 5 s in
        for i in range(240):
            lines.append({"event_type": "rf_health", "utc": START + i, "channels": [1, 2, 3]})
        lines.append({"event_type": "host_health", "utc": START + 40 * 60})
        (d / "events.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n{cut short by a power cut")
        (d / "w00000001.iq").write_bytes(b"\0" * 1000)
        self.out = base / "package.zip"

    def tearDown(self):
        support.ROOT, support.CAPTURES, support.HOME = self.saved
        self.tmp.cleanup()

    def make(self, **kw):
        manifest = support.make(self.out, run=answer, doctor={"items": [["Bluetooth", "ok", "on, shown as Slot17"]]}, **kw)
        z = zipfile.ZipFile(self.out)
        return manifest, z, {n: z.read(n).decode("utf-8", "replace") for n in z.namelist() if not n.endswith(".iq")}

    def test_the_drive_list_uses_the_corrected_start(self):
        rows = support.drives()
        self.assertEqual(rows[0]["start"], "2026-11-20T18:40:00+00:00")
        self.assertEqual((rows[0]["minutes"], rows[0]["matches"], rows[0]["dropped_after_start"]), (60.0, 4, 262144))

    def test_the_quick_check_starts_with_the_report_and_holds_no_events(self):
        manifest, z, files = self.make()
        self.assertEqual(z.namelist()[0], "REPORT.txt")
        self.assertIn("receiver input very low", files["REPORT.txt"])
        self.assertIn("262,144", files["REPORT.txt"])
        self.assertIn("on, shown as Slot17", files["REPORT.txt"])
        self.assertIn(f"drives/{self.session}/summary.json", files)
        self.assertNotIn(f"drives/{self.session}/events.jsonl", files)
        self.assertEqual(manifest["choice"], "quick")

    def test_nothing_secret_goes_in(self):
        manifest, z, files = self.make(session=self.session)
        everything = "".join(files.values())
        self.assertNotIn(SECRET.split()[1], everything)
        self.assertNotIn("t" * 40, everything)
        self.assertIn("[removed]", files["journal/boot0.txt"])
        settings = json.loads(files["settings.json"])
        self.assertEqual(settings["display_server"], "configured")
        self.assertNotIn("key", json.dumps(settings.get("bluetooth", {})).replace("key_id", ""))

    def test_one_drive_without_candidate_lines_and_with_fewer_channel_tables(self):
        manifest, z, files = self.make(session=self.session)
        events = [json.loads(x) for x in files[f"drives/{self.session}/events.jsonl"].splitlines()]
        kinds = [e["event_type"] for e in events]
        self.assertNotIn("energy_only", [e.get("classification") for e in events])
        self.assertIn("tetra_training_match", [e.get("classification") for e in events])
        tables = [e for e in events if e["event_type"] == "rf_health" and "channels" in e]
        self.assertEqual(len(tables), 240 // support.CHANNEL_TABLE_EVERY)
        self.assertEqual(kinds.count("rf_health"), 240)
        self.assertNotIn(f"drives/{self.session}/w00000001.iq", z.namelist())
        self.assertIn("(1 candidate lines left out)", files["journal/boot0.txt"])

    def test_everything_keeps_every_line_and_recordings_come_when_asked(self):
        manifest, z, files = self.make(session=self.session, everything=True, recordings=True)
        events = files[f"drives/{self.session}/events.jsonl"]
        self.assertIn("energy_only", events)
        self.assertEqual(events.count('"channels"'), 240)
        self.assertIn(f"drives/{self.session}/w00000001.iq", z.namelist())

    def test_a_window_counts_from_the_corrected_start(self):
        manifest, z, files = self.make(session=self.session, window=(0, 2))
        events = [json.loads(x) for x in files[f"drives/{self.session}/events.jsonl"].splitlines()]
        times = {e["event_type"] for e in events}
        self.assertIn("host_health", times)            # written 100 s early by the wrong clock: 5 s in, corrected
        self.assertEqual(sum(1 for e in events if e["event_type"] == "rf_health"), 121)   # minute 0 to 2 only
        self.assertNotIn(START + 40 * 60, [e["utc"] for e in events])

    def test_a_window_holds_only_the_recordings_made_in_it(self):
        """Found in review, 24 November 2026: the window chose the events, and every recording went in anyway."""
        d = support.CAPTURES / self.session
        (d / "w00000400.iq").write_bytes(b"\1" * 1000)
        with open(d / "events.jsonl", "a", encoding="utf-8") as fh:      # after the cut-short line, like a new boot
            fh.write("\n" + json.dumps({"event_type": "rf_health", "utc": START + 61}) + "\n")
            fh.write(json.dumps({"event_type": "window_recording", "name": "w00000001.iq", "state": "written"}) + "\n")
            fh.write(json.dumps({"event_type": "rf_health", "utc": START + 30 * 60}) + "\n")
            fh.write(json.dumps({"event_type": "window_recording", "name": "w00000400.iq", "state": "written"}) + "\n")
        manifest, z, files = self.make(session=self.session, window=(0, 2), everything=True, recordings=True)
        self.assertIn(f"drives/{self.session}/w00000001.iq", z.namelist())
        self.assertNotIn(f"drives/{self.session}/w00000400.iq", z.namelist(), "made at minute 30")
        manifest, z, files = self.make(session=self.session, everything=True, recordings=True)
        self.assertIn(f"drives/{self.session}/w00000400.iq", z.namelist(), "no window: all of them")
        self.assertEqual(z.read(f"drives/{self.session}/w00000400.iq"), b"\1" * 1000)

    def test_a_window_without_a_corrected_start_holds_no_recordings_and_says_so(self):
        """Fourth review round, 24 November 2026: with no session-time.json the window was ignored, and every
        recording of the drive went in."""
        d = support.CAPTURES / self.session
        (d / "session-time.json").unlink()
        with open(d / "events.jsonl", "a", encoding="utf-8") as fh:      # the drive's events name the recording
            fh.write("\n" + json.dumps({"event_type": "window_recording", "name": "w00000001.iq", "state": "written"}) + "\n")
        manifest, z, files = self.make(session=self.session, window=(0, 2), everything=True, recordings=True)
        self.assertNotIn(f"drives/{self.session}/w00000001.iq", z.namelist())
        self.assertEqual(manifest["left_out_recordings"][0]["drive"], self.session)
        manifest, z, files = self.make(session=self.session, everything=True, recordings=True)
        self.assertIn(f"drives/{self.session}/w00000001.iq", z.namelist(), "without a window: all of them")

    def test_a_recording_is_copied_in_paced_pieces(self):
        from unittest.mock import patch
        (support.CAPTURES / self.session / "w00000001.iq").write_bytes(b"\0" * 100_000)
        pauses = {}
        for asked in (False, True):
            with patch.object(support, "PACE_BYTES", 4096), patch.object(support.time, "sleep") as sleep:
                self.make(session=self.session, everything=True, recordings=asked)
            pauses[asked] = sleep.call_count
        self.assertGreaterEqual(pauses[True] - pauses[False], 100_000 // 4096 - 1)

    def test_an_unknown_drive_and_bad_options_are_refused(self):
        with self.assertRaises(ValueError):
            support.make(self.out, session="nope", run=answer, doctor={})
        with self.assertRaises(SystemExit):
            support.main(["--window", "30-50"])
        with self.assertRaises(SystemExit):
            support.main(["--drive", self.session, "--window", "50-30"])

    def test_the_manifest_says_what_is_in_and_what_never_is(self):
        manifest, z, files = self.make()
        stored = json.loads(files["manifest.json"])
        self.assertEqual(stored["software"], "1.4.0")
        self.assertIn("Bluetooth key", stored["left_out"])
        self.assertIn("REPORT.txt", stored["files"])
        self.assertEqual(stored["dropped_samples_before"], 1310720)


if __name__ == "__main__":
    unittest.main()
