"""The setup check (1.3.1): the car computer's doctor verdicts travel to the apps, bounded and kept."""
from pathlib import Path
import subprocess
import sys
import threading
import time
import types
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import display_publisher as pubm  # noqa: E402
import server as srv  # noqa: E402

ITEMS = [["Receiver", "PROBLEM", "the Airspy isn't on USB"], ["Service", "ok", "enabled"]]


class ServerSide(unittest.TestCase):
    def test_verdicts_are_bounded(self):
        got = srv.clean_doctor({"items": ITEMS, "age_s": 3})
        self.assertEqual(got, {"items": ITEMS, "age_s": 3})
        for bad in ({"items": [["a", "WRONG", "x"]]}, {"items": [["a", "ok"]]}, {"items": [ITEMS[0]] * 17},
                    {"items": [["a" * 41, "ok", "x"]]}, {"items": "x"}):
            with self.assertRaises(ValueError):
                srv.clean_doctor(bad)

    def test_an_odd_doctor_never_refuses_the_push(self):
        state = {"v": 3, "source": "pi", "ticket": "t", "snapshot_age_s": 0, "seq": 1, "detector": {"state": "running"},
                 "channels": [], "matches": [], "doctor": {"items": "nonsense"}}
        self.assertNotIn("doctor", srv.clean_state(state))

    def test_the_last_verdicts_stay_between_pushes(self):
        store = srv.Store()
        ticket = store.ticket("pi")
        base = {"v": 3, "source": "pi", "snapshot_age_s": 0.0, "detector": {"state": "running"}, "channels": [], "matches": []}
        store.put(dict(base, ticket=ticket, seq=1, doctor={"items": ITEMS, "age_s": 0.0}))
        store.put(dict(base, ticket=store.ticket("pi"), seq=2))
        self.assertEqual(store.latest["doctor"]["items"], ITEMS)
        self.assertGreaterEqual(store.latest["doctor"]["age_s"], 0.0)


class CarSide(unittest.TestCase):
    def test_runs_late_then_now_and_then(self):
        d = pubm.Doctor(run=lambda: {"items": ITEMS})
        d.started = 0.0
        self.assertFalse(d.due(10.0))
        self.assertTrue(d.due(pubm.DOCTOR_FIRST_S))
        d.last = 100.0
        self.assertFalse(d.due(100.0 + pubm.DOCTOR_EVERY_S - 1))
        self.assertTrue(d.due(100.0 + pubm.DOCTOR_EVERY_S))

    def test_sent_when_new_and_not_with_every_push(self):
        answers = []
        d = pubm.Doctor(run=lambda: {"items": ITEMS})
        d.start(time.monotonic(), then=lambda ok, detail: answers.append((ok, detail)))
        for _ in range(50):
            if not d.busy:
                break
            time.sleep(0.02)
        self.assertEqual(answers, [(True, "1 problem(s)")])
        now = time.monotonic()
        first = d.for_push(now)
        self.assertEqual(first["items"], ITEMS)
        self.assertIsNone(d.for_push(now + 1), "not again with the next push")
        self.assertIsNotNone(d.for_push(now + 301), "but again after five minutes")

    def test_a_failing_doctor_is_an_answer_not_a_crash(self):
        answers = []

        def broken():
            raise OSError("no python")
        d = pubm.Doctor(run=broken)
        d.start(time.monotonic(), then=lambda ok, detail: answers.append((ok, detail)))
        for _ in range(50):
            if not d.busy:
                break
            time.sleep(0.02)
        self.assertEqual(answers, [(False, "doctor did not run: OSError")])
        self.assertIsNone(d.for_push(time.monotonic()))

    def test_a_request_during_a_check_gets_that_checks_answer(self):
        """1.8.0: asked while the publisher's own check ran, the app got "the check is already running"."""
        gate = threading.Event()

        def slow():
            gate.wait(5)
            return {"items": ITEMS}
        answers = []
        d = pubm.Doctor(run=slow)
        d.start(time.monotonic())                          # the publisher's own, every 15 minutes
        d.start(time.monotonic(), then=lambda ok, detail: answers.append((ok, detail)))
        d.start(time.monotonic(), then=lambda ok, detail: answers.append((ok, detail)))
        self.assertTrue(d.busy)
        self.assertEqual(answers, [], "no failure while it runs")
        gate.set()
        for _ in range(100):
            if not d.busy:
                break
            time.sleep(0.02)
        self.assertEqual(answers, [(True, "1 problem(s)")] * 2)
        self.assertEqual(d.waiting, [])

    def test_the_check_comes_with_signed_updates(self):
        """The publisher runs p0/doctor.py, which signed updates replace; never deploy/, which they don't. Until
        1.8.0 it ran deploy/doctor.py, and a car computer set up before 1.3.1 kept one without --json."""
        repo = HERE.parent
        sys.path.insert(0, str(repo / "p0"))
        import update
        self.assertTrue(update.allowed("p0/doctor.py"))
        ran = []
        answer = types.SimpleNamespace(stdout='{"items": []}', stderr="", returncode=0)
        with mock.patch.object(pubm.subprocess, "run", side_effect=lambda cmd, **kw: ran.append(cmd) or answer):
            self.assertEqual(pubm.Doctor(root=repo)._run(), {"items": []})
        self.assertEqual(Path(ran[0][2]), repo / "p0" / "doctor.py")
        self.assertTrue((repo / "p0" / "doctor.py").is_file())

    def test_a_check_without_verdicts_says_why(self):
        old = types.SimpleNamespace(stdout="", returncode=2,
                                    stderr="usage: doctor.py [-h]\ndoctor.py: error: unrecognized arguments: --json\n")
        answers = []
        with mock.patch.object(pubm.subprocess, "run", return_value=old):
            d = pubm.Doctor(root=HERE.parent)
            d.start(time.monotonic(), then=lambda ok, detail: answers.append((ok, detail)))
            for _ in range(100):
                if not d.busy:
                    break
                time.sleep(0.02)
        self.assertEqual(answers, [(False, "the check gave no answer: exit 2, doctor.py: error: unrecognized "
                                           "arguments: --json")])

    def test_setup_sh_doctor_still_runs_the_check(self):
        """deploy/doctor.py, which setup.sh and the image's first start call, starts p0/doctor.py."""
        done = subprocess.run([sys.executable, "-B", str(HERE.parent / "deploy/doctor.py"), "--help"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage", done.stdout)

    def test_the_app_may_ask_for_it(self):
        self.assertIn("doctor", pubm.FEATURES)
        self.assertEqual(srv.clean_op({"kind": "doctor", "id": "a" * 32})[0], "doctor")


if __name__ == "__main__":
    unittest.main()
