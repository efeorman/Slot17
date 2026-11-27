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

    def open(self, routes=None, width=390, cookie=True, start=True, init="", height=800):
        ctx = self.browser.new_context(viewport={"width": width, "height": height})
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
        # No server sound levels unless a test gives them: the other tests check the page's own rules at its own
        # 35 dB, the level app-config.json ships with (22 until 26 November 2026). Routes added later win over this one.
        page.route("**/api/config", lambda r: r.fulfill(status=404, body='{"error":"no settings"}'))
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
               "channels": [channel(154, 41.0, 0.3, "e2e/w9-ch154")],
               "matches": [match(154, 41.0, 0.3, "e2e/w9-ch154")]}
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
        first = message(1, [channel(190, 40.0, 1.0)], [])
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
        older = message(3, [channel(190, 40.0, 0.5)], [])
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
                self.emit(page, message(1, [channel(190, 40, 1)],
                                       [match(190, 40, 1, "s/red")], state=state))
                self.assertEqual(page.evaluate(self.TONE_COUNT), 0)

    def test_pending_start_chime_is_cancelled_by_stop_or_link_loss(self):
        for cause in ("stop", "link", "session", "car", "auth"):
            with self.subTest(cause=cause):
                page = self.controlled(start=False)
                self.emit(page, message(1, [channel(190, 40, 0.5)], [match(190, 40, 0.5, "s/red")]))
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
        self.emit(page, message(1, [channel(190, 40, 0.5)], [match(190, 40, 0.5, "s/red")]))
        self.assertEqual(page.evaluate(self.TONE_COUNT), 0)
        page.click("#startBtn")
        self.assertEqual(page.evaluate(self.TONE_COUNT), 2)

    def test_same_sequence_delayed_frame_cannot_freshen_stale_state(self):
        page = self.controlled()
        old = message(1, [channel(190, 40, 0.5)], [], server_age=6)
        old["server_mono_s"] = page.evaluate("window.__serverNow()")
        page.evaluate("window.__advance += 2.5")
        self.emit(page, message(1, [channel(190, 40, 0.5)], [], server_age=8.5))
        self.assertEqual(page.evaluate(self.TITLE), "No data from the car")
        page.evaluate("m => window.__stream.onmessage({data: JSON.stringify(m)})", old)
        self.assertEqual(page.evaluate(self.TITLE), "No data from the car")

    def test_wire_delay_is_counted_for_sound_and_car_freshness(self):
        page = self.controlled()
        # Start with a valid prior clock sample; serialize the next SSE frame
        # now, then hold it in the network for 3 seconds before delivering it.
        page.evaluate("window.__advance += 3.1")
        self.emit(page, message(1, [channel(190, 40, 8)], [match(190, 40, 8, "s/red")]), wire_delay=3)
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
        self.emit(page, message(99, [channel(190, 40, 0.5)], [match(190, 40, 0.5, "s/red")], boot="b1"))
        self.assertEqual(page.evaluate(self.TITLE), "Detector stopped")
        self.assertEqual(page.evaluate(self.TONE_COUNT), 0)

    def test_a_burst_that_is_old_end_to_end_never_chimes(self):
        # Repro: new to this viewer, 1 s old at the Pi but 14 s at the server.
        # The car link itself is then stale, so nothing is shown and nothing sounds.
        stale = message(1, [channel(190, 40.0, 1.0, "s/a")], [match(190, 40.0, 1.0, "s/a")], server_age=14.0)
        page = self.open(self.scripted(stale))
        self.wait(page, self.TITLE, "No data from the car")
        time.sleep(0.5)
        self.assertEqual(page.evaluate(self.TONE_COUNT), 0)
        # The sound rule on its own: 9 s at the Pi + 3 s at the server = 12 s, link fresh.
        # The old rule (Pi age only, 9 <= 10) would have chimed here.
        late = message(1, [channel(190, 40.0, 9.0, "s/b")], [match(190, 40.0, 9.0, "s/b")], server_age=3.0)
        page = self.open(self.scripted(late))
        self.wait(page, "/1[2-9] s ago/.test(document.querySelector('.meter .meta').textContent)", True)
        time.sleep(0.5)
        self.assertEqual(page.evaluate(self.TONE_COUNT), 0)

    def test_red_among_three_newer_greens_is_shown_and_chimes(self):
        chans = [channel(190, 40.0, 0.9)] + [channel(c, 8.0 + i, 0.8 - 0.1 * i) for i, c in enumerate((10, 11, 12))]
        mats = [match(190, 40.0, 0.9, "s/r")] + [match(c, 8.0 + i, 0.8 - 0.1 * i, f"s/g{c}")
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
        # 20 dB: under the page's 35 dB red, over the 18 dB call level.
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
        once = message(1, [channel(190, 40.0, 0.5)], [match(190, 40.0, 0.5, "s/one")])
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
        twice = message(1, [channel(190, 40.0, 0.5)], [match(190, 40.0, 0.5, "s/two")])
        twice["state"]["channels"][0]["hits_60s"] = 2
        page3 = self.open(self.scripted(twice), init="localStorage.setItem('s17cfg', JSON.stringify({repeat: true}));")
        self.wait(page3, self.TONE_COUNT, 2)
        self.assertEqual(page3.evaluate("document.getElementById('repeatBtn').getAttribute('aria-checked')"), "true")

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
        self.assertEqual(page.evaluate("document.getElementById('antennaBtn').getAttribute('aria-checked')"), "false")
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
        row = channel(190, 40.0, 0.3, "why/one")
        row["hits_60s"] = 1
        self.push({"v": 2, "seq": 2, "session": "why", "detector": {"state": "running", "windows": 3},
                   "channels": [row], "matches": [match(190, 40.0, 0.3, "why/one")]})
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
        page.click("#carBtn")                       # stop lives under Car computer since the cockpit (1.7)
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
                   "channels": [channel(10, 40.0, 0.3, "u4/o"), channel(178, 60.0, 0.3, "u4/u")],
                   "matches": [match(10, 40.0, 0.3, "u4/o"), match(178, 60.0, 0.3, "u4/u")]})
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

    # -- aircraft channels (1.8.0): 190-199, a helicopter in the air -----------------------------------------------------
    def test_an_aircraft_channel_shows_the_helicopter_mark_and_a_ground_one_does_not(self):
        self.push({"v": 2, "seq": 1, "session": "air1", "detector": {"state": "running", "windows": 1},
                   "channels": [channel(190, 40.0, 0.3, "air1/a"), channel(189, 30.0, 0.5, "air1/g")], "matches": []})
        page = self.open()
        self.wait(page, self.TITLE, "Detector running")
        self.wait(page, "document.querySelectorAll('.meter[data-air]').length", 1)
        first = page.evaluate("(() => { const m = document.querySelectorAll('.meter')[0]; return [m.querySelector('.ch').textContent,"
                              " !!m.querySelector('.ch .air-glyph'), m.querySelector('.chip.air')?.textContent, m.hasAttribute('data-air')]; })()")
        self.assertEqual(first, ["L190", True, "Aircraft", True])
        second = page.evaluate("(() => { const m = document.querySelectorAll('.meter')[1]; return [m.querySelector('.ch').textContent,"
                               " !!m.querySelector('.air-glyph'), m.hasAttribute('data-air')]; })()")
        self.assertEqual(second, ["L189", False, False])
        self.assertIn("aircraft", page.evaluate("document.querySelectorAll('.meter')[0].querySelector('.ch').getAttribute('aria-label')"))

    def test_an_aircraft_chimes_by_default_and_says_so(self):
        page = self.urgent_page("air2")
        self.feed("air2", 2, 195, 40.0, "air2/a")
        self.wait(page, self.TONE_COUNT, 2)
        self.wait(page, "document.getElementById('alertTitle').textContent.includes('L195 · aircraft')", True)

    def test_sound_for_aircraft_off_silences_only_the_aircraft_channels(self):
        page = self.urgent_page("air3", init="localStorage.setItem('s17cfg', JSON.stringify({aircraft: false}));")
        self.feed("air3", 2, 198, 60.0, "air3/a")
        time.sleep(0.8)
        self.assertEqual(page.evaluate(self.TONE_COUNT), 0)
        self.assertEqual(page.evaluate("document.getElementById('airBtn').getAttribute('aria-checked')"), "false")
        self.feed("air3", 3, 178, 40.0, "air3/g")                          # a ground channel still chimes
        self.wait(page, self.TONE_COUNT, 2)

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
                   "channels": [channel(178, 60.0, 0.3, "u10/u"), channel(10, 40.0, 0.3, "u10/o")],
                   "matches": [match(178, 60.0, 0.3, "u10/u"), match(10, 40.0, 0.3, "u10/o")]})
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
        self.assertEqual(page.evaluate("document.getElementById('urgentBtn').getAttribute('aria-checked')"), "false")
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

    # -- the cockpit and drive mode (1.7) ------------------------------------------------------
    def strong(self, seq=1):
        return message(seq, [dict(channel(190, 40.0, 0.5), rising=True), channel(60, 14.0, 5.0)],
                       [match(190, 40.0, 0.5, f"d/strong{seq}")])

    def test_drive_mode_shows_the_strongest_channel_and_is_remembered(self):
        page = self.open(self.scripted(self.strong()))
        self.wait(page, self.TITLE, "Detector running")
        page.click("#driveBtn")
        self.wait(page, "document.getElementById('drive').hidden", False)
        self.wait(page, "document.getElementById('dDb').textContent", "40.0")
        self.assertIn("alerting", page.evaluate("document.getElementById('drive').className"))
        self.assertIn("L190", page.evaluate("document.getElementById('dCh').textContent"))
        self.assertIn("L60", page.evaluate("document.getElementById('dOthers').textContent"))
        page.reload()
        page.click("#startBtn")
        self.wait(page, "document.getElementById('drive').hidden", False)
        page.click("#dExit")
        self.wait(page, "document.getElementById('drive').hidden", True)
        page.reload()
        self.assertTrue(page.evaluate("document.getElementById('drive').hidden"))

    def test_a_drive_mode_sighting_goes_to_the_car_computer_like_the_apps(self):
        page = self.open(self.scripted(message(1, [], [], state="running")))
        self.wait(page, self.TITLE, "Detector running")
        before = set(self.store.ops)
        page.click("#driveBtn")
        page.click("#dSaw button[data-label='police']")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not set(self.store.ops) - before:
            time.sleep(0.05)
        new = [self.store.ops[k] for k in set(self.store.ops) - before]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["kind"], "mark")
        self.assertEqual(new[0]["payload"]["label"], "police")
        self.assertIn("Police saved", page.evaluate("document.getElementById('dNote').textContent"))

    def test_sections_switch_and_the_last_one_is_remembered(self):
        page = self.open(self.scripted(message(1, [], [], state="running")))
        self.wait(page, self.TITLE, "Detector running")
        page.click("#carBtn")
        self.assertFalse(page.evaluate("document.getElementById('v-car').hidden"))
        self.assertTrue(page.evaluate("document.getElementById('v-live').hidden"))
        self.assertEqual(page.evaluate("location.hash"), "#car")
        page.goto(self.url + "/")
        self.assertFalse(page.evaluate("document.getElementById('v-car').hidden"))

    def test_the_car_computer_section_shows_its_health_and_where_to_start(self):
        state = self.strong()
        state["state"].update(pi={"session_s": 1200, "temp_c": 52.0, "throttled": 0x50000, "dropped_samples": 0},
                              features=["mark", "doctor"],
                              doctor={"items": [["Receiver", "ok", "Airspy Mini found"],
                                                ["Power supply", "PROBLEM", "Undervoltage seen"]], "age_s": 30})
        state["state"]["detector"].update(hits=514, unchecked=1, partial=0)
        page = self.open(self.scripted(state))
        self.wait(page, self.TITLE, "Detector running")
        page.click("#carBtn")
        self.wait(page, "document.getElementById('carNotice').hidden", False)
        self.assertIn("Power supply", page.evaluate("document.getElementById('carNoticeTitle').textContent"))
        health = page.evaluate("document.getElementById('healthKv').textContent")
        self.assertIn("Undervoltage seen", health)
        self.assertIn("about 20 min", health)
        self.assertTrue(page.evaluate("document.getElementById('offBtn').disabled"))     # not in its features

    def screen_routes(self, msg, saved, bars=1):
        def screen(route):
            if route.request.method == "POST":
                saved.append(json.loads(route.request.post_data))
                return route.fulfill(status=204, body="")
            route.fulfill(status=200, headers={"Content-Type": "application/json"}, body=json.dumps({"screen": True, "bars": bars}))
        return dict(self.scripted(msg), **{"**/api/screen": screen})

    # The operator, 26 November 2026: "always start by default in drive mode instead of cockpit, even when they shut
    # down the pi on cockpit mode".
    def test_the_car_computers_own_screen_starts_without_a_tap_in_drive_mode_whatever_it_was_left_on(self):
        saved = []
        page = self.open(self.screen_routes(self.strong(), saved), start=False, width=800, height=480,
                         init="if (!sessionStorage.getItem('seen')) { sessionStorage.setItem('seen', '1');"
                              " localStorage.setItem('s17view', JSON.stringify({section: 'live', mode: 'cockpit'})); }")
        self.wait(page, "document.getElementById('startOverlay').hidden", True)
        self.wait(page, "document.getElementById('drive').hidden", False)
        self.assertTrue(page.evaluate("document.body.classList.contains('kiosk')"))
        self.wait(page, "document.body.dataset.audio !== 'idle'", True)   # started without a tap (the kiosk browser may play sound)
        page.click("#dExit")
        self.wait(page, "document.getElementById('drive').hidden", True)
        page.click("#driveBtn")
        page.click("#dExit")
        page.reload()                                                       # the next start, left on the cockpit
        self.assertFalse(page.evaluate("document.getElementById('drive').hidden"))   # at once: no cockpit first
        self.wait(page, "document.body.classList.contains('kiosk')", True)
        self.assertFalse(page.evaluate("document.getElementById('drive').hidden"))
        time.sleep(0.3)
        self.assertEqual(saved, [])                                         # drive or cockpit is not kept any more

    def three(self, seq=1):
        return message(seq, [dict(channel(190, 40.0, 0.5), rising=True), channel(60, 14.0, 5.0), channel(119, None, 31.0)],
                       [match(190, 40.0, 0.5, f"d/strong{seq}")])

    def test_drive_mode_shows_one_bar_and_three_when_asked_and_keeps_the_choice(self):
        page = self.open(self.scripted(self.three()), width=1024, height=600)
        self.wait(page, self.TITLE, "Detector running")
        page.click("#driveBtn")
        self.wait(page, "document.getElementById('dDb').textContent", "40.0")
        self.assertTrue(page.evaluate("document.getElementById('dThree').hidden"))          # one bar by default
        self.assertFalse(page.evaluate("document.getElementById('dBar').hidden"))
        self.assertEqual(page.evaluate("document.getElementById('dBarsText').textContent"), "3 bars")
        page.click("#dBars")
        self.wait(page, "document.querySelectorAll('#dThree .d-row').length", 3)
        for sel in ("dNum", "dLine", "dBar", "dOthers"):
            self.assertTrue(page.evaluate(f"document.getElementById('{sel}').hidden"), sel)
        rows = page.evaluate("[...document.querySelectorAll('#dThree .d-row')].map(r => [r.className, r.querySelector('.d-rh').innerText,"
                             " r.querySelectorAll('.d-rbar i').length, r.querySelectorAll('.d-rbar i.on').length])")
        self.assertEqual([r[0] for r in rows], ["d-row lvl-red", "d-row lvl-yellow", "d-row lvl-grey"])
        self.assertIn("40.0", rows[0][1]); self.assertIn("STRONG", rows[0][1]); self.assertIn("L190", rows[0][1])
        self.assertIn("rising", rows[0][1])
        self.assertIn("L60", rows[1][1]); self.assertIn("n/a", rows[2][1]); self.assertIn("L119", rows[2][1])
        self.assertEqual([r[2] for r in rows], [12, 12, 12])
        self.assertEqual([r[3] for r in rows], [12, 4, 0])                                   # the one bar's own scale: 40 dB fills it
        self.assertEqual(page.evaluate("document.getElementById('dBarsText').textContent"), "1 bar")
        page.reload()
        page.click("#startBtn")
        self.wait(page, "document.querySelectorAll('#dThree .d-row').length", 3)            # kept on this device
        page.click("#dBars")
        self.wait(page, "document.getElementById('dThree').hidden", True)
        self.assertFalse(page.evaluate("document.getElementById('dBar').hidden"))

    def test_three_bars_with_fewer_channels_shows_those_and_with_none_says_why(self):
        page = self.open(self.scripted(self.strong()), width=800, height=480,
                         init="localStorage.setItem('s17view', JSON.stringify({section: 'live', mode: 'drive', bars: 3}));")
        self.wait(page, "document.querySelectorAll('#dThree .d-row').length", 2)            # no empty third row
        page = self.open(self.scripted(message(1, [], [], state="stopped")), width=800, height=480,
                         init="localStorage.setItem('s17view', JSON.stringify({section: 'live', mode: 'drive', bars: 3}));")
        self.wait(page, "document.getElementById('dEmptyTitle').textContent", "Detector stopped")
        self.assertTrue(page.evaluate("document.getElementById('dThree').hidden"))

    def test_on_the_car_computers_screen_three_bars_are_kept_on_the_car_computer(self):
        saved = []
        page = self.open(self.screen_routes(self.three(), saved, bars=3), start=False, width=800, height=480)
        self.wait(page, "document.querySelectorAll('#dThree .d-row').length", 3)
        page.click("#dBars")
        self.wait(page, "document.getElementById('dThree').hidden", True)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not saved:
            time.sleep(0.05)
        self.assertEqual(saved, [{"bars": 1}])

    def test_three_bars_fit_every_screen_and_no_word_is_cut(self):
        cut = ("[...document.querySelectorAll('#drive .d-acts button span, #dBars span, #dStatus, #dVia, #dAlert, .d-rch')]"
               ".filter(e => e.offsetParent && e.scrollWidth > e.clientWidth + 1).map(e => e.id || e.className + ':' + e.textContent)")
        for width, height in ((1920, 1080), (1280, 800), (1024, 600), (800, 480), (844, 390), (390, 844)):
            page = self.open(self.scripted(self.three()), width=width, height=height,
                             init="localStorage.setItem('s17view', JSON.stringify({section: 'live', mode: 'drive', bars: 3}));")
            self.wait(page, "document.querySelectorAll('#dThree .d-row').length", 3)
            self.wait(page, "document.getElementById('dAlert').hidden", False)       # the sound's line: the widest top line
            self.assertEqual(page.evaluate(cut), [], (width, height))
            # Its longest text (shown up to 10 s): whole, inside the top line's height. It was cut: "…ag", standing up "2 s ag".
            worst = page.evaluate("""() => { const t = document.getElementById('dAlertText'), a = document.getElementById('dAlert');
                t.textContent = 'Urgent\u00a0beep\u00a0\u00b7 10\u00a0s\u00a0ago';
                const r = a.getBoundingClientRect(), b = document.getElementById('dExit').getBoundingClientRect();
                return [a.scrollWidth <= a.clientWidth + 1, r.height <= b.height + 1, t.getClientRects().length]; }""")
            self.assertEqual(worst[:2], [True, True], (width, height, worst))
            for sel in ("#dThree .d-row:last-child .d-rbar", "#dNote", "#dBars", "#dExit", "#dSaw button[data-label='noise']"):
                box = json.loads(page.evaluate(f"JSON.stringify(document.querySelector(\"{sel}\").getBoundingClientRect())"))
                self.assertLessEqual(box["bottom"], height + 0.5, (width, height, sel))
                self.assertLessEqual(box["right"], width + 0.5, (width, height, sel))
            note = json.loads(page.evaluate("JSON.stringify(document.getElementById('dNote').getBoundingClientRect())"))
            last = json.loads(page.evaluate("JSON.stringify(document.querySelector('#dThree .d-row:last-child').getBoundingClientRect())"))
            self.assertLessEqual(last["bottom"], note["top"] + 0.5, (width, height))        # nothing on top of each other
            self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), width)

    # Found on the car computer's own screen, 25 November 2026: it had neither font, so it fell back to DejaVu
    # Sans and looked like another app; drive mode at 1920 x 1080 lost the design's proportions; with no data it
    # showed a grey bar where the number goes; and it opened on the section a test had left it on.
    def test_the_page_carries_its_own_fonts_and_the_policy_lets_them_load(self):
        page = self.open(self.scripted(self.strong()), width=1440, height=900)
        self.wait(page, self.TITLE, "Detector running")
        self.wait(page, "[...document.fonts].filter(f => f.status === 'loaded').map(f => f.family).sort().join()",
                  "Bricolage Grotesque,Inter")
        self.assertTrue(page.evaluate("document.fonts.check('700 22px \"Bricolage Grotesque\"') && document.fonts.check('15px Inter')"))
        self.assertTrue(page.evaluate("getComputedStyle(document.body).fontFamily.startsWith('Inter')"))

    def test_drive_mode_keeps_the_designs_proportions_on_a_big_screen(self):
        page = self.open(self.scripted(self.strong()), width=1920, height=1080)
        self.wait(page, self.TITLE, "Detector running")
        page.click("#driveBtn")
        self.wait(page, "document.getElementById('dDb').textContent", "40.0")
        unit = 1920 / 844                                        # drawn at 844 x 390, scaled as a whole
        size = page.evaluate("parseFloat(getComputedStyle(document.getElementById('dDb')).fontSize)")
        drawn = 120 if page.evaluate("document.getElementById('drive').classList.contains('alerting')") else 112
        self.assertAlmostEqual(size, drawn * unit, delta=2)      # B: 112 px quiet, 120 px in a strong burst
        column = page.evaluate("document.getElementById('dSaw').getBoundingClientRect().width")
        self.assertAlmostEqual(column, 340 * unit, delta=2)
        for sel in ("#dSaw button[data-label='noise']", "#dExit", "#dDb", "#dNote"):
            box = json.loads(page.evaluate(f"JSON.stringify(document.querySelector(\"{sel}\").getBoundingClientRect())"))
            self.assertLessEqual(box["bottom"], 1080, sel)
            self.assertLessEqual(box["right"], 1920, sel)

    def test_drive_mode_with_nothing_to_show_says_why_in_words(self):
        page = self.open(self.scripted(message(1, [], [], state="stopped")), width=1024, height=600)
        self.wait(page, self.TITLE, "Detector stopped")
        page.click("#driveBtn")
        self.wait(page, "document.getElementById('dEmpty').hidden", False)
        self.assertTrue(page.evaluate("document.getElementById('dNum').hidden"))
        self.assertEqual(page.evaluate("document.getElementById('dEmptyTitle').textContent"), "Detector stopped")
        self.assertTrue(page.evaluate("document.getElementById('dBar').hidden"))   # no empty bar under the words

    def test_the_cockpit_with_no_channel_says_so_in_words_not_with_empty_cards(self):
        page = self.open(self.scripted(message(1, [], [])), width=1920, height=1080)
        self.wait(page, self.TITLE, "Detector running")
        self.wait(page, "document.getElementById('mNone').hidden", False)
        self.assertTrue(page.evaluate("document.getElementById('meters').hidden"))
        self.assertEqual(page.evaluate("document.getElementById('mNoneTitle').textContent"), "No TETRA heard")
        self.assertEqual(page.evaluate("document.getElementById('emptyNote').textContent"), "")

    def test_the_cockpit_with_the_detector_stopped_gives_its_reason_where_the_cards_go(self):
        page = self.open(self.scripted(message(1, [], [], state="stopped")), width=1920, height=1080)
        self.wait(page, self.TITLE, "Detector stopped")
        self.wait(page, "document.getElementById('mNoneTitle').textContent", "Detector stopped")

    # Review of 26 November 2026: the README said the chime comes at 35 dB; the apps take that from the server, but
    # the page kept its own 22 dB.
    def config_route(self, rules):
        return {"**/api/config": lambda r: r.fulfill(status=200, headers={"Content-Type": "application/json"},
                                                      body=json.dumps({"version": "x", "config": {"rules": rules}}))}

    def test_the_page_takes_the_servers_sound_levels_like_the_apps(self):
        routes = dict(self.scripted(message(1, [], [])), **self.config_route({"red_db": 35, "yellow_db": 12, "urgent_db": 50}))
        page = self.open(routes, width=1440, height=900)
        self.wait(page, "document.getElementById('redOut').textContent", "35 dB")
        page.reload()                                                   # settings saved by an older page: 22 is its default
        page.evaluate("localStorage.setItem('s17cfg', JSON.stringify({red: 22, yellow: 12, sound: false}))")
        page.reload()
        page.click("#startBtn")
        self.wait(page, "document.getElementById('redOut').textContent", "35 dB")

    def test_a_level_set_by_hand_in_the_browser_stays(self):
        routes = dict(self.scripted(message(1, [], [])), **self.config_route({"red_db": 35}))
        page = self.open(routes, width=1440, height=900,
                         init="localStorage.setItem('s17cfg', JSON.stringify({red: 22, redByHand: true}));")
        time.sleep(0.8)
        self.assertEqual(page.evaluate("document.getElementById('redOut').textContent"), "22 dB")
        page = self.open(routes, width=1440, height=900, init="localStorage.setItem('s17cfg', JSON.stringify({red: 30}));")
        time.sleep(0.8)
        self.assertEqual(page.evaluate("document.getElementById('redOut').textContent"), "30 dB")   # not the old default

    def test_without_server_levels_the_page_chimes_from_35_and_an_old_saved_22_moves_once(self):
        # Review of 26 November 2026: the car computer's own screen has no /api/config, so it chimed from
        # the page's own 22 dB while the README and the apps say 35.
        page = self.open(self.scripted(message(1, [], [])), width=1440, height=900)
        self.wait(page, "document.getElementById('redOut').textContent", "35 dB")
        page = self.open(self.scripted(message(1, [], [])), width=1440, height=900,
                         init="if (!sessionStorage.getItem('seeded')) { sessionStorage.setItem('seeded', '1'); "
                              "localStorage.setItem('s17cfg', JSON.stringify({red: 22, yellow: 12})); }")
        self.wait(page, "document.getElementById('redOut').textContent", "35 dB")
        page.evaluate("const c = JSON.parse(localStorage.getItem('s17cfg')); c.red = 22; localStorage.setItem('s17cfg', JSON.stringify(c))")
        page.reload()                                         # once only: a 22 chosen after the move stays
        time.sleep(0.8)
        self.assertEqual(page.evaluate("document.getElementById('redOut').textContent"), "22 dB")

    def test_the_phones_status_row_says_car_on_one_line_like_the_others(self):
        # Seen in the docs review's new pictures, 26 November 2026: "Car computer" took two lines there.
        page = self.open(self.scripted(message(1, [], [])), width=390, height=844)
        self.wait(page, self.TITLE, "Detector running")
        names = "[...document.querySelectorAll('#lightsLive .light b')].map(b => [b.innerText.trim(), b.getClientRects().length > 0 ? Math.round(b.getBoundingClientRect().height) : 0])"
        got = page.evaluate(names)
        self.assertEqual([n for n, _ in got][1], "Car", got)
        self.assertEqual(len({h for _, h in got}), 1, got)                     # every name one line high
        page = self.open(self.scripted(message(1, [], [])), width=1440, height=900)
        self.wait(page, self.TITLE, "Detector running")
        self.assertIn("Car computer", page.evaluate("document.getElementById('lights').innerText"))

    def test_the_rail_says_car_and_the_sidebar_car_computer(self):
        label = "document.querySelector('#carBtn > span').innerText.trim()"
        page = self.open(self.scripted(message(1, [], [])), width=800, height=480)
        self.wait(page, self.TITLE, "Detector running")
        self.assertEqual(page.evaluate(label), "Car")
        page = self.open(self.scripted(message(1, [], [])), width=1920, height=1080)
        self.wait(page, self.TITLE, "Detector running")
        self.assertEqual(page.evaluate(label), "Car computer")

    def test_the_car_computers_screen_opens_on_live_whatever_a_browser_left(self):
        routes = self.screen_routes(self.strong(), [])
        page = self.open(routes, start=False, width=1024, height=600,
                         init="localStorage.setItem('s17view', JSON.stringify({section: 'car', mode: 'cockpit'}));")
        self.wait(page, "document.body.classList.contains('kiosk')", True)
        self.wait(page, "document.getElementById('v-live').hidden", False)
        self.assertTrue(page.evaluate("document.getElementById('v-car').hidden"))

    def test_an_800_by_480_screen_fits_in_both_modes(self):
        page = self.open(self.scripted(self.strong()), width=800, height=480)
        self.wait(page, self.TITLE, "Detector running")
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 800)
        self.assertLessEqual(page.evaluate("document.getElementById('nav').getBoundingClientRect().width"), 80)
        page.click("#driveBtn")
        self.wait(page, "document.getElementById('dDb').textContent", "40.0")
        for sel in ("#dSaw button[data-label='noise']", "#dExit", "#dDb"):
            box = page.evaluate(f"JSON.stringify(document.querySelector(\"{sel}\").getBoundingClientRect())")
            box = json.loads(box)
            self.assertLessEqual(box["bottom"], 480, sel)
            self.assertLessEqual(box["right"], 800, sel)

    def test_narrow_phone_layout_fits(self):
        msg = message(1, [channel(190, 40.0, 1.0), channel(60, 14.0, 5.0), channel(119, None, 30.0)],
                      [match(190, 40.0, 1.0, "s/layout")])
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
