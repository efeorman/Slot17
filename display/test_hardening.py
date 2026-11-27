"""Regressions from the November 2026 quality review: limits, races and clean refusals.

Everything runs on loopback with made-up keys. Run: python -m unittest display/test_hardening.py
"""
import http.client
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import display_publisher as pub  # noqa: E402
import server as srv             # noqa: E402

KEY, TOKEN = "k" * 24, "t" * 40
ID = "a" * 32


class Live(unittest.TestCase):
    MAX_THREADS = 8
    PER_ADDRESS = 3
    HEADER_DEADLINE = 1.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = srv.Store()
        self.httpd = srv.CappedServer(("127.0.0.1", 0), srv.make_handler(
            {"pi_token": TOKEN, "viewer_key": KEY}, self.store, b"<html></html>", log_dir=Path(self.tmp.name) / "logs"),
            max_threads=self.MAX_THREADS, max_per_address=self.PER_ADDRESS, header_deadline=self.HEADER_DEADLINE)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def get(self, path="/healthz", cookie=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request("GET", path, headers={"Cookie": f"s17={KEY}"} if cookie else {})
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def post(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            conn.request("POST", path, body=body, headers={"Cookie": f"s17={KEY}"})
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def attacker(self, source="127.0.0.2"):
        s = socket.socket()
        s.settimeout(3)
        s.bind((source, 0))
        s.connect(("127.0.0.1", self.port))
        return s


class ConnectionLimits(Live):
    def test_one_address_cannot_take_every_connection(self):
        socks = [self.attacker() for _ in range(self.MAX_THREADS + 4)]
        time.sleep(0.2)
        try:
            self.assertEqual(self.get()[0], 200, "another address still gets in")
        finally:
            for s in socks:
                s.close()

    def test_dripped_headers_are_cut_off_at_the_deadline(self):
        s = self.attacker()
        s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")
        started = time.monotonic()
        try:
            while time.monotonic() - started < self.HEADER_DEADLINE + 3:
                s.sendall(b"X-a: b\r\n")         # always well inside the idle limit
                time.sleep(0.2)
            self.fail("still connected after the deadline")
        except OSError:
            pass
        finally:
            s.close()
        self.assertLess(time.monotonic() - started, self.HEADER_DEADLINE + 2.5)

    def test_slots_come_back_after_closed_connections(self):
        for _ in range(3):
            socks = [self.attacker() for _ in range(self.PER_ADDRESS)]
            for s in socks:
                s.close()
            time.sleep(0.3)
        deadline = time.monotonic() + 3
        while self.httpd.per_address and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(self.httpd.per_address, {}, "nothing is left counted")

    def test_a_normal_request_is_not_cut_off(self):
        time.sleep(self.HEADER_DEADLINE + 0.8)
        self.assertEqual(self.get()[0], 200)

    def test_a_kept_open_connection_gets_a_fresh_deadline_for_each_request(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        for _ in range(3):
            conn.request("GET", "/healthz")
            self.assertEqual(conn.getresponse().read(), b"ok\n")
            time.sleep(self.HEADER_DEADLINE * 0.5)
        conn.close()

    def test_an_idle_kept_open_connection_is_closed_and_a_new_one_works(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request("GET", "/healthz")
        conn.getresponse().read()
        time.sleep(self.HEADER_DEADLINE + 1.0)
        conn.close()
        self.assertEqual(self.get()[0], 200)


class Refusals(Live):
    def test_wrong_json_types_get_a_refusal_not_a_dropped_connection(self):
        for body in ('{"kind":[]}', '{"kind":{}}', '{"kind":1}', '{"kind":"mark","id":"%s","label":[]}' % ID,
                     '{"kind":"mark","id":"%s","label":{"a":1}}' % ID, '{"kind":"logs","id":[]}', "[]", "null", "1", '"x"'):
            self.assertEqual(self.post("/api/op", body)[0], 400, body)

    def test_a_malformed_request_line_does_not_crash_the_handler(self):
        errors = []
        original = self.httpd.handle_error
        self.httpd.handle_error = lambda *_args: errors.append(sys.exc_info()[1])
        try:
            for data in (b"GET\r\n\r\n", b"GET / HTTP/9.9\r\n\r\n", b"\x00\xff\xfe\r\n\r\n", b"GET " + b"/" * 70000 + b" HTTP/1.1\r\n\r\n"):
                s = self.attacker()
                s.sendall(data)
                try:
                    s.recv(100)
                except OSError:
                    pass
                s.close()
            time.sleep(0.4)
        finally:
            self.httpd.handle_error = original
        self.assertEqual(errors, [])

    def test_a_key_for_the_on_or_off_answer_opens_nothing_else(self):
        self.assertEqual(self.get("/api/pi-on?token=")[0], 401)

    def test_no_path_escapes_the_log_and_sound_folders(self):
        for path in ("/api/logs/../config.json", "/api/logs/%2e%2e%2fconfig.json", "/api/sounds/../config.json",
                     "/api/sounds/..%2f..%2fconfig.json", "/api/sounds/", "/api/logs/app-1.log"):
            self.assertEqual(self.get(path, cookie=True)[0], 404, path)


class KeptConnections(Live):
    def test_a_connection_with_no_key_ends_after_one_answer(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request("GET", "/healthz")
        r = conn.getresponse()
        r.read()
        self.assertTrue(r.will_close, "a client with no key cannot keep a connection")
        conn.close()

    def test_a_connection_with_the_key_stays_open(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request("GET", "/api/state", headers={"Cookie": f"s17={KEY}"})
        r = conn.getresponse()
        r.read()
        self.assertFalse(r.will_close)
        conn.close()

    def test_a_wrong_key_does_not_keep_it_either(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request("GET", "/api/state", headers={"Cookie": "s17=wrong"})
        r = conn.getresponse()
        r.read()
        self.assertEqual(r.status, 401)
        self.assertTrue(r.will_close)
        conn.close()


class OtherSites(Live):
    def post_from(self, origin):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            headers = {"Cookie": f"s17={KEY}"}
            if origin:
                headers["Origin"] = origin
            conn.request("POST", "/api/command", body='{"action":"stop"}', headers=headers)
            r = conn.getresponse()
            r.read()
            return r.status
        finally:
            conn.close()

    def test_a_page_on_another_port_cannot_stop_the_detector(self):
        self.assertEqual(self.post_from("http://127.0.0.1:3006"), 403)
        self.assertEqual(self.post_from("https://example.invalid"), 403)
        self.assertEqual(self.post_from("null"), 403)
        self.assertEqual(self.store.command_state, "none")

    def test_a_malformed_origin_is_refused_without_a_crash(self):
        self.assertEqual(self.post_from("http://[bad"), 403)

    def test_the_page_itself_and_the_app_still_can(self):
        self.assertEqual(self.post_from(f"http://127.0.0.1:{self.port}"), 200)
        self.assertEqual(self.post_from(None), 200)


class OneOffRun(Live):
    def test_a_one_off_publisher_leaves_commands_for_the_real_one(self):
        self.store.request_stop()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {"Authorization": f"Bearer {TOKEN}", "X-Display-Source": "a", "X-Display-Commands": "none"}
        conn.request("GET", "/api/clock", headers=headers)
        body = json.loads(conn.getresponse().read())
        conn.close()
        self.assertNotIn("command", body)
        self.assertEqual(self.store.command_state, "pending")


class Operations(unittest.TestCase):
    def setUp(self):
        self.store = srv.Store()
        self.store.features = ["logs", "poweroff", "mark"]

    def op_id(self, n):
        return f"{n:032x}"

    def test_unfinished_work_is_never_evicted(self):
        for n in range(srv.MAX_OPS):
            self.store.add_op("logs", self.op_id(n), {})
        for _ in range(srv.MAX_OPS):
            self.store.take_ops()
        with self.assertRaises(ValueError):
            self.store.add_op("logs", self.op_id(9999), {})
        self.assertEqual(len(self.store.ops), srv.MAX_OPS)

    def test_finished_work_makes_room(self):
        for n in range(srv.MAX_OPS):
            self.store.add_op("logs", self.op_id(n), {})
        self.store.apply_acks([{"id": self.op_id(0), "state": "done", "detail": ""}])
        self.store.add_op("logs", self.op_id(9999), {})
        self.assertIn(self.op_id(9999), self.store.ops)
        self.assertNotIn(self.op_id(0), self.store.ops)

    def test_one_batch_fits_what_the_publisher_can_hold(self):
        for n in range(srv.MAX_OPS):
            self.store.add_op("logs", self.op_id(n), {})
        batch = self.store.take_ops()
        self.assertEqual(len(batch), srv.MAX_OP_BATCH)
        self.assertLessEqual(srv.MAX_OP_BATCH * 2, 32, "two takes before a drain must still fit")
        self.assertEqual(len(self.store.take_ops()), srv.MAX_OP_BATCH, "the rest come with the next request")

    def test_a_command_never_answered_is_reported_and_not_sent_twice(self):
        self.store.add_op("poweroff", self.op_id(1), {})
        self.assertEqual(len(self.store.take_ops()), 1)
        self.store.ops[self.op_id(1)]["sent"] -= srv.OP_ANSWER_S + 1
        self.assertEqual(self.store.take_ops(), [], "a power off is never resent")
        self.assertEqual(self.store.ops[self.op_id(1)]["state"], "failed")

    def test_a_sighting_is_still_resent(self):
        self.store.add_op("mark", self.op_id(2), {"label": "police", "note": "", "age_s": 0, "phone_utc": 2e9})
        self.assertEqual(len(self.store.take_ops()), 1)
        self.store.ops[self.op_id(2)]["sent"] -= srv.OP_RESEND_S + 1
        self.assertEqual(len(self.store.take_ops()), 1)


class StopCommand(unittest.TestCase):
    def push(self, store, state):
        ticket = store.ticket("pi")
        store.put({"v": 3, "source": "pi", "ticket": ticket, "snapshot_age_s": 0.0, "seq": store.source_seq + 1,
                   "session": None, "detector": {"state": state}, "channels": [], "matches": []})

    def test_a_stopped_detector_completes_the_stop(self):
        store = srv.Store()
        store.request_stop()
        store.take_command()
        self.assertEqual(store.command_status()["state"], "sent")
        self.push(store, "running")
        self.assertEqual(store.command_status()["state"], "sent", "not until it has really stopped")
        self.push(store, "stopped")
        self.assertEqual(store.command_status()["state"], "done")

    def test_a_stopped_state_does_not_complete_a_stop_nobody_collected(self):
        store = srv.Store()
        store.request_stop()
        self.push(store, "stopped")
        self.assertEqual(store.command_status()["state"], "pending")


class SettingsFile(unittest.TestCase):
    def test_a_stamp_always_belongs_to_its_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app-config.json"
            config = srv.AppConfig(path)
            stop = threading.Event()

            def reader():
                while not stop.is_set():
                    config.load()
            threads = [threading.Thread(target=reader, daemon=True) for _ in range(6)]
            for t in threads:
                t.start()
            for n in range(120):
                tmp_file = Path(tmp) / "next"
                tmp_file.write_text(json.dumps({"n": n, "pad": "x" * n}))
                for _ in range(200):
                    try:
                        tmp_file.replace(path)
                        break
                    except PermissionError:      # Windows only: a reader has it open for a moment
                        time.sleep(0.005)
            stop.set()
            for t in threads:
                t.join(3)
            self.assertEqual(json.loads(config.load())["config"]["n"], 119)

    def test_too_deep_a_file_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app-config.json"
            path.write_text("[" * 60000)
            config = srv.AppConfig(path)
            self.assertIsNone(config.load())
            self.assertTrue(config.error)


class Logs(unittest.TestCase):
    def test_no_partial_file_is_left_or_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            name = srv.save_log(tmp, "app", b"hello")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], [name])
            self.assertEqual(srv.list_logs(tmp)[0]["name"], name)
            (Path(tmp) / "app-20260101-000000Z-00000000.log.partial").write_bytes(b"half")
            self.assertEqual(len(srv.list_logs(tmp)), 1, "a partial upload is not offered")

    def test_saving_and_listing_at_once_never_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            errors = []
            stop = threading.Event()

            def lister():
                while not stop.is_set():
                    try:
                        srv.list_logs(tmp)
                    except Exception as exc:  # noqa: BLE001 - this is what the test looks for
                        errors.append(exc)

            def saver():
                try:
                    for _ in range(40):
                        srv.save_log(tmp, "app", b"x")
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)
            original = srv.LOG_KEEP_FILES
            srv.LOG_KEEP_FILES = 3
            try:
                listers = [threading.Thread(target=lister, daemon=True) for _ in range(3)]
                savers = [threading.Thread(target=saver, daemon=True) for _ in range(4)]
                for t in listers + savers:
                    t.start()
                for t in savers:
                    t.join(30)
            finally:
                stop.set()
                srv.LOG_KEEP_FILES = original
            self.assertEqual(errors, [])


class UploadKinds(unittest.TestCase):
    def test_a_flood_of_one_kind_cannot_delete_the_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            keep = srv.save_log(tmp, "pi", b"pi record")
            original = srv.LOG_KEEP_FILES
            srv.LOG_KEEP_FILES = 3
            try:
                for _ in range(8):
                    srv.save_log(tmp, "app", b"x")
            finally:
                srv.LOG_KEEP_FILES = original
            names = [p.name for p in Path(tmp).iterdir()]
            self.assertIn(keep, names)
            self.assertLessEqual(sum(n.startswith("app-") for n in names), 3)


class WhatTheServerSends(unittest.TestCase):
    """The Pi must survive a compromised or broken server."""

    class Response:
        def __init__(self, status, body):
            self.status, self.body = status, body

        def read(self, limit=-1):
            return self.body if limit < 0 else self.body[:limit]

        def getheader(self, _name, default=""):
            return default

    class Connection:
        def __init__(self, responses):
            self.responses = list(responses)

        def request(self, *_a, **_k):
            pass

        def getresponse(self):
            return self.responses.pop(0)

        def close(self):
            pass

    def sender_with(self, body):
        s = pub.Sender("http://127.0.0.1:9/api/push", "t" * 40)
        s.conn = self.Connection([self.Response(200, body)])
        return s

    def payload(self):
        return {"v": 3, "snapshot_mono": time.monotonic(), "seq": 1, "session": None, "detector": {"state": "running"}, "channels": [], "matches": []}

    def test_an_answer_of_the_wrong_shape_is_a_refused_push_not_a_crash(self):
        for body in (b"[]", b"null", b"1", b"{}", b'{"ticket":5}', b"not json", b"\xff\xfe"):
            with self.assertRaises(pub.PushRejected, msg=body):
                self.sender_with(body)(self.payload())

    def test_a_huge_answer_is_refused(self):
        with self.assertRaises(pub.PushRejected):
            self.sender_with(b" " * (pub.MAX_ANSWER_BYTES + 5))(self.payload())

    def test_a_bad_sighting_does_not_stop_the_publisher(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "s"
            session.mkdir()
            (session / "session.json").write_text("{}")
            for op in ({"id": ID, "label": []}, {"id": ID, "label": {"a": 1}}, {"id": ID, "label": "police", "age_s": "x"},
                       {"id": ID, "label": "police", "age_s": -1}, {"id": "nope", "label": "police"}):
                with self.assertRaises(ValueError, msg=op):
                    pub.write_mark(session, op)


class PendingMatches(unittest.TestCase):
    def publisher(self, send):
        with tempfile.TemporaryDirectory() as tmp:
            return pub.Publisher(send, heartbeat=Path(tmp) / "hb.json")

    def match(self, n, db, now):
        return {"id": f"m{n}", "ch": n, "strength_db": db, "seen": now, "delay_s": 1.0}

    def test_many_at_once_lose_nothing_and_the_strongest_go_first(self):
        batches = []
        p = self.publisher(lambda payload: batches.append([m["id"] for m in payload["matches"]]))
        now = time.monotonic()
        p.pending.extend(self.match(n, 10.0, now) for n in range(30))
        p.pending.appendleft(self.match(99, 55.0, now))
        p.push(now)
        self.assertEqual(len(batches[0]), pub.MAX_MATCHES)
        self.assertIn("m99", batches[0], "the urgent one is not left behind")
        p.push(now)
        self.assertEqual(len(set(batches[0]) | set(batches[1])), 31, "the rest follow")
        self.assertEqual(len(p.pending), 0)

    def test_a_failed_push_keeps_them(self):
        def fail(_payload):
            raise OSError("offline")
        p = self.publisher(fail)
        now = time.monotonic()
        p.pending.append(self.match(1, 30.0, now))
        self.assertFalse(p.push(now))
        self.assertEqual(len(p.pending), 1)


class Acknowledgements(unittest.TestCase):
    def publisher(self, send):
        with tempfile.TemporaryDirectory() as tmp:
            return pub.Publisher(send, heartbeat=Path(tmp) / "hb.json")

    def test_an_ack_added_during_a_push_cannot_crash_it(self):
        added = {}

        def send(_payload):
            for n in range(pub.MAX_ACKS_WAITING):    # more than the queue holds, while the send is under way
                added["p"].ack(f"{n:032x}", "done")
        p = self.publisher(send)
        added["p"] = p
        for n in range(pub.MAX_ACKS):
            p.ack(f"{n + 90000:032x}", "done")
        self.assertTrue(p.push(time.monotonic()))

    def test_one_push_carries_no_more_than_the_server_accepts(self):
        seen = []
        p = self.publisher(lambda payload: seen.append(len(payload["acks"])))
        for n in range(pub.MAX_ACKS + 10):
            p.ack(f"{n:032x}", "done")
        p.push(time.monotonic())
        self.assertEqual(seen, [pub.MAX_ACKS])
        self.assertEqual(len(p.acks), 10, "the rest wait for the next push")

    def test_a_full_recorder_is_not_reported_as_recorded(self):
        p = self.publisher(lambda _payload: None)
        p.mark_ops["m.request"] = ID
        p.tail.mark_results = [("m", "queue_full")]
        p.report_marks()
        self.assertIn("no recording", p.acks[-1]["detail"])


class Redaction(unittest.TestCase):
    def test_every_kind_of_credential_is_removed(self):
        secret = "S3cretValue123"
        for line in (f'"token": "{secret}"', f"token={secret}", f"Authorization: Bearer {secret}", f'"user": "{secret}"',
                     f"PUSHOVER_USER={secret}", f"GET /?key={secret} HTTP/1.1", f"Cookie: s17={secret}", f'"viewer_key":"{secret}"',
                     f'"pi_status_key": "{secret}"', f"password {secret}", f"X-Api-Key: {secret}", f"user_key={secret}",
                     f"password='{secret}'", f"{{'token': '{secret}'}}", f"{{'user': '{secret}'}}",
                     # Bluetooth's pairing code (1.8.0) is as good as its key.
                     f"bluetooth_code={secret}", f'"pairing code": "{secret}"', f"SLOT17_BLUETOOTH_CODE={secret}"):
            self.assertNotIn(secret, pub.redact(line), line)
        code = "000G-40R4-0M30-E209-185G-R38E-1W1"      # on its own, as the setup prints it
        self.assertNotIn(code, pub.redact(f"  type this pairing code:      {code}"))

    def test_ordinary_lines_stay_readable(self):
        line = "detector stop queued by operator command; windows 40"
        self.assertEqual(pub.redact(line), line)

    def test_a_character_is_never_cut_in_half(self):
        out = pub.last_bytes("a" + "\u00e9" * 10, 5)
        self.assertLessEqual(len(out), 5)
        out.decode("utf-8")


class ReceiverQuiet(unittest.TestCase):
    def test_the_detectors_count_is_used_even_if_this_loop_missed_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            hb = pub.Heartbeat(Path(tmp) / "hb.json")
            hb._count_input({"quiet_windows": 41})
            self.assertEqual(hb.quiet_windows, 41)
            hb._count_input({"quiet_windows": 0})
            self.assertEqual(hb.quiet_windows, 0)

    def test_an_older_detector_is_still_counted_here(self):
        with tempfile.TemporaryDirectory() as tmp:
            hb = pub.Heartbeat(Path(tmp) / "hb.json")
            hb._count_input({"rf_health": {"window_id": "w1", "input": {"antenna_suspect": True}}})
            hb._count_input({"rf_health": {"window_id": "w2", "input": {"antenna_suspect": True}}})
            self.assertEqual(hb.quiet_windows, 2)


class PiLog(unittest.TestCase):
    def test_a_huge_file_is_not_read_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            big = Path(tmp) / "hb.json"
            big.write_bytes(b"x" * (pub.LOG_FILE_BYTES * 3))
            body = pub.collect_pi_log(None, big, runner=lambda _cmd: type("D", (), {"stdout": "", "stderr": ""})())
            self.assertLess(len(body), pub.LOG_FILE_BYTES + 2000)

    def test_only_one_upload_at_a_time(self):
        release = threading.Event()

        def collect(_session, _heartbeat):
            release.wait(5)
            raise OSError("stopping the test")
        with tempfile.TemporaryDirectory() as tmp:
            p = pub.Publisher(lambda _payload: None, heartbeat=Path(tmp) / "hb.json")
        first = pub.upload_pi_log({"id": "1" * 32}, p, {"url": "http://127.0.0.1:9", "token": "t"}, collect=collect)
        second = pub.upload_pi_log({"id": "2" * 32}, p, {"url": "http://127.0.0.1:9", "token": "t"}, collect=collect)
        self.assertIsNone(second)
        self.assertEqual(p.acks[-1]["id"], "2" * 32)
        self.assertEqual(p.acks[-1]["state"], "failed")
        release.set()
        first.join(5)


if __name__ == "__main__":
    unittest.main()
