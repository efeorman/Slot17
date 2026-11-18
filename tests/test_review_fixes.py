"""Regressions from the November 2026 quality review. Offline: no receiver, no network, no real notices."""
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify_isolation  # noqa: E402,F401 - see the module
import notify
import recording
import supervise
import watch


class CallbackFailure(unittest.TestCase):
    def test_a_failed_block_is_marked_as_a_gap(self):
        """Samples lost in Python must not look continuous with the next block."""
        ring = watch.Ring(64)
        stats = watch.Stats()
        transfer = types.SimpleNamespace(contents=types.SimpleNamespace(sample_count=100, dropped_samples=0, samples=None))
        with patch("ctypes.string_at", side_effect=RuntimeError("boom")), \
                patch.dict(sys.modules, {"airspy_ctypes": types.SimpleNamespace(SAMPLE_BLOCK_CB=lambda f: f)}):
            func = watch.make_callback(ring, stats)
            self.assertEqual(func(transfer), 0, "nothing escapes into the driver's thread")
        self.assertEqual(stats.callback_errors, 1)
        self.assertEqual(int(stats.dropped), 100)
        self.assertEqual(len(stats.discontinuities), 1)


class StatusFiles(unittest.TestCase):
    def test_a_write_failure_is_reported_as_a_fault(self):
        stats = watch.Stats()
        watch.note_metadata_error(stats, "summary", OSError(28, "No space left on device"))
        faults = watch._reporting_faults(stats)
        self.assertEqual(faults[0]["fault"], "status file errors")
        self.assertIn("summary", stats.last_metadata_error)

    def test_a_notice_problem_is_reported_too(self):
        stats = watch.Stats()
        stats.notify_problem = "notification worker stopped"
        self.assertEqual(watch._reporting_faults(stats)[0]["fault"], "notification worker stopped")

    def test_nothing_wrong_reports_nothing(self):
        self.assertEqual(watch._reporting_faults(watch.Stats()), [])


class MarkRename(unittest.TestCase):
    def test_a_mark_that_cannot_be_renamed_is_not_read_again(self):
        import marking
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "marks"
            inbox.mkdir()
            events = []
            ctx = types.SimpleNamespace(outdir=tmp, fs=6_000_000, centre=382.5, stats=watch.Stats(),
                                        recorder=types.SimpleNamespace(event=events.append,
                                                                       samples=lambda *_a: "queued"))
            ring = types.SimpleNamespace(write=0, extract=lambda *_a: np.zeros(1, np.int16))
            marks = marking.Marks(ctx, ring) if hasattr(marking, "Marks") else None
            if marks is None:
                self.skipTest("marking class has another name")
            marks.inbox = inbox
            (inbox / "abc.request.json").write_text(json.dumps({"utc": 1.0, "label": "police", "note": ""}))
            with patch.object(Path, "replace", side_effect=OSError(28, "No space left")),                     patch("marking.emit", side_effect=lambda _ctx, event: events.append(event)):
                marks.poll()
                seen = len(events)
                marks.last_poll = -1e30
                marks.poll()
            self.assertEqual(len(events), seen, "handled once only")
            self.assertTrue(any(e.get("event_type") == "mark_error" for e in events))


class RecorderDrain(unittest.TestCase):
    def test_a_dead_recorder_with_work_left_does_not_claim_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = recording.Recorder(tmp)
            r.events.put_nowait("left behind\n")
            self.assertFalse(r.stop(timeout=0.1))


class Credentials(unittest.TestCase):
    def test_a_config_of_the_wrong_shape_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            for text in ("[]", "null", "1", '"x"'):
                path = Path(tmp) / "c.json"
                path.write_text(text)
                with patch.dict("os.environ", {}, clear=True):
                    self.assertEqual(notify.load_config(str(path)), {}, text)

    def test_credentials_of_the_wrong_type_are_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text('{"token": 5, "user": ["x"], "device": null}')
            with patch.dict("os.environ", {}, clear=True):
                cfg = notify.load_config(str(path))
            self.assertNotIn("token", cfg)
            self.assertNotIn("user", cfg)


class SavingCredentials(unittest.TestCase):
    def test_replacing_leaves_one_whole_file_readable_only_by_its_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "c.json")
            notify.save_config("A" * 8, "B" * 8, path=path)
            notify.save_config("C" * 8, "D" * 8, path=path)
            self.assertEqual(json.loads(Path(path).read_text())["token"], "C" * 8)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["c.json"], "no partial file is left")


