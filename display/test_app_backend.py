"""Tests for what the iPhone app adds to the display. Run: python -m unittest display/test_app_backend.py

These run the real server and the real publisher's Sender over a real socket. Nothing here can
power anything off or touch a receiver: every command runner is replaced with a recorder.
"""
import base64
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import unittest.mock
import urllib.error
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import display_publisher as pub  # noqa: E402
import server as srv             # noqa: E402

ID_A, ID_B = "a" * 32, "b" * 32


class Base(unittest.TestCase):
    TOKEN, KEY, STATUS_KEY = "t" * 40, "k" * 24, "s" * 24

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.logs = self.dir / "logs"
        self.store = srv.Store()
        self.httpd = srv.CappedServer(("127.0.0.1", 0), srv.make_handler(
            {"pi_token": self.TOKEN, "viewer_key": self.KEY, "pi_status_key": self.STATUS_KEY},
            self.store, b"<html>__NONCE__</html>",
            log_dir=self.logs))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.port}"
        self.session = self.dir / "session"
        self.session.mkdir()
        (self.session / "session.json").write_text("{}")
        (self.session / "events.jsonl").write_text("")
        self.hb = self.dir / "hb.json"
        self.write_hb()
        self.sender = pub.Sender(self.url + "/api/push", self.TOKEN)
        self.pub = pub.Publisher(self.sender, heartbeat=self.hb)
        self.cfg = {"url": self.url + "/api/push", "token": self.TOKEN}

    def tearDown(self):
        self.sender.close()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def write_hb(self):
        self.hb.write_text(json.dumps({"pid": 1, "t": time.time(), "samples_in": 1, "windows_started": 4,
                                       "windows": 4, "hits": 2, "dropped": 0, "ready": True,
                                       "session": str(self.session)}))

    def req(self, method, path, body=None, cookie=True, token=False, headers=None):
        all_headers = dict(headers or {})
        if cookie:
            all_headers["Cookie"] = f"s17={self.KEY}"
        if token:
            all_headers["Authorization"] = "Bearer " + self.TOKEN
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=all_headers)
        resp = conn.getresponse()
        out = resp.status, resp.read()
        conn.close()
        return out

    def op(self, cookie=True, **fields):
        return self.req("POST", "/api/op", json.dumps(fields), cookie=cookie)

    def mark(self, op_id=ID_A, label="police", **kw):
        return self.op(kind="mark", id=op_id, label=label, age_s=kw.get("age_s", 0.5),
                       phone_utc=1793814741.0, note=kw.get("note", ""))

    def cycle(self):
        """One publisher pass: read the heartbeat, push, then do what the push collected."""
        self.pub.last_hb_poll = -1e9
        self.pub.last_push = -1e9
        self.pub.step()
        return pub.handle_ops(self.sender, self.pub, self.cfg)

    def op_state(self, op_id=ID_A):
        message = json.loads(self.req("GET", "/api/state")[1])
        return next(o for o in message["ops"] if o["id"] == op_id)


class AutomaticPiLog(Base):
    """The Pi cannot send a log when the ignition cuts its power, so the server asks during the drive."""

    def auto_ops(self):
        return [op for op in self.store.ops.values() if op["payload"].get("auto")]

    def test_a_log_is_asked_for_early_in_a_drive_and_then_regularly(self):
        self.store.features = ["logs"]
        self.store._auto_log(1000.0, False)
        self.assertEqual(self.auto_ops(), [], "not at the first push")
        self.store._auto_log(1000.0 + srv.AUTO_PI_LOG_FIRST_S - 1, True)
        self.assertEqual(self.auto_ops(), [])
        self.store._auto_log(1000.0 + srv.AUTO_PI_LOG_FIRST_S + 1, True)
        self.assertEqual(len(self.auto_ops()), 1)
        for op in self.store.ops.values():
            op["state"] = "done"
        self.store._auto_log(1000.0 + srv.AUTO_PI_LOG_FIRST_S + 60, True)
        self.assertEqual(len(self.auto_ops()), 1, "not again within the interval")
        self.store._auto_log(1000.0 + srv.AUTO_PI_LOG_FIRST_S + srv.AUTO_PI_LOG_S + 2, True)
        self.assertEqual(len(self.auto_ops()), 2)

    def test_the_pi_really_uploads_it_and_the_phone_is_not_shown_the_request(self):
        self.store.auto_log_due = -1e9
        self.store.received_mono = srv.time.monotonic()
        self.store.features = ["logs"]
        self.cycle()                    # the push queues the request
        self.cycle()                    # the next one hands it to the Pi, which uploads in the background
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not list(self.logs.glob("pi-*.log")):
            time.sleep(0.05)
        saved = list(self.logs.glob("pi-*.log"))
        self.assertEqual(len(saved), 1)
        self.assertIn("Slot17 Pi log", saved[0].read_text(encoding="utf-8"))
        message = json.loads(self.req("GET", "/api/state")[1])
        self.assertNotIn("ops", message)

    def test_a_new_drive_starts_its_own_schedule(self):
        self.store.features = ["logs"]
        self.store._auto_log(1000.0, False)
        self.store._auto_log(1000.0 + 5 * srv.AUTO_PI_LOG_S, False)   # the Pi was away: a new drive's first push
        self.assertEqual(self.auto_ops(), [])
        self.store._auto_log(1000.0 + 5 * srv.AUTO_PI_LOG_S + srv.AUTO_PI_LOG_FIRST_S, True)
        self.assertEqual(len(self.auto_ops()), 1)

    def test_no_request_while_the_phones_own_is_waiting_or_when_turned_off(self):
        self.store.features = ["logs"]
        self.assertEqual(self.op(kind="logs", id=ID_A)[0], 200)
        self.store.auto_log_due = -1e9
        self.store.received_mono = srv.time.monotonic()
        self.store._auto_log(srv.time.monotonic(), True)
        self.assertEqual(self.auto_ops(), [])
        self.store.ops.clear()
        self.store.auto_log_s, self.store.auto_log_due = 0, -1e9
        self.store._auto_log(srv.time.monotonic(), True)
        self.assertEqual(self.store.ops, {})

    def test_an_old_publisher_without_logs_is_never_asked(self):
        self.store.features = ["mark"]
        self.store.auto_log_due = -1e9
        self.store._auto_log(srv.time.monotonic(), True)
        self.assertEqual(self.store.ops, {})


class PushoverSwitch(Base):
    """1.2.9: the app switches the car computer's Pushover messages off and on, and sees the result."""

    def use_switch(self):
        """The detector's notify module with its switch in this test's folder. The display host has no
        p0/, so there the round trip cannot run; the server's own checks below still do."""
        notify = pub.notify_module()
        if notify is None:
            self.skipTest("p0/notify.py is not here (a display-only copy)")
        self.addCleanup(setattr, notify, "SWITCH_PATH", notify.SWITCH_PATH)
        notify.SWITCH_PATH = str(self.dir / "slot17-notify.json")
        return notify

    def reported(self):
        self.pub.last_health = -1e9                # read the switch again on this push
        self.cycle()
        return json.loads(self.req("GET", "/api/state")[1])["state"].get("notify")

    def test_off_and_on_again_from_the_app(self):
        self.notify = self.use_switch()
        self.cycle()                               # the Pi announces its features
        self.assertIn("notify", self.store.features)
        self.assertEqual(self.reported()["pushover"], True)
        self.assertEqual(self.op(kind="notify", id=ID_A, pushover=False)[0], 200)
        self.cycle()                               # handed to the Pi and carried out
        self.cycle()                               # its answer travels back
        self.assertEqual(self.op_state(ID_A)["state"], "done")
        self.assertEqual(self.op_state(ID_A)["detail"], "Pushover messages off")
        self.assertFalse(self.notify.pushover_enabled())
        self.assertEqual(self.reported()["pushover"], False)
        self.assertEqual(self.op(kind="notify", id=ID_B, pushover=True)[0], 200)
        self.cycle()
        self.cycle()
        self.assertTrue(self.notify.pushover_enabled())
        self.assertEqual(self.reported()["pushover"], True)

    def test_only_a_real_boolean_is_accepted(self):
        for wrong in ("no", 0, None, [False]):
            self.assertEqual(self.op(kind="notify", id=ID_A, pushover=wrong)[0], 400, wrong)
        self.assertEqual(self.op(kind="notify", id=ID_A)[0], 400)

    def test_an_older_car_computer_is_never_sent_it(self):
        self.store.features = ["mark", "logs"]
        self.assertEqual(self.op(kind="notify", id=ID_A, pushover=False)[0], 200)
        self.assertEqual(self.store.take_ops(), [])
        self.assertEqual(self.op_state(ID_A)["state"], "pending")

    def test_a_malformed_report_is_dropped_not_refused(self):
        state = {"v": 3, "detector": {"state": "running"}, "channels": [], "matches": [], "source": "s",
                 "ticket": "t", "snapshot_age_s": 0, "seq": 1}
        self.assertNotIn("notify", srv.clean_state(dict(state, notify={"pushover": "off"})))
        self.assertEqual(srv.clean_state(dict(state, notify={"pushover": False}))["notify"],
                         {"pushover": False, "configured": False})


