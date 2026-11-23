"""The recorder of 1.5.1: events never wait for recordings, and recordings are made durable in batches.

A busy band (42 candidates a second, on a user's drive) outran an SD card when every recording cost two fsyncs:
161 of 16,652 snippets were skipped, and a match could wait behind a recording before the publisher saw it.
These tests use a stand-in for the file-system sync, so they show the behaviour on any computer; the last one
uses the real syncfs where there is one (the car computer runs it before every start).
"""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
import recording  # noqa: E402
import watch  # noqa: E402


def lines(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()] if Path(path).exists() else []


class SlowSync:
    """A file-system sync that can be held, counts its calls and checks nothing was renamed early."""

    def __init__(self, folder):
        self.folder, self.calls, self.gate = Path(folder), 0, threading.Event()
        self.gate.set()
        self.renamed_early = []
        self.fail = False

    def __call__(self, directory):
        self.calls += 1
        for tmp in self.folder.glob("*.tmp"):
            final = self.folder / tmp.name[:-len(".tmp")]
            if final.exists() and not final.name.endswith(".json"):
                self.renamed_early.append(final.name)
        self.gate.wait(10)
        if self.fail:
            raise OSError(5, "Input/output error")


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.sync = SlowSync(self.dir)

    def tearDown(self):
        self.tmp.cleanup()

    def recorder(self, **kw):
        return recording.Recorder(self.dir, min_free=0, sync=kw.pop("sync", self.sync), **kw)

    def test_a_match_is_written_while_a_recording_waits_for_the_disk(self):
        r = self.recorder().start()
        self.sync.gate.clear()                               # the card is busy with a recording
        self.assertEqual(r.samples("w1-ch3.c64", np.zeros(4096, np.complex64), {}), "queued")
        time.sleep(0.2)
        self.assertTrue(r.event({"event_id": "m1", "confirmed": True}))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not lines(self.dir / "events.jsonl"):
            time.sleep(0.01)
        self.assertEqual([e["event_id"] for e in lines(self.dir / "events.jsonl")], ["m1"])
        self.assertFalse((self.dir / "w1-ch3.c64").exists())  # the recording is still waiting, and that is fine
        self.sync.gate.set()
        self.assertTrue(r.stop())
        self.assertTrue((self.dir / "w1-ch3.c64").exists())

    def test_one_sync_makes_a_whole_batch_durable(self):
        r = self.recorder()
        for i in range(60):
            self.assertEqual(r.samples(f"w{i}-ch1.c64", np.full(1000, i, np.complex64), {"i": i}), "queued")
        r.start()
        self.assertTrue(r.stop())
        outcomes = lines(self.dir / "recordings.jsonl")
        self.assertEqual([o["state"] for o in outcomes], ["written"] * 60)
        self.assertEqual(r.batches, 1)
        self.assertEqual(r.largest_batch, 60)
        self.assertLessEqual(self.sync.calls, 2)              # the batch, and settling the names at the end
        self.assertEqual(self.sync.renamed_early, [])
        self.assertEqual(list(self.dir.glob("*.tmp")), [])
        np.testing.assert_array_equal(np.fromfile(self.dir / "w7-ch1.c64", np.complex64), np.full(1000, 7, np.complex64))
        self.assertEqual(json.loads((self.dir / "w7-ch1.c64.json").read_text())["i"], 7)
        self.assertEqual(r.snapshot()["durable"], "syncfs")

    def test_a_failed_sync_keeps_nothing_and_the_next_batch_works(self):
        r = self.recorder()
        self.sync.fail = True
        r.samples("bad.c64", np.zeros(10, np.complex64), {})
        r.start()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not lines(self.dir / "recordings.jsonl"):
            time.sleep(0.01)
        self.sync.fail = False
        r.samples("good.c64", np.zeros(10, np.complex64), {})
        self.assertTrue(r.stop())
        states = {o["name"]: o["state"] for o in lines(self.dir / "recordings.jsonl")}
        self.assertEqual(states, {"bad.c64": "error", "good.c64": "written"})
        self.assertFalse((self.dir / "bad.c64").exists())
        self.assertEqual(list(self.dir.glob("*.tmp")), [])
        self.assertGreaterEqual(r.data_errors, 1)

    def test_without_syncfs_each_file_is_fsynced_as_before(self):
        r = recording.Recorder(self.dir, min_free=0, sync=None).start()
        r.samples("a.power", np.arange(200, dtype=np.int8), {"format": "int8_db"})
        self.assertTrue(r.event({"event_id": "e"}))
        self.assertTrue(r.stop())
        self.assertEqual(lines(self.dir / "recordings.jsonl")[0]["state"], "written")
        self.assertEqual(r.snapshot()["durable"], "fsync")
        np.testing.assert_array_equal(np.fromfile(self.dir / "a.power", np.int8), np.arange(200, dtype=np.int8))

    def test_many_more_recordings_can_wait_than_before(self):
        r = self.recorder()
        states = [r.samples(f"s{i}.c64", np.zeros(8, np.complex64), {}) for i in range(1000)]
        self.assertEqual(states.count("queued"), 1000)        # 256 before 1.5.1; the byte limit still applies
        self.assertEqual(r.samples("huge.iq", np.zeros(recording.MAX_PENDING_BYTES, np.int8), {}), "queue_full")
        r.start()
        self.assertTrue(r.stop())

    @unittest.skipUnless(recording.SYNCFS, "syncfs is Linux only; elsewhere the fsync path is used")
    def test_the_real_syncfs(self):
        r = recording.Recorder(self.dir, min_free=0).start()
        for i in range(20):
            r.samples(f"w{i}.c64", np.zeros(512, np.complex64), {})
        self.assertTrue(r.stop())
        self.assertEqual({o["state"] for o in lines(self.dir / "recordings.jsonl")}, {"written"})
        self.assertGreaterEqual(r.syncs, 1)


class SkippedRecordingsAreNoFault(unittest.TestCase):
    def test_only_lost_events_and_failed_writes_degrade_the_detector(self):
        class Stats:
            worker_alive = True
            quiet_windows = 0
        stats = Stats()
        for name in ("overflow", "callback_errors", "window_errors", "event_errors", "confirm_errors",
                     "telemetry_errors", "heartbeat_errors", "metadata_errors"):
            setattr(stats, name, 0)
        recorder = {"dropped_events": 0, "data_errors": 0, "event_errors": 0, "skipped_data": 161, "alive": True}
        faults = [r["fault"] for r in watch._recorded_faults(stats, recorder)]
        self.assertNotIn("recordings skipped", faults)
        recorder.update(dropped_events=3, data_errors=1)
        faults = [r["fault"] for r in watch._recorded_faults(stats, recorder)]
        self.assertIn("recording events dropped", faults)
        self.assertIn("recording data errors", faults)


if __name__ == "__main__":
    unittest.main()