class SteadyFaults(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.n = notify.Notifier({"token": "PRIVATE_TOKEN", "user": "PRIVATE_USER"}, spool=str(Path(self.tmp.name) / "o.sqlite3"))
        self.posted = []
        self.n.post = lambda *a, **k: self.posted.append(a) or True

    def tearDown(self):
        self.tmp.cleanup()

    def test_each_episode_of_a_steady_fault_is_told_once(self):
        fault = [{"fault": "receiver input very low", "total": 40, "steady": True}]
        self.assertTrue(self.n.alert_health(fault))
        self.assertFalse(self.n.alert_health(fault), "not repeated during the episode")
        self.assertFalse(self.n.alert_health([]))
        self.assertTrue(self.n.alert_health(fault), "a new episode is news")
        self.assertEqual(len(self.posted), 2)

    def test_a_dead_worker_is_named(self):
        self.assertIsNone(self.n.problem())
        self.n._thread = types.SimpleNamespace(is_alive=lambda: False)
        self.assertEqual(self.n.problem(), "notification worker stopped")
        self.n._thread = None
        self.n.storage_errors = 1
        self.assertEqual(self.n.problem(), "notification storage errors")


class Supervisor(unittest.TestCase):
    def test_one_attempt_is_the_default(self):
        """Reopening the receiver after a failure can wedge this Airspy (rule 3 in CONTRIBUTING.md)."""
        with patch.object(sys, "argv", ["supervise.py"]), patch.object(supervise, "run", return_value=0) as run:
            try:
                supervise.main()
            except SystemExit:
                pass
        self.assertTrue(run.called)
        self.assertTrue(run.call_args[0][0].no_restart)


class BootMessage(unittest.TestCase):
    def test_it_does_not_claim_the_detector_has_not_started(self):
        source = Path(notify.__file__).read_text(encoding="utf-8")
        self.assertNotIn("detector not started yet", source)


class ClockAndDataFindings(unittest.TestCase):
    def test_a_clock_correction_does_not_delete_a_queued_notice(self):
        from outbox import Outbox
        with tempfile.TemporaryDirectory() as folder:
            spool = str(Path(folder) / "spool.db")
            n = notify.Notifier(cfg={"token": "t", "user": "u"}, spool=spool, transport=lambda f: None)
            self.assertTrue(n.post("receiver input very low", timestamp=1_000_000))
            box = Outbox(spool)
            try:
                box.add(n.q.get_nowait())
                n.q.task_done()
                self.assertEqual(box.expire(1_000_000 + 30 * 86400), 0, "kept after the clock jumps a month")
                self.assertEqual(box.count(), 1)
            finally:
                box.close()

    def test_an_old_send_does_not_end_the_wait_for_the_final_notice(self):
        class Slow:
            def __init__(self):
                self.calls = 0

            def __call__(self, fields):
                self.calls += 1

        with tempfile.TemporaryDirectory() as folder:
            transport = Slow()
            n = notify.Notifier(cfg={"token": "t", "user": "u"}, spool=str(Path(folder) / "s.db"), transport=transport)
            self.assertTrue(n.start())
            hardware = supervise.HardwareNotifier.__new__(supervise.HardwareNotifier)
            hardware._n = n
            n.sent = 5                   # earlier messages in this session
            n.post("stopped")
            hardware.finish(grace=5)
            self.assertEqual(transport.calls, 1, "the final notice went out before the worker stopped")

    def test_a_successful_send_clears_the_last_error(self):
        results = [OSError("down"), None]

        def transport(fields):
            result = results.pop(0)
            if result:
                raise result

        with tempfile.TemporaryDirectory() as folder, patch.object(notify, "RETRY_MIN_S", 0.01):
            n = notify.Notifier(cfg={"token": "t", "user": "u"}, spool=str(Path(folder) / "s.db"), transport=transport)
            self.assertTrue(n.start())
            n.post("one")
            deadline = time.monotonic() + 5
            while n.sent == 0 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(n.sent, 1)
            self.assertIsNone(n.last_error)
            n.stop(timeout=2)

    def test_the_recorder_survives_metadata_json_cannot_carry(self):
        with tempfile.TemporaryDirectory() as folder:
            recorder = recording.Recorder(folder, min_free=0)
            recorder.start()
            samples = np.zeros(8, np.complex64)
            self.assertEqual(recorder.samples("nan.c64", samples, {"snr": float("nan")}), "queued")
            self.assertEqual(recorder.samples("bad.c64", samples, {"bad": object()}), "queued")
            self.assertEqual(recorder.samples("good.c64", samples, {"snr": 12.0}), "queued")
            deadline = time.monotonic() + 5
            while recorder.written_data < 1 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(recorder.snapshot()["alive"], "still running after two bad records")
            self.assertTrue(recorder.stop())
            lines = (Path(folder) / "recordings.jsonl").read_text(encoding="utf-8").splitlines()
            rows = {row["name"]: row for row in map(json.loads, lines)}
            self.assertEqual(set(rows), {"nan.c64", "bad.c64", "good.c64"})
            self.assertEqual(rows["good.c64"]["state"], "written")
            self.assertEqual(rows["nan.c64"]["state"], "error")
            self.assertEqual(list(Path(folder).glob("*.tmp")), [], "no half-written files stay behind")

    def test_a_mark_with_an_impossible_time_is_refused(self):
        import marking
        with tempfile.TemporaryDirectory() as tmp:
            inbox = Path(tmp) / "marks"
            inbox.mkdir()
            events = []
            ctx = types.SimpleNamespace(outdir=tmp, fs=6_000_000, centre=382.5, stats=watch.Stats(),
                                        recorder=types.SimpleNamespace(event=events.append,
                                                                       samples=lambda *_a: "queued"))
            ring = types.SimpleNamespace(write=0, extract=lambda *_a: np.zeros(1, np.int16))
            marks = marking.Marks(ctx, ring)
            marks.inbox = inbox
            for number, utc in enumerate(("NaN", "Infinity", "[1]")):
                (inbox / f"m{number}.request.json").write_text('{"utc": %s, "label": "police"}' % utc)
            (inbox / "m9.request.json").write_text("[1, 2]")
            with patch("marking.emit", side_effect=lambda _ctx, event: events.append(event)):
                marks.poll()
            self.assertEqual(sum(e.get("event_type") == "mark_error" for e in events), 4)
            self.assertFalse(any(e.get("event_type") == "human_observation" for e in events))

    def test_metadata_json_cannot_carry_does_not_end_the_recorder(self):
        line = recording._index_line({"name": "a.cf32", "bytes": 8, "state": "error", "metadata": {"snr": float("nan")}})
        parsed = json.loads(line, parse_constant=lambda text: self.fail("not strict JSON"))
        self.assertEqual(parsed["state"], "error")
        self.assertEqual(parsed["metadata_error"], "not valid JSON")
        self.assertNotIn("metadata", parsed)
        line = recording._index_line({"name": "b.cf32", "state": "written", "metadata": {"bad": object()}})
        self.assertEqual(json.loads(line)["name"], "b.cf32")

    def test_network_wait_ignores_a_clock_step(self):
        offline = {"online": False}
        with patch.object(notify, "network_info", return_value=offline), \
                patch.object(notify.time, "sleep"), \
                patch.object(notify.time, "monotonic", side_effect=[0.0, 1.0, 999.0]), \
                patch.object(notify.time, "time", side_effect=AssertionError("wall clock used")):
            self.assertIs(notify.wait_for_network(max_wait_s=10), offline)

    def test_a_zero_noise_floor_gives_a_finite_strength(self):
        import channelize
        import detect
        ch = channelize.channelise(np.zeros(1920 * 400, np.complex64), 6_000_000, 382.5e6, apply_cal=False)
        ch.power = np.zeros_like(ch.power)
        row = ch.row_for(104)
        frames = max(1, int(round(0.014 / ch.frame_s)))
        ch.power[row, 100:100 + frames] = 1.0
        ch.occupancy_hz = None
        with patch.object(type(ch), "noise_floor", lambda _self: np.zeros(ch.power.shape[0])):
            hits = detect.detect(ch, threshold_db=6, require_periodic=False)
        target = next(h for h in hits if h["tetra_channel"] == 104)
        self.assertTrue(np.isfinite(target["peak_snr_db"]), target["peak_snr_db"])


if __name__ == "__main__":
    unittest.main()