class MatchRuleAndVersion(Base):
    """1.2.9: the app hears which rule recognised each match, and the car computer's software version."""

    def test_the_rule_and_version_reach_the_app_and_nothing_else_does(self):
        self.cycle()
        now = time.monotonic()
        with open(self.session / "events.jsonl", "a") as fh:
            for eid, ch, rule in (("e1", 10, "coherent"), ("e2", 11, "weak"), ("e3", 12, "<script>"), ("e4", 13, None)):
                fh.write(json.dumps({"event_type": "candidate", "confirmed": True, "tetra_channel": ch,
                                     "event_id": eid, "frequency_hz": 380.2e6, "peak_snr_db": 30.0,
                                     "strength_reliable": True, "burst_mono": now, "matched_timing_refined": True,
                                     "matched_burst": {"rule": rule}}) + "\n")
        self.cycle()                               # matches travel once, in the next push
        state = json.loads(self.req("GET", "/api/state")[1])["state"]
        rules = {m["ch"]: m.get("rule") for m in state["matches"]}
        self.assertEqual(rules, {10: "coherent", 11: "weak", 12: None, 13: None})
        if pub.software_version() is None:         # the display host has no p0/: no version, and no error
            self.assertNotIn("software", state)
        else:
            self.assertEqual(state["software"], pub.software_version())
            self.assertRegex(state["software"], r"^\d+\.\d+\.\d+$")

    def test_the_server_drops_an_odd_version_or_rule(self):
        state = {"v": 3, "detector": {"state": "running"}, "channels": [], "source": "s", "ticket": "t",
                 "snapshot_age_s": 0, "seq": 1,
                 "matches": [{"id": "a", "ch": 1, "strength_db": 20, "age_s": 1, "delay_s": 1, "rule": "police"}]}
        for odd in ("1.2", "1.2.9; rm", "v1.2.9", 129):
            self.assertNotIn("software", srv.clean_state(dict(state, software=odd)), odd)
        self.assertNotIn("rule", srv.clean_state(state)["matches"][0])
        self.assertEqual(srv.clean_state(dict(state, software="1.2.9"))["software"], "1.2.9")

    def test_the_version_in_p0_is_the_repository_version(self):
        root = Path(__file__).resolve().parents[1]
        if not (root / "VERSION").exists() or not (root / "p0/version.py").exists():
            self.skipTest("no VERSION or p0/ here (a display-only copy)")
        sys.path.insert(0, str(root / "p0"))
        import version
        self.assertEqual(version.SOFTWARE, (root / "VERSION").read_text().strip())


class ReleaseNotice(Base):
    """1.2.9: the server tells the app and page which release is newest, from GitHub, and nothing more."""

    RELEASE = {"tag_name": "v1.3.0", "draft": False, "prerelease": False,
               "html_url": "https://github.com/efeorman/Slot17/releases/tag/v1.3.0"}

    def fake(self, release=None, versions=None, fail=None):
        asked = []

        def get(url):
            asked.append(url)
            if fail:
                raise fail
            if url.startswith("https://api.github.com/"):
                return json.dumps(release or self.RELEASE).encode()
            if versions is None:
                raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
            return json.dumps(versions).encode()
        return get, asked

    def test_the_newest_release_and_its_app_version(self):
        get, asked = self.fake(versions={"software": "1.3.0", "app": "1.7.0", "android": "1.8.0"})
        found = srv.fetch_latest_release("efeorman/Slot17", get=get)
        self.assertEqual(found, {"software": "1.3.0", "app": "1.7.0", "android": "1.8.0", "tag": "v1.3.0",
                                 "url": "https://github.com/efeorman/Slot17/releases/tag/v1.3.0"})
        self.assertEqual(asked, ["https://api.github.com/repos/efeorman/Slot17/releases/latest",
                                 "https://raw.githubusercontent.com/efeorman/Slot17/v1.3.0/versions.json"])

    def test_an_older_release_without_versions_json_still_names_the_software(self):
        get, _ = self.fake(versions=None)
        self.assertEqual(srv.fetch_latest_release("efeorman/Slot17", get=get)["app"], None)

    def test_drafts_odd_tags_odd_links_and_odd_repos_are_refused(self):
        for release in (dict(self.RELEASE, draft=True), dict(self.RELEASE, prerelease=True),
                        dict(self.RELEASE, tag_name="v1.3.0/../../x"), dict(self.RELEASE, tag_name="latest")):
            self.assertIsNone(srv.fetch_latest_release("efeorman/Slot17", get=self.fake(release)[0]), release)
        odd_link = dict(self.RELEASE, html_url="https://evil.example/phish")
        found = srv.fetch_latest_release("efeorman/Slot17", get=self.fake(odd_link, {"app": "<b>"})[0])
        self.assertEqual(found["url"], "https://github.com/efeorman/Slot17/releases/latest")
        self.assertIsNone(found["app"])
        for repo in ("efeorman/Slot17/../x", "a b/c", "", "x"):
            with self.assertRaises(ValueError):
                srv.fetch_latest_release(repo, get=self.fake()[0])

    def test_only_githubs_https_addresses_are_opened(self):
        for url in ("http://api.github.com/x", "file:///etc/passwd", "https://example.com/x"):
            with self.assertRaises(ValueError):
                srv._https_get(url)

    def test_the_app_hears_it_and_a_failure_keeps_the_last_answer(self):
        get, _ = self.fake(versions={"software": "1.3.0", "app": "1.7.0"})
        check = srv.ReleaseCheck(self.store, "efeorman/Slot17", fetch=lambda repo: srv.fetch_latest_release(repo, get=get))
        check.check_once()
        message = json.loads(self.req("GET", "/api/state")[1])
        self.assertEqual(message["release"]["app"], "1.7.0")
        private = urllib.error.HTTPError("u", 404, "Not Found", {}, None)    # what a private repository answers
        failing = srv.ReleaseCheck(self.store, "efeorman/Slot17",
                                   fetch=lambda repo: srv.fetch_latest_release(repo, get=self.fake(fail=private)[0]))
        self.assertIsNone(failing.check_once())
        self.assertEqual(json.loads(self.req("GET", "/api/state")[1])["release"]["software"], "1.3.0")

    def test_versions_json_matches_the_software_and_the_app(self):
        root = Path(__file__).resolve().parents[1]
        if not (root / "versions.json").exists():
            self.skipTest("no versions.json here (a display-only copy)")
        versions = json.loads((root / "versions.json").read_text(encoding="utf-8"))
        self.assertEqual(versions["software"], (root / "VERSION").read_text().strip())
        project = (root / "ios/project.yml").read_text(encoding="utf-8")
        self.assertIn(f'MARKETING_VERSION: "{versions["app"]}"', project)
        android = (root / "android/app/build.gradle.kts").read_text(encoding="utf-8")
        self.assertIn(f'versionName = "{versions["android"]}"', android)


