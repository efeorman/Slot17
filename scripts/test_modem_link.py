"""The 4G stick check, without a stick: what it asks, what it logs, how it recovers and ranks the route."""
import contextlib
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))
import modem_link  # noqa: E402

RADIO = {"network_type": "LTE", "signalbar": "3", "lte_rsrp": "-107", "lte_rsrq": "-12", "lte_snr": "-1.0",
         "cell_id": "24c315", "lte_pci": "177", "lte_band": "", "roam_setting_option": "on",
         "modem_main_state": "modem_init_complete"}


class Stick:
    """Stands in for the stick's status page."""

    def __init__(self, states, radio=None, fail_status=False):
        self.states = list(states)          # ppp_status per status read; the last one repeats
        self.radio = dict(RADIO, **(radio or {}))
        self.fail_status = fail_status
        self.posts = []

    def __call__(self, gateway, source, method, path, body=None):
        if method == "POST":
            self.posts.append(body)
            if "roam_setting_option=on" in body:
                self.radio["roam_setting_option"] = "on"
            return {"result": "success"}
        if self.fail_status:
            raise OSError("no answer")
        if getattr(self, "up_at", None) is not None and self.clock.now >= self.up_at:
            return dict(self.radio, ppp_status="ppp_connected")
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return dict(self.radio, ppp_status=state)

    def asked(self, goform_id):
        return sum(("goformId=" + goform_id) in body for body in self.posts)


class Clock:
    """time.sleep that costs nothing and moves time.monotonic on."""

    def __init__(self, now=100.0):
        self.now = now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@contextlib.contextmanager
def stick_world(stick, clock=None, trip=(5, 5, 48.0, 90.0)):
    clock = clock or Clock()
    out = io.StringIO()
    with patch.object(modem_link, "request", stick), patch.object(modem_link.time, "monotonic", clock.monotonic), \
            patch.object(modem_link.time, "sleep", clock.sleep), \
            patch.object(modem_link, "round_trip", lambda device, host, count=5: trip), \
            patch.object(modem_link, "ping_host", lambda: "203.0.113.5"), contextlib.redirect_stdout(out):
        yield clock, out


def run_session(stick):
    with stick_world(stick) as (_, out):
        connected = modem_link.ensure_session("192.168.0.1", "192.168.0.178")
    return connected, out.getvalue()


class SessionTests(unittest.TestCase):
    def test_a_session_that_is_up_is_left_alone_and_says_nothing(self):
        stick = Stick(["ppp_connected"])
        self.assertEqual(run_session(stick), (True, ""))
        self.assertEqual(stick.posts, [])

    def test_a_dropped_session_is_asked_back_and_the_log_says_how_long_it_took(self):
        stick = Stick(["ppp_disconnected", "ppp_connecting", "ppp_connected"])
        connected, log = run_session(stick)
        self.assertTrue(connected)
        self.assertEqual(stick.asked("CONNECT_NETWORK"), 1)
        self.assertIn("asked the stick to connect", log)
        self.assertIn("data session up after 4 s (LTE, 3 bars, RSRP -107 dBm", log)

    def test_a_session_that_stays_down_reports_the_reception(self):
        stick = Stick(["ppp_disconnected", "ppp_connecting"],
                      radio={"network_type": "NO_SERVICE", "signalbar": "0", "lte_rsrp": "", "lte_rsrq": "",
                             "lte_snr": "", "cell_id": ""})
        connected, log = run_session(stick)
        self.assertFalse(connected)
        self.assertIn("no data session (ppp_connecting; NO_SERVICE, 0 bars)", log)

    def test_connecting_is_waited_for_and_not_asked_again(self):
        stick = Stick(["ppp_connecting"])
        connected, _ = run_session(stick)
        self.assertFalse(connected)
        self.assertEqual(stick.posts, [], "a second request while it is connecting only restarts the attempt")

    def test_the_other_connected_states_count(self):
        for state in ("ppp_connected", "ipv6_connected", "ipv4_ipv6_connected"):
            self.assertTrue(modem_link.is_connected(state), state)
        for state in ("ppp_disconnected", "ppp_connecting", "ppp_disconnecting", ""):
            self.assertFalse(modem_link.is_connected(state), state)

    def test_describe_tolerates_anything_the_stick_sends(self):
        self.assertEqual(modem_link.describe({}), "")
        for odd in (None, [], "text", {"signalbar": 3}, {"lte_band": "LTE BAND 20"}):
            self.assertIsInstance(modem_link.describe(odd), str)
        self.assertIn("band 20", modem_link.describe({"lte_band": "LTE BAND 20"}))
        self.assertIn("cell 24c315/177", modem_link.describe(RADIO))


