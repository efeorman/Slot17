"""Real-browser tests for the phone page (headless Chrome via Playwright, muted audio).

Run: python -m unittest display/test_display_browser.py   (skipped without Playwright/Chrome)

Chimes are counted by wrapping AudioContext.createOscillator: one chime = two tones.
Most tests feed the page scripted messages through request interception, so the
network conditions reproduced (hung requests, late stale responses) are exact.
"""
import json
from pathlib import Path
import sys
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server as srv  # noqa: E402
import display_publisher as pub

try:
    from playwright.sync_api import sync_playwright
except ImportError:                                   # pragma: no cover
    sync_playwright = None

PAGE = Path(__file__).resolve().parent / "page.html"
TOKEN, KEY = "p" * 43, "v" * 24
TONES = """
window.__tones = 0;
window.__freqs = [];
window.__ctx = null;
const RealAC = window.AudioContext;
window.AudioContext = class extends RealAC {
  constructor(...a) { super(...a); window.__ctx = this; }
  createOscillator() {
    const o = super.createOscillator(); const start = o.start.bind(o);
    o.start = (...a) => { window.__tones++; window.__freqs.push(o.frequency.value); return start(...a); };
    return o;
  }
};
"""
AUDIO_STATE = "document.body.dataset.audio"


def channel(ch, strength, age, cid=None):
    return {"ch": ch, "id": cid or f"s/{ch}", "freq_mhz": round(380.0125 + ch * 0.025, 4),
            "strength_db": strength, "age_s": age, "hits_60s": 1, "activity": "intermittent",
            "approx_time": False}


def match(ch, strength, age, mid):
    return {"id": mid, "ch": ch, "strength_db": strength, "age_s": age, "delay_s": age}


def message(seq, channels, matches, server_age=0.1, state="running", boot="b1"):
    return {"boot": boot, "seq": seq, "server_mono_s": time.monotonic(), "server_age_s": server_age,
            "state": {"v": 3, "source": "test", "snapshot_age_s": 0, "seq": seq, "session": "s", "detector": {"state": state, "windows": 5},
                      "channels": channels, "matches": matches}}


def sse(msg):
    return "retry: 60000\n\n" + "data: " + json.dumps(msg) + "\n\n"