class RelayUpdate(unittest.TestCase):
    """The team's signed car computer update, copied from the newest GitHub release (release_updates)."""
    BUNDLE = b"\x1f\x8b pretend signed bundle " * 50
    REPO = "efeorman/Slot17"

    def setUp(self):
        import hashlib
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "release"
        self.meta = {"seq": 19, "commit": "a" * 40, "sha256": hashlib.sha256(self.BUNDLE).hexdigest(),
                     "size": len(self.BUNDLE), "signature": "ab" * 64, "created": "2026-11-19T12:00:00Z"}

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, meta=None, bundle=None, assets=None):
        base = f"https://api.github.com/repos/{self.REPO}/releases/assets/"
        release = {"tag_name": "v1.3.1", "assets": assets if assets is not None else [
            {"name": "slot17-update.json", "url": base + "1"}, {"name": "slot17-update.tar.gz", "url": base + "2"}]}
        asked = []

        def get(url, accept="application/vnd.github+json", limit=0):
            asked.append((url, accept))
            if url.endswith("/releases/latest"):
                return json.dumps(release).encode()
            if url == base + "1":
                return json.dumps(meta or self.meta).encode()
            if url == base + "2":
                return self.BUNDLE if bundle is None else bundle
            raise AssertionError(url)
        return get, asked

    def test_copies_it_once(self):
        get, asked = self.fake()
        self.assertEqual(srv.relay_update(self.REPO, self.dir, get=get), "release 19 is ready for the car computer")
        self.assertEqual((self.dir / "bundle.tar.gz").read_bytes(), self.BUNDLE)
        self.assertEqual(json.loads((self.dir / "release.json").read_text())["seq"], 19)
        self.assertEqual(asked[1][1], "application/octet-stream")
        get, asked = self.fake()
        self.assertEqual(srv.relay_update(self.REPO, self.dir, get=get), "release 19 is current")
        self.assertEqual(len(asked), 2, "the bundle isn't downloaded again")

    def test_a_bundle_that_differs_from_its_description_is_refused(self):
        for bundle in (self.BUNDLE + b"x", b"other" * 200):
            with self.assertRaises(ValueError):
                srv.relay_update(self.REPO, self.dir, get=self.fake(bundle=bundle)[0])
        self.assertFalse((self.dir / "release.json").exists())

    def test_odd_addresses_and_descriptions_are_refused(self):
        odd = [{"name": "slot17-update.json", "url": "https://evil.example/1"},
               {"name": "slot17-update.tar.gz", "url": f"https://api.github.com/repos/{self.REPO}/releases/assets/2"}]
        with self.assertRaises(ValueError):
            srv.relay_update(self.REPO, self.dir, get=self.fake(assets=odd)[0])
        for meta in (dict(self.meta, signature="zz"), dict(self.meta, size=srv.RELEASE_MAX + 1), dict(self.meta, sha256="x")):
            with self.assertRaises(ValueError):
                srv.relay_update(self.REPO, self.dir, get=self.fake(meta=meta)[0])

    def test_a_release_without_an_update_changes_nothing(self):
        said = srv.relay_update(self.REPO, self.dir, get=self.fake(assets=[])[0])
        self.assertEqual(said, "the newest release has no car computer update")
        self.assertFalse(self.dir.exists())

    def test_new_servers_pass_updates_on(self):
        path = Path(self.tmp.name) / "config.json"
        with unittest.mock.patch("builtins.print"):
            srv.init_config(path)
        self.assertIs(json.loads(path.read_text())["release_updates"], True)


class SoftwareUpdate(Base):
    def setUp(self):
        super().setUp()
        self.release = self.dir / "release"
        self.httpd.shutdown()
        self.httpd.server_close()
        self.httpd = srv.CappedServer(("127.0.0.1", 0), srv.make_handler(
            {"pi_token": self.TOKEN, "viewer_key": self.KEY}, self.store, b"<html>__NONCE__</html>",
            log_dir=self.logs, release_dir=self.release))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.sender.close()
        self.sender = pub.Sender(f"http://127.0.0.1:{self.port}/api/push", self.TOKEN)
        self.pub = pub.Publisher(self.sender, heartbeat=self.hb)

    def test_a_release_is_handed_only_to_the_car_computer(self):
        self.assertEqual(self.req("GET", "/api/release", cookie=False, token=True)[0], 404, "none published yet")
        self.release.mkdir()
        (self.release / "release.json").write_text('{"seq": 4, "signature": "ab"}')
        (self.release / "bundle.tar.gz").write_bytes(b"\x1f\x8b" + b"x" * 5000)
        for path in ("/api/release", "/api/release/bundle"):
            self.assertEqual(self.req("GET", path, cookie=False)[0], 401, path)
            self.assertEqual(self.req("GET", path, cookie=True)[0], 401, "the phone's key is not enough")
        status, body = self.req("GET", "/api/release", cookie=False, token=True)
        self.assertEqual((status, json.loads(body)["seq"]), (200, 4))
        status, body = self.req("GET", "/api/release/bundle", cookie=False, token=True)
        self.assertEqual((status, len(body)), (200, 5002))

    def test_the_update_state_reaches_the_phone_and_a_wrong_one_is_refused(self):
        class Fake:
            def status(self):
                return {"state": "installed", "commit": "a46b01c", "seq": 7, "detail": ""}
        self.pub.updater = Fake()
        self.cycle()
        state = json.loads(self.req("GET", "/api/state")[1])["state"]
        self.assertEqual(state["update"], {"state": "installed", "commit": "a46b01c", "seq": 7, "detail": ""})
        good = {"v": 3, "source": "s", "ticket": "t", "snapshot_age_s": 0, "seq": 1, "detector": {"state": "running"},
                "channels": [], "matches": []}
        for bad in ({"state": "rooted"}, {"state": "current", "commit": "x" * 13}, "current",
                    {"state": "current", "seq": -1}, {"state": "current", "detail": "y" * 121}):
            with self.assertRaises(ValueError, msg=bad):
                srv.clean_state(dict(good, update=bad))
        self.assertNotIn("update", srv.clean_state(good))

    def test_the_publisher_checks_soon_after_starting_and_then_rarely(self):
        calls = []

        class Fake:
            def status(self):
                return {"state": "current", "commit": "", "seq": 0, "detail": ""}

            def fetch(self):
                calls.append(1)
                return "nothing newer"
        self.pub.updater = Fake()
        self.assertIsNone(self.pub.check_for_update(100.0), "not at once: the link may not be up")
        thread = self.pub.check_for_update(100.0 + pub.UPDATE_FIRST_CHECK_S)
        thread.join(5)
        self.assertIsNone(self.pub.check_for_update(100.0 + pub.UPDATE_FIRST_CHECK_S + 60))
        self.pub.check_for_update(100.0 + pub.UPDATE_FIRST_CHECK_S + pub.UPDATE_CHECK_EVERY_S).join(5)
        self.assertEqual(len(calls), 2)

    def test_a_publisher_without_an_updater_sends_no_update_state(self):
        self.pub.updater = None
        self.cycle()
        self.assertNotIn("update", json.loads(self.req("GET", "/api/state")[1])["state"])
        self.assertIsNone(self.pub.check_for_update(1e9))


class ReceiverReadings(Base):
    def test_the_noise_floor_and_whole_band_count_reach_the_phone(self):
        hb = json.loads(self.hb.read_text())
        hb.update(rf_health={"median_floor_db": 85.24, "input": {"peak_counts": 5000.0}}, whole_band_windows=3)
        self.hb.write_text(json.dumps(hb))
        self.cycle()
        detector = json.loads(self.req("GET", "/api/state")[1])["state"]["detector"]
        self.assertEqual((detector["floor_db"], detector["whole_band_windows"]), (85.2, 3))

    def test_an_older_detector_without_them_sends_neither(self):
        self.cycle()
        detector = json.loads(self.req("GET", "/api/state")[1])["state"]["detector"]
        self.assertNotIn("floor_db", detector)
        self.assertNotIn("whole_band_windows", detector)

    def test_a_floor_that_is_not_a_number_is_refused(self):
        good = {"v": 3, "source": "s", "ticket": "t", "snapshot_age_s": 0, "seq": 1, "channels": [], "matches": []}
        for bad in ("85", float("nan"), 1e9, True):
            with self.assertRaises(ValueError, msg=bad):
                srv.clean_state(dict(good, detector={"state": "running", "floor_db": bad}))


