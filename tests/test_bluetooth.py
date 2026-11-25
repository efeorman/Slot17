"""1.4.0: the Bluetooth link to the phone apps. Its wire format, its settings, and what the link process does.

None of this needs Bluetooth or D-Bus: the BlueZ side (p0/bluetooth_bluez.py) is replaced by a recorder.
"""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
import bluetooth_config as config  # noqa: E402
import bluetooth_frames as frames  # noqa: E402
import bluetooth_link as link  # noqa: E402

KEY = bytes(range(32))
DETECTOR = {"state": "running", "windows": 14400, "hits": 37, "quiet_windows": 3, "quiet_alarm_windows": 240,
            "whole_band_windows": 12, "heartbeat_unchanged_s": 0.4, "floor_db": -1.6, "reasons": ["ignored"]}
MATCH = {"id": "s1/e1", "ch": 154, "strength_db": 55.2, "age_s": 0.123, "rule": "weak"}
CHANNEL = {"ch": 154, "id": "s1/e1", "freq_mhz": 383.8625, "strength_db": 55.2, "age_s": 0.1, "hits_60s": 3,
           "activity": "intermittent", "approx_time": True, "rising": True, "call": False}


class FrameTests(unittest.TestCase):
    def setUp(self):
        self.enc = frames.Encoder(KEY, boot=7)

    def test_status_round_trip(self):
        got = frames.decode(self.enc.status(DETECTOR), KEY)
        self.assertEqual((got["kind"], got["boot"], got["counter"]), ("status", 7, 1))
        d = got["detector"]
        self.assertEqual((d["state"], d["windows"], d["hits"], d["quiet_windows"]), ("running", 14400, 37, 3))
        self.assertEqual((d["quiet_alarm_windows"], d["whole_band_windows"]), (240, 12))
        self.assertAlmostEqual(d["heartbeat_unchanged_s"], 0.4)
        self.assertAlmostEqual(d["floor_db"], -1.6)

    def test_matches_carry_what_the_server_carries(self):
        got = frames.decode(self.enc.matches([MATCH])[0], KEY)["matches"]
        self.assertEqual(got, [{"key": frames.match_key("s1/e1").hex(), "ch": 154, "strength_db": 55.2,
                                "age_s": 0.123, "rule": "weak"}])

    def test_channels_round_trip(self):
        row = frames.decode(self.enc.channels([CHANNEL]), KEY)["channels"][0]
        self.assertEqual((row["ch"], row["freq_mhz"], row["strength_db"], row["hits_60s"]), (154, 383.8625, 55.2, 3))
        self.assertEqual((row["rising"], row["call"], row["approx_time"], row["activity"]), (True, False, True, "intermittent"))
        self.assertEqual(row["burst"], frames.match_key("s1/e1")[:4].hex())

    def test_unknown_values_stay_unknown(self):
        got = frames.decode(self.enc.matches([dict(MATCH, strength_db=None, rule="new-rule")])[0], KEY)["matches"][0]
        self.assertIsNone(got["strength_db"])
        self.assertIsNone(got["rule"])
        d = frames.decode(self.enc.status({"state": "something new"}), KEY)["detector"]
        self.assertEqual(d["state"], "unknown")
        self.assertIsNone(d["floor_db"])
        self.assertIsNone(d["quiet_alarm_windows"])

    def test_figures_are_clamped_not_wrapped(self):
        got = frames.decode(self.enc.matches([dict(MATCH, age_s=99.0, strength_db=9999.0)])[0], KEY)["matches"][0]
        self.assertEqual(got["age_s"], 65.535)
        self.assertEqual(got["strength_db"], 3276.7)
        d = frames.decode(self.enc.status(dict(DETECTOR, quiet_windows=10**9, windows=-5)), KEY)["detector"]
        self.assertEqual((d["quiet_windows"], d["windows"]), (0xFFFF, 0))

    def test_every_frame_fits_one_notification(self):
        many = [dict(MATCH, id=f"s1/e{i}") for i in range(25)]
        out = self.enc.matches(many)
        self.assertEqual([len(frames.decode(f, KEY)["matches"]) for f in out], [10, 10, 5])
        self.assertTrue(all(len(f) <= frames.MAX_FRAME for f in out))
        self.assertLessEqual(len(self.enc.channels([CHANNEL] * 20)), frames.MAX_FRAME)
        self.assertEqual(len(frames.decode(self.enc.channels([CHANNEL] * 20), KEY)["channels"]), 8)

    def test_counter_rises_by_one(self):
        counters = [frames.decode(f, KEY)["counter"] for f in (self.enc.status(DETECTOR), self.enc.channels([]),
                                                             self.enc.status(DETECTOR))]
        self.assertEqual(counters, [1, 2, 3])

    def test_a_changed_or_foreign_frame_is_refused(self):
        frame = bytearray(self.enc.matches([MATCH])[0])
        with self.assertRaises(frames.FrameError):
            frames.decode(bytes(frame), bytes(32))                  # another car computer's key
        frame[13] ^= 1
        with self.assertRaises(frames.FrameError):
            frames.decode(bytes(frame), KEY)
        for bad in (b"", bytes(17), bytes(181)):
            with self.assertRaises(frames.FrameError):
                frames.decode(bad, KEY)

    def test_match_key_is_the_start_of_sha256(self):
        import hashlib
        self.assertEqual(frames.match_key("s/é"), hashlib.sha256("s/é".encode()).digest()[:8])

    def test_a_replay_is_refused(self):
        acc = frames.Acceptor()
        steps = [(7, 1, True), (7, 2, True), (7, 2, False), (7, 1, False), (9, 1, True), (7, 3, False), (9, 2, True)]
        self.assertEqual([acc.accept(b, c) for b, c, _ in steps], [ok for _, _, ok in steps])

    def test_the_key_must_be_32_bytes(self):
        with self.assertRaises(ValueError):
            frames.Encoder(b"short", boot=1)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "slot17-bluetooth.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_or_damaged_means_off(self):
        self.assertEqual(config.load(self.path), {"on": False, "name": "Slot17", "key": None})
        for text in ("not json", "[true]", json.dumps({"on": True}), json.dumps({"on": True, "key": "abc"}),
                     json.dumps({"on": True, "key": "zz" * 32})):
            Path(self.path).write_text(text)
            self.assertFalse(config.load(self.path)["on"], text)

    def test_switch_on_makes_one_key_and_keeps_it(self):
        first = config.save(self.path, on=True)
        self.assertTrue(first["on"])
        self.assertEqual(len(first["key"]), 32)
        again = config.save(self.path, on=False)
        self.assertFalse(again["on"])
        self.assertEqual(again["key"], first["key"])
        self.assertEqual(os.listdir(self.tmp.name), ["slot17-bluetooth.json"])     # no partial file left
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_names(self):
        for good in ("Slot17", "Car 2", "a", "VW.Golf_7-GTI", "x" * 20):
            self.assertTrue(config.valid_name(good), good)
        for bad in ("", " Slot17", "Slot17 ", "x" * 21, "a:b", "a;b", "Éva", "-x", None, 17):
            self.assertFalse(config.valid_name(bad), bad)
        config.save(self.path, on=True, name="Car 2")
        self.assertEqual(config.load(self.path)["name"], "Car 2")
        with self.assertRaises(ValueError):
            config.save(self.path, name="a;b")
        self.assertEqual(config.load(self.path)["name"], "Car 2")

    def test_public_never_holds_the_key(self):
        settings = config.save(self.path, on=True)
        shown = json.dumps(config.public(settings))
        self.assertNotIn(settings["key"].hex(), shown)
        self.assertIn(frames.key_id(settings["key"]), shown)

    def test_command_line(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(config.main(["--path", self.path, "--on", "--name", "Car 2"]), 0)
        self.assertEqual(json.loads(out.getvalue())["name"], "Car 2")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(config.main(["--path", self.path, "--name", "a;b"]), 2)

    def test_a_new_key_keeps_the_rest(self):
        """After 1.6.0 sent the key over Bluetooth (CHANGELOG), a car computer can be given a new one."""
        import contextlib
        import io
        first = config.save(self.path, on=True, name="Car 2")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(config.main(["--path", self.path, "--new-key"]), 0)
        now = config.load(self.path)
        self.assertNotEqual(now["key"], first["key"])
        self.assertEqual((now["on"], now["name"], len(now["key"])), (True, "Car 2", 32))
        self.assertEqual(json.loads(out.getvalue())["key_id"], frames.key_id(now["key"]))
        self.assertNotIn(now["key"].hex(), out.getvalue())


class FakePeripheral:
    """Records what the link asks of BlueZ."""

    def __init__(self):
        self.calls, self.notified, self.subscribed = [], [], False

    def register(self):
        self.calls.append("register")

    def restart(self):
        self.calls.append("restart")

    def advertise(self, name, interval):
        self.calls.append(("advertise", name, interval))

    def rename(self, name):
        self.calls.append(("rename", name))

    def notify(self, data):
        if self.subscribed:
            self.notified.append(bytes(data))
        return self.subscribed


class LinkBase(unittest.TestCase):
    """A link on a fake BlueZ peripheral. No tests of its own, so the classes below do not run each other's."""

    def setUp(self):
        self.t = [100.0]
        self.lines = []
        self.p = FakePeripheral()
        self.settings = {"on": True, "name": "Slot17", "key": KEY}
        self.link = link.Link(dict(self.settings), self.p, self.lines.append, software="1.4.0", now=lambda: self.t[0],
                              boot=7, load=lambda: dict(self.settings))

    def connect(self):
        self.link.event("registered", None)
        self.link.event("advertising", link.FAST_MS)
        self.p.subscribed = True
        self.link.event("subscribed", True)

    def controlled(self):
        """A connected link that has sent a few frames, with its settings saved in this test."""
        self.saved = []
        self.link.save = lambda **change: (self.saved.append(change), self.settings.update(change))[1]
        self.connect()
        for _ in range(3):
            self.link.command({"status": DETECTOR})
        return self.link.encoder.boot, self.link.encoder.counter

    def replies(self):
        return [frames.decode(f, KEY) for f in self.p.notified if frames.decode(f, KEY)["kind"] == "reply"]


class LinkTests(LinkBase):
    def test_registers_then_advertises_fast_then_slower(self):
        self.link.event("registered", None)
        self.assertEqual(self.p.calls[-1], ("advertise", "Slot17", link.FAST_MS))
        self.link.event("advertising", link.FAST_MS)
        self.assertEqual(self.link.state, "advertising")
        self.t[0] += link.FAST_ADVERTISING_S + 1
        self.link.tick()
        self.assertEqual(self.p.calls[-1], ("advertise", "Slot17", link.NORMAL_MS))

    def test_a_connected_phone_gets_frames_and_another_can_still_join(self):
        self.connect()
        self.assertEqual(self.link.state, "connected")
        self.assertEqual(self.p.calls[-1], ("advertise", "Slot17", link.CONNECTED_MS))
        self.assertTrue(self.link.command({"matches": [MATCH]}))
        self.assertTrue(self.link.command({"status": DETECTOR, "channels": [CHANNEL]}))
        kinds = [frames.decode(f, KEY)["kind"] for f in self.p.notified]
        self.assertEqual(kinds, ["matches", "status", "channels"])
        self.assertEqual((self.link.sent_frames, self.link.sent_matches), (3, 1))

    def test_nothing_is_encoded_while_no_phone_listens(self):
        self.link.event("registered", None)
        self.link.command({"matches": [MATCH], "status": DETECTOR})
        self.assertEqual(self.p.notified, [])
        self.assertEqual(self.link.encoder.counter, 0)

    def test_after_a_disconnect_it_advertises_fast_again(self):
        self.connect()
        self.t[0] += 300
        self.p.subscribed = False
        self.link.event("subscribed", False)
        self.assertEqual(self.link.state, "advertising")
        self.assertEqual(self.p.calls[-1], ("advertise", "Slot17", link.FAST_MS))

    def test_a_refused_registration_is_retried_later_and_only_that_part(self):
        self.link.event("gatt_failed", "Failed")
        self.assertEqual(self.link.state, "error")
        self.assertIn("GATT", self.lines[-1]["error"])
        self.link.tick()
        self.assertEqual(self.p.calls, [])
        self.t[0] += link.RETRY_S
        self.link.tick()
        self.assertEqual(self.p.calls, ["register"])
        self.link.event("registered", None)
        self.link.event("advertising_failed", "Maximum advertisements reached")
        calls = len(self.p.calls)
        self.t[0] += link.RETRY_S
        self.link.tick()
        self.assertEqual(self.p.calls[calls:], [("advertise", "Slot17", link.NORMAL_MS)])   # past the fast 30 s

    def test_renamed_and_switched_off_from_the_app(self):
        self.connect()
        self.settings["name"] = "Car 2"
        self.assertTrue(self.link.command({"reload": True}))
        self.assertIn(("rename", "Car 2"), self.p.calls)
        self.assertEqual(self.p.calls[-1], ("advertise", "Car 2", link.CONNECTED_MS))
        self.assertEqual(json.loads(self.link.info())["name"], "Car 2")
        self.settings["on"] = False
        self.assertFalse(self.link.command({"reload": True}))

    def test_bluetoothd_restarting(self):
        self.connect()
        self.link.lost()
        self.assertEqual((self.link.state, self.link.subscribed), ("starting", False))
        self.link.restart()
        self.assertEqual(self.p.calls[-1], "restart")
        self.assertIsNone(self.link.error)

    def test_info_names_the_key_but_never_holds_it(self):
        info = json.loads(self.link.info())
        self.assertEqual(info, {"v": 1, "name": "Slot17", "key_id": frames.key_id(KEY), "software": "1.4.0",
                                "boot": "00000007", "can": ["state", "ops", "files"]})
        self.assertNotIn(KEY.hex(), self.link.info().decode())

    def test_status_lines_on_change_and_every_few_seconds(self):
        self.link.event("registered", None)
        self.link.event("advertising", link.FAST_MS)
        count = len(self.lines)
        self.link.report()
        self.assertEqual(len(self.lines), count)               # nothing changed
        self.t[0] += link.STATUS_EVERY_S
        self.link.report()
        self.assertEqual(len(self.lines), count + 1)
        self.assertNotIn(KEY.hex(), json.dumps(self.lines))
        self.link.event("read", 185)
        self.assertEqual(self.lines[-1]["mtu"], 185)

    def test_renamed_over_bluetooth(self):
        boot, counter = self.controlled()
        self.link.event("control", frames.control_frame(KEY, boot, counter, frames.RENAME, "Car 2"))
        self.assertEqual(self.saved, [{"name": "Car 2"}])
        self.assertIn(("rename", "Car 2"), self.p.calls)
        self.assertEqual(self.replies()[-1]["text"], "Bluetooth name: Car 2")
        self.assertTrue(self.replies()[-1]["ok"])
        self.assertTrue(self.lines[-1]["settings_changed"])

    def test_switched_off_over_bluetooth_it_answers_then_ends(self):
        boot, counter = self.controlled()
        self.link.event("control", frames.control_frame(KEY, boot, counter, frames.OFF))
        self.assertEqual(self.saved, [{"on": False}])
        self.assertTrue(self.link.stop_requested)
        self.assertEqual((self.replies()[-1]["action"], self.replies()[-1]["ok"]), ("off", True))

    def test_a_command_works_once_and_only_from_this_boot_and_key(self):
        boot, counter = self.controlled()
        good = frames.control_frame(KEY, boot, counter, frames.RENAME, "Car 2")
        self.link.event("control", good)
        self.link.event("control", good)                        # played back
        self.assertEqual(len(self.saved), 1)
        self.assertIn("already used", self.replies()[-1]["text"])
        self.link.event("control", frames.control_frame(KEY, boot ^ 1, counter, frames.RENAME, "Car 3"))
        self.assertIn("before a restart", self.replies()[-1]["text"])
        self.link.event("control", frames.control_frame(bytes(32), boot, counter - 1, frames.RENAME, "Car 4"))
        self.assertIn("not signed", self.replies()[-1]["text"])
        self.link.event("control", frames.control_frame(KEY, boot, counter + 5, frames.RENAME, "Car 5"))
        self.assertIn("too old", self.replies()[-1]["text"])      # names a frame never sent
        self.assertEqual(len(self.saved), 1)
        self.assertFalse(any(r["ok"] for r in self.replies()[1:]))

    def test_an_old_command_is_refused(self):
        boot, counter = self.controlled()
        for _ in range(frames.CONTROL_WINDOW + 1):
            self.link.command({"status": DETECTOR})
        self.link.event("control", frames.control_frame(KEY, boot, counter, frames.OFF))
        self.assertEqual(self.saved, [])

    def test_a_bad_name_is_refused_by_the_settings(self):
        boot, counter = self.controlled()
        def refuse(**change):
            raise ValueError("not a usable name")
        self.link.save = refuse
        self.link.event("control", frames.control_frame(KEY, boot, counter, frames.RENAME, "a;b"))
        self.assertEqual(self.replies()[-1]["text"], "not a usable name")
        self.assertFalse(self.replies()[-1]["ok"])

    def test_control_and_reply_frames(self):
        f = frames.control_frame(KEY, 7, 42, frames.RENAME, "Car 2")
        self.assertEqual(frames.decode_control(f, KEY), {"boot": 7, "counter": 42, "action": "rename", "name": "Car 2"})
        for bad in (f[:-1] + bytes([f[-1] ^ 1]), f[:12] + f[13:], frames.Encoder(KEY, 7).status(DETECTOR)):
            with self.assertRaises(frames.FrameError):
                frames.decode_control(bad, KEY)
        r = frames.decode(frames.Encoder(KEY, 7).reply(frames.OFF, False, "x" * 200), KEY)
        self.assertEqual((r["kind"], r["action"], r["ok"], len(r["text"])), ("reply", "off", False, frames.REPLY_TEXT_BYTES))

    def test_bad_commands_are_ignored(self):
        self.connect()
        for bad in (None, [], "x", {"matches": "x"}, {"status": 3}):
            self.assertTrue(self.link.command(bad))
        self.assertEqual(self.p.notified, [])


class PieceTests(unittest.TestCase):
    """1.6.0: a state, a file or a phone's operation, in signed pieces."""

    def test_a_message_in_pieces_back_together_in_any_order_and_twice(self):
        data = bytes(range(256)) * 3
        enc = frames.Encoder(KEY, boot=7)
        pieces = [frames.decode(f, KEY) for f in enc.pieces(frames.FILE, data)]
        self.assertEqual(len(pieces), 5)
        self.assertTrue(all(len(f) <= frames.MAX_FRAME for f in enc.pieces(frames.FILE, data)))
        a = frames.Assembler()
        order = [pieces[i] for i in (3, 0, 0, 4, 1)]
        self.assertEqual([a.add(p["message"], p["index"], p["count"], bytes.fromhex(p["data"])) for p in order],
                         [None] * 5)
        self.assertEqual(a.add(pieces[2]["message"], 2, 5, bytes.fromhex(pieces[2]["data"])), data)

    def test_a_lost_piece_costs_only_its_own_message(self):
        enc = frames.Encoder(KEY, boot=7)
        first, second, third = ([frames.decode(f, KEY) for f in enc.pieces(frames.STATE, b"x" * 400)] for _ in range(3))
        a = frames.Assembler(keep=2)
        a.add(first[0]["message"], 0, 3, b"x")                  # its other pieces never come
        for p in second[:2] + third:
            a.add(p["message"], p["index"], p["count"], bytes.fromhex(p["data"]))
        self.assertNotIn(first[0]["message"], a.parts)
        got = a.add(second[2]["message"], 2, 3, bytes.fromhex(second[2]["data"]))
        self.assertEqual(got, b"x" * 400)

    def test_limits(self):
        with self.assertRaises(frames.FrameError):
            frames.Assembler(limit=100).add(1, 0, 2, b"y" * 101)
        with self.assertRaises(frames.FrameError):
            frames.Assembler().add(1, 2, 2, b"")
        with self.assertRaises(frames.FrameError):
            frames.unpack(frames.pack({"a": "z" * 5000}), 1000)
        with self.assertRaises(frames.FrameError):
            frames.unpack(b"not deflate", 1000)
        self.assertEqual(frames.unpack(frames.pack({"a": [1, 2]}), 1000), {"a": [1, 2]})
        self.assertEqual(frames.unpack(frames.stored(b'{"a":1}'), 1000), {"a": 1})

    def test_the_parity_state_blob_still_holds_the_parity_state(self):
        self.assertEqual(frames.unpack(frames.PARITY_STATE_Z, frames.STATE_MAX_BYTES), frames.PARITY_STATE)

    def test_an_operation_is_its_json_in_control_frames(self):
        op = {"kind": "mark", "id": "d" * 32, "label": "police", "note": "n" * 400}
        fs = frames.op_frames(KEY, 7, 42, 0x1234, op)
        self.assertEqual(len(fs), 4)
        decoded = [frames.decode_control(f, KEY) for f in fs]
        self.assertEqual({(d["action"], d["boot"], d["counter"], d["message"], d["count"]) for d in decoded},
                         {("op", 7, 42, 0x1234, 4)})
        self.assertEqual(json.loads(b"".join(d["data"] for d in decoded)), op)
        with self.assertRaises(frames.FrameError):
            frames.decode_control(frames.op_frames(bytes(32), 7, 42, 1, op)[0], KEY)


class LinkMessageTests(LinkBase):
    """1.6.0: the whole state and files over the link, and operations from the phone back to the publisher."""

    def setUp(self):
        super().setUp()
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        self.link.cache = Path(self.cache.name)
        self.woken = []
        self.link.wake = lambda: self.woken.append(True)

    def decoded(self):
        return [frames.decode(f, KEY) for f in self.p.notified]

    def test_the_state_goes_in_pieces_only_to_a_listening_phone(self):
        import base64
        doc = {"v": 1, "state": {"detector": {"state": "running"}, "pi": {"temp_c": 50.1}}, "ops": []}
        line = {"state": base64.b64encode(frames.pack(doc)).decode()}
        self.link.command(line)
        self.assertEqual(self.p.notified, [])
        self.connect()
        self.link.command(line)
        got = self.decoded()
        self.assertEqual({g["kind"] for g in got}, {"state"})
        a = frames.Assembler()
        whole = [a.add(g["message"], g["index"], g["count"], bytes.fromhex(g["data"])) for g in got][-1]
        self.assertEqual(frames.unpack(whole, frames.STATE_MAX_BYTES), doc)
        self.link.command({"state": "not base64!"})
        self.assertEqual(len(self.p.notified), len(got))

    def test_a_file_goes_a_few_pieces_at_a_time_each_signed_as_it_goes(self):
        self.connect()
        packed = os.urandom(2000)                       # does not compress: 13 pieces
        path = Path(self.cache.name) / "slot17-bt-0123456789abcdef.z"
        path.write_bytes(packed)
        self.link.command({"file": {"path": str(path)}})
        self.assertFalse(path.exists(), "the link deletes it once read")
        self.assertEqual(self.woken, [True])
        self.assertTrue(self.link.pump(4))
        self.link.command({"status": DETECTOR})        # a frame between two pieces
        while self.link.pump(4):
            pass
        counters = [g["counter"] for g in self.decoded()]
        self.assertEqual(counters, sorted(counters), "numbered as sent, so the phone takes every one")
        pieces = [g for g in self.decoded() if g["kind"] == "file"]
        self.assertEqual(len(pieces), 13)
        a = frames.Assembler()
        whole = [a.add(g["message"], g["index"], g["count"], bytes.fromhex(g["data"])) for g in pieces][-1]
        self.assertEqual(whole, packed)

    def test_only_its_own_files_and_none_after_the_phone_left(self):
        self.connect()
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        for path in (Path(elsewhere.name) / "slot17-bt-0123456789abcdef.z", Path(self.cache.name) / "notes.txt"):
            path.write_bytes(b"x")
            self.link.command({"file": {"path": str(path)}})
            self.assertTrue(path.exists(), "not its own: left alone")
        good = Path(self.cache.name) / "slot17-bt-0123456789abcdef.z"
        good.write_bytes(os.urandom(1000))
        self.link.command({"file": {"path": str(good)}})
        self.p.subscribed = False
        self.link.event("subscribed", False)
        self.assertFalse(self.link.pump(4))
        self.assertEqual([g for g in self.decoded() if g["kind"] == "file"], [])

    def test_an_operation_from_the_phone_goes_to_the_publisher(self):
        boot, counter = self.controlled()
        op = {"kind": "mark", "id": "e" * 32, "label": "police", "note": "n" * 300, "age_s": 1.0}
        for f in frames.op_frames(KEY, boot, counter, 0x77, op):       # every piece names the same counter
            self.link.event("control", f)
        self.assertEqual([line["op"] for line in self.lines if "op" in line], [op])
        self.assertEqual(self.replies(), [], "answered in the state, not with a reply")

    def test_an_operation_is_refused_from_another_boot_too_old_or_malformed(self):
        boot, counter = self.controlled()
        op = {"kind": "poweroff", "id": "f" * 32, "age_s": 0}
        self.link.event("control", frames.op_frames(KEY, boot ^ 1, counter, 1, op)[0])
        self.assertIn("before a restart", self.replies()[-1]["text"])
        for _ in range(frames.CONTROL_WINDOW + 1):
            self.link.command({"status": DETECTOR})
        self.link.event("control", frames.op_frames(KEY, boot, counter, 2, op)[0])
        self.assertIn("too old", self.replies()[-1]["text"])
        counter = self.link.encoder.counter
        for bad, why in (({"kind": "poweroff", "id": "nope"}, "not an operation"), ([1, 2], "not an operation")):
            self.link.event("control", frames.op_frames(KEY, boot, counter, 3, bad)[0])
            self.assertIn(why, self.replies()[-1]["text"])
        self.assertEqual([line for line in self.lines if "op" in line], [])
        self.assertEqual(self.replies()[-1]["action"], "op")


class GattClient(unittest.TestCase):
    """1.5.1: BlueZ as a GATT client made iPhones ask to pair at every connection; the setup switches it off."""

    DEBIAN = ("[General]\n#Name = BlueZ\n\n[GATT]\n# GATT attribute cache.\n#Cache = always\n\n"
              "# Possible values: true, false\n#Client = true\n\n#ExportClaimedServices = read-only\n\n"
              "[CSIS]\n#SIRK = x\n#Client = true\n")

    def test_read(self):
        from bluetooth_config import gatt_client
        self.assertTrue(gatt_client(self.DEBIAN))                      # commented out: BlueZ's default, on
        self.assertTrue(gatt_client(""))
        self.assertTrue(gatt_client("[GATT]\nClient = true\n"))
        self.assertFalse(gatt_client("[GATT]\nClient = false\n"))
        self.assertFalse(gatt_client("[GATT]\n  Client=FALSE\n"))
        self.assertTrue(gatt_client("[CSIS]\nClient = false\n"))      # another section's Client is not it

    def test_switched_off_once_everything_else_kept(self):
        from bluetooth_config import gatt_client, gatt_client_off
        new, changed = gatt_client_off(self.DEBIAN)
        self.assertTrue(changed)
        self.assertFalse(gatt_client(new))
        self.assertEqual(new.replace("Client = false\n", "").replace(
            "# Slot17: the car computer is only a GATT server; as a client it made phones ask to pair.\n", ""), self.DEBIAN)
        self.assertLess(new.index("Client = false"), new.index("[CSIS]"))
        self.assertEqual(gatt_client_off(new), (new, False))
        for text in ("[GATT]\nClient = true\n", "[General]\nName = x", ""):
            out, changed = gatt_client_off(text)
            self.assertTrue(changed, text)
            self.assertFalse(gatt_client(out), text)

    def test_the_file_with_a_copy_of_the_old_one(self):
        from bluetooth_config import set_gatt_client_off
        with tempfile.TemporaryDirectory() as folder:
            conf = os.path.join(folder, "main.conf")
            with open(conf, "w", encoding="utf-8") as fh:
                fh.write(self.DEBIAN)
            self.assertTrue(set_gatt_client_off(conf).startswith("switched off"))
            self.assertEqual(set_gatt_client_off(conf), "already off")
            copies = [f for f in os.listdir(folder) if ".slot17-backup-" in f]
            self.assertEqual(len(copies), 1)
            with open(os.path.join(folder, copies[0]), encoding="utf-8") as fh:
                self.assertEqual(fh.read(), self.DEBIAN)


if __name__ == "__main__":
    unittest.main()
