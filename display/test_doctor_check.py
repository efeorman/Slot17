"""The setup check (1.3.1): the car computer's doctor verdicts travel to the apps, bounded and kept."""
from pathlib import Path
import sys
import time
import unittest

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

    def test_the_app_may_ask_for_it(self):
        self.assertIn("doctor", pubm.FEATURES)
        self.assertEqual(srv.clean_op({"kind": "doctor", "id": "a" * 32})[0], "doctor")


if __name__ == "__main__":
    unittest.main()