class OperationValidation(Base):
    def test_operations_need_the_viewer_key(self):
        self.assertEqual(self.op(cookie=False, kind="poweroff", id=ID_A)[0], 401)
        self.assertEqual(self.store.ops, {})

    def test_only_known_operations_are_accepted(self):
        for bad in ({"kind": "reboot", "id": ID_A}, {"kind": "start", "id": ID_A},
                    {"kind": "poweroff", "id": "../../etc"}, {"kind": "poweroff"},
                    {"kind": "mark", "id": ID_A, "label": "tank", "phone_utc": 1793814741.0},
                    {"kind": "mark", "id": ID_A, "label": "police"},
                    {"kind": "mark", "id": ID_A, "label": "police", "phone_utc": 1793814741.0, "age_s": -1}):
            self.assertEqual(self.op(**bad)[0], 400, bad)
        self.assertEqual(self.store.ops, {})

    def test_a_retry_with_the_same_id_is_one_operation(self):
        self.mark()
        self.mark()
        self.assertEqual(len(self.store.ops), 1)

    def test_the_queue_is_bounded(self):
        for n in range(srv.MAX_OPS):
            self.assertEqual(self.mark(op_id=f"{n:032x}")[0], 200)
        self.assertEqual(self.mark(op_id=ID_A)[0], 400)   # all pending: refuse, do not evict


class OlderPublisher(Base):
    def test_a_publisher_without_features_never_receives_operations(self):
        self.store.ticket("old")
        self.mark()
        self.op(kind="poweroff", id=ID_B)
        self.assertEqual(self.store.take_ops(), [])

    def test_its_push_is_still_accepted(self):
        state = srv.clean_state({"v": 3, "source": "old", "ticket": "x", "snapshot_age_s": 0, "seq": 1,
                                 "session": "s", "detector": {"state": "running"}, "channels": [], "matches": []})
        self.assertNotIn("features", state)
        self.assertNotIn("pi", state)


class Expiry(Base):
    def test_a_queued_power_off_cannot_wait_for_a_later_drive(self):
        self.store.features = ["poweroff"]
        self.op(kind="poweroff", id=ID_A)
        self.store.ops[ID_A]["created"] -= srv.OP_TTL_S["poweroff"] + 1
        self.assertEqual(self.store.take_ops(), [])
        self.assertEqual(self.op_state()["state"], "expired")

    def test_an_uncollected_stop_cannot_end_a_later_drive(self):
        self.store.request_stop("from the phone")
        self.store.command_at -= srv.STOP_TTL_S + 1
        self.assertIsNone(self.store.take_command())
        self.assertEqual(self.store.command_status()["state"], "expired")

    def test_a_fresh_stop_is_still_delivered(self):
        self.store.request_stop("from the phone")
        self.assertEqual(self.store.take_command()["action"], "stop")

    def test_a_sighting_waits_a_long_time(self):
        self.store.features = ["mark"]
        self.mark()
        self.store.ops[ID_A]["created"] -= 3600
        taken = self.store.take_ops()
        self.assertEqual(len(taken), 1)
        self.assertGreater(taken[0]["age_s"], 3600)   # the wait counts towards how long ago it was


class Sightings(Base):
    def deliver(self):
        self.assertTrue(self.cycle())             # the first push declares what the Pi understands
        self.assertTrue(self.cycle())
        self.assertTrue(self.cycle())

    def requests(self):
        return list((self.session / "marks").glob("*.request.json"))

    def test_a_sighting_reaches_the_detector_inbox_in_the_format_it_reads(self):
        self.assertEqual(self.mark(note="blue lights")[0], 200)
        self.deliver()
        files = self.requests()
        self.assertEqual([f.name for f in files], [ID_A + ".request.json"])
        saved = json.loads(files[0].read_text())
        self.assertEqual(saved["label"], "police")
        self.assertIn("blue lights", saved["note"])
        self.assertIn("phone_utc=1793814741.0", saved["note"])
        self.assertLess(abs(time.time() - saved["utc"]), 5)

    def test_time_follows_the_pi_clock_not_the_phone_clock(self):
        # No RTC: the Pi can be hours off. Using the phone's time directly would make the
        # detector read the mark as old and lose its IQ snapshot.
        pi_clock = 1000000.0
        self.pub.heartbeat.poll(time.monotonic())
        self.pub.handle_mark({"id": ID_A, "label": "police", "age_s": 2.0, "phone_utc": 1793814741.0},
                             wall=pi_clock)
        saved = json.loads((self.session / "marks" / (ID_A + ".request.json")).read_text())
        self.assertEqual(saved["utc"], pi_clock - 2.0)

    def test_a_resend_after_the_detector_processed_it_is_not_saved_twice(self):
        self.pub.heartbeat.poll(time.monotonic())
        op = {"id": ID_A, "label": "police", "age_s": 1.0, "phone_utc": 1793814741.0}
        self.pub.handle_mark(op)
        path = self.session / "marks" / (ID_A + ".request.json")
        path.replace(self.session / "marks" / (ID_A + ".request.processed"))  # what marking.py does
        self.pub.handle_mark(op)
        self.assertEqual(self.requests(), [])

    def test_the_phone_hears_whether_iq_was_captured(self):
        self.mark()
        self.deliver()
        with open(self.session / "events.jsonl", "a") as fh:
            fh.write(json.dumps({"event_type": "mark_recording", "mark_id": ID_A + ".request",
                                 "name": ID_A + ".request.iq", "state": "queued"}) + "\n")
        self.deliver()
        got = self.op_state()
        self.assertEqual(got["state"], "done")
        self.assertIn("recording", got["detail"])

    def test_the_phone_hears_when_no_iq_was_captured(self):
        self.mark()
        self.deliver()
        with open(self.session / "events.jsonl", "a") as fh:
            fh.write(json.dumps({"event_type": "mark_recording", "mark_id": ID_A + ".request",
                                 "state": "observation_only"}) + "\n")
        self.deliver()
        self.assertIn("no recording", self.op_state()["detail"])

    def test_no_session_is_reported_and_does_not_stop_the_display(self):
        self.pub.handle_mark({"id": ID_A, "label": "police", "age_s": 0, "phone_utc": 1793814741.0})
        self.assertEqual(self.pub.acks[-1]["state"], "failed")

    def test_a_label_the_detector_tool_would_refuse_is_refused(self):
        self.pub.heartbeat.poll(time.monotonic())
        self.pub.handle_mark({"id": ID_A, "label": "tank", "age_s": 0, "phone_utc": 1793814741.0})
        self.assertEqual(self.pub.acks[-1]["state"], "failed")
        self.assertEqual(self.requests() if (self.session / "marks").exists() else [], [])

    def test_a_malformed_mark_id_from_the_server_is_ignored(self):
        self.pub.heartbeat.poll(time.monotonic())
        self.sender.pending_ops.append({"kind": "mark", "id": "../../evil", "label": "police"})
        self.assertTrue(pub.handle_ops(self.sender, self.pub, self.cfg))
        self.assertFalse((self.session / "marks").exists())


class PowerOff(Base):
    def recorder(self, returncode=0, stderr=""):
        self.calls = []

        def run(cmd):
            self.calls.append(cmd)
            return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)
        return run

    def test_the_argv_is_exactly_what_sudoers_grants_and_is_never_a_reboot(self):
        root = Path(__file__).resolve().parent.parent
        grants = [ln for ln in (root / "deploy/61-slot17-poweroff.sudoers").read_text().splitlines()
                  if ln and not ln.startswith("#")]
        self.assertEqual(len(grants), 1)
        self.assertTrue(grants[0].endswith("/usr/bin/systemctl " + " ".join(pub.POWEROFF_ARGV[3:])))
        self.assertNotIn("reboot", grants[0])
        self.assertEqual(pub.POWEROFF_ARGV[:2], ["sudo", "-n"])

    def test_it_reports_before_powering_off_and_winds_up(self):
        self.pub.heartbeat.poll(time.monotonic())
        order = []
        self.pub.push_now = lambda now: order.append(("push", list(self.pub.acks)))
        run = self.recorder()
        keep = pub.run_poweroff({"id": ID_A, "age_s": 1.0}, self.pub,
                                runner=lambda cmd: (order.append(("run", cmd)), run(cmd))[1])
        self.assertFalse(keep)
        self.assertEqual(order[0][0], "push")
        self.assertEqual(order[0][1][-1]["state"], "done")
        self.assertEqual(order[1], ("run", pub.POWEROFF_ARGV))

    def test_an_old_request_is_refused_on_the_pi_too(self):
        run = self.recorder()
        self.assertTrue(pub.run_poweroff({"id": ID_A, "age_s": pub.POWEROFF_MAX_AGE_S + 1}, self.pub, runner=run))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.pub.acks[-1]["state"], "failed")

    def test_a_request_with_no_age_is_refused(self):
        run = self.recorder()
        self.assertTrue(pub.run_poweroff({"id": ID_A}, self.pub, runner=run))
        self.assertEqual(self.calls, [])

    def test_a_refusal_is_reported_and_keeps_the_display_running(self):
        self.pub.heartbeat.poll(time.monotonic())
        self.pub.push_now = lambda now: True
        keep = pub.run_poweroff({"id": ID_A, "age_s": 1.0}, self.pub,
                                runner=self.recorder(returncode=1, stderr="a password is required"))
        self.assertTrue(keep)
        self.assertEqual(self.pub.acks[-1]["state"], "failed")
        self.assertIn("password", self.pub.acks[-1]["detail"])

    def test_power_off_arrives_through_the_real_server_once(self):
        self.cycle()                               # declares the feature
        self.op(kind="poweroff", id=ID_A)
        self.pub.last_hb_poll = self.pub.last_push = -1e9
        self.pub.step()
        self.assertEqual([o["kind"] for o in self.sender.pending_ops], ["poweroff"])
        self.sender.pending_ops.clear()
        self.pub.last_push = -1e9
        self.pub.step()
        self.assertEqual(list(self.sender.pending_ops), [])


