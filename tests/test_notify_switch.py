"""1.2.9: Pushover can be switched off from the app; off must stop every Pushover message, and only those."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify_isolation  # noqa: E402,F401
import notify  # noqa: E402


class SwitchFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "slot17-notify.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_or_unreadable_means_on(self):
        self.assertTrue(notify.pushover_enabled(self.path))
        Path(self.path).write_text("not json")
        self.assertTrue(notify.pushover_enabled(self.path))
        Path(self.path).write_text(json.dumps({"pushover": "no"}))
        self.assertTrue(notify.pushover_enabled(self.path))
        Path(self.path).write_text(json.dumps([False]))
        self.assertTrue(notify.pushover_enabled(self.path))

    def test_set_and_read_back(self):
        self.assertFalse(notify.set_pushover(False, self.path))
        self.assertFalse(notify.pushover_enabled(self.path))
        self.assertTrue(notify.set_pushover(True, self.path))
        self.assertTrue(notify.pushover_enabled(self.path))
        self.assertEqual(os.listdir(self.dir.name), ["slot17-notify.json"])     # no partial file left


class SwitchedOffTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.saved = notify.SWITCH_PATH
        notify.SWITCH_PATH = os.path.join(self.dir.name, "slot17-notify.json")

    def tearDown(self):
        notify.SWITCH_PATH = self.saved
        self.dir.cleanup()

    def notifier(self):
        cfg = {"token": "t" * 30, "user": "u" * 30}
        return notify.Notifier(cfg=cfg, spool=os.path.join(self.dir.name, "outbox.sqlite3"),
                               transport=lambda fields: None)

    def test_off_stops_queueing_and_counts_it(self):
        n = self.notifier()
        self.assertTrue(n.post("on"))
        notify.set_pushover(False)
        self.assertFalse(n.post("off"))
        self.assertFalse(n.alert_health([{"fault": "dropped samples", "total": 5}]))
        self.assertEqual(n.switched_off, 2)
        self.assertFalse(n.snapshot()["pushover_enabled"])
        notify.set_pushover(True)
        self.assertTrue(n.post("on again"))

    def test_send_now_sends_nothing_when_off(self):
        notify.set_pushover(False)
        called = []
        saved = notify._post
        notify._post = lambda fields, **kw: called.append(fields)
        try:
            self.assertIsNone(notify.send_now("hello", cfg={"token": "t" * 30, "user": "u" * 30}))
        finally:
            notify._post = saved
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
