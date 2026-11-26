"""The car computer's own screen (p0/screen.py) with the real publisher: no server between them.

Run: python -m unittest display/test_screen.py. Nothing here stops, powers off or opens anything: the stop
command is replaced with a recorder.
"""
import http.client
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
import unittest.mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
import display_publisher as pub  # noqa: E402
import screen as scr             # noqa: E402
from test_app_backend import Base, ID_A, ID_B  # noqa: E402


class ScreenTests(Base):
    def setUp(self):
        super().setUp()
        self.sdir = self.dir / "screen"
        self.settings = self.dir / "screen.json"
        self.unit = {"ActiveState": "inactive", "Result": "success"}
        self.screen = scr.Screen(self.sdir, self.settings, systemctl=lambda: dict(self.unit))
        self.pub.screen = pub.ScreenLink(self.sdir)
        self.page = self.dir / "page.html"
        self.page.write_text("<html><script nonce=\"__NONCE__\"></script>THE PAGE</html>", encoding="utf-8")
        self.shttpd = scr.Server(("127.0.0.1", 0), scr.make_handler(self.screen, self.page))
        self.sport = self.shttpd.server_address[1]
        threading.Thread(target=self.shttpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.shttpd.shutdown()
        self.shttpd.server_close()
        super().tearDown()

    def sreq(self, method, path, body=None, host=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.sport, timeout=10)
        h = dict(headers or {})
        h["Host"] = host or f"127.0.0.1:{self.sport}"
        if body is not None:
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=None if body is None else json.dumps(body), headers=h)
        resp = conn.getresponse()
        out = resp.status, resp.read(), dict(resp.getheaders())
        conn.close()
        return out

    def state(self):
        return json.loads(self.sreq("GET", "/api/state")[1])

    def burst(self, eid, ch=154, snr=55.0):
        with open(self.session / "events.jsonl", "a") as fh:
            fh.write(json.dumps({"event_type": "candidate", "confirmed": True, "tetra_channel": ch, "event_id": eid,
                                 "frequency_hz": 380.0125e6 + ch * 25e3, "peak_snr_db": snr, "strength_reliable": True,
                                 "burst_mono": time.monotonic(), "matched_timing_refined": True,
                                 "matched_burst": {"rule": "standard"}}) + "\n")

    def test_a_match_reaches_the_screen_with_no_server_in_between(self):
        self.cycle()
        self.burst("e1")
        self.cycle()
        msg = self.state()
        self.assertEqual(msg["state"]["source"], "screen")
        self.assertEqual(msg["state"]["detector"]["state"], "running")
        self.assertLess(msg["server_age_s"], 2.0)
        self.assertEqual([(m["ch"], m["strength_db"], m.get("rule")) for m in msg["state"]["matches"]], [(154, 55.0, "standard")])
        self.assertEqual(msg["state"]["channels"][0]["ch"], 154)
        self.assertIn("mark", msg["state"]["features"])
        # The same match again in the next state (a page that reloads still has it); the page keeps it once by id.
        self.cycle()
        self.assertEqual(len(self.state()["state"]["matches"]), 1)

    def test_the_screen_folder_is_only_written_while_its_service_runs(self):
        for path in (self.sdir / "ops").iterdir():
            path.unlink()
        (self.sdir / "ops").rmdir()
        self.pub.screen.checked = -1e9
        self.cycle()
        self.assertFalse((self.sdir / "state.json").exists())

    def test_a_sighting_on_the_screen_is_saved_like_the_apps_and_answered_there(self):
        self.cycle()
        status, body, _ = self.sreq("POST", "/api/op", {"kind": "mark", "id": ID_A, "label": "police", "age_s": 0,
                                                       "note": "from the web page", "phone_utc": time.time()})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["state"], "pending")
        self.assertTrue((self.sdir / "ops" / f"{ID_A}.json").exists())
        self.cycle()                                    # the publisher takes it, saves it, answers
        self.assertFalse((self.sdir / "ops" / f"{ID_A}.json").exists())
        saved = self.session / "marks" / f"{ID_A}.request.json"     # where the detector collects a sighting
        self.assertEqual(json.loads(saved.read_text())["label"], "police")
        self.cycle()
        answer = next(o for o in self.state()["ops"] if o["id"] == ID_A)
        self.assertIn(answer["state"], ("sent", "done"))
        # Offered again by the server route as well: carried out once (by id), answered as before.
        self.cycle()
        self.assertEqual(len(list((self.session / "marks").glob("*.json"))), 1)

    def test_stop_from_the_screen_is_carried_out_and_reported(self):
        self.cycle()
        status, body, _ = self.sreq("POST", "/api/command", {"action": "stop", "note": "from the car computer's screen"})
        self.assertEqual(status, 202)
        self.assertEqual(self.state()["command"]["state"], "pending")
        with unittest.mock.patch.object(pub, "run_stop_command", return_value=False) as stop:
            keep_going = self.cycle()
        self.assertFalse(keep_going)                    # the publisher winds up, as for the app's stop
        self.assertEqual(stop.call_args[0][0]["note"], "from the car computer's screen")
        self.assertEqual(self.state()["command"]["state"], "done")

    def test_the_link_key_never_comes_through_the_screen(self):
        status, _, _ = self.sreq("POST", "/api/op", {"kind": "bluetooth", "id": ID_B, "pair": True})
        self.assertEqual(status, 400)
        self.assertFalse((self.sdir / "ops" / f"{ID_B}.json").exists())
        for bad in ({"kind": "reboot", "id": ID_B}, {"kind": "mark", "id": "nothex", "label": "police"},
                    {"kind": "mark", "id": ID_B, "label": "police", "note": "x" * 501},
                    {"kind": "mark", "id": ID_B, "label": "spaceship"}):
            self.assertEqual(self.sreq("POST", "/api/op", bad)[0], 400, bad)

    def test_only_this_computer_may_use_it(self):
        self.assertEqual(self.sreq("GET", "/api/state", host="evil.example:5733")[0], 403)
        self.assertEqual(self.sreq("GET", "/", host=f"attacker.test:{self.sport}")[0], 403)
        status, _, _ = self.sreq("POST", "/api/op", {"kind": "poweroff", "id": ID_B},
                                 headers={"Origin": "http://evil.example"})
        self.assertEqual(status, 400)
        self.assertFalse((self.sdir / "ops" / f"{ID_B}.json").exists())

    def test_the_page_comes_with_a_fresh_nonce_and_its_policy(self):
        status, body, headers = self.sreq("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"THE PAGE", body)
        self.assertNotIn(b"__NONCE__", body)
        nonce = body.split(b'nonce="')[1].split(b'"')[0].decode()
        self.assertIn(f"script-src 'nonce-{nonce}'", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("font-src data:", headers["Content-Security-Policy"])        # the page carries its own fonts

    def test_one_bar_or_three_is_kept_on_the_car_computer(self):
        self.assertEqual(json.loads(self.sreq("GET", "/api/screen")[1]), {"screen": True, "bars": 1})
        self.assertEqual(self.sreq("POST", "/api/screen", {"bars": 3})[0], 204)
        self.assertEqual(json.loads(self.settings.read_text()), {"bars": 3})
        self.assertEqual(json.loads(self.sreq("GET", "/api/screen")[1]), {"screen": True, "bars": 3})
        for wrong in ({"bars": 2}, {"bars": True}, {"bars": "3"}, {"bars": 3, "x": 1}, {"mode": "cockpit"}, [3]):
            self.assertEqual(self.sreq("POST", "/api/screen", wrong)[0], 400, wrong)
        self.assertEqual(json.loads(self.settings.read_text()), {"bars": 3})

    def test_drive_or_cockpit_is_no_longer_kept_the_screen_opens_in_drive_mode(self):
        # What release 31 to 33 kept: the page now opens in drive mode at every start, and this reads as one bar.
        self.settings.write_text('{"mode": "cockpit"}')
        self.assertEqual(json.loads(self.sreq("GET", "/api/screen")[1]), {"screen": True, "bars": 1})

    def test_with_no_publisher_the_screen_says_why(self):
        msg = self.state()
        self.assertEqual(msg["state"]["detector"]["state"], "stopped")
        self.assertIn("not running", msg["state"]["detector"]["reasons"][0])
        # Fresh each second, so the page shows the reason, not one old snapshot growing older.
        time.sleep(1.05)
        self.assertGreater(self.state()["seq"], msg["seq"])
        # The detector runs but nothing reaches the screen (no publisher, or one older than the screen): not "stopped".
        self.unit = {"ActiveState": "active", "Result": "success"}
        self.screen.unit = (None, -1e9)
        running = self.state()["state"]["detector"]
        self.assertEqual(running["state"], "no-data")
        self.assertIn("older than this screen", running["reasons"][0])
        self.unit = {"ActiveState": "failed", "Result": "exit-code"}
        self.screen.unit = (None, -1e9)
        self.assertIn("fault (exit-code)", self.state()["state"]["detector"]["reasons"][0])
        # Once the publisher writes, its state is shown; when it stops writing, the reason comes back with the last details.
        self.cycle()
        self.assertEqual(self.state()["state"]["detector"]["state"], "running")
        doc = json.loads((self.sdir / "state.json").read_text())
        doc["mono"] -= 60
        (self.sdir / "state.json").write_text(json.dumps(doc))
        stale = self.state()["state"]
        self.assertEqual(stale["detector"]["state"], "stopped")
        self.assertIn("pi", stale)

    def test_the_stream_sends_a_new_state_at_once(self):
        self.cycle()
        conn = http.client.HTTPConnection("127.0.0.1", self.sport, timeout=10)
        conn.request("GET", "/api/stream", headers={"Host": f"127.0.0.1:{self.sport}"})
        resp = conn.getresponse()
        first = json.loads(resp.readline().decode().removeprefix("data: "))
        resp.readline()
        self.burst("e9", ch=60, snr=30.0)
        started = time.monotonic()
        self.cycle()
        while True:
            line = resp.readline().decode()
            if line.startswith("data: ") and json.loads(line[6:])["seq"] > first["seq"]:
                break
        self.assertLess(time.monotonic() - started, 1.0)
        conn.close()

    @unittest.skipUnless(hasattr(os, "getuid"), "folder ownership is a Unix check")
    def test_a_folder_others_can_write_is_not_used(self):
        os.chmod(self.sdir, 0o755)
        self.assertFalse(scr.owned(self.sdir))
        self.pub.screen.checked = -1e9
        self.assertFalse(self.pub.screen.active(time.monotonic()))
        with self.assertRaises(SystemExit):
            scr.Screen(self.sdir, self.settings)


if __name__ == "__main__":
    unittest.main()