class Logs(Base):
    def test_the_app_log_is_stored_under_a_name_the_server_chooses(self):
        status, body = self.req("POST", "/api/app-log", b"line one\n", headers={"X-Name": "../../evil"})
        self.assertEqual(status, 200)
        name = json.loads(body)["name"]
        self.assertRegex(name, srv.LOG_NAME)
        self.assertEqual((self.logs / name).read_bytes(), b"line one\n")

    def test_uploads_need_the_right_secret(self):
        self.assertEqual(self.req("POST", "/api/app-log", b"x", cookie=False)[0], 401)
        self.assertEqual(self.req("POST", "/api/pi-log", b"x", cookie=True)[0], 401)
        self.assertEqual(self.req("POST", "/api/pi-log", b"x", cookie=False, token=True)[0], 200)
        self.assertEqual(len(list(self.logs.iterdir())), 1)

    def test_an_oversized_log_is_refused(self):
        self.assertEqual(self.req("POST", "/api/app-log", b"x" * (srv.MAX_LOG_BODY + 1))[0], 413)
        self.assertEqual(self.req("POST", "/api/app-log", b"x" * srv.MAX_LOG_BODY)[0], 200)

    def test_other_endpoints_keep_the_small_limit(self):
        self.assertEqual(self.req("POST", "/api/op", b"x" * (srv.MAX_BODY + 1))[0], 413)

    def test_listing_and_reading_need_the_viewer_key(self):
        name = json.loads(self.req("POST", "/api/app-log", b"secret-ish")[1])["name"]
        self.assertEqual(self.req("GET", "/api/logs", cookie=False)[0], 401)
        self.assertEqual(self.req("GET", f"/api/logs/{name}", cookie=False)[0], 401)
        self.assertEqual(json.loads(self.req("GET", "/api/logs")[1])["logs"][0]["name"], name)
        self.assertEqual(self.req("GET", f"/api/logs/{name}")[1], b"secret-ish")

    def test_a_path_cannot_escape_the_log_folder(self):
        (self.dir / "config.json").write_text("{}")
        for path in ("/api/logs/../config.json", "/api/logs/..%2Fconfig.json", "/api/logs/config.json",
                     "/api/logs/app-20260101-000000Z-00000000.log"):
            self.assertEqual(self.req("GET", path)[0], 404, path)

    def test_the_folder_is_bounded(self):
        for _ in range(5):
            srv.save_log(self.logs, "app", b"x" * 10)
        original = srv.LOG_KEEP_FILES
        srv.LOG_KEEP_FILES = 3
        try:
            newest = srv.save_log(self.logs, "app", b"x" * 10)
        finally:
            srv.LOG_KEEP_FILES = original
        names = [p.name for p in self.logs.iterdir()]
        self.assertEqual(len(names), 3)
        self.assertIn(newest, names)

    def test_secrets_are_removed_from_a_pi_log(self):
        text = 'token: abc123 "password":"hunter2" Authorization: Bearer xyz999 keep this'
        cleaned = pub.redact(text)
        for secret in ("abc123", "hunter2", "xyz999"):
            self.assertNotIn(secret, cleaned)
        self.assertIn("keep this", cleaned)

    def test_a_pi_log_is_bounded_and_survives_a_broken_journal(self):
        (self.session / "summary.json").write_text("{" + "x" * 2000000)

        def broken(_):
            raise OSError("no journal")
        body = pub.collect_pi_log(self.session, self.hb, runner=broken)
        self.assertLessEqual(len(body), pub.LOG_UPLOAD_CAP)
        self.assertIn(b"journal: unavailable", body)

    def test_the_whole_request_goes_from_app_to_saved_log(self):
        self.cycle()
        self.assertEqual(self.op(kind="logs", id=ID_A)[0], 200)
        self.pub.last_hb_poll = self.pub.last_push = -1e9
        self.pub.step()
        op = self.sender.pending_ops.popleft()
        collect = lambda session, hb: b"journal text"   # noqa: E731
        pub.upload_pi_log(op, self.pub, self.cfg, collect=collect).join(10)
        self.cycle()
        got = self.op_state()
        self.assertEqual(got["state"], "done")
        name = got["detail"].split()[-1]
        self.assertEqual(self.req("GET", f"/api/logs/{name}")[1], b"journal text")

    def test_a_failed_upload_is_reported(self):
        collect = lambda session, hb: b"x"   # noqa: E731
        cfg = {"url": "http://127.0.0.1:9/api/push", "token": self.TOKEN}
        pub.upload_pi_log({"id": ID_A}, self.pub, cfg, collect=collect).join(10)
        self.assertEqual(self.pub.acks[-1]["state"], "failed")


class RemoteSettings(Base):
    """The app reads its sounds, rules and timings from here, so they change without an app update."""

    def handler(self, config_text=None, sounds=None):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.config = self.dir / "app-config.json"
        if config_text is not None:
            self.config.write_text(config_text)
        self.sounds = self.dir / "sounds"
        self.sounds.mkdir(exist_ok=True)
        for name, data in (sounds or {}).items():
            (self.sounds / name).write_bytes(data)
        self.httpd = srv.CappedServer(("127.0.0.1", 0), srv.make_handler(
            {"pi_token": self.TOKEN, "viewer_key": self.KEY, "pi_status_key": self.STATUS_KEY},
            self.store, b"<html>__NONCE__</html>",
            log_dir=self.logs, config_path=self.config, sound_dir=self.sounds))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def test_the_shipped_settings_are_valid_json_with_today_s_values(self):
        shipped = json.loads((Path(__file__).resolve().parent / "app-config.json").read_text())
        self.assertEqual(shipped["rules"]["red_db"], 35)
        self.assertEqual(shipped["rules"]["urgent_db"], 50)
        self.assertEqual(set(shipped["sounds"]), {"urgent", "chime", "warning", "link_lost"})

    def test_settings_need_the_viewer_key_and_carry_a_version(self):
        self.handler('{"rules": {"red_db": 25}}')
        self.assertEqual(self.req("GET", "/api/config", cookie=False)[0], 401)
        status, body = self.req("GET", "/api/config")
        self.assertEqual(status, 200)
        got = json.loads(body)
        self.assertEqual(got["config"]["rules"]["red_db"], 25)
        self.assertRegex(got["version"], "^[0-9a-f]{12}$")

    def test_a_change_to_the_file_is_served_without_a_restart(self):
        self.handler('{"rules": {"red_db": 25}}')
        first = json.loads(self.req("GET", "/api/config")[1])
        time.sleep(0.05)
        self.config.write_text('{"rules": {"red_db": 31}, "message": "new"}')
        second = json.loads(self.req("GET", "/api/config")[1])
        self.assertEqual(second["config"]["rules"]["red_db"], 31)
        self.assertNotEqual(first["version"], second["version"])

    def test_a_broken_file_is_never_served(self):
        for text in ('{"rules": ', '[1, 2]', ' '):
            self.handler(text)
            status, body = self.req("GET", "/api/config")
            self.assertEqual(status, 404, text)
            self.assertIn("error", json.loads(body))

    def test_no_file_means_the_app_keeps_its_own_defaults(self):
        self.handler()
        self.assertEqual(self.req("GET", "/api/config")[0], 404)

    def test_an_oversized_file_is_refused(self):
        self.handler('{"message": "' + "x" * srv.APP_CONFIG_MAX + '"}')
        self.assertEqual(self.req("GET", "/api/config")[0], 404)

    def test_sounds_are_served_by_safe_name_only(self):
        self.handler("{}", sounds={"soft_bell.wav": b"RIFF....WAVEfmt ", "Bad Name.wav": b"x"})
        self.assertEqual(self.req("GET", "/api/sounds/soft_bell.wav", cookie=False)[0], 401)
        status, body = self.req("GET", "/api/sounds/soft_bell.wav")
        self.assertEqual((status, body[:4]), (200, b"RIFF"))
        (self.dir / "secret.wav").write_bytes(b"secret")
        for path in ("/api/sounds/Bad%20Name.wav", "/api/sounds/../secret.wav", "/api/sounds/..%2Fsecret.wav",
                     "/api/sounds/missing.wav", "/api/sounds/soft_bell.mp3"):
            self.assertEqual(self.req("GET", path)[0], 404, path)