class RoamingTests(unittest.TestCase):
    def test_roaming_off_is_switched_on_with_the_stick_s_own_command(self):
        """Lycamobile reports 'Internal' roaming; with roaming off the stick may refuse to dial."""
        stick = Stick(["ppp_connected"], radio={"roam_setting_option": "off"})
        with stick_world(stick) as (_, out):
            self.assertTrue(modem_link.ensure_roaming("g", "s", stick("g", "s", "GET", "/")))
        self.assertEqual(stick.asked("SET_CONNECTION_MODE"), 1)
        self.assertIn("ConnectionMode=auto_dial&roam_setting_option=on", stick.posts[0])
        self.assertIn("switched it on", out.getvalue())

    def test_roaming_already_on_or_not_reported_is_left_alone(self):
        for value in ("on", ""):
            stick = Stick(["ppp_connected"], radio={"roam_setting_option": value})
            with stick_world(stick):
                self.assertFalse(modem_link.ensure_roaming("g", "s", stick("g", "s", "GET", "/")))
            self.assertEqual(stick.posts, [])

    def test_the_owner_can_tell_it_to_leave_roaming_alone(self):
        stick = Stick(["ppp_connected"], radio={"roam_setting_option": "off"})
        with stick_world(stick), patch.object(modem_link, "ROAMING", "leave"):
            self.assertFalse(modem_link.ensure_roaming("g", "s", stick("g", "s", "GET", "/")))
        self.assertEqual(stick.posts, [])


class RecoveryTests(unittest.TestCase):
    def runs(self, stick, minutes, clock=None):
        """Run the 30-second check for this many minutes; return the state and the log."""
        state, clock = {}, clock or Clock()
        with stick_world(stick, clock) as (_, out):
            end = clock.now + minutes * 60
            while clock.now < end:
                modem_link.check("eth1", "192.168.0.1", "192.168.0.178", state, clock.now)
                clock.sleep(30)
        return state, out.getvalue()

    def test_a_stuck_stick_is_redialled_then_restarted_without_hands(self):
        """14 November: seven minutes in 'connecting' until it was replugged by hand."""
        stick = Stick(["ppp_connecting"])
        state, log = self.runs(stick, 8)
        self.assertGreaterEqual(stick.asked("DISCONNECT_NETWORK"), 1)
        self.assertEqual(stick.asked("REBOOT_DEVICE"), 1)
        first_redial = log.index("dropping and redialling")
        self.assertLess(first_redial, log.index("restart itself"))
        self.assertIn("as after a replug", log)

    def test_the_restart_is_not_repeated_within_its_gap(self):
        stick = Stick(["ppp_connecting"])
        self.runs(stick, 14)
        self.assertEqual(stick.asked("REBOOT_DEVICE"), 1)
        stick2 = Stick(["ppp_connecting"])
        self.runs(stick2, 5 + modem_link.RESTART_GAP_S / 60 + 1)
        self.assertEqual(stick2.asked("REBOOT_DEVICE"), 2)

    def test_a_short_drop_is_only_redialled_never_restarted(self):
        clock = Clock()
        stick = Stick(["ppp_connecting"])
        stick.up_at, stick.clock = clock.now + 150, clock      # down for two and a half minutes
        state, log = self.runs(stick, 6, clock)
        self.assertEqual(stick.asked("REBOOT_DEVICE"), 0)
        self.assertIn("back after", log)
        self.assertNotIn("down_since", state)

    def test_while_up_it_reports_signal_and_round_trip_every_five_minutes(self):
        stick = Stick(["ppp_connected"])
        state, log = self.runs(stick, 11)
        lines = [line for line in log.splitlines() if "link up" in line]
        self.assertEqual(len(lines), 3)                      # at 0, 5 and 10 minutes
        self.assertIn("RSRP -107 dBm, RSRQ -12 dB, SNR -1.0 dB, cell 24c315/177", lines[0])
        self.assertIn("round trip to 203.0.113.5 48 ms (worst 90 ms)", lines[0])

    def test_a_cell_change_or_lost_packets_are_reported_at_once(self):
        stick = Stick(["ppp_connected"])
        state, clock = {}, Clock()
        with stick_world(stick, clock) as (_, out):
            modem_link.check("eth1", "g", "s", state, clock.now)
            stick.radio["cell_id"] = "24c316"
            clock.sleep(30)
            modem_link.check("eth1", "g", "s", state, clock.now)
        with stick_world(stick, clock, trip=(5, 3, 70.0, 300.0)) as (_, out2):
            clock.sleep(30)
            modem_link.check("eth1", "g", "s", state, clock.now)
        self.assertIn("cell changed from 24c315/177", out.getvalue())
        self.assertIn("2 of 5 lost", out2.getvalue())

    def test_a_status_page_that_does_not_answer_counts_as_down(self):
        stick = Stick(["ppp_connected"], fail_status=True)
        state, log = self.runs(stick, 1)
        self.assertIn("status page not reachable", log)
        self.assertIn("down_since", state)