@unittest.skipIf(sync_playwright is None, "Playwright not installed")
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = srv.Store()
        cls.httpd = srv.CappedServer(("127.0.0.1", 0), srv.make_handler(
            {"pi_token": TOKEN, "viewer_key": KEY}, cls.store, PAGE.read_bytes()))
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(channel="chrome", headless=True, args=["--mute-audio"])
        except Exception as exc:                      # pragma: no cover
            cls.pw.stop()
            raise unittest.SkipTest(f"Chrome unavailable: {exc}")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def open(self, routes=None, width=390, cookie=True, start=True, init=""):
        ctx = self.browser.new_context(viewport={"width": width, "height": 800})
        if cookie:
            ctx.add_cookies([{"name": "s17", "value": KEY, "url": self.url}])
        ctx.add_init_script(TONES)
        if init:
            ctx.add_init_script(init)
        page = ctx.new_page()
        def close():
            page.unroute_all(behavior="ignoreErrors")
            ctx.close()
        self.addCleanup(close)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        for pattern, handler in (routes or {}).items():
            page.route(pattern, handler)
        page.goto(self.url + "/")
        if start:
            page.click("#startBtn")
        return page

    def wait(self, page, expr, want, timeout=5.0):
        deadline, got = time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            got = page.evaluate(expr)
            if got == want:
                return
            time.sleep(0.05)
        self.fail(f"{expr!r}: got {got!r}, wanted {want!r}")

    TITLE = "document.getElementById('stTitle').textContent"
    CHANNELS = "[...document.querySelectorAll('.meter .ch')].map(e => e.textContent)"
    CLASSES = "[...document.querySelectorAll('.meter')].map(e => e.className)"
    TONE_COUNT = "window.__tones"

    def push(self, payload):
        if not hasattr(self, "sender"):
            self.sender = pub.Sender(self.url + "/api/push", TOKEN)
            self.addCleanup(self.sender.close)
        self.sender(payload)
        return 204

    # -- end to end through the real server ------------------------------------------------
    def test_push_reaches_the_page_immediately_and_chimes_once(self):
        self.push({"v": 2, "seq": 1, "session": "e2e", "detector": {"state": "running", "windows": 2},
                   "channels": [], "matches": []})
        page = self.open()
        self.wait(page, self.TITLE, "Detector running")
        red = {"v": 2, "seq": 2, "session": "e2e", "detector": {"state": "running", "windows": 3},
               "channels": [channel(154, 31.0, 0.3, "e2e/w9-ch154")],
               "matches": [match(154, 31.0, 0.3, "e2e/w9-ch154")]}
        t0 = time.monotonic()
        self.assertEqual(self.push(red), 204)
        self.wait(page, self.TONE_COUNT, 2, timeout=3)
        latency = time.monotonic() - t0
        self.assertLess(latency, 1.0, f"push to chime took {latency:.2f} s")
        self.assertEqual(page.evaluate(self.CHANNELS)[0], "L154")
        red["seq"] = 3                                               # the same match repeated
        self.push(red)
        time.sleep(0.8)
        self.assertEqual(page.evaluate(self.TONE_COUNT), 2)
        print(f"\n    push -> chime in the browser: {latency * 1000:.0f} ms", file=sys.stderr)

    # -- scripted network conditions -------------------------------------------------------
    def test_link_loss_with_hung_requests_is_shown_and_ages_keep_running(self):
        first = message(1, [channel(190, 30.0, 1.0)], [])
        served = {"n": 0}

        def stream(route):
            served["n"] += 1
            if served["n"] == 1:
                route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=sse(first))
            # later requests are left hanging, like a dead mobile link
        def initial_probe(r):
            if not served.get("probe"):
                served["probe"] = True
                r.fulfill(status=200, content_type="application/json", body=json.dumps(first))
        page = self.open({"**/api/stream": stream, "**/api/state": initial_probe})
        self.wait(page, self.CLASSES + "[0]", "meter red")
        self.wait(page, self.TITLE, "Connection lost", timeout=8)
        self.assertEqual(page.evaluate(self.CLASSES)[0], "meter grey")
        age = page.evaluate("document.querySelector('.meter .meta').textContent")
        self.assertRegex(age, r"(?<!\d)([6-9]|1\d) s ago")           # kept counting locally

    def test_a_late_older_response_cannot_replace_a_newer_one(self):
        newer = message(5, [channel(190, 10.0, 0.5)], [])
        older = message(3, [channel(190, 30.0, 0.5)], [])
        older["server_mono_s"] = newer["server_mono_s"] - 0.01

        def state(route):
            time.sleep(1.0)                                          # the old answer arrives late
            route.fulfill(status=200, headers={"Content-Type": "application/json"}, body=json.dumps(older))
        page = self.open({"**/api/stream": lambda r: r.fulfill(
            status=200, headers={"Content-Type": "text/event-stream"}, body=sse(newer)),
            "**/api/state": state})
        self.wait(page, "document.querySelector('.meter .db').textContent", "10.0 dB")
        time.sleep(1.5)
        self.assertEqual(page.evaluate("document.querySelector('.meter .db').textContent"), "10.0 dB")

    def scripted(self, msg):
        return {"**/api/stream": lambda r: r.fulfill(
                    status=200, headers={"Content-Type": "text/event-stream"}, body=sse(msg)),
                "**/api/state": lambda r: r.fulfill(
                    status=200, headers={"Content-Type": "application/json"}, body=json.dumps(msg))}

    def controlled(self, start=True):
        # Real page/audio API, controllable transport and monotonic time. The
        # mock server clock has a deliberately unrelated epoch (+1000 seconds).
        init = """
        window.__advance = 0;
        const nativeNow = performance.now.bind(performance);
        performance.now = () => nativeNow() + window.__advance * 1000;
        window.__serverNow = () => 1000 + performance.now() / 1000;
        window.EventSource = class { constructor() { window.__stream = this; } close() {} };
        """
        def probe(route):
            m = message(0, [], [])
            m["server_mono_s"] = route.request.frame.evaluate("window.__serverNow()")
            route.fulfill(status=200, content_type="application/json", body=json.dumps(m))
        page = self.open({"**/api/state": probe}, start=start, init=init)
        self.wait(page, self.TITLE, "Detector running")
        return page

    def emit(self, page, m, wire_delay=0):
        m["server_mono_s"] = page.evaluate("window.__serverNow()") - wire_delay
        page.evaluate("m => window.__stream.onmessage({data: JSON.stringify(m)})", m)
        return m

    def test_stopped_stale_and_starting_states_never_chime(self):
        for state in ("stopped", "stale", "starting"):
            with self.subTest(state=state):
                page = self.controlled()
                self.emit(page, message(1, [channel(190, 30, 1)],
                                       [match(190, 30, 1, "s/red")], state=state))
                self.assertEqual(page.evaluate(self.TONE_COUNT), 0)

    def test_pending_start_chime_is_cancelled_by_stop_or_link_loss(self):
        for cause in ("stop", "link", "session", "car", "auth"):
            with self.subTest(cause=cause):
                page = self.controlled(start=False)
                self.emit(page, message(1, [channel(190, 30, 0.5)], [match(190, 30, 0.5, "s/red")]))
                if cause == "stop":
                    self.emit(page, message(2, [], [], state="stopped"))
                elif cause == "link":
                    page.evaluate("window.__advance += 7")
                elif cause == "session":
                    other = message(2, [], [])
                    other["state"]["session"] = "next-drive"
                    self.emit(page, other)
                elif cause == "car":
                    self.emit(page, message(2, [], [], server_age=8.5))
                else:
                    page.route("**/api/state", lambda r: r.fulfill(status=401, body="{}"))
                    page.evaluate("window.__stream.onerror()")
                    self.wait(page, self.TITLE, "Not authorised")
                page.click("#startBtn")
                self.assertEqual(page.evaluate(self.TONE_COUNT), 0)
                # Recovery must not resurrect the discarded pending match.
                if cause not in ("auth", "link"):
                    self.emit(page, message(3, [], []))
                    self.assertEqual(page.evaluate(self.TONE_COUNT), 0)

    def test_start_still_chimes_for_a_fresh_live_red(self):
        page = self.controlled(start=False)
        self.emit(page, message(1, [channel(190, 30, 0.5)], [match(190, 30, 0.5, "s/red")]))
        self.assertEqual(page.evaluate(self.TONE_COUNT), 0)
        page.click("#startBtn")
        self.assertEqual(page.evaluate(self.TONE_COUNT), 2)

    def test_same_sequence_delayed_frame_cannot_freshen_stale_state(self):
        page = self.controlled()
        old = message(1, [channel(190, 30, 0.5)], [], server_age=6)
        old["server_mono_s"] = page.evaluate("window.__serverNow()")
        page.evaluate("window.__advance += 2.5")
        self.emit(page, message(1, [channel(190, 30, 0.5)], [], server_age=8.5))
        self.assertEqual(page.evaluate(self.TITLE), "No data from the car")
        page.evaluate("m => window.__stream.onmessage({data: JSON.stringify(m)})", old)
        self.assertEqual(page.evaluate(self.TITLE), "No data from the car")

    def test_wire_delay_is_counted_for_sound_and_car_freshness(self):
        page = self.controlled()
        # Start with a valid prior clock sample; serialize the next SSE frame
        # now, then hold it in the network for 3 seconds before delivering it.
        page.evaluate("window.__advance += 3.1")
        self.emit(page, message(1, [channel(190, 30, 8)], [match(190, 30, 8, "s/red")]), wire_delay=3)
        self.assertEqual(page.evaluate(self.TITLE), "Detector running")
        self.assertEqual(page.evaluate(self.TONE_COUNT), 0)
        label = page.locator("#stSub").inner_text()
        self.assertRegex(label, r"Delay up to 11\.[1-9] s")
        page.evaluate("window.__advance += 9")
        self.emit(page, message(2, [], []), wire_delay=8.5)
        self.assertEqual(page.evaluate(self.TITLE), "No data from the car")

    def test_superseded_boot_cannot_reset_new_server_state(self):
        page = self.controlled()
        def new_probe(route):
            m = message(0, [], [], boot="b2")
            m["server_mono_s"] = page.evaluate("window.__serverNow()")
            route.fulfill(status=200, content_type="application/json", body=json.dumps(m))
        page.route("**/api/state", new_probe)
        page.evaluate("window.__stream.onerror()")
        page.wait_for_timeout(100)
        self.emit(page, message(1, [], [], boot="b2", state="stopped"))
        self.emit(page, message(99, [channel(190, 30, 0.5)], [match(190, 30, 0.5, "s/red")], boot="b1"))
        self.assertEqual(page.evaluate(self.TITLE), "Detector stopped")
        self.assertEqual(page.evaluate(self.TONE_COUNT), 0)

    def test_a_burst_that_is_old_end_to_end_never_chimes(self):
        # Repro: new to this viewer, 1 s old at the Pi but 14 s at the server.
        # The car link itself is then stale, so nothing is shown and nothing sounds.
        stale = message(1, [channel(190, 30.0, 1.0, "s/a")], [match(190, 30.0, 1.0, "s/a")], server_age=14.0)
        page = self.open(self.scripted(stale))
        self.wait(page, self.TITLE, "No data from the car")
        time.sleep(0.5)
        self.assertEqual(page.evaluate(self.TONE_COUNT), 0)
        # The sound rule on its own: 9 s at the Pi + 3 s at the server = 12 s, link fresh.
        # The old rule (Pi age only, 9 <= 10) would have chimed here.
        late = message(1, [channel(190, 30.0, 9.0, "s/b")], [match(190, 30.0, 9.0, "s/b")], server_age=3.0)
        page = self.open(self.scripted(late))
        self.wait(page, "/1[2-9] s ago/.test(document.querySelector('.meter .meta').textContent)", True)
        time.sleep(0.5)
        self.assertEqual(page.evaluate(self.TONE_COUNT), 0)

    def test_red_among_three_newer_greens_is_shown_and_chimes(self):
        chans = [channel(190, 30.0, 0.9)] + [channel(c, 8.0 + i, 0.8 - 0.1 * i) for i, c in enumerate((10, 11, 12))]
        mats = [match(190, 30.0, 0.9, "s/r")] + [match(c, 8.0 + i, 0.8 - 0.1 * i, f"s/g{c}")
                                                for i, c in enumerate((10, 11, 12))]
        msg = message(1, chans, mats)
        page = self.open({"**/api/stream": lambda r: r.fulfill(
            status=200, headers={"Content-Type": "text/event-stream"}, body=sse(msg)),
            "**/api/state": lambda r: r.fulfill(status=200, headers={"Content-Type": "application/json"},
                                                body=json.dumps(msg))})
        self.wait(page, self.TONE_COUNT, 2)
        self.assertEqual(page.evaluate(self.CHANNELS)[0], "L190")
        self.assertEqual(page.evaluate(self.CLASSES)[0], "meter red")
        self.assertEqual(page.evaluate("document.getElementById('emptyNote').textContent"), "+1 more channel")

    def test_lost_authorisation_clears_the_display(self):
        page = self.open({"**/api/stream": lambda r: r.fulfill(status=401, body="{}"),
                          "**/api/state": lambda r: r.fulfill(status=401, body="{}")})
        self.wait(page, self.TITLE, "Not authorised")
        self.assertEqual(page.evaluate(self.CHANNELS), ["—", "—", "—"])

    def test_a_call_below_red_gets_the_soft_tone_and_the_meter_says_so(self):
        # 20 dB: under the page's 22 dB red, over the 18 dB call level.
        row = dict(channel(170, 20.0, 0.5), call=True, rising=True)
        calling = message(1, [row], [dict(match(170, 20.0, 0.5, "s/call"), call=True, rising=True)])
        page = self.open(self.scripted(calling))
        self.wait(page, self.TONE_COUNT, 2)                   # the two soft notes
        meta = page.evaluate("document.querySelector('#meters .meter .meta, #meters .meter').textContent")
        self.assertIn("rising", meta)
        self.assertIn("in a call", meta)
        # The same strength without the call pattern stays silent.
        plain = message(1, [channel(171, 20.0, 0.5)], [match(171, 20.0, 0.5, "s/plain")])
        page2 = self.open(self.scripted(plain))
        self.wait(page2, self.TITLE, "Detector running")
        time.sleep(0.6)
        self.assertEqual(page2.evaluate(self.TONE_COUNT), 0)

    def test_quiet_receiver_shows_a_banner_and_sounds_a_warning(self):
        # Below the threshold nothing is said; above it the banner names the fault.
        ok = message(1, [], [], state="running")
        ok["state"]["detector"]["quiet_windows"] = 39
        page = self.open(self.scripted(ok))
        self.wait(page, self.TITLE, "Detector running")
        self.assertTrue(page.evaluate("document.getElementById('antennaBanner').hidden"))
        self.assertEqual(page.evaluate(self.TONE_COUNT), 0)

        bad = message(1, [], [], state="running")
        bad["state"]["detector"].update(quiet_windows=40, input_peak_counts=781.0)
        page2 = self.open(self.scripted(bad))
        self.wait(page2, "document.getElementById('antennaBanner').hidden", False)
        text = page2.evaluate("document.getElementById('antennaBanner').textContent")
        self.assertIn("HEARS NOTHING", text)
        self.assertIn("1 min", text)
        self.assertIn("781", text)
        self.wait(page2, self.TONE_COUNT, 2)          # the low warning pair, not the red chime
        time.sleep(0.6)
        self.assertEqual(page2.evaluate(self.TONE_COUNT), 2)   # not repeated every tick

    def test_quiet_receiver_limit_follows_the_window_length_when_no_limit_is_sent(self):
        # 0.5 s windows and no stated limit: a minute is 120 windows, not the 40 of 1.5 s windows.
        early = message(1, [], [], state="running")
        early["state"]["detector"].update(quiet_windows=119, window_s=0.5)
        page = self.open(self.scripted(early))
        self.wait(page, self.TITLE, "Detector running")
        self.assertTrue(page.evaluate("document.getElementById('antennaBanner').hidden"))
        self.assertEqual(page.evaluate(self.TONE_COUNT), 0)

        due = message(1, [], [], state="running")
        due["state"]["detector"].update(quiet_windows=120, window_s=0.5)
        page2 = self.open(self.scripted(due))
        self.wait(page2, "document.getElementById('antennaBanner').hidden", False)
        self.assertIn("1 min", page2.evaluate("document.getElementById('antennaBanner').textContent"))

        # The car's own figure always wins.
        stated = message(1, [], [], state="running")
        stated["state"]["detector"].update(quiet_windows=30, window_s=0.5, quiet_alarm_windows=30)
        page3 = self.open(self.scripted(stated))
        self.wait(page3, "document.getElementById('antennaBanner').hidden", False)

    def test_repeat_needed_setting_gates_the_chime(self):
        # One red match on a channel seen once in the last minute.
        once = message(1, [channel(190, 30.0, 0.5)], [match(190, 30.0, 0.5, "s/one")])
        once["state"]["channels"][0]["hits_60s"] = 1
        # Default: a single burst still chimes (a passing vehicle may transmit once).
        page = self.open(self.scripted(once))
        self.wait(page, self.TONE_COUNT, 2)

        # With the control on, the same single burst is silent...
        page2 = self.open(self.scripted(once), init="localStorage.setItem('s17cfg', JSON.stringify({repeat: true}));")
        self.wait(page2, self.CLASSES + "[0]", "meter red")
        time.sleep(0.8)
        self.assertEqual(page2.evaluate(self.TONE_COUNT), 0)

        # ... while a channel that matched twice in the window still chimes.
        twice = message(1, [channel(190, 30.0, 0.5)], [match(190, 30.0, 0.5, "s/two")])
        twice["state"]["channels"][0]["hits_60s"] = 2
        page3 = self.open(self.scripted(twice), init="localStorage.setItem('s17cfg', JSON.stringify({repeat: true}));")
        self.wait(page3, self.TONE_COUNT, 2)
        self.assertEqual(page3.evaluate("document.getElementById('repeatBtn').textContent"), "Repeat needed: on")

    def test_the_antenna_warning_can_be_switched_off_for_bench_testing(self):
        """The Pi is started at home with no antenna on purpose, to test."""
        bad = message(1, [], [], state="running")
        bad["state"]["detector"].update(quiet_windows=40, input_peak_counts=781.0)
        page = self.open(self.scripted(bad),
                         init="localStorage.setItem('s17cfg', JSON.stringify({antennaWarn: false}));")
        self.wait(page, self.TITLE, "Detector running")
        time.sleep(0.8)
        # No banner, and none of the warning tone pair.
        self.assertTrue(page.evaluate("document.getElementById('antennaBanner').hidden"))
        self.assertEqual(page.evaluate(self.TONE_COUNT), 0)
        page.click("#gearBtn")
        self.assertEqual(page.evaluate("document.getElementById('antennaBtn').textContent"),
                         "Antenna warning: off")
        # Turning it back on must restore the banner without a reload: the
        # operator arriving at a test site should not have to know that.
        page.click("#antennaBtn")
        self.wait(page, "document.getElementById('antennaBanner').hidden", False)

    def test_the_page_says_why_a_match_stayed_silent(self):
        """"Works on the laptop, not on the phone" must never need a code read.

        A red match that is deliberately gated must report the gate, not leave
        the operator guessing whether the phone's audio is broken.
        """
        self.push({"v": 2, "seq": 1, "session": "why", "detector": {"state": "running", "windows": 2},
                   "channels": [], "matches": []})
        page = self.open(init="localStorage.setItem('s17cfg', JSON.stringify({repeat: true}));")
        self.wait(page, self.TITLE, "Detector running")
        # A red match arriving after Start, on a channel seen once in the window.
        row = channel(190, 30.0, 0.3, "why/one")
        row["hits_60s"] = 1
        self.push({"v": 2, "seq": 2, "session": "why", "detector": {"state": "running", "windows": 3},
                   "channels": [row], "matches": [match(190, 30.0, 0.3, "why/one")]})
        self.wait(page, self.CLASSES + "[0]", "meter red")
        page.click("#gearBtn")
        self.wait(page,
                  "document.getElementById('soundHint').textContent.includes('repeat needed')", True)
        hint = page.evaluate("document.getElementById('soundHint').textContent")
        # The audio itself is fine, and the page says so in the same breath - the
        # operator must be able to tell a deliberate gate from broken sound.
        self.assertIn("Sound ready", hint)
        self.assertEqual(page.evaluate(self.TONE_COUNT), 0)

    def test_sound_switched_off_is_reported_as_the_reason_not_as_a_fault(self):
        page = self.open(self.scripted(message(1, [], [], state="running")),
                         init="localStorage.setItem('s17cfg', JSON.stringify({sound: false}));")
        self.wait(page, self.TITLE, "Detector running")
        page.click("#gearBtn")
        self.assertIn("Sound is switched off",
                      page.evaluate("document.getElementById('soundHint').textContent"))

    def test_audio_survives_the_phone_suspending_it(self):
        """iOS suspends the context on backgrounding; resume needs a gesture.

        visibilitychange is not a gesture, so the old code's resume() there
        failed silently and the chime stayed dead for the rest of the drive.
        """
        page = self.open(self.scripted(message(1, [], [], state="running")))
        self.wait(page, self.TITLE, "Detector running")
        self.wait(page, AUDIO_STATE, "running")
        page.evaluate("window.__ctx.suspend()")
        self.wait(page, AUDIO_STATE, "suspended")
        page.click("#gearBtn")
        self.assertIn("suspended by the phone",
                      page.evaluate("document.getElementById('soundHint').textContent"))
        # Any tap re-arms it. This is the whole repair.
        page.click("#closeSheet")
        self.wait(page, AUDIO_STATE, "running")

    def test_a_silent_audio_element_holds_the_ios_session_open(self):
        """Web Audio alone is muted by an iPhone's ring/silent switch."""
        page = self.open(self.scripted(message(1, [], [], state="running")))
        self.wait(page, self.TITLE, "Detector running")
        # Built as a blob and actually playing - which is why the server's CSP
        # must allow media-src blob:, or this is blocked with no visible error.
        self.assertTrue(page.evaluate(
            "!!document.getElementById('silentAudio') && "
            "document.getElementById('silentAudio').src.startsWith('blob:')"))
        self.wait(page, "!document.getElementById('silentAudio').paused", True)
        self.assertTrue(page.evaluate("document.getElementById('silentAudio').loop"))

    def test_the_page_csp_permits_the_audio_element_it_depends_on(self):
        """A CSP that blocks media makes the iOS repair silently useless."""
        page = self.open(self.scripted(message(1, [], [], state="running")), start=False)
        violations = []
        page.on("console", lambda m: violations.append(m.text)
                if "Content Security Policy" in m.text else None)
        page.click("#startBtn")
        self.wait(page, "!document.getElementById('silentAudio').paused", True)
        self.assertEqual([v for v in violations if "media" in v.lower()], [])

    def test_stopping_the_detector_from_the_phone_needs_two_taps(self):
        page = self.open(self.scripted(message(1, [], [], state="running")))
        self.wait(page, self.TITLE, "Detector running")
        page.click("#gearBtn")
        page.click("#stopBtn")
        # One tap only arms it; nothing has been sent.
        self.assertEqual(page.evaluate("document.getElementById('stopBtn').textContent"),
                         "Tap again to confirm")
        self.assertIsNone(self.store.command_status())
        page.click("#stopBtn")
        self.wait(page, "document.getElementById('stopHint').textContent.includes('Stop queued')", True)
        status = self.store.command_status()
        self.assertEqual(status["action"], "stop")
        self.assertEqual(status["state"], "pending")
        # The Pi collects it exactly once, on the challenge it already makes.
        self.assertEqual(self.store.take_command()["action"], "stop")
        self.assertIsNone(self.store.take_command())
        self.assertEqual(self.store.command_status()["state"], "sent")

    # -- urgent beep: one per recognised burst at 50 dB or more, no timer ----------------
    URGENT_TONE = 2000

    def sounds(self, page):
        freqs = page.evaluate("window.__freqs")
        return {"urgent": freqs.count(self.URGENT_TONE) // 3,
                "chime": sum(f != self.URGENT_TONE for f in freqs) // 2}

    def feed(self, session, seq, ch, strength, mid, age=0.3):
        self.push({"v": 2, "seq": seq, "session": session, "detector": {"state": "running", "windows": seq},
                   "channels": [channel(ch, strength, age, mid)], "matches": [match(ch, strength, age, mid)]})

    def urgent_page(self, session, init=""):
        self.push({"v": 2, "seq": 1, "session": session, "detector": {"state": "running", "windows": 1},
                   "channels": [], "matches": []})
        page = self.open(init=init)
        self.wait(page, self.TITLE, "Detector running")
        return page

    def test_a_very_strong_match_plays_the_urgent_beep_and_not_the_chime(self):
        page = self.urgent_page("u1")
        self.feed("u1", 2, 178, 50.0, "u1/a")
        self.wait(page, self.TONE_COUNT, 3)
        time.sleep(0.5)
        self.assertEqual(self.sounds(page), {"urgent": 1, "chime": 0})

    def test_just_below_the_urgent_level_is_an_ordinary_chime(self):
        page = self.urgent_page("u2")
        self.feed("u2", 2, 178, 49.9, "u2/a")
        self.wait(page, self.TONE_COUNT, 2)
        time.sleep(0.5)
        self.assertEqual(self.sounds(page), {"urgent": 0, "chime": 1})

    def test_urgent_beeps_skip_the_chime_limits_but_keep_a_one_second_gap(self):
        page = self.urgent_page("u3")
        self.feed("u3", 2, 178, 60.0, "u3/a")
        self.wait(page, self.TONE_COUNT, 3)
        self.feed("u3", 3, 178, 60.0, "u3/b")              # same channel, inside 1 s: dropped
        time.sleep(1.2)
        self.assertEqual(self.sounds(page), {"urgent": 1, "chime": 0})
        self.feed("u3", 4, 178, 60.0, "u3/c")              # same channel, inside 30 s and 6 s: plays
        self.wait(page, self.TONE_COUNT, 6)
        time.sleep(1.5)                                    # the dropped one never plays late
        self.assertEqual(self.sounds(page), {"urgent": 2, "chime": 0})

    def test_an_ordinary_chime_in_the_same_push_cannot_block_the_urgent_beep(self):
        page = self.urgent_page("u4")
        self.push({"v": 2, "seq": 2, "session": "u4", "detector": {"state": "running", "windows": 2},
                   "channels": [channel(10, 30.0, 0.3, "u4/o"), channel(178, 60.0, 0.3, "u4/u")],
                   "matches": [match(10, 30.0, 0.3, "u4/o"), match(178, 60.0, 0.3, "u4/u")]})
        self.wait(page, self.TONE_COUNT, 3)
        time.sleep(0.8)
        self.assertEqual(self.sounds(page), {"urgent": 1, "chime": 0})

    def test_a_duplicate_or_reconnect_does_not_replay_the_urgent_beep(self):
        page = self.urgent_page("u5")
        self.feed("u5", 2, 178, 60.0, "u5/a")
        self.wait(page, self.TONE_COUNT, 3)
        time.sleep(1.2)
        self.feed("u5", 3, 178, 60.0, "u5/a")
        time.sleep(0.5)
        self.assertEqual(page.evaluate(self.TONE_COUNT), 3)
        page.reload()
        page.click("#startBtn")
        self.wait(page, self.TITLE, "Detector running")
        self.feed("u5", 4, 178, 60.0, "u5/a")
        time.sleep(1.0)
        self.assertEqual(page.evaluate(self.TONE_COUNT), 0)

    def test_an_old_urgent_match_stays_silent_and_says_why(self):
        page = self.urgent_page("u6")
        self.feed("u6", 2, 178, 60.0, "u6/a", age=11.0)
        time.sleep(0.8)
        self.assertEqual(page.evaluate(self.TONE_COUNT), 0)
        page.click("#gearBtn")
        self.assertIn("s old", page.evaluate("document.getElementById('soundHint').textContent"))

    def test_mute_and_sound_off_also_silence_the_urgent_beep(self):
        page = self.urgent_page("u7")
        page.click("#muteBtn")
        self.feed("u7", 2, 178, 60.0, "u7/a")
        time.sleep(0.8)
        self.assertEqual(page.evaluate(self.TONE_COUNT), 0)
        off = self.urgent_page("u8", init="localStorage.setItem('s17cfg', JSON.stringify({sound: false}));")
        self.feed("u8", 2, 178, 60.0, "u8/a")
        time.sleep(0.8)
        self.assertEqual(off.evaluate(self.TONE_COUNT), 0)

    def test_a_stopped_detector_never_plays_the_urgent_beep(self):
        page = self.urgent_page("u9")
        self.push({"v": 2, "seq": 2, "session": "u9", "detector": {"state": "stopped"},
                   "channels": [channel(178, 60.0, 0.3, "u9/a")], "matches": [match(178, 60.0, 0.3, "u9/a")]})
        time.sleep(0.8)
        self.assertEqual(page.evaluate(self.TONE_COUNT), 0)

    def test_an_urgent_match_before_start_is_not_replaced_by_a_later_chime(self):
        self.push({"v": 2, "seq": 1, "session": "u10", "detector": {"state": "running", "windows": 1},
                   "channels": [], "matches": []})
        page = self.open(start=False)
        self.wait(page, self.TITLE, "Detector running")
        self.push({"v": 2, "seq": 2, "session": "u10", "detector": {"state": "running", "windows": 2},
                   "channels": [channel(178, 60.0, 0.3, "u10/u"), channel(10, 30.0, 0.3, "u10/o")],
                   "matches": [match(178, 60.0, 0.3, "u10/u"), match(10, 30.0, 0.3, "u10/o")]})
        time.sleep(0.4)
        page.click("#startBtn")
        self.wait(page, self.TONE_COUNT, 3)
        self.assertEqual(self.sounds(page), {"urgent": 1, "chime": 0})

    def test_the_urgent_beep_can_be_switched_off_and_tested(self):
        page = self.urgent_page("u11", init="localStorage.setItem('s17cfg', JSON.stringify({urgent: false}));")
        self.feed("u11", 2, 178, 60.0, "u11/a")
        self.wait(page, self.TONE_COUNT, 2)
        self.assertEqual(self.sounds(page), {"urgent": 0, "chime": 1})
        page.click("#gearBtn")
        self.assertEqual(page.evaluate("document.getElementById('urgentBtn').textContent"), "Urgent beep: off")
        page.click("#testUrgentBtn")
        self.wait(page, self.TONE_COUNT, 5)
        self.assertEqual(self.sounds(page), {"urgent": 1, "chime": 1})
        page.click("#urgentBtn")
        self.assertIn("not police", page.evaluate("document.getElementById('urgentHint').textContent"))

    def test_after_a_silent_link_a_late_urgent_is_dropped_and_a_fresh_one_plays(self):
        page = self.urgent_page("u12")
        self.feed("u12", 2, 178, 60.0, "u12/a")
        self.wait(page, self.TONE_COUNT, 3)
        time.sleep(7.5)                                    # nothing arrives: the link counts as lost
        self.wait(page, self.TITLE + " !== 'Detector running'", True)
        self.feed("u12", 3, 178, 60.0, "u12/late", age=12.0)   # backlog delivered after the gap
        time.sleep(1.0)
        self.assertEqual(self.sounds(page), {"urgent": 1, "chime": 0})
        self.feed("u12", 4, 178, 60.0, "u12/fresh")
        self.wait(page, self.TONE_COUNT, 6)
        time.sleep(1.0)
        self.assertEqual(self.sounds(page), {"urgent": 2, "chime": 0})

    def test_the_old_quiet_saved_volume_is_raised_once_and_a_later_choice_stays(self):
        page = self.open(self.scripted(message(1, [], [])), start=False,
                         init="if (!sessionStorage.getItem('t')) { sessionStorage.setItem('t', '1'); "
                              "localStorage.setItem('s17cfg', JSON.stringify({vol: 30})); }")
        self.assertEqual(page.evaluate("JSON.parse(localStorage.getItem('s17cfg')).vol"), 100)
        page.evaluate("localStorage.setItem('s17cfg', JSON.stringify(Object.assign("
                      "JSON.parse(localStorage.getItem('s17cfg')), {vol: 50})))")
        page.reload()
        self.assertEqual(page.evaluate("JSON.parse(localStorage.getItem('s17cfg')).vol"), 50)

    def test_narrow_phone_layout_fits(self):
        msg = message(1, [channel(190, 30.0, 1.0), channel(60, 14.0, 5.0), channel(119, None, 30.0)],
                      [match(190, 30.0, 1.0, "s/layout")])
        page = self.open({"**/api/stream": lambda r: r.fulfill(
            status=200, headers={"Content-Type": "text/event-stream"}, body=sse(msg)),
            "**/api/state": lambda r: r.fulfill(status=200, headers={"Content-Type": "application/json"},
                                                body=json.dumps(msg))}, width=320)
        self.wait(page, "document.querySelectorAll('.meter.empty').length", 0)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 320)
        self.assertTrue(page.evaluate("document.getElementById('gearBtn').getBoundingClientRect().right <= 320"))
        self.assertTrue(page.locator("#stSub").inner_text().startswith("Delay up to "))
        self.assertTrue(page.evaluate("document.getElementById('stSub').scrollWidth <= document.getElementById('stSub').clientWidth"))


if __name__ == "__main__":
    unittest.main()