class Telemetry(Base):
    def test_pi_readings_reach_the_phone_validated(self):
        self.pub.pi_health = {}
        self.cycle()
        state = json.loads(self.req("GET", "/api/state")[1])["state"]
        self.assertEqual(state["pi"]["session_s"], 12.0)     # 4 windows of 3 s, not from any clock
        self.assertEqual(state["features"], pub.FEATURES)

    def test_out_of_range_readings_are_refused(self):
        with self.assertRaises(ValueError):
            srv.clean_state({"v": 3, "source": "s", "ticket": "x", "snapshot_age_s": 0, "seq": 1, "session": "s",
                             "detector": {"state": "running"}, "channels": [], "matches": [],
                             "pi": {"temp_c": 9999}})

    def test_unknown_readings_are_dropped(self):
        state = srv.clean_state({"v": 3, "source": "s", "ticket": "x", "snapshot_age_s": 0, "seq": 1, "session": "s",
                                 "detector": {"state": "running"}, "channels": [], "matches": [],
                                 "pi": {"temp_c": 50, "evil": "x"}})
        self.assertEqual(state["pi"], {"temp_c": 50.0})

    def test_acks_do_not_leak_into_what_the_phone_receives(self):
        self.pub.ack(ID_A, "done", "x")
        self.cycle()
        self.assertNotIn("acks", json.loads(self.req("GET", "/api/state")[1])["state"])

    def test_an_ack_added_during_a_push_is_not_lost(self):
        self.pub.heartbeat.poll(time.monotonic())
        sent = []

        def send(payload):
            sent.append(payload)
            self.pub.ack(ID_B, "done", "arrived during the push")
        self.pub.send = send
        self.pub.ack(ID_A, "done", "first")
        self.pub.push(time.monotonic())
        self.assertEqual([a["id"] for a in self.pub.acks], [ID_B])



class PiOnTests(Base):
    def ask(self, token):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", f"/api/pi-on?token={token}")
        response = conn.getresponse()
        body = response.read().decode()
        conn.close()
        return response.status, body.strip()

    def test_off_until_the_pi_pushes_then_on_then_off_again(self):
        self.assertEqual(self.ask(self.STATUS_KEY), (200, "off"))
        with self.store.cond:
            self.store.received_mono = time.monotonic()
        self.assertEqual(self.ask(self.STATUS_KEY), (200, "on"))
        with self.store.cond:
            self.store.received_mono = time.monotonic() - srv.PI_ON_WITHIN_S - 1
        self.assertEqual(self.ask(self.STATUS_KEY), (200, "off"))

    def test_a_wrong_or_missing_key_gets_no_answer(self):
        self.assertEqual(self.ask("wrong")[0], 401)
        self.assertEqual(self.ask("")[0], 401)

    def test_this_key_opens_nothing_else(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/state", headers={"Cookie": f"{srv.COOKIE}={self.STATUS_KEY}"})
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 401)

    def test_with_no_key_configured_nobody_gets_an_answer(self):
        httpd = srv.CappedServer(("127.0.0.1", 0), srv.make_handler(
            {"pi_token": self.TOKEN, "viewer_key": self.KEY}, self.store, b"", log_dir=self.logs))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            conn.request("GET", "/api/pi-on?token=")
            response = conn.getresponse()
            response.read()
            conn.close()
            self.assertEqual(response.status, 401)
        finally:
            httpd.shutdown()
            httpd.server_close()


class BluetoothSwitch(Base):
    """1.4.0: the app switches the car computer's Bluetooth link, renames it, asks for its key, and sees it all."""

    def setUp(self):
        super().setUp()
        self.config = pub.bluetooth_module()
        if self.config is None:
            self.skipTest("p0/bluetooth_config.py is not here (a display-only copy)")
        self.addCleanup(setattr, self.config, "PATH", self.config.PATH)
        self.config.PATH = str(self.dir / "slot17-bluetooth.json")

        class Quiet(pub.BluetoothLink):
            def _spawn(self):
                return SimpleNamespace(poll=lambda: None, stdin=SimpleNamespace(close=lambda: None), stdout=[],
                                       wait=lambda timeout=None: 0, kill=lambda: None, returncode=0)

            def _write(self, data):
                pass
        self.pub.bluetooth = Quiet(config=self.config)

    def reported(self):
        self.pub.bluetooth.settings_at = -1e9
        self.cycle()
        return json.loads(self.req("GET", "/api/state")[1])["state"].get("bluetooth")

    def answer(self, op_id, **fields):
        self.assertEqual(self.op(kind="bluetooth", id=op_id, **fields)[0], 200)
        self.cycle()
        self.cycle()
        return self.op_state(op_id)

    def test_switch_rename_and_pair_from_the_app(self):
        self.cycle()
        self.assertIn("bluetooth", self.store.features)
        self.assertEqual(self.reported()["on"], False)
        self.assertEqual(self.answer(ID_A, on=True)["detail"], "Bluetooth on")
        shown = self.reported()
        self.assertEqual((shown["on"], shown["name"]), (True, "Slot17"))
        self.assertRegex(shown["key_id"], "^[0-9a-f]{8}$")
        self.assertEqual(self.answer(ID_B, name="Car 2")["detail"], "Bluetooth name: Car 2")
        self.assertEqual(self.reported()["name"], "Car 2")
        paired = self.answer("d" * 32, pair=True)
        self.assertEqual(paired["detail"], "key:" + self.config.load()["key"].hex())
        self.assertNotIn(self.config.load()["key"].hex(), json.dumps(self.reported()))

    def test_the_key_never_goes_out_over_bluetooth(self):
        """1.6.0 put the pairing answer, key and all, in the Bluetooth state, which a phone nearby reads without
        the key (SECURITY.md): the server's answer carries it, the Bluetooth state and the file on disk don't."""
        self.cycle()
        self.answer(ID_A, on=True)
        key = self.config.load()["key"].hex()
        self.assertEqual(self.answer("d" * 32, pair=True)["detail"], "key:" + key)
        doc = self.pub.bluetooth_state(time.monotonic())
        self.assertNotIn(key, json.dumps(doc))
        self.assertNotIn("d" * 32, [a["id"] for a in doc["ops"]])
        fed = []
        self.pub.bluetooth.feed = fed.append
        self.pub._feed_state(time.monotonic())
        self.assertTrue(fed)
        for line in fed:
            self.assertNotIn(key, zlib.decompress(base64.b64decode(line["state"]), -15).decode("utf-8"))
        # Asked again with the same id (the phone missed the answer), it runs again rather than "running".
        self.assertIsNone(self.pub.ops_seen.seen("d" * 32))
        self.assertEqual(self.answer("d" * 32, pair=True)["detail"], "key:" + key)

    def test_answers_on_disk_never_hold_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ops-seen.json"
            path.write_text(json.dumps([["e" * 32, "done", "Bluetooth on"], ["f" * 32, "done", "key:" + "ab" * 32]]),
                            encoding="utf-8")
            memory = pub.OpMemory(path)          # 1.6.0's file: the key in it goes, the rest stays
            self.assertEqual(memory.seen("e" * 32), ("done", "Bluetooth on"))
            self.assertIsNone(memory.seen("f" * 32))
            self.assertNotIn("key:", path.read_text(encoding="utf-8"))
            self.pub.ops_seen = memory
            self.cycle()
            self.answer(ID_A, on=True)
            self.answer("c" * 32, pair=True)
            saved = path.read_text(encoding="utf-8")
            self.assertNotIn(self.config.load()["key"].hex(), saved)
            self.assertIn(ID_A, saved)

    def test_only_one_well_formed_request_is_accepted(self):
        for wrong in ({"on": "yes"}, {"name": "a;b"}, {"name": " x"}, {"name": "x" * 21}, {"pair": False},
                      {"on": True, "pair": True}, {}):
            self.assertEqual(self.op(kind="bluetooth", id=ID_A, **wrong)[0], 400, wrong)

    def test_an_older_car_computer_is_never_sent_it(self):
        self.store.features = ["mark", "logs", "notify"]
        self.assertEqual(self.op(kind="bluetooth", id=ID_A, on=True)[0], 200)
        self.assertEqual(self.store.take_ops(), [])

    def test_a_malformed_report_is_dropped_not_refused(self):
        state = {"v": 3, "detector": {"state": "running"}, "channels": [], "matches": [], "source": "s",
                 "ticket": "t", "snapshot_age_s": 0, "seq": 1}
        for wrong in ("on", {"on": "yes"}, {"state": "connected"}):
            self.assertNotIn("bluetooth", srv.clean_state(dict(state, bluetooth=wrong)))
        cleaned = srv.clean_state(dict(state, bluetooth={
            "on": True, "state": "nonsense", "name": "a;b", "key_id": "XYZ", "frames": -1, "mtu": 9000,
            "since_s": 12.34, "error": "e" * 500, "extra": "dropped"}))["bluetooth"]
        self.assertEqual(cleaned, {"on": True, "state": "off", "subscribed": False, "since_s": 12.3,
                                   "error": "e" * 120})


