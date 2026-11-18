from pathlib import Path
import queue
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify_isolation  # noqa: E402,F401 - see the module
import notify
from outbox import Outbox


def until(predicate, seconds=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(.02)
    return False


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "outbox.sqlite3")
        self.cfg = {"token": "PRIVATE_TOKEN", "user": "PRIVATE_USER"}
        self.instances = []

    def tearDown(self):
        for n in self.instances:
            n.stop()
        self.tmp.cleanup()

    def sender(self, transport):
        n = notify.Notifier(cfg=self.cfg, spool=self.path, transport=transport)
        self.instances.append(n)
        return n

    def test_reconnect_retries_without_restart_and_does_not_store_secrets(self):
        calls = []
        def transport(fields):
            calls.append(dict(fields))
            if len(calls) == 1:
                raise notify.Transient("offline")
        with patch.object(notify, "RETRY_MIN_S", .1):
            n = self.sender(transport)
            self.assertTrue(n.start())
            self.assertTrue(n.post("live RF", ttl=30))
            self.assertTrue(until(lambda: n.sent == 1))
            self.assertEqual(len(calls), 2)
            n.stop()
        data = Path(self.path).read_bytes()
        self.assertNotIn(b"PRIVATE_TOKEN", data)
        self.assertNotIn(b"PRIVATE_USER", data)
        self.assertEqual(calls[-1]["token"], "PRIVATE_TOKEN")

    def test_expired_startup_messages_are_never_sent(self):
        box = Outbox(self.path)
        box.add({"message": "stale", "timestamp": time.time() - 60, "ttl": 30})
        box.add({"message": "fresh", "timestamp": time.time(), "ttl": 30})
        box.close()
        calls = []
        n = self.sender(lambda f: calls.append(f))
        n.start()
        self.assertTrue(until(lambda: n.sent == 1))
        self.assertEqual([f["message"] for f in calls], ["fresh"])
        self.assertEqual(n.expired, 1)

    def test_permanent_failure_is_not_retried(self):
        calls = []
        def reject(f):
            calls.append(f)
            raise notify.Permanent("invalid")
        n = self.sender(reject)
        n.start()
        n.post("bad")
        self.assertTrue(until(lambda: n.failed == 1))
        n.stop()
        self.assertEqual(len(calls), 1)
        self.assertEqual(notify.spool_count(self.path), 0)

    def test_shutdown_restart_keeps_unsent_messages(self):
        n = self.sender(lambda f: (_ for _ in ()).throw(notify.Transient("offline")))
        n.start()
        n.post("retained")
        self.assertTrue(until(lambda: n.last_error is not None))
        self.assertTrue(n.stop())
        calls = []
        m = self.sender(lambda f: calls.append(f))
        self.assertTrue(m.start())
        self.assertTrue(until(lambda: m.sent == 1))
        self.assertEqual(calls[0]["message"], "retained")

    def test_single_sender_and_bounded_producer(self):
        n = self.sender(lambda f: None)
        self.assertTrue(n.start())
        self.assertFalse(self.sender(lambda f: None).start())
        m = notify.Notifier(cfg=self.cfg, spool=self.path + "second")
        m.q = queue.Queue(maxsize=1)
        h = {"tetra_channel": 1, "confirmed": True, "peak_snr_db": 20}
        self.assertTrue(m.post("fills queue"))
        self.assertFalse(m.alert_hit(h))
        self.assertNotIn(1, m._last_alert)
        m.q.get_nowait()
        self.assertTrue(m.alert_hit(h))
        self.assertFalse(m.alert_hit(h))
        fields = m.q.get_nowait()
        self.assertNotIn("token", fields)
        self.assertIn("identity or distance", fields["message"])
        self.assertFalse(m.alert_hit({"confirmed": None}))

    def test_quota_suspends_future_sends(self):
        calls = []
        def quota(f):
            calls.append(f)
            raise notify.Quota("quota", time.time() + 100)
        n = self.sender(quota)
        n.start()
        n.post("first")
        self.assertTrue(until(lambda: n.failed == 1))
        n.post("second")
        time.sleep(.15)
        self.assertEqual(len(calls), 1)

    def test_detection_messages_can_be_turned_off_without_touching_fault_messages(self):
        hit = {"tetra_channel": 119, "confirmed": True, "peak_snr_db": 55}
        off = notify.Notifier(cfg=dict(self.cfg, rf_alerts=False), spool=self.path + "off")
        self.assertFalse(off.alert_hit(hit))
        self.assertTrue(off.q.empty())
        self.assertEqual(off.rf_alerts_turned_off, 1)
        self.assertEqual(off.rf_alerts_queued, 0)
        self.assertTrue(off.post("Receiver input very low", title="TETRA health"))
        self.assertEqual(off.q.qsize(), 1)
        for value in (True, None, 0, "false"):      # only exactly false turns them off
            on = notify.Notifier(cfg=dict(self.cfg, rf_alerts=value), spool=self.path + "on")
            self.assertTrue(on.alert_hit(hit), value)
        missing = notify.Notifier(cfg=dict(self.cfg), spool=self.path + "missing")
        self.assertTrue(missing.alert_hit(hit))

    def test_calm_channel_cooldown_preserves_first_and_other_channel_alerts(self):
        # Exercise the real producer with a clock, without starting a sender or
        # contacting the network. Repeats at 16 s must stay quiet after the drive
        # feedback; expiry of the channel rate limit is exactly 60 s.
        n = self.sender(lambda fields: None)
        hit = {"tetra_channel": 119, "confirmed": True, "peak_snr_db": 16}
        with patch.object(notify.time, 'monotonic', return_value=1000.0) as clock:
            self.assertTrue(n.alert_hit(hit))
            clock.return_value = 1016.0
            self.assertFalse(n.alert_hit(hit))
            self.assertTrue(n.alert_hit(dict(hit, tetra_channel=170)))
            clock.return_value = 1059.999
            self.assertFalse(n.alert_hit(hit))
            clock.return_value = 1060.0
            self.assertTrue(n.alert_hit(hit))
            self.assertFalse(n.alert_hit(dict(hit, confirmed=False)))
            self.assertFalse(n.alert_hit(dict(hit, confirmed=None)))
        state = n.snapshot()
        self.assertEqual(state['alert_cooldown_s'], 60)
        self.assertEqual(state['rf_alerts_queued'], 3)
        self.assertEqual(state['rf_alerts_suppressed_cooldown'], 2)
        self.assertEqual(n.q.qsize(), 3)
        self.assertEqual(state['sent'], 0)

    def test_status_messages_are_audible_and_do_not_delete_themselves(self):
        # Pushover removes a message from the phone once its ttl elapses, and
        # priority -1 delivers it with no sound. The readiness push used both, so
        # "your detector is running" arrived silently and erased itself after 30 s
        # - the operator only ever saw the separate boot-notify message.
        n = self.sender(lambda fields: None)
        self.assertTrue(n.post("Monitoring 382.5 MHz.", title="TETRA monitor ready",
                               priority=notify.PRIORITY_INFO, ttl=notify.STATUS_TTL_S))
        ready = n.q.get_nowait()
        self.assertEqual(ready['priority'], 0)          # audible, not -1
        self.assertEqual(ready['ttl'], notify.STATUS_TTL_S)
        self.assertGreaterEqual(notify.STATUS_TTL_S, 3600)
        # Anything that does not ask for an expiry must persist, not vanish in 30 s.
        self.assertTrue(n.post("Detector stopped.", title="TETRA detector stopped"))
        self.assertEqual(n.q.get_nowait()['ttl'], notify.STATUS_TTL_S)
        # An RF alert still expires quickly: a stale detection is worse than none.
        hit = {"tetra_channel": 119, "confirmed": True, "peak_snr_db": 16,
               "burst_mono": 1000.0}
        with patch.object(notify.time, 'monotonic', return_value=1001.0):
            self.assertTrue(n.alert_hit(hit))
        fields = n.q.get_nowait()
        self.assertEqual(fields['ttl'], 900)
        self.assertEqual(fields['_freshness']['deadline_mono'], 1120)
        # And the outbox must not clamp a status message back down to five minutes.
        box = Outbox(self.path + '.ttltest')
        try:
            box.add({'message': 'x', 'ttl': notify.STATUS_TTL_S, 'timestamp': 1000})
            _rowid, fields, expires = box.next()
            self.assertEqual(fields['ttl'], notify.STATUS_TTL_S)
            self.assertEqual(expires, 1000 + notify.STATUS_TTL_S)
        finally:
            box.close()

    def test_clock_step_does_not_silence_rf_alerts(self):
        # The Pi cold-boots in the car on a stale clock and NTP corrects it
        # minutes later. Judging freshness on the wall clock made every match
        # look minutes old and dropped every RF alert for the rest of the drive.
        n = self.sender(lambda fields: None)
        hit = {"tetra_channel": 119, "confirmed": True, "peak_snr_db": 16,
               "utc": "2026-09-09T14:10:45.000+00:00",   # stale-clock stamp
               "burst_mono": 1000.0}
        with patch.object(notify.time, 'monotonic', return_value=1002.0), \
             patch.object(notify.time, 'time', return_value=1788963045.0 + 558):
            # Wall clock says the burst is 558 s old; the sample clock says 2 s.
            self.assertTrue(n.alert_hit(hit))
        self.assertEqual(n.snapshot()['expired'], 0)
        self.assertEqual(n.snapshot()['rf_alerts_queued'], 1)
        # A genuinely stale detection is still refused.
        with patch.object(notify.time, 'monotonic', return_value=1400.0):
            self.assertFalse(n.alert_hit(dict(hit, tetra_channel=170)))
        self.assertEqual(n.snapshot()['expired'], 1)

    def test_degraded_session_pushes_once_per_episode_and_escalates_on_new_faults(self):
        # A degraded drive must reach the phone while it is still happening, but
        # these counters only grow, so a naive "still degraded" check would push
        # every heartbeat. New fault -> immediate; same fault -> reminder only.
        def dropped(total):
            return [{'fault': 'dropped samples', 'total': total}]
        stopped = {'fault': 'recorder stopped', 'total': None}
        n = self.sender(lambda fields: None)
        with patch.object(notify.time, 'monotonic', return_value=500.0) as clock:
            self.assertFalse(n.alert_health([]))          # healthy: silence
            self.assertTrue(n.alert_health(dropped(4096)))
            clock.return_value = 501.0                    # next heartbeat, same fault
            self.assertFalse(n.alert_health(dropped(8192)))
            clock.return_value = 560.0                    # a second kind of fault is news
            self.assertTrue(n.alert_health(dropped(8192) + [stopped]))
            clock.return_value = 700.0                    # inside the reminder window
            self.assertFalse(n.alert_health(dropped(9000) + [stopped]))
            clock.return_value = 860.0                    # reminder due, totals have moved
            self.assertTrue(n.alert_health(dropped(9000) + [stopped]))
        state = n.snapshot()
        self.assertEqual(state['health_reminder_s'], 300)
        self.assertEqual(state['health_alerts_queued'], 3)
        # A healthy heartbeat is not a suppression: only the two reminder-window
        # repeats count.
        self.assertEqual(state['health_alerts_suppressed'], 2)
        self.assertEqual(state['sent'], 0)
        messages = [n.q.get_nowait() for _ in range(3)]
        self.assertTrue(all(m['title'] == 'TETRA detector degraded' for m in messages))
        self.assertIn('dropped samples 4096', messages[0]['message'])
        self.assertIn('recorder stopped', messages[1]['message'])
        # A health message must outlive an RF one; 30 s expiry would discard it.
        self.assertEqual(messages[0]['ttl'], notify.HEALTH_TTL_S)
        self.assertGreater(notify.HEALTH_TTL_S, 30)


    def test_a_steady_fault_says_its_piece_once_and_then_stops_buzzing(self):
        """A dead antenna is one condition, not an accumulating fault.

        Its total is the run length, which rises every three seconds. Treated as
        growth that earns a reminder, a deaf receiver would push every five
        minutes for a whole drive - and every evening the Pi is started at home
        with the antenna deliberately off. Once per episode is the whole point.
        """
        def quiet(windows):
            return [{'fault': 'receiver input very low - antenna connection unverified',
                     'total': windows, 'steady': True}]
        n = self.sender(lambda fields: None)
        with patch.object(notify.time, 'monotonic', return_value=1000.0) as clock:
            self.assertTrue(n.alert_health(quiet(20)))
            clock.return_value = 1001.0
            self.assertFalse(n.alert_health(quiet(21)))
            # Well past the reminder window, and the run has grown by 800
            # windows - still one condition, still nothing new to say.
            clock.return_value = 4000.0
            self.assertFalse(n.alert_health(quiet(820)))
            clock.return_value = 9000.0
            self.assertFalse(n.alert_health(quiet(8208)))
            # A genuinely different fault is still news, immediately.
            clock.return_value = 9001.0
            self.assertTrue(n.alert_health(quiet(8209) +
                                          [{'fault': 'dropped samples', 'total': 4096}]))
        self.assertEqual(n.snapshot()['health_alerts_queued'], 2)
        messages = [n.q.get_nowait() for _ in range(2)]
        # The run length still reaches the operator in the text it does appear in.
        self.assertIn('unverified 20', messages[0]['message'])

    def test_growth_permits_the_reminder_but_never_bypasses_it(self):
        """Regression: an early version pushed on every heartbeat that grew."""
        def dropped(total):
            return [{'fault': 'dropped samples', 'total': total}]
        n = self.sender(lambda fields: None)
        with patch.object(notify.time, 'monotonic', return_value=100.0) as clock:
            self.assertTrue(n.alert_health(dropped(10)))
            # Growing fast, but one second later. The reminder window rules.
            for t, total in ((101.0, 200), (150.0, 900), (399.0, 5000)):
                clock.return_value = t
                self.assertFalse(n.alert_health(dropped(total)))
            clock.return_value = 400.0           # window elapsed and it grew
            self.assertTrue(n.alert_health(dropped(5001)))
            # Window elapsed but the counter has stopped moving: silence.
            clock.return_value = 1200.0
            self.assertFalse(n.alert_health(dropped(5001)))
        self.assertEqual(n.snapshot()['health_alerts_queued'], 2)


if __name__ == "__main__":
    unittest.main()
