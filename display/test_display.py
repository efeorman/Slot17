"""Offline tests for the phone display. Run: python -m unittest display/test_display.py

Kept outside tests/ so the detector's preflight suite and its pinned count are unchanged.
Browser behaviour is covered separately by test_display_browser.py.
"""
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import display_publisher as pub  # noqa: E402
import server as srv             # noqa: E402


def event(ch, eid, snr=25.0, mono=100.0, reliable=True, compact=False, **kw):
    e = {"event_type": "candidate", "confirmed": True, "tetra_channel": ch, "event_id": eid,
         "frequency_hz": 380.0125e6 + ch * 25e3, "peak_snr_db": snr, "strength_reliable": reliable,
         "burst_mono": mono, "activity": "intermittent", "matched_timing_refined": True}
    e.update(kw)
    return json.dumps(e, separators=(",", ":") if compact else None)


def heartbeat(path, session, t=1000.0, samples=1, **kw):
    hb = {"t": t, "pid": 7, "samples_in": samples, "windows_started": samples, "ready": True,
          "session": str(session), "windows": 3}
    hb.update(kw)
    path.write_text(json.dumps(hb))


class ParseTests(unittest.TestCase):
    def test_any_json_formatting_and_only_confirmed(self):
        lines = [event(1, "a", compact=True), event(2, "b"),
                 json.dumps({"event_type": "candidate", "confirmed": False, "tetra_channel": 3}),
                 json.dumps({"event_type": "candidate", "confirmed": None, "tetra_channel": 4}),
                 json.dumps({"event_type": "rf_health", "confirmed": 2}), "not json", "\0" * 20]
        got = pub.parse_lines("\n".join(lines).encode())
        self.assertEqual([e["event_id"] for e in got], ["a", "b"])

    def test_tail_partial_lines_and_nul_padding(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "events.jsonl"
            p.write_text(event(1, "old") + "\n")
            t = pub.Tail()
            t.follow(p, from_end=True)
            self.assertEqual(t.read(), [])
            with open(p, "ab") as fh:
                fh.write(event(2, "a").encode() + b"\n" + event(3, "b").encode()[:40])
            self.assertEqual([e["event_id"] for e in t.read()], ["a"])
            with open(p, "ab") as fh:
                fh.write(event(3, "b").encode()[40:] + b"\n" + b"\0" * 64)
            self.assertEqual([e["event_id"] for e in t.read()], ["b"])
            self.assertEqual(t.read(), [])


class PublisherTests(unittest.TestCase):
    def test_second_publisher_including_once_cannot_take_over(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "publisher.lock"
            with pub.publisher_lock(path):
                with self.assertRaisesRegex(SystemExit, "already running"):
                    pub.publisher_lock(path)
            with pub.publisher_lock(path):
                pass

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.s1 = self.dir / "20260911-100000Z_watch"
        self.s1.mkdir()
        (self.s1 / "events.jsonl").write_text("")
        self.hb = self.dir / "hb.json"
        self.sent = []
        self.pub = pub.Publisher(send=self.sent.append, heartbeat=self.hb)

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, session, *lines):
        with open(session / "events.jsonl", "a") as fh:
            fh.write("".join(line + "\n" for line in lines))

    def test_a_collected_stop_runs_systemctl_and_winds_the_publisher_up(self):
        from types import SimpleNamespace
        calls, notes = [], []
        self.pub.phone = lambda m, t, p: notes.append((t, p))
        ran = pub.run_stop_command({"action": "stop", "note": "from the phone display"}, self.pub,
                                   runner=lambda cmd: (calls.append(cmd),
                                                       SimpleNamespace(returncode=0, stdout="", stderr=""))[1])
        self.assertEqual(calls, [["sudo", "-n", "systemctl", "--no-block", "stop", "tetra-detector.service"]])
        self.assertFalse(ran)                      # the publisher stops publishing
        self.assertEqual(notes, [("TETRA detector stopping", 0)])

    def test_a_refused_stop_never_also_says_stopping(self):
        # 16 September: the phone got "stopping" AND "stop failed" for one tap.
        from types import SimpleNamespace
        notes = []
        self.pub.phone = lambda m, t, p: notes.append(t)
        pub.run_stop_command({"action": "stop"}, self.pub, runner=lambda cmd: SimpleNamespace(
            returncode=1, stdout="", stderr='sudo: The "no new privileges" flag is set'))
        self.assertEqual(notes, ["TETRA stop failed"])

    def test_the_stop_argv_is_exactly_what_sudoers_grants(self):
        root = Path(pub.__file__).resolve().parent.parent
        grant = [ln for ln in (root / "deploy/60-slot17-stop.sudoers").read_text().splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
        self.assertEqual(len(grant), 1)
        self.assertNotIn("start", grant[0])
        granted = grant[0].split("NOPASSWD:", 1)[1].split()
        self.assertEqual(["sudo", "-n", "systemctl"] + granted[1:], pub.STOP_ARGV)
        self.assertEqual(granted[0], "/usr/bin/systemctl")

    def test_the_publisher_unit_lets_sudo_elevate(self):
        # NoNewPrivileges=yes made every phone stop fail on 16 September.
        unit = (Path(pub.__file__).resolve().parent / "tetra-display-publisher.service").read_text()
        directives = [ln.split("=", 1)[0].strip() for ln in unit.splitlines()
                      if "=" in ln and not ln.lstrip().startswith("#")]
        self.assertNotIn("NoNewPrivileges", directives)

    def test_a_refused_stop_is_reported_and_never_takes_the_display_down(self):
        from types import SimpleNamespace
        notes = []
        self.pub.phone = lambda m, t, p: notes.append((m, t, p))
        # No sudo rule installed: systemctl refuses. The operator must be told,
        # not left believing the detector stopped.
        ran = pub.run_stop_command({"action": "stop"}, self.pub,
                                   runner=lambda cmd: SimpleNamespace(
                                       returncode=1, stdout="", stderr="sudo: a password is required"))
        self.assertTrue(ran)                       # keep publishing; nothing was stopped
        self.assertEqual(notes[-1][1], "TETRA stop failed")
        self.assertEqual(notes[-1][2], 1)
        self.assertIn("SSH", notes[-1][0])

    def test_a_stop_that_raises_is_contained(self):
        notes = []
        self.pub.phone = lambda m, t, p: notes.append(t)

        def explode(cmd):
            raise OSError("systemctl missing")
        self.assertTrue(pub.run_stop_command({"action": "stop"}, self.pub, runner=explode))
        self.assertEqual(notes[-1], "TETRA stop failed")

    def test_only_a_stop_is_ever_taken_from_the_server(self):
        sender = pub.Sender("http://example.invalid/api/push", "t" * 43)
        self.assertIsNone(sender.pending_command)
        # The publisher acts on nothing else, whatever the server sends.
        for payload in ({"action": "start"}, {"action": "reboot"}, "stop", None, {}):
            answer = {"ticket": "abc", "command": payload}
            command = answer.get("command")
            if isinstance(command, dict) and command.get("action") == "stop":
                sender.pending_command = command
            self.assertIsNone(sender.pending_command, payload)

    def test_a_notice_that_cannot_reach_the_phone_says_so_once(self):
        self.pub.phone = None
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.pub._notify("a", "b", 0)
            self.pub._notify("c", "d", 0)
        # Said once, not on every warning - but never silently, which is what
        # made the 16 September antenna warning impossible to audit.
        self.assertEqual(out.getvalue().count("phone notices unavailable"), 1)

    def test_red_is_never_cut_by_the_three_bar_limit(self):
        # Repro: a red match followed within 0.3 s by three newer green ones.
        heartbeat(self.hb, self.s1)
        self.pub.step(now_mono=50)
        self.append(self.s1, event(190, "r", snr=30, mono=50.0), event(10, "g1", snr=8, mono=50.1),
                    event(11, "g2", snr=9, mono=50.2), event(12, "g3", snr=10, mono=50.3))
        self.assertTrue(self.pub.step(now_mono=51))
        last = self.sent[-1]
        self.assertEqual(sorted(c["ch"] for c in last["channels"]), [10, 11, 12, 190])
        self.assertIn(190, [m["ch"] for m in last["matches"]])
        red = next(m for m in last["matches"] if m["ch"] == 190)
        self.assertEqual((red["strength_db"], red["age_s"]), (30.0, 1.0))

    def test_join_shows_recent_history_without_chiming_and_session_reset(self):
        self.append(self.s1, event(154, "early", snr=30, mono=45.0))    # before the publisher started
        heartbeat(self.hb, self.s1)
        self.pub.step(now_mono=50)
        first = self.sent[-1]
        self.assertEqual([c["ch"] for c in first["channels"]], [154])   # shown ...
        self.assertEqual(first["matches"], [])                          # ... never announced
        s2 = self.dir / "20260911-110000Z_watch"
        s2.mkdir()
        (s2 / "events.jsonl").write_text(event(60, "w1-ch60", mono=80.0) + "\n")
        heartbeat(self.hb, s2, t=2000, samples=2)
        self.pub.step(now_mono=81)
        last = self.sent[-1]
        self.assertEqual([c["ch"] for c in last["channels"]], [60])      # old session cleared
        self.assertEqual(last["matches"][0]["id"], "20260911-110000Z_watch/w1-ch60")

    def test_unreliable_strength_is_sent_as_null(self):
        heartbeat(self.hb, self.s1)
        self.pub.step(now_mono=50)
        self.append(self.s1, event(20, "u", snr=40, reliable=False, mono=50.5))
        self.pub.step(now_mono=51)
        self.assertIsNone(self.sent[-1]["matches"][0]["strength_db"])
        self.assertIsNone(self.sent[-1]["channels"][0]["strength_db"])

    def test_health_follows_progress_not_wall_clock(self):
        heartbeat(self.hb, self.s1, t=1000)
        self.pub.step(now_mono=50)
        self.assertEqual(self.sent[-1]["detector"]["state"], "running")
        heartbeat(self.hb, self.s1, t=500, samples=2)                   # clock stepped back: still progress
        self.pub.step(now_mono=52.1)
        self.assertEqual(self.sent[-1]["detector"]["state"], "running")
        heartbeat(self.hb, self.s1, t=1881, samples=3)                  # forward step: still progress
        self.pub.step(now_mono=54.2)
        self.assertEqual(self.sent[-1]["detector"]["state"], "running")
        self.pub.step(now_mono=54.2 + pub.STALE_S + 0.6)                 # file present but frozen
        self.assertEqual(self.sent[-1]["detector"]["state"], "stale")

    def test_unsent_matches_survive_a_failed_push(self):
        heartbeat(self.hb, self.s1)
        self.pub.step(now_mono=50)
        fail = [True]

        def send(payload):
            if fail[0]:
                raise OSError("offline")
            self.sent.append(payload)
        self.pub.send = send
        self.append(self.s1, event(7, "m", mono=50.2))
        self.assertFalse(self.pub.step(now_mono=50.3))
        fail[0] = False
        self.assertTrue(self.pub.step(now_mono=52.4))
        self.assertEqual([m["id"].split("/")[-1] for m in self.sent[-1]["matches"]], ["m"])
        self.pub.step(now_mono=54.5)
        self.assertEqual(self.sent[-1]["matches"], [])                  # delivered once

    def quiet_beat(self, window, suspect, peak=780.0, t=1000.0, samples=1):
        heartbeat(self.hb, self.s1, t=t, samples=samples,
                  rf_health={"window_id": window, "input": {"antenna_suspect": suspect, "peak_counts": peak}})

    def test_quiet_receiver_is_counted_per_window_and_warns_once(self):
        phoned = []
        self.pub.phone = lambda m, t, p: phoned.append((t, p, m))
        now = 0.0
        for i in range(pub.QUIET_ALARM_WINDOWS - 1):          # just under the threshold
            now += 1
            self.quiet_beat(f"w{i:05d}", True, t=1000 + i, samples=i + 1)
            self.pub.step(now_mono=now)
        self.assertEqual(phoned, [])
        self.assertEqual(self.sent[-1]["detector"]["quiet_windows"], pub.QUIET_ALARM_WINDOWS - 1)
        self.assertEqual(self.sent[-1]["detector"]["input_peak_counts"], 780.0)
        now += 1                                              # reading the same window twice cannot inflate it
        self.pub.step(now_mono=now)
        self.assertEqual(phoned, [])
        now += 1
        self.quiet_beat("w00019", True, t=2000, samples=99)
        self.pub.step(now_mono=now)
        self.assertEqual(len(phoned), 1)
        self.assertEqual(phoned[0][1], 1)                      # high priority
        self.assertIn("antenna", phoned[0][2].lower())
        now += 1                                               # persisting: no repeat before the interval
        self.quiet_beat("w00020", True, t=2001, samples=100)
        self.pub.step(now_mono=now)
        self.assertEqual(len(phoned), 1)
        now += pub.QUIET_REPEAT_S
        self.quiet_beat("w00021", True, t=2002, samples=101)
        self.pub.step(now_mono=now)
        self.assertEqual(len(phoned), 2)

    def test_recovered_input_clears_the_warning_and_says_so(self):
        phoned = []
        self.pub.phone = lambda m, t, p: phoned.append((t, p, m))
        now = 0.0
        for i in range(pub.QUIET_ALARM_WINDOWS):
            now += 1
            self.quiet_beat(f"w{i:05d}", True, t=1000 + i, samples=i + 1)
            self.pub.step(now_mono=now)
        self.assertEqual(len(phoned), 1)
        now += 1
        self.quiet_beat("w00099", False, peak=5200.0, t=3000, samples=500)
        self.pub.step(now_mono=now)
        self.assertEqual(self.sent[-1]["detector"]["quiet_windows"], 0)
        self.assertEqual(len(phoned), 2)
        self.assertEqual(phoned[1][1], 0)                      # recovery is not an alarm
        self.assertIn("normal", phoned[1][2].lower())

    def test_quiet_limit_is_a_minute_whatever_the_window_length(self):
        self.assertEqual(pub.quiet_limit({"quiet_input_alarm_windows": 120, "window_s": 0.5}), 120)
        self.assertEqual(pub.quiet_limit({"quiet_input_alarm_windows": 7, "window_s": 0.5}), 7)   # the detector decides
        self.assertEqual(pub.quiet_limit({"window_s": 0.5}), 120)   # its figure went missing: still a minute
        self.assertEqual(pub.quiet_limit({"window_s": 1.5}), 40)
        self.assertEqual(pub.quiet_limit({"window_s": 3.0}), pub.QUIET_ALARM_WINDOWS)
        self.assertIsNone(pub.quiet_limit({}))
        for bad in (0, -1, float("nan"), "0.5", True, 600):
            self.assertIsNone(pub.quiet_limit({"window_s": bad}), bad)

    def test_half_second_windows_without_a_stated_limit_do_not_warn_after_ten_seconds(self):
        """The old fallback of 20 windows would have meant 10 s of 0.5 s windows."""
        phoned = []
        self.pub.phone = lambda m, t, p: phoned.append((t, p, m))
        heartbeat(self.hb, self.s1, t=1000, samples=1, window_s=0.5, quiet_windows=119)
        self.pub.step(now_mono=1.0)
        self.assertEqual(phoned, [])
        self.assertEqual(self.sent[-1]["detector"]["quiet_alarm_windows"], 120)
        heartbeat(self.hb, self.s1, t=1001, samples=2, window_s=0.5, quiet_windows=120)
        self.pub.step(now_mono=2.0)
        self.assertEqual(len(phoned), 1)
        self.assertIn("1 min", phoned[0][2])

    def test_a_failed_push_is_retried_within_half_a_second_then_backs_off(self):
        calls = []

        def failing(payload):
            calls.append(payload)
            raise OSError("timed out")
        p = pub.Publisher(send=failing, heartbeat=self.hb)
        heartbeat(self.hb, self.s1)
        now = 0.0
        p.step(now_mono=now)                                   # first push fails
        self.assertEqual(len(calls), 1)
        now += pub.RETRY_S + 0.01
        p.step(now_mono=now)                                   # retried at once, not after the keepalive
        self.assertEqual(len(calls), 2)
        for _ in range(pub.FAST_RETRIES):
            now += pub.RETRY_S + 0.01
            p.step(now_mono=now)
        before = len(calls)
        now += pub.RETRY_S + 0.01
        p.step(now_mono=now)
        self.assertEqual(len(calls), before, "after the quick retries it waits for the keepalive again")
        now += pub.KEEPALIVE_S
        p.step(now_mono=now)
        self.assertEqual(len(calls), before + 1)

    def test_exits_only_after_detector_gone_long_enough(self):
        heartbeat(self.hb, self.s1)
        self.pub.step(now_mono=0)
        self.hb.unlink()
        self.pub.step(now_mono=1)
        self.assertEqual(self.sent[-1]["detector"]["state"], "stopped")
        self.assertFalse(self.pub.finished(1 + pub.STOPPED_EXIT_S - 0.5))
        heartbeat(self.hb, self.s1, samples=9)
        self.pub.step(now_mono=20)
        self.assertFalse(self.pub.finished(20 + pub.STOPPED_EXIT_S))
        self.hb.unlink()
        self.pub.step(now_mono=25)
        self.assertTrue(self.pub.finished(25 + pub.STOPPED_EXIT_S))


class CallAndRisingTests(unittest.TestCase):
    def test_rising_needs_three_matches_six_db_up_and_the_newest_strongest(self):
        self.assertFalse(pub.rising([10, 20], 20))
        self.assertTrue(pub.rising([10, 13, 16], 16))
        self.assertFalse(pub.rising([10, 13, 15.9], 15.9), "under 6 dB of rise")
        self.assertFalse(pub.rising([10, 30, 17], 17), "not the strongest of the minute")
        self.assertFalse(pub.rising([10, 12, 18], None))

    def test_the_publisher_carries_call_and_rising_to_the_phone(self):
        ch = pub.Channels()
        for k, (snr, mono) in enumerate([(12.0, 100.0), (15.0, 110.0), (19.0, 120.0)]):
            m = ch.add(json.loads(event(170, f"w{k}", snr=snr, mono=mono, call_like=(k == 2))), "s", mono)
        self.assertEqual((m["call"], m["rising"]), (True, True))
        row = ch.snapshot(121.0)[0]
        self.assertEqual((row["call"], row["rising"]), (True, True))
        self.assertFalse(ch.snapshot(121.0 + pub.CALL_HOLD_S + 1)[0]["call"], "the call mark fades after 10 s")
        old = ch.add(json.loads(event(170, "w9", snr=11.0, mono=200.0)), "s", 200.0)
        self.assertEqual((old["call"], old["rising"]), (False, False), "a minute later the history has gone")

    def test_the_server_keeps_the_new_fields_and_only_as_booleans(self):
        state = {"v": 3, "source": "x", "ticket": "t", "snapshot_age_s": 0, "seq": 1, "session": "s",
                 "detector": {"state": "running"},
                 "channels": [{"ch": 1, "id": "a", "freq_mhz": 380.0, "strength_db": 20, "age_s": 1, "hits_60s": 2,
                               "rising": True, "call": "yes"}],
                 "matches": [{"id": "a", "ch": 1, "strength_db": 20, "age_s": 1, "delay_s": 1, "call": True}]}
        clean = srv.clean_state(state)
        self.assertEqual((clean["channels"][0]["rising"], clean["channels"][0]["call"]), (True, False))
        self.assertEqual((clean["matches"][0]["call"], clean["matches"][0]["rising"]), (True, False))


class ConnectionCardTests(unittest.TestCase):
    def test_the_link_block_names_the_network_and_carries_the_stick_reading(self):
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "modem.json"
            state.write_text(json.dumps({"device": "eth1", "connected": True, "reading_at": 100.0,
                                         "reading": {"network_type": "LTE", "signalbar": "3", "lte_rsrp": "-107",
                                                     "lte_rsrq": "-12", "lte_snr": "-1.0", "cell_id": "24c315",
                                                     "lte_pci": "177", "roam_setting_option": "on",
                                                     "network_provider": "LycaMobile"},
                                         "trip": [5, 5, 48.2, 90.1]}))
            stats = pub.LinkStats()
            stats.ok(52, None), stats.ok(140, None)
            stats.failed = 1
            stats.reset()
            link = pub.read_link(stats, routes=["eth1", "wlan0"], modem_state=state, now_mono=130.0, synced=True)
            self.assertEqual((link["via"], link["networks"], link["device"]), ("4G stick", ["4G stick", "Wi-Fi"], "eth1"))
            self.assertEqual((link["pushes"], link["failed"], link["median_ms"], link["slowest_ms"]), (2, 1, 140, 140))
            self.assertTrue(link["clock_synced"])
            stick = link["stick"]
            self.assertEqual((stick["network"], stick["rsrp"], stick["rsrq"], stick["snr"], stick["bars"]), ("LTE", -107, -12, -1.0, 3))
            self.assertEqual((stick["ping_ms"], stick["ping_lost"], stick["roaming"], stick["cell"]), (48.2, 0, True, "24c315/177"))
            # A reading older than two minutes is not shown; the route still is.
            old = pub.read_link(stats, routes=["eth0"], modem_state=state, now_mono=100.0 + pub.MODEM_FRESH_S + 1)
            self.assertEqual(old["via"], "cable")
            self.assertNotIn("stick", old)
            self.assertEqual(pub.read_link(None, routes=[], modem_state=Path(d) / "missing")["via"], "none")

    def test_the_server_keeps_only_known_link_fields(self):
        dirty = {"via": "4G stick", "networks": ["4G stick", "evil"], "median_ms": 52, "clock_synced": "yes",
                 "stick": {"connected": True, "rsrp": -107, "cell": "x" * 99, "extra": 1}, "junk": 5}
        clean = srv.clean_link(dirty)
        self.assertEqual(clean["networks"], ["4G stick", "other"])
        self.assertFalse(clean["clock_synced"])
        self.assertEqual(len(clean["stick"]["cell"]), 24)
        self.assertNotIn("extra", clean["stick"])
        self.assertNotIn("junk", clean)
        with self.assertRaises(ValueError):
            srv.clean_link("not a dict")


class FailoverTests(unittest.TestCase):
    """A push that stalls on one network goes out over the next one straight away."""

    def sender(self, works, routes=("eth1", "wlan0")):
        """A Sender whose exchange succeeds only over the devices in `works`."""
        s = pub.Sender("http://203.0.113.5:5732/api/push", "t" * 43, routes=lambda: list(routes))
        used = []

        def connect(device):
            s.device, s.conn = device, mock.Mock()

        def exchange(payload):
            used.append(s.device)
            if s.device not in works:
                raise TimeoutError("timed out")
        s._connect, s._exchange = connect, exchange
        return s, used

    def test_a_stall_on_the_stick_goes_out_over_the_hotspot_in_the_same_push(self):
        s, used = self.sender(works={"wlan0"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            s({"snapshot_mono": time.monotonic()})
        self.assertEqual(used, [None, "wlan0"])                # the kernel's route (the stick), then the hotspot
        self.assertEqual((s.device, s.stats.failovers, s.stats.failed), ("wlan0", 1, 0))
        self.assertIn("push went over wlan0", out.getvalue())
        used.clear()
        s({"snapshot_mono": time.monotonic()})                 # stays on the hotspot for now
        self.assertEqual(used, ["wlan0"])

    def test_it_goes_back_to_the_preferred_network_after_a_while(self):
        s, used = self.sender(works={"wlan0", None})
        with contextlib.redirect_stdout(io.StringIO()):
            s.device, s.conn, s.pinned_since = "wlan0", mock.Mock(), time.monotonic() - s.PREFERRED_AFTER_S - 1
            s({"snapshot_mono": time.monotonic()})
        self.assertEqual(used, [None])
        self.assertIsNone(s.device)

    def test_when_every_network_fails_the_push_fails_once_each(self):
        s, used = self.sender(works=set())
        with self.assertRaises(TimeoutError):
            s({"snapshot_mono": time.monotonic()})
        self.assertEqual(used, [None, "wlan0"])
        self.assertEqual(s.stats.failed, 1)

    def test_a_dropped_kept_open_connection_is_first_reopened_on_the_same_network(self):
        s, used = self.sender(works={"wlan0"})
        s.device, s.conn = "wlan0", mock.Mock()
        calls = []

        def exchange(payload):
            used.append(s.device)
            calls.append(s.conn)
            if len(calls) == 1:
                raise ConnectionResetError("server closed it")
        s._exchange = exchange
        s({"snapshot_mono": time.monotonic()})
        self.assertEqual(used, ["wlan0", "wlan0"])
        self.assertEqual(s.stats.failovers, 0)

    def test_a_refused_push_is_not_retried_elsewhere(self):
        s, used = self.sender(works=set())

        def refused(payload):
            used.append(s.device)
            raise pub.PushRejected("server answered 401")
        s._exchange = refused
        with self.assertRaises(pub.PushRejected):
            s({"snapshot_mono": time.monotonic()})
        self.assertEqual(used, [None])

    def test_the_link_line_says_how_many_how_fast_and_over_what(self):
        stats = pub.LinkStats()
        self.assertIsNone(stats.line(60))
        for ms in (40, 50, 60, 900):
            stats.ok(ms, "eth1")
        stats.ok(70, "wlan0")
        stats.failed, stats.failovers = 1, 1
        line = stats.line(60)
        self.assertIn("link: 5 pushes in 60 s, 1 failed, median 60 ms, slowest 900 ms", line)
        self.assertIn("via eth1 4, wlan0 1", line)
        self.assertIn("1 sent over another network", line)

    def test_default_routes_are_read_best_first(self):
        out = ("default via 192.168.0.1 dev eth1 proto dhcp src 192.168.0.178 metric 200 \n"
               "default via 172.20.10.1 dev wlan0 proto dhcp src 172.20.10.2 metric 600 \n"
               "default via 192.168.1.1 dev eth0 proto dhcp src 192.168.1.57 metric 100 \n")
        with mock.patch.object(pub.subprocess, "run", return_value=mock.Mock(stdout=out)):
            self.assertEqual(pub.default_routes(), ["eth0", "eth1", "wlan0"])
        with mock.patch.object(pub.subprocess, "run", side_effect=OSError("no ip")):
            self.assertEqual(pub.default_routes(), [])

    def test_a_pinned_connection_really_connects(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            conn = pub.PinnedConnection("127.0.0.1", listener.getsockname()[1], timeout=2, device=None)
            conn.connect()
            self.assertIsNotNone(conn.sock)
            conn.close()
        finally:
            listener.close()


class ServerTests(unittest.TestCase):
    TOKEN, KEY = "p" * 43, "v" * 24

    def setUp(self):
        self.store = srv.Store()
        self.httpd = srv.CappedServer(("127.0.0.1", 0), srv.make_handler(
            {"pi_token": self.TOKEN, "viewer_key": self.KEY}, self.store, b"<html>__NONCE__</html>"))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def req(self, method, path, body=None, headers=None, conn=None):
        c = conn or http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        out = r.status, dict(r.getheaders()), r.read()
        if conn is None:
            c.close()
        return out

    def state(self, seq=1, **kw):
        s = {"v": 3, "source": "test", "ticket": "placeholder", "snapshot_age_s": 0,
             "seq": seq, "session": "s", "detector": {"state": "running", "windows": 4},
             "channels": [{"ch": 190, "id": "s/a", "freq_mhz": 384.7625, "strength_db": 30.0, "age_s": 1.0,
                           "hits_60s": 2, "activity": "intermittent", "approx_time": False}],
             "matches": [{"id": "s/a", "ch": 190, "strength_db": 30.0, "age_s": 1.0, "delay_s": 4.1}]}
        s.update(kw)
        return s

    def push(self, payload, conn=None):
        payload = dict(payload, ticket=self.store.ticket(payload.get("source", "test")))
        return self.req("POST", "/api/push", json.dumps(payload),
                        {"Authorization": "Bearer " + self.TOKEN, "Content-Type": "application/json"}, conn)[0]

    def cmd(self, body, cookie=True):
        headers = {"Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = f"s17={self.KEY}"
        return self.req("POST", "/api/command", body, headers)

    def test_stop_command_needs_the_viewer_key_and_only_accepts_stop(self):
        self.assertEqual(self.cmd('{"action":"stop"}', cookie=False)[0], 401)
        self.assertIsNone(self.store.command_status())
        # No verb but stop exists. A start button would invite the repeated
        # Airspy cycling this board wedges on (rule 3 in CONTRIBUTING.md), and there is
        # nothing on the Pi listening to act on one anyway.
        for bad in ('{"action":"start"}', '{"action":"restart"}', '{"action":"stop","note":' + '"x"' * 200 + '}',
                    '"stop"', '{}', 'not json'):
            self.assertEqual(self.cmd(bad)[0], 400, bad[:40])
        self.assertIsNone(self.store.command_status())
        self.assertEqual(self.cmd('{"action":"stop","note":"from the phone"}')[0], 200)
        self.assertEqual(self.store.command_status()["state"], "pending")

    def test_the_pi_collects_a_stop_on_the_clock_challenge_exactly_once(self):
        """The Pi is outbound-only: a command can only ride a request it makes."""
        head = {"Authorization": "Bearer " + self.TOKEN, "X-Display-Source": "src1"}
        status, _, body = self.req("GET", "/api/clock", headers=head)
        self.assertEqual(status, 200)
        self.assertNotIn("command", json.loads(body))        # nothing queued
        self.assertEqual(self.cmd('{"action":"stop"}')[0], 200)
        status, _, body = self.req("GET", "/api/clock", headers=head)
        answer = json.loads(body)
        self.assertEqual(answer["command"]["action"], "stop")
        self.assertTrue(answer["ticket"])                    # still a usable challenge
        # Collected once; a retrying publisher must not stop a second session.
        self.assertNotIn("command", json.loads(self.req("GET", "/api/clock", headers=head)[2]))
        self.assertEqual(self.store.command_status()["state"], "sent")

    def test_asking_twice_queues_one_stop(self):
        self.assertEqual(self.cmd('{"action":"stop"}')[0], 200)
        first = self.store.command["id"]
        self.assertEqual(self.cmd('{"action":"stop"}')[0], 200)
        self.assertEqual(self.store.command["id"], first)

    def test_the_phone_is_told_how_far_the_stop_has_got(self):
        self.assertNotIn("command", self.store.message())
        self.cmd('{"action":"stop"}')
        self.assertEqual(self.store.message()["command"]["state"], "pending")
        self.store.take_command()
        self.assertEqual(self.store.message()["command"]["state"], "sent")
        ticket = self.store.ticket("pi")
        self.store.put({"v": 3, "source": "pi", "ticket": ticket, "snapshot_age_s": 0.0, "seq": 1, "session": None,
                        "detector": {"state": "stopped"}, "channels": [], "matches": []})
        self.assertEqual(self.store.message()["command"]["state"], "done")

    def test_the_page_csp_allows_the_audio_element_but_nothing_else_new(self):
        """media-src blob: is load-bearing: without it the iOS fix is dead."""
        status, headers, _ = self.req("GET", "/", headers={"Cookie": f"s17={self.KEY}"})
        self.assertEqual(status, 200)
        csp = headers["Content-Security-Policy"]
        self.assertIn("media-src blob:", csp)
        self.assertIn("default-src 'none'", csp)
        # The widening must stop at blob: media.
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("media-src *", csp)
        self.assertIn("frame-ancestors 'none'", csp)

    def test_home_screen_app_files_are_public_and_harmless(self):
        status, headers, body = self.req("GET", "/manifest.webmanifest")
        self.assertEqual(status, 200)
        manifest = json.loads(body)
        self.assertEqual((manifest["start_url"], manifest["display"]), ("/", "standalone"))
        self.assertNotIn(self.KEY, body.decode())
        for path in ("/icon-180.png", "/icon-512.png", "/apple-touch-icon.png"):
            status, headers, body = self.req("GET", path)
            self.assertEqual((status, headers["Content-Type"], body[:4]), (200, "image/png", b"\x89PNG"), path)

    def test_a_home_screen_app_without_the_cookie_can_paste_the_link(self):
        status, headers, body = self.req("GET", "/")
        self.assertEqual(status, 401)
        self.assertIn(b'name="link"', body)
        csp = headers["Content-Security-Policy"]
        self.assertIn("form-action 'self'", csp)
        self.assertNotIn("script-src", csp)
        self.assertNotIn(b"__NONCE__", body)
        for pasted in (f"http://203.0.113.5:5732/?key={self.KEY}", self.KEY, f"  {self.KEY}  "):
            status, headers, _ = self.req("GET", "/?" + urllib.parse.urlencode({"link": pasted}))
            self.assertEqual(status, 303, pasted)
            self.assertIn(f"s17={self.KEY}", headers["Set-Cookie"])
        status, _, body = self.req("GET", "/?" + urllib.parse.urlencode({"link": "http://x/?key=wrong"}))
        self.assertEqual(status, 401)
        self.assertIn(b"not this server", body)
        self.assertNotIn(b"wrong", body, "nothing pasted is echoed back")

    def test_auth_flow(self):
        body = json.dumps(self.state())
        self.assertEqual(self.req("POST", "/api/push", body, {"Content-Type": "application/json"})[0], 401)
        self.assertEqual(self.req("POST", "/api/push", body, {"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.push(self.state()), 204)
        self.assertEqual(self.req("GET", "/")[0], 401)
        self.assertEqual(self.req("GET", "/api/state")[0], 401)
        self.assertEqual(self.req("GET", "/api/stream")[0], 401)
        self.assertEqual(self.req("GET", "/api/clock")[0], 401)
        self.assertEqual(self.req("GET", "/?key=nope")[0], 401)
        status, headers, _ = self.req("GET", "/?key=" + self.KEY)
        self.assertEqual(status, 303)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        cookie = headers["Set-Cookie"].split(";")[0]
        status, headers, body = self.req("GET", "/", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertNotIn(b"__NONCE__", body)
        self.assertIn("nonce-", headers["Content-Security-Policy"])
        msg = json.loads(self.req("GET", "/api/state", headers={"Cookie": cookie})[2])
        self.assertEqual((msg["seq"], msg["state"]["channels"][0]["ch"]), (1, 190))

    def test_validation_rejects_bad_fields_and_drops_unknown_ones(self):
        ok = self.state()
        ok["channels"][0]["evil"] = "<script>"
        clean = srv.clean_state(json.loads(json.dumps(ok)))
        self.assertNotIn("evil", clean["channels"][0])
        bad_cases = []
        for mutate in (lambda s: s["channels"][0].update(strength_db=float("nan")),
                       lambda s: s["channels"][0].update(age_s=True),
                       lambda s: s["channels"][0].update(ch="190"),
                       lambda s: s["matches"][0].update(id="x" * 500),
                       lambda s: s["detector"].update(state="hacked"),
                       lambda s: s["detector"].update(state=[]),
                       lambda s: s.update(channels=[{}] * 9),
                       lambda s: s.update(v=1)):
            s = self.state()
            mutate(s)
            bad_cases.append(s)
        for s in bad_cases:
            with self.assertRaises(ValueError):
                srv.clean_state(s)
        self.assertEqual(self.req("POST", "/api/push", '{"v":2,"seq":1,"detector":{"state":"running"},'
                                  '"channels":[{"ch":1,"id":"a","freq_mhz":380,"strength_db":NaN,"age_s":1,'
                                  '"hits_60s":1}]}',
                                  {"Authorization": "Bearer " + self.TOKEN})[0], 400)

    def test_old_and_duplicate_producer_sequences_never_refresh_state(self):
        self.assertEqual(self.push(self.state(seq=2)), 204)
        before = self.store.message()
        received = self.store.received_mono
        for seq in (1, 2):
            self.assertEqual(self.push(self.state(seq=seq, detector={"state": "stopped"})), 204)
            self.assertEqual(self.store.seq, before["seq"])
            self.assertEqual(self.store.received_mono, received)
            self.assertEqual(self.store.latest["detector"]["state"], "running")

    def test_old_publisher_and_expired_tickets_are_fenced(self):
        old = self.state(ticket=self.store.ticket("test"))
        new = self.state(source="new", ticket=self.store.ticket("new"))
        self.assertTrue(self.store.put(srv.clean_state(new)))
        with self.assertRaises(ValueError):
            self.store.put(srv.clean_state(old))
        with self.assertRaises(ValueError):
            self.store.ticket("test")
        with mock.patch.object(srv.time, "monotonic", return_value=100):
            ticket = self.store.ticket("new")
        with mock.patch.object(srv.time, "monotonic", return_value=107):
            with self.assertRaises(ValueError):
                self.store.put(srv.clean_state(self.state(source="new", ticket=ticket, seq=2)))
        self.assertEqual(self.store.seq, 1)

    def test_delayed_upload_ages_burst_and_snapshot_before_publishing(self):
        # Ticket issued on server at 100, delayed upload arrives at 103.
        # Ages came from the Pi AFTER its receipt of the challenge. No clocks
        # are compared across machines, and none of this delay may disappear.
        with mock.patch.object(srv.time, "monotonic", return_value=100):
            ticket = self.store.ticket("test")
        state = self.state(ticket=ticket, snapshot_age_s=0.4)
        state["matches"][0]["age_s"] = 8.1
        with mock.patch.object(srv.time, "monotonic", return_value=103):
            self.store.put(srv.clean_state(state))
            msg = self.store.message()
        self.assertGreaterEqual(msg["state"]["matches"][0]["age_s"], 11.1)
        self.assertGreaterEqual(msg["state"]["snapshot_age_s"], 3.4)
        with mock.patch.object(srv.time, "monotonic", return_value=105):
            self.assertEqual(self.store.message()["server_age_s"], 2)

    def test_sender_includes_wait_for_challenge_without_mutating_snapshot(self):
        sender = pub.Sender(f"http://127.0.0.1:{self.port}/api/push", self.TOKEN)
        self.addCleanup(sender.close)
        state = self.state(snapshot_mono=time.monotonic() - 2)
        sender(state)
        self.assertGreaterEqual(self.store.latest["snapshot_age_s"], 2)
        self.assertGreaterEqual(self.store.latest["matches"][0]["age_s"], 3)
        self.assertEqual(state["matches"][0]["age_s"], 1)

    def test_keep_alive_pushes_share_one_connection(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(c.close)
        for seq in (1, 2, 3):
            self.assertEqual(self.push(self.state(seq=seq), conn=c), 204)
        self.assertEqual(self.store.seq, 3)

    def test_stream_delivers_pushes_immediately_and_repeats(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(f"GET /api/stream HTTP/1.1\r\nHost: x\r\nCookie: s17={self.KEY}\r\n\r\n".encode())
        f = s.makefile("rb")
        self.addCleanup(f.close)
        self.addCleanup(s.close)
        self.assertIn(b"200", f.readline())

        def next_event():
            while True:
                line = f.readline()
                if line.startswith(b"data: "):
                    return json.loads(line[6:])
        first = next_event()
        self.assertIsNone(first["state"])
        t0 = time.monotonic()
        self.assertEqual(self.push(self.state(seq=5)), 204)
        got = next_event()
        self.assertLess(time.monotonic() - t0, 0.5)                      # pushed, not polled
        self.assertEqual((got["seq"], got["state"]["channels"][0]["ch"]), (1, 190))
        again = next_event()                                            # keepalive repeat
        self.assertEqual(again["seq"], got["seq"])
        self.assertGreaterEqual(again["server_age_s"], 1.5)
        s.close()

    def test_incomplete_upload_is_released_by_the_deadline(self):
        with mock.patch.object(srv, "BODY_DEADLINE_S", 0.5):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
            s.sendall(b"POST /api/push HTTP/1.1\r\nHost: x\r\nContent-Length: 500\r\n\r\nx")
            t0 = time.monotonic()
            status = s.recv(64)
            self.assertIn(b"408", status)
            self.assertLess(time.monotonic() - t0, 2)
            s.close()

    def test_oversize_upload_gets_413(self):
        good = {"Authorization": "Bearer " + self.TOKEN, "Content-Type": "application/json"}
        self.assertEqual(self.req("POST", "/api/push", "x" * (srv.MAX_BODY + 1), good)[0], 413)
        self.assertEqual(self.req("POST", "/api/push", "{not json", good)[0], 400)

    def test_idle_connections_time_out_and_capacity_is_capped(self):
        httpd = srv.CappedServer(("127.0.0.1", 0), srv.make_handler(
            {"pi_token": self.TOKEN, "viewer_key": self.KEY}, srv.Store(), b"x"), max_threads=2)
        httpd.RequestHandlerClass.timeout = 0.5
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            idle = [socket.create_connection(("127.0.0.1", port), timeout=5) for _ in range(2)]
            time.sleep(0.1)
            extra = socket.create_connection(("127.0.0.1", port), timeout=5)
            extra.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n")
            try:
                dropped = extra.recv(64) == b""                          # over capacity: dropped ...
            except ConnectionError:
                dropped = True                                           # ... or reset (Windows)
            self.assertTrue(dropped)
            time.sleep(1.0)                                              # idle sockets time out
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            self.addCleanup(c.close)
            c.request("GET", "/healthz")
            self.assertEqual(c.getresponse().status, 200)                # slots were released
            for s in idle + [extra]:
                s.close()
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_sender_reconnects_after_the_server_drops_keepalive(self):
        sender = pub.Sender(f"http://127.0.0.1:{self.port}/api/push", self.TOKEN)
        sender(self.state(seq=1))
        sender.conn.sock.close()                                        # connection silently gone
        sender(self.state(seq=2))
        self.assertEqual(self.store.seq, 2)
        bad = pub.Sender(f"http://127.0.0.1:{self.port}/api/push", "q" * 40)
        with self.assertRaises(pub.PushRejected):
            bad(self.state(seq=3))
        sender.close()

    def test_init_config_is_private_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.json"
            srv.init_config(p)
            cfg = srv.load_config(p)
            self.assertGreaterEqual(len(cfg["pi_token"]), 32)
            if os.name == "posix":
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(SystemExit):
                srv.init_config(p)


class FakeLinkProcess:
    """Stands in for p0/bluetooth_link.py's process."""

    def __init__(self, lines=()):
        self.code = None
        self.stdout = [json.dumps(x).encode() + b"\n" for x in lines]
        self.stdin = SimpleNamespace(closed=False)
        self.stdin.close = lambda: setattr(self.stdin, "closed", True)

    def poll(self):
        return self.code

    @property
    def returncode(self):
        return self.code

    def wait(self, timeout=None):
        self.code = 0 if self.code is None else self.code
        return self.code

    def kill(self):
        self.code = -9


class RecordingLink(pub.BluetoothLink):
    """A BluetoothLink whose writes are recorded; `full` makes the pipe refuse them."""

    def __init__(self, config, processes):
        super().__init__(root=Path(__file__).resolve().parents[1], config=config, spawn=self._next)
        self.processes, self.written, self.full = processes, [], False

    def _next(self):
        if not self.processes:
            raise OSError("no more processes")
        return self.processes.pop(0)

    def _write(self, data):
        if self.full:
            raise BlockingIOError
        self.written.append(json.loads(data))


class BluetoothLinkTests(unittest.TestCase):
    """1.4.0: the publisher runs the Bluetooth link while it is switched on, and nothing it does can stop a push."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.config = pub.bluetooth_module()
        if self.config is None:
            self.skipTest("p0/bluetooth_config.py is not here (a display-only copy)")
        self.addCleanup(setattr, self.config, "PATH", self.config.PATH)
        self.config.PATH = str(self.dir / "slot17-bluetooth.json")

    def tearDown(self):
        self.tmp.cleanup()

    def link(self, *processes, on=True):
        if on is not None:
            self.config.save(on=on)
        return RecordingLink(self.config, list(processes))

    def test_switched_off_it_starts_nothing(self):
        link = self.link(FakeLinkProcess(), on=False)
        link.poll(10.0)
        self.assertFalse(link.running())
        self.assertEqual(link.state(10.0)["state"], "off")
        self.assertFalse(link.feed({"matches": []}))

    def test_switched_on_it_starts_once_and_takes_commands(self):
        link = self.link(FakeLinkProcess(), FakeLinkProcess())
        link.poll(10.0)
        link.poll(11.0)
        self.assertTrue(link.running())
        self.assertTrue(link.feed({"matches": [{"id": "s/e"}]}))
        self.assertEqual(link.written, [{"matches": [{"id": "s/e"}]}])

    def test_a_full_pipe_drops_instead_of_waiting(self):
        link = self.link(FakeLinkProcess())
        link.poll(10.0)
        link.full = True
        self.assertFalse(link.feed({"status": {}}))
        self.assertEqual(link.dropped, 1)
        self.assertFalse(link.feed({"x": "y" * pub.BLUETOOTH_LINE_BYTES}))   # too long to be all or nothing
        self.assertEqual(link.dropped, 2)

    def test_an_ending_link_is_started_again_ever_more_slowly(self):
        first, second, third = FakeLinkProcess(), FakeLinkProcess(), FakeLinkProcess()
        link = self.link(first, second, third)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            link.poll(10.0)
            first.code = 3
            link.poll(11.0)
            self.assertFalse(link.running())
            link.poll(15.9)
            self.assertFalse(link.running())
            link.poll(16.1)
            self.assertTrue(link.running())
            second.code = 3
            link.poll(17.0)
            link.poll(46.0)
            self.assertFalse(link.running())
            link.poll(47.1)
            self.assertTrue(link.running())
        self.assertIn("starting it again in 5 s", out.getvalue())
        self.assertIn("starting it again in 30 s", out.getvalue())

    def test_switched_off_from_the_app_it_is_stopped(self):
        proc = FakeLinkProcess()
        link = self.link(proc)
        link.poll(10.0)
        self.config.save(on=False)
        link.reload(11.0)
        self.assertFalse(link.running())
        self.assertTrue(proc.stdin.closed)
        self.assertEqual(link.state(11.0), {"on": False, "state": "off", "name": "Slot17", "subscribed": False,
                                            "frames": 0, "matches": 0,
                                            "key_id": self.config.public(self.config.load())["key_id"]})

    def test_what_the_link_says_reaches_the_push(self):
        proc = FakeLinkProcess([{"state": "connected", "since": 100.0, "subscribed": True, "frames": 40,
                                 "matches": 3, "mtu": 185, "error": None}])
        link = self.link(proc)
        link.poll(110.0)
        link._read(proc)
        state = link.state(110.0)
        self.assertEqual((state["state"], state["subscribed"], state["frames"], state["mtu"], state["since_s"]),
                         ("connected", True, 40, 185, 10.0))
        self.assertNotIn(self.config.load()["key"].hex(), json.dumps(state))

    def test_no_bluetooth_module_means_no_state(self):
        link = pub.BluetoothLink()
        link.config = None              # a display-only install, or a p0 older than 1.4.0
        link.poll(1.0)
        self.assertIsNone(link.state(1.0))


class BluetoothPublisherTests(unittest.TestCase):
    """The link gets new matches at once, with the server's fields, and the state at the server's rhythm."""

    class Recorder:
        def __init__(self, fail=False):
            self.fed, self.last_feed, self.fail = [], -1e9, fail

        def poll(self, now):
            if self.fail:
                raise AssertionError("poll must not be reached when it failed")

        def running(self):
            return True

        def feed(self, command):
            self.fed.append(command)
            return True

        def state(self, now):
            return {"on": True, "state": "connected", "name": "Slot17", "subscribed": True, "frames": 1, "matches": 0}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.s1 = self.dir / "20260911-100000Z_watch"
        self.s1.mkdir()
        (self.s1 / "events.jsonl").write_text("")
        self.hb = self.dir / "hb.json"
        self.sent = []
        self.link = self.Recorder()
        self.pub = pub.Publisher(send=self.sent.append, heartbeat=self.hb, bluetooth=self.link)

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_matches_go_out_at_once_with_the_servers_fields(self):
        heartbeat(self.hb, self.s1)
        self.pub.step(now_mono=50)
        with open(self.s1 / "events.jsonl", "a") as fh:
            fh.write(event(154, "e1", snr=30, mono=50.5, matched_burst={"rule": "weak"}) + "\n")
        self.pub.step(now_mono=51)
        sent = [c["matches"] for c in self.link.fed if "matches" in c]
        server = self.sent[-1]["matches"]
        self.assertEqual(len(sent), 1)
        m = sent[0][0]
        self.assertEqual((m["id"], m["ch"], m["strength_db"], m["rule"]), (server[0]["id"], 154, 30.0, "weak"))
        self.assertEqual(set(m), {"id", "ch", "strength_db", "age_s", "rule"})
        self.assertAlmostEqual(m["age_s"], 0.5)
        self.assertEqual(self.sent[-1]["bluetooth"]["state"], "connected")

    def test_the_state_follows_the_keepalive(self):
        heartbeat(self.hb, self.s1)
        self.pub.step(now_mono=50)
        statuses = [c for c in self.link.fed if "status" in c]
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["status"]["state"], "running")
        self.pub.step(now_mono=50.5)
        self.assertEqual(len([c for c in self.link.fed if "status" in c]), 1)
        self.pub.step(now_mono=52.1)
        self.assertEqual(len([c for c in self.link.fed if "status" in c]), 2)

    def test_a_link_that_cannot_start_never_stops_the_pushes(self):
        config = pub.bluetooth_module()
        if config is None:
            self.skipTest("p0/bluetooth_config.py is not here")
        self.addCleanup(setattr, config, "PATH", config.PATH)
        config.PATH = str(self.dir / "slot17-bluetooth.json")
        config.save(on=True)

        def broken():
            raise OSError("no such file: bluetooth_link.py")
        self.pub.bluetooth = pub.BluetoothLink(config=config, spawn=broken)
        heartbeat(self.hb, self.s1)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertTrue(self.pub.step(now_mono=50))
        self.assertIn("did not start", out.getvalue())
        self.assertEqual(self.sent[-1]["bluetooth"]["state"], "starting")


class BluetoothOpTests(unittest.TestCase):
    """The app's Bluetooth switch, name and pairing request, carried out on the car computer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = pub.bluetooth_module()
        if self.config is None:
            self.skipTest("p0/bluetooth_config.py is not here (a display-only copy)")
        self.addCleanup(setattr, self.config, "PATH", self.config.PATH)
        self.config.PATH = str(Path(self.tmp.name) / "slot17-bluetooth.json")
        self.pub = pub.Publisher(send=lambda p: None, heartbeat=Path(self.tmp.name) / "hb.json",
                                 bluetooth=RecordingLink(self.config, [FakeLinkProcess(), FakeLinkProcess()]))

    def tearDown(self):
        self.tmp.cleanup()

    def run_op(self, **fields):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            pub.run_bluetooth(dict(fields, id="c" * 32), self.pub)
        ack = self.pub.acks.pop()
        return ack["state"], ack["detail"], out.getvalue()

    def test_on_then_named_then_off(self):
        self.assertEqual(self.run_op(on=True)[:2], ("done", "Bluetooth on"))
        self.assertTrue(self.pub.bluetooth.running())
        self.assertEqual(self.run_op(name="Car 2")[:2], ("done", "Bluetooth name: Car 2"))
        self.assertEqual(self.pub.bluetooth.written[-1], {"reload": True})
        self.assertEqual(self.config.load()["name"], "Car 2")
        self.assertEqual(self.run_op(on=False)[:2], ("done", "Bluetooth off"))
        self.assertFalse(self.config.load()["on"])
        self.assertFalse(self.pub.bluetooth.running())

    def test_a_bad_name_is_refused_and_changes_nothing(self):
        self.run_op(on=True)
        state, detail, _ = self.run_op(name="a;b")
        self.assertEqual(state, "failed")
        self.assertIn("not a usable name", detail)
        self.assertEqual(self.config.load()["name"], "Slot17")

    def test_the_key_goes_only_to_a_pairing_request_and_never_into_the_log(self):
        self.assertEqual(self.run_op(pair=True)[:2], ("failed", "Bluetooth is switched off on the car computer"))
        self.run_op(on=True)
        state, detail, log = self.run_op(pair=True)
        key = self.config.load()["key"].hex()
        self.assertEqual((state, detail), ("done", "key:" + key))
        self.assertNotIn(key, log)

    def test_an_unknown_request_and_a_car_computer_without_bluetooth(self):
        self.assertEqual(self.run_op(scan=True)[0], "failed")
        self.pub.bluetooth = None
        self.assertEqual(self.run_op(on=True)[:2], ("failed", "Bluetooth is not available on this car computer"))


class PusherTests(unittest.TestCase):
    """A server that does not answer never holds up reading new matches, nor the Bluetooth link that needs them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.s1 = self.dir / "20260911-100000Z_watch"
        self.s1.mkdir()
        (self.s1 / "events.jsonl").write_text("")
        self.hb = self.dir / "hb.json"
        self.release, self.failing, self.sent = threading.Event(), False, []
        self.link = BluetoothPublisherTests.Recorder()
        self.pub = pub.Publisher(send=self.slow_send, heartbeat=self.hb, bluetooth=self.link)
        self.pub.pusher = pub.Pusher(self.slow_send)

    def tearDown(self):
        self.release.set()
        self.tmp.cleanup()

    def slow_send(self, payload):
        """A server that answers only when the test lets it, as one does whose 4G has no service."""
        self.release.wait(10)
        if self.failing:
            raise OSError("no route to host")
        self.sent.append(payload)

    def test_matches_reach_bluetooth_while_a_push_hangs(self):
        heartbeat(self.hb, self.s1)
        self.assertTrue(self.pub.step(now_mono=50))            # handed over; it hangs
        with open(self.s1 / "events.jsonl", "a") as fh:
            fh.write(event(154, "e1", snr=30, mono=50.8) + "\n")
        started = time.monotonic()
        self.assertFalse(self.pub.step(now_mono=51))           # still one in flight: not sent, but not waited for
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual([m["ch"] for c in self.link.fed if "matches" in c for m in c["matches"]], [154])
        self.release.set()
        self.assertTrue(self.pub.pusher.wait(5))
        self.pub.step(now_mono=51.2)                           # settles the first, hands over the next
        self.assertTrue(self.pub.pusher.wait(5))
        self.pub.step(now_mono=51.4)
        self.assertEqual([m["ch"] for m in self.sent[-1]["matches"]], [154])
        self.assertEqual(list(self.pub.pending), [])           # delivered, so no longer waiting
        self.assertEqual(self.pub.failures, 0)

    def test_a_failed_push_is_counted_when_it_comes_back(self):
        self.failing = True
        self.release.set()
        heartbeat(self.hb, self.s1)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.pub.step(now_mono=50)
            self.assertTrue(self.pub.pusher.wait(5))
            self.pub.step(now_mono=50.1)
        self.assertEqual(self.pub.failures, 1)
        self.assertIn("push failed (1x): OSError", out.getvalue())

    def test_power_off_waits_for_the_push_in_flight_and_reports_first(self):
        from types import SimpleNamespace
        heartbeat(self.hb, self.s1)
        self.pub.step(now_mono=50)                             # one in flight
        threading.Timer(0.3, self.release.set).start()
        ran = pub.run_poweroff({"id": "c" * 32, "age_s": 1.0}, self.pub,
                               runner=lambda cmd: SimpleNamespace(returncode=0, stdout="", stderr=""))
        self.assertFalse(ran)
        self.assertEqual(self.sent[-1]["acks"], [{"id": "c" * 32, "state": "done", "detail": "Powering off"}])

    def test_the_sender_takes_one_push_at_a_time(self):
        sender = pub.Sender("http://example.invalid/api/push", "t" * 43)
        inside, most = [0], [0]

        def exchange(payload):
            inside[0] += 1
            most[0] = max(most[0], inside[0])
            time.sleep(0.05)
            inside[0] -= 1
        sender._send = exchange
        threads = [threading.Thread(target=sender, args=({},)) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(most[0], 1)


if __name__ == "__main__":
    unittest.main()