def fake_team_inbox(folder):
    """Stands in for the Slot17 team's inbox at support.slot17.cloud, as far as a display server sees it: a
    package in with the consent header gives a code and a token; the token deletes it again."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import secrets

    class Inbox(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def answer(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.path != "/v1/packages" or self.headers.get("X-Slot17-Consent") != "yes" or not body.startswith(b"PK"):
                return self.answer(400, {"error": "refused"})
            code = "S17-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
            token = secrets.token_urlsafe(16)
            (folder / f"{code}.zip").write_bytes(body)
            (folder / f"{code}.token").write_text(token)
            self.answer(200, {"code": code, "delete_token": token, "expires_utc": "2026-12-22T20:00:00Z"})

        def do_DELETE(self):
            code = self.path.rsplit("/", 1)[-1]
            token_file = folder / f"{code}.token"
            if not token_file.exists():
                return self.answer(404, {"deleted": False})
            if self.headers.get("X-Delete-Token") != token_file.read_text():
                return self.answer(403, {"error": "wrong token"})
            for f in (folder / f"{code}.zip", token_file):
                f.unlink()
            self.answer(200, {"deleted": True})

    return ThreadingHTTPServer(("127.0.0.1", 0), Inbox)


class SupportPackages(Base):
    """1.5.0: the app asks for a support package; the car computer makes it and uploads it in pieces; the app
    shares it, deletes it, or sends it to the Slot17 team's inbox, and can take it back from there."""

    def setUp(self):
        self.inbox_dir = tempfile.TemporaryDirectory()
        self.inbox_httpd = fake_team_inbox(Path(self.inbox_dir.name))
        threading.Thread(target=self.inbox_httpd.serve_forever, daemon=True).start()
        self.saved_inbox, srv.TEAM_INBOX = srv.TEAM_INBOX, f"http://127.0.0.1:{self.inbox_httpd.server_address[1]}/v1/packages"
        self.saved_piece, pub.SUPPORT_PIECE_BYTES = pub.SUPPORT_PIECE_BYTES, 4096      # several pieces per package
        super().setUp()
        self.made = []
        self.pub.support = pub.SupportJob(run=self.make)

    def tearDown(self):
        super().tearDown()
        srv.TEAM_INBOX, pub.SUPPORT_PIECE_BYTES = self.saved_inbox, self.saved_piece
        self.inbox_httpd.shutdown()
        self.inbox_httpd.server_close()
        self.inbox_dir.cleanup()

    def make(self, args):
        """Stands in for p0/support.py: a real package's two files, and enough besides for several pieces."""
        import os
        import zipfile
        self.made.append(args)
        out = Path(args[args.index("--out") + 1])
        with zipfile.ZipFile(out, "w") as z:
            z.writestr("REPORT.txt", "Slot17 support package\n")
            z.writestr("manifest.json", json.dumps({"software": "1.5.0", "choice": "quick"}))
            z.writestr("journal/boot0.txt", os.urandom(20000).hex())
        return {"file": str(out), "bytes": out.stat().st_size}

    def ask(self, op_id=ID_A, **fields):
        self.cycle()                               # the car computer announces "support"
        self.assertEqual(self.op(kind="support", id=op_id, **fields)[0], 200)
        self.cycle()                               # handed over; the package is made and uploaded in the background
        self.assertTrue(self.pub.support.busy.acquire(timeout=20))
        self.pub.support.busy.release()
        self.cycle()                               # the answer travels back
        return self.op_state(op_id)

    def packages(self, cookie=True):
        status, body = self.req("GET", "/api/support", cookie=cookie)
        return status, json.loads(body).get("packages") if status == 200 else None

    def test_quick_package_from_the_app_to_a_download(self):
        state = self.ask(choice="quick")
        self.assertEqual(state["state"], "done", state)
        name = state["detail"]
        self.assertRegex(name, srv.PACKAGE_NAME)
        self.assertEqual(self.made[0][:2], ["--out", self.made[0][1]])
        rows = self.packages()[1]
        self.assertEqual([r["name"] for r in rows], [name])
        status, data = self.req("GET", f"/api/support/{name}")
        self.assertEqual(status, 200)
        import io
        import zipfile
        self.assertIn("REPORT.txt", zipfile.ZipFile(io.BytesIO(data)).namelist())
        self.assertFalse(list(Path.home().joinpath(".cache").glob(f"slot17-support-{ID_A[:8]}.zip")))  # cleaned up

    def test_a_drive_with_a_window_reaches_support_py(self):
        state = self.ask(choice="everything", session="20261119-171847-301483Z_watch_382.5MHz", window=[30, 50],
                         recordings=True)
        self.assertEqual(state["state"], "done", state)
        self.assertEqual(self.made[0][2:], ["--drive", "20261119-171847-301483Z_watch_382.5MHz", "--window", "30-50",
                                            "--everything", "--recordings"])

    def test_the_phones_files_only_when_asked_and_places_only_with_places(self):
        import io
        import zipfile
        self.assertEqual(self.req("POST", "/api/app-log", b"the app's own log\n")[0], 200)
        self.assertEqual(self.req("POST", "/api/app-log", b'{"slot17-drive-data": 1, "places": []}')[0], 200)
        name = self.ask(choice="quick", phone_log=True)["detail"]
        names = zipfile.ZipFile(io.BytesIO(self.req("GET", f"/api/support/{name}")[1])).namelist()
        self.assertIn("phone/app-log.txt", names)
        self.assertNotIn("phone/drive-data-with-places.json", names)
        name = self.ask(op_id=ID_B, choice="quick", phone_log=True, places=True)["detail"]
        names = zipfile.ZipFile(io.BytesIO(self.req("GET", f"/api/support/{name}")[1])).namelist()
        self.assertIn("phone/drive-data-with-places.json", names)

    def test_sent_to_the_team_only_with_consent_and_taken_back(self):
        name = self.ask(choice="quick")["detail"]
        self.assertEqual(self.req("POST", "/api/support/send", json.dumps({"name": name}))[0], 400)
        status, body = self.req("POST", "/api/support/send", json.dumps({"name": name, "consent": True}))
        self.assertEqual(status, 200, body)
        code = json.loads(body)["code"]
        self.assertTrue((Path(self.inbox_dir.name) / f"{code}.zip").exists())
        self.assertEqual(self.packages()[1][0]["team_code"], code)
        self.assertNotIn("team_token", self.req("GET", "/api/support")[1].decode())   # the token stays on the server
        self.assertEqual(self.req("POST", "/api/support/unsend", json.dumps({"name": name}))[0], 200)
        self.assertFalse((Path(self.inbox_dir.name) / f"{code}.zip").exists())
        self.assertNotIn("team_code", self.packages()[1][0])
        self.assertEqual(self.req("POST", "/api/support/delete", json.dumps({"name": name}))[0], 200)
        self.assertEqual(self.packages()[1], [])

    def test_only_the_viewer_and_the_car_computer(self):
        self.assertEqual(self.packages(cookie=False)[0], 401)
        self.assertEqual(self.req("GET", "/api/support/support-20261122-120000Z-aaaaaaaa.zip", cookie=False)[0], 401)
        status, _ = self.req("POST", "/api/support-package?name=support-20261122-120000Z-aaaaaaaa.zip&offset=0&total=3",
                             b"abc", cookie=False)
        self.assertEqual(status, 401)
        for bad in ({"choice": "all"}, {"choice": "drive"}, {"choice": "drive", "session": "../etc"},
                    {"choice": "drive", "session": "s", "window": [50, 30]}, {"choice": "drive", "session": "s", "window": [0, 9999]}):
            self.assertEqual(self.op(kind="support", id=ID_A, **bad)[0], 400, bad)

    def test_pieces_resume_and_a_wrong_checksum_keeps_nothing(self):
        packages = srv.Packages(self.logs / "support", self.logs)
        name = "support-20261122-120000Z-bbbbbbbb.zip"
        self.assertEqual(packages.append(name, 0, b"abc", 6), 3)
        self.assertEqual(packages.append(name, 0, b"abc", 6), 3)      # a piece sent twice: the size says where to go on
        self.assertEqual(packages.append(name, 3, b"def", 6), 6)
        with self.assertRaises(ValueError):
            packages.finish(name, "0" * 64)
        self.assertEqual(packages.listing(), [])
        with self.assertRaises(ValueError):
            packages.append("../../x.zip", 0, b"a", 1)

    def test_a_long_build_is_not_reported_as_unanswered(self):
        store = srv.Store()
        store.add_op("support", ID_A, {"choice": "quick"})
        store.add_op("doctor", ID_B, {})
        store.features = {"support", "doctor"}                 # as a car computer with 1.5.0 announces
        store.take_ops()
        later = time.monotonic() + srv.OP_ANSWER_S + 60
        store._expire_ops(later)
        states = {op["kind"]: op["state"] for op in store.ops.values()}
        self.assertEqual(states, {"support": "sent", "doctor": "failed"})

    def test_at_most_three_are_kept(self):
        import hashlib
        packages = srv.Packages(self.logs / "support", self.logs)
        for i in range(5):
            name = f"support-20261122-12000{i}Z-{i:08x}.zip"
            packages.append(name, 0, b"zip", 3)
            packages.finish(name, hashlib.sha256(b"zip").hexdigest())
            time.sleep(0.02)
        self.assertEqual(len(packages.listing()), srv.PACKAGE_KEEP)


