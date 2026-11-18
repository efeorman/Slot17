"""Tests for the encounter scorecard. Run: python -m unittest scripts/test_scorecard.py"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scorecard  # noqa: E402

T0 = 1_800_000_000.0


def iso(t):
    return scorecard.dt.datetime.fromtimestamp(t, scorecard.dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def alert(t, sound=None, n=[0]):
    n[0] += 1
    out = {"id": f"s/w{n[0]:08d}-ch1", "at": iso(t), "strength": 40.0, "channel": 1}
    if sound:
        out["sound"] = sound
    return out


def sighting(t, label="police", ident=None, **kw):
    return dict({"id": ident or f"{int(t)}{label}", "seenAt": iso(t), "label": label, "note": "", "isRecordingRequest": False}, **kw)


class Scorecard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, **data):
        (self.dir / name).write_text(json.dumps(dict({"kind": "slot17-drive-data"}, **data)), encoding="utf-8")

    def run_score(self, **kw):
        alerts, sightings, track, files = scorecard.load([self.dir])
        return scorecard.score(alerts, sightings, track, **kw), files

    def test_warned_missed_and_lonely_beeps_are_counted(self):
        self.write("app-1.log",
                   alerts=[alert(T0 + 100, "chime"), alert(T0 + 500, "urgent"), alert(T0 + 600)],
                   sightings=[sighting(T0 + 130), sighting(T0 + 900, "ambulance"), sighting(T0 + 300, "noise")],
                   track=[{"at": iso(T0), "speedKmh": 50}, {"at": iso(T0 + 1000), "speedKmh": 50}])
        result, files = self.run_score()
        row, = result["rows"]
        self.assertEqual((files, row["matches"], row["beeps"], row["vehicles"], row["warned"]), (1, 3, 2, 2, 1))
        self.assertEqual(row["leads"], [30.0])
        self.assertEqual(row["beeps_alone"], 1, "the urgent beep had no vehicle near it")
        self.assertEqual((row["nothing"], row["nothing_after_beep"]), (1, 0))
        self.assertAlmostEqual(row["moments_covered"] / row["moments"], 0.13, delta=0.02)

    def test_a_double_tap_is_one_sighting_and_records_are_not_counted_twice(self):
        both = dict(alerts=[alert(T0 + 10, "chime")],
                    sightings=[sighting(T0 + 20, ident="a"), sighting(T0 + 21.5, ident="b"), sighting(T0 + 40, ident="c")],
                    track=[{"at": iso(T0), "speedKmh": 1}, {"at": iso(T0 + 60), "speedKmh": 1}])
        self.write("app-1.log", **both)
        self.write("app-2.log", **both)          # the same records uploaded again
        result, files = self.run_score()
        self.assertEqual((files, result["merged"]), (2, 1))
        self.assertEqual((result["rows"][0]["vehicles"], result["rows"][0]["beeps"]), (2, 1))

    def test_a_long_gap_starts_a_new_drive_and_other_files_are_ignored(self):
        self.write("app-1.log", alerts=[alert(T0, "chime"), alert(T0 + 300), alert(T0 + 5000), alert(T0 + 5300)],
                   sightings=[], track=[])
        (self.dir / "app-text.log").write_text("App version: 1.4.3\n2026 a text log\n", encoding="utf-8")
        (self.dir / "broken.log").write_text("{not json", encoding="utf-8")
        result, files = self.run_score()
        self.assertEqual((files, len(result["rows"])), (1, 2))
        self.assertEqual([r["beeps"] for r in result["rows"]], [1, 0])

    def test_the_report_warns_when_there_are_too_few_sightings(self):
        self.write("app-1.log", alerts=[alert(T0, "chime"), alert(T0 + 300)], sightings=[sighting(T0 + 20)], track=[])
        result, files = self.run_score()
        text = scorecard.report(result, files)
        self.assertIn("Too few sightings", text)
        self.assertIn("| police | 20 s before |", text)

    def test_a_recording_request_is_not_a_sighting(self):
        self.write("app-1.log", alerts=[alert(T0, "chime"), alert(T0 + 300)],
                   sightings=[sighting(T0 + 20, isRecordingRequest=True)], track=[])
        result, _ = self.run_score()
        self.assertEqual(result["rows"][0]["vehicles"], 0)


if __name__ == "__main__":
    unittest.main()