class RoundTripTests(unittest.TestCase):
    def test_ping_output_is_read(self):
        out = ("--- 203.0.113.5 ping statistics ---\n5 packets transmitted, 4 received, 20% packet loss, time 1203ms\n"
               "rtt min/avg/max/mdev = 35.646/70.482/137.815/47.620 ms\n")
        with patch.object(modem_link.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": out})()):
            self.assertEqual(modem_link.round_trip("eth1", "203.0.113.5"), (5, 4, 70.482, 137.815))
        lost = "5 packets transmitted, 0 received, 100% packet loss, time 4000ms\n"
        with patch.object(modem_link.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": lost})()):
            self.assertEqual(modem_link.round_trip("eth1", "x"), (5, 0, None, None))
        self.assertEqual(modem_link.describe_trip((5, 0, None, None), "x"), "round trip to x: 5 of 5 lost")
        self.assertIsNone(modem_link.round_trip("eth1", None))

    def test_state_survives_between_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "state.json")
            self.assertEqual(modem_link.load_state(path), {})
            modem_link.save_state({"down_since": 12.5, "cell": "a/b"}, path)
            self.assertEqual(modem_link.load_state(path), {"down_since": 12.5, "cell": "a/b"})


class RouteTests(unittest.TestCase):
    def ranked(self, connected, current):
        calls = []

        def nmcli(*args):
            calls.append(args)
            if args[:1] == ("-t",):
                return "netplan-eth0:eth0\nzte-mf79n:eth1"
            if args[:2] == ("-g", "IP4.GATEWAY"):
                return "192.168.0.1"
            if args[:2] == ("-g", "IP4.ADDRESS"):
                return "192.168.0.178/24"
            if args[:2] == ("-g", "ipv4.route-metric,ipv4.dns-priority"):
                return current
            return ""
        with tempfile.TemporaryDirectory() as tmp, patch.object(modem_link, "STATE_FILE", str(Path(tmp) / "s.json")), \
                patch.object(modem_link, "nmcli", nmcli), \
                patch.object(modem_link, "check", lambda *a: connected), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(modem_link.main(), 0)
        return [c for c in calls if c[:2] == ("connection", "modify")]

    def test_with_a_session_the_stick_ranks_between_wired_and_wifi(self):
        changed = self.ranked(True, "700\n0")
        self.assertEqual(len(changed), 1)
        self.assertIn(str(modem_link.METRIC_UP), changed[0])
        self.assertLess(100, modem_link.METRIC_UP)
        self.assertLess(modem_link.METRIC_UP, 600)

    def test_without_a_session_it_ranks_behind_wifi_so_a_hotspot_takes_over(self):
        changed = self.ranked(False, "200\n0")
        self.assertEqual(len(changed), 1)
        self.assertIn(str(modem_link.METRIC_DOWN), changed[0])
        self.assertGreater(modem_link.METRIC_DOWN, 600)

    def test_a_route_that_is_already_right_is_not_touched(self):
        self.assertEqual(self.ranked(True, "%d\n0" % modem_link.METRIC_UP), [])

    def test_no_stick_means_nothing_happens_and_its_loss_is_logged(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(modem_link, "STATE_FILE", str(Path(tmp) / "s.json")):
            modem_link.save_state({"present": True})
            out = io.StringIO()
            with patch.object(modem_link, "nmcli", lambda *a: "netplan-eth0:eth0"), contextlib.redirect_stdout(out):
                self.assertEqual(modem_link.main(), 0)
            self.assertIn("interface is gone", out.getvalue())


if __name__ == "__main__":
    unittest.main()