class OperationsOverBluetooth(Base):
    """1.6.0: the app's operations over Bluetooth, for a car without internet: carried out like the server's,
    once by id whichever route brings them, and answered in the state the phone gets over Bluetooth."""

    class Link:
        def __init__(self):
            self.ops, self.fed, self.listening = [], [], True
            self.last_feed = self.last_state = -1e9
            self.sent_detector, self.was_subscribed, self.dropped = None, False, 0

        def poll(self, now):
            pass

        def running(self):
            return True

        def state(self, now):
            return {"on": True, "state": "connected", "name": "Slot17", "subscribed": True}

        def take_ops(self):
            out, self.ops = self.ops, []
            return out

        def subscribed(self):
            return self.listening

        def feed(self, command):
            self.fed.append(command)
            return True

    def setUp(self):
        super().setUp()
        self.link = self.Link()
        self.pub.bluetooth = self.link
        self.pub.heartbeat.poll(time.monotonic())

    def over_bluetooth(self, **op):
        self.link.ops.append(op)
        return pub.handle_ops(self.sender, self.pub, self.cfg)

    def answer(self, op_id):
        return next(a for a in self.pub._answers(time.monotonic()) if a["id"] == op_id)

    def test_a_sighting_over_bluetooth_is_saved_like_one_from_the_server(self):
        self.assertTrue(self.over_bluetooth(kind="mark", id=ID_A, label="police", note="A12", age_s=1.0,
                                            phone_utc=1793814741.0))
        saved = json.loads((self.session / "marks" / (ID_A + ".request.json")).read_text())
        self.assertIn("A12", saved["note"])
        self.assertEqual(self.answer(ID_A)["state"], "sent", "until the detector reports it")

    def test_the_same_sighting_by_both_routes_is_saved_once(self):
        self.over_bluetooth(kind="mark", id=ID_A, label="police", age_s=1.0, phone_utc=1793814741.0)
        self.pub.ack(ID_A, "done", "Saved with recording (queued)")
        (self.session / "marks" / (ID_A + ".request.json")).replace(self.session / "marks" / (ID_A + ".x"))
        self.assertEqual(self.mark()[0], 200)                 # the phone also queued it on the server
        self.cycle()
        self.cycle()
        self.assertFalse((self.session / "marks" / (ID_A + ".request.json")).exists(), "not saved a second time")
        self.assertEqual(self.op_state()["detail"], "Saved with recording (queued)", "the server hears the answer")

    def test_answers_are_remembered_across_a_restart(self):
        path = self.dir / "ops-seen.json"
        first = pub.OpMemory(path)
        first.start(ID_B)
        first.answer(ID_B, "done", "Pushover messages off")
        first.start(ID_A)                                   # still running when the process ended
        again = pub.OpMemory(path)
        self.assertEqual(again.seen(ID_B), ("done", "Pushover messages off"))
        self.assertIsNone(again.seen(ID_A), "carried out again, as before 1.6.0")

    def test_what_is_refused_over_bluetooth(self):
        for op_id, op, why in (("1" * 32, {"kind": "bluetooth", "pair": True}, "only comes through the server"),
                               ("2" * 32, {"kind": "reboot"}, "Not something"),
                               ("3" * 32, {"kind": "mark", "label": "police", "note": "n" * 501}, "too long"),
                               ("4" * 32, {"kind": "poweroff", "age_s": "soon"}, "Unknown age")):
            self.assertTrue(self.over_bluetooth(id=op_id, **op))
            self.assertEqual(self.answer(op_id)["state"], "failed")
            self.assertIn(why, self.answer(op_id)["detail"])

    def test_stop_over_bluetooth_answers_first_then_winds_up(self):
        calls = []
        real = pub.run_stop_command
        self.addCleanup(setattr, pub, "run_stop_command", real)
        pub.run_stop_command = lambda command, p, runner=None: (calls.append(command), False)[1]
        self.assertFalse(self.over_bluetooth(kind="stop", id=ID_A))
        self.assertEqual(calls[0]["action"], "stop")
        self.assertEqual(self.answer(ID_A)["state"], "done")
        self.assertTrue(any("state" in c for c in self.link.fed), "the answer went out before the end")

    def test_the_pi_log_goes_to_the_phone_that_asked(self):
        import zlib
        home = self.dir / "home"
        self.addCleanup(setattr, pub.Path, "home", pub.Path.home)
        pub.Path.home = staticmethod(lambda: home)
        thread = pub.send_pi_log_over_bluetooth({"id": ID_A, "kind": "logs", "to": "phone"}, self.pub,
                                                collect=lambda session, hb: b"journal lines\n")
        thread.join(5)
        path = pub.Path(self.link.fed[-1]["file"]["path"])
        self.assertEqual(path.parent, home / ".cache")
        doc = json.loads(zlib.decompress(path.read_bytes(), -15))
        self.assertEqual(doc, {"v": 1, "op": ID_A, "kind": "pi-log", "text": "journal lines\n"})
        self.assertEqual(self.pub.acks[-1]["state"], "done")
        self.link.listening = False
        pub.send_pi_log_over_bluetooth({"id": ID_B, "kind": "logs", "to": "phone"}, self.pub)
        self.assertIn("No phone", self.pub.acks[-1]["detail"])


if __name__ == "__main__":
    unittest.main()
