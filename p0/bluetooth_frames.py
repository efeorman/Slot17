"""The Bluetooth link's wire format, version 1: what the car computer sends the phone apps.

Standard library only. The iPhone and Android apps decode the same bytes; `shared/parity/ble-frames.json`
holds frames that all three must read identically (regenerate it with `python -B p0/bluetooth_frames.py`).
docs/design/BLUETOOTH.md describes the link as a whole.

Every frame, little-endian:

    offset  size  field
    0       1     version, 1
    1       1     kind: 1 status, 2 matches, 3 channels, 4 reply, 5 state, 6 file; 16 control (from the phone)
    2       4     boot: random for each start of the link (uint32)
    6       4     counter: one higher for every frame within a boot (uint32)
    10      n     body, per kind (below)
    10+n    8     tag: the first 8 bytes of HMAC-SHA256(key, all bytes before the tag)

A frame is at most MAX_FRAME bytes, so it fits in one notification once the phone has asked for a
larger MTU, which iPhones and Android phones do by themselves.

Matches carry exactly what the display server passes on for a match, so a sound never depends on the
route that delivered the match first. Their key is the first 8 bytes of SHA-256 of the match's id;
the apps compute the same key for matches from the server, and play each key once.

State and file frames (1.6.0) carry a message too long for one frame, in pieces: a message number, the
piece's index and the number of pieces, then a slice of the message. A state is everything the car
computer tells the server (`{"v": 1, "state": {...}, "ops": [...]}`), so an app without internet sees all of
it; a file is a Pi log asked for over Bluetooth. Both are raw DEFLATE (RFC 1951) of compact JSON. Every
piece is signed and numbered like any frame, so a piece cannot be changed, swapped or played back.

A control frame goes the other way, written by a phone to the control characteristic: switch the link
off, or rename it. Its boot and counter are those of a frame the car computer sent lately, and each counter
works once, so a recorded command cannot be played back later. The car computer answers with a reply.
From 1.6.0 a control frame can also carry an operation, the same JSON the app sends the server (a sighting,
power off, the Pi log...), in pieces of plain JSON. Its pieces may name the same counter; an operation is
carried out once by its id instead, and is answered in the next state's `ops`.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import hmac
import json
import math
import struct
import zlib

VERSION = 1
SERVICE_UUID = "f9cb0001-aa93-4349-a576-0e70a736266f"
FRAMES_UUID = "f9cb0002-aa93-4349-a576-0e70a736266f"     # notify: the frames
INFO_UUID = "f9cb0003-aa93-4349-a576-0e70a736266f"       # read: JSON about this car computer
CONTROL_UUID = "f9cb0004-aa93-4349-a576-0e70a736266f"    # write: a control frame from a phone

STATUS, MATCHES, CHANNELS, REPLY, STATE, FILE, CONTROL = 1, 2, 3, 4, 5, 6, 16
KINDS = {STATUS: "status", MATCHES: "matches", CHANNELS: "channels", REPLY: "reply", STATE: "state", FILE: "file"}
OFF, RENAME, OP = 1, 2, 3
ACTIONS = {OFF: "off", RENAME: "rename", OP: "op"}
# What this link can do beyond 1.4.0, in the info characteristic: an app uses only what is listed.
CAN = ["state", "ops", "files"]
CONTROL_WINDOW = 300            # a control frame may name any of the car computer's last 300 frames
REPLY_TEXT_BYTES = 80
DETECTOR_STATES = ("stopped", "starting", "running", "degraded", "stale")
RULES = (None, "standard", "weak", "coherent")
ACTIVITIES = (None, "active", "intermittent")

HEADER = struct.Struct("<BBII")
STATUS_BODY = struct.Struct("<BBIIHHHHh")
MATCH_ROW = struct.Struct("<8sHhHB")
CHANNEL_ROW = struct.Struct("<HIhHHB4s")
TAG_BYTES = 8
MAX_FRAME = 180
MATCHES_PER_FRAME = 10
CHANNELS_PER_FRAME = 8
KEY_BYTES = 32
PIECE = struct.Struct("<HHH")                                           # message, index, count
PIECE_BYTES = MAX_FRAME - HEADER.size - TAG_BYTES - PIECE.size          # 156 bytes of the message per frame
CONTROL_PIECE_BYTES = MAX_FRAME - HEADER.size - 2 - TAG_BYTES - PIECE.size   # 154: after the action and length
MAX_PIECES = 4096
STATE_MAX_BYTES = 256 << 10     # a state, inflated; a real one is 1 to 4 KB
FILE_MAX_BYTES = 4 << 20        # a Pi log, inflated; a real one is under 1 MB
OP_MAX_BYTES = 4 << 10          # an operation from a phone: a sighting's note is at most 500 characters

I16_NONE = -32768
U16_MAX = 0xFFFF
U32_MAX = 0xFFFFFFFF


class FrameError(ValueError):
    """A frame that is malformed, of an unknown version or kind, or not signed with the key."""


def match_key(match_id: str) -> bytes:
    """The 8-byte key of a match id, the same on every route."""
    return hashlib.sha256(str(match_id).encode("utf-8")).digest()[:8]


def key_id(key: bytes) -> str:
    """A short, public name for a key, so an app can tell which car computer a key belongs to."""
    return hashlib.sha256(key).hexdigest()[:8]


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _u16(x, scale=1.0) -> int:
    return min(U16_MAX, max(0, int(round(float(x) * scale)))) if _finite(x) else 0


def _u32(x) -> int:
    return min(U32_MAX, max(0, int(x))) if _finite(x) else 0


def _tenths(x) -> int:
    """A dB figure in tenths, or I16_NONE when unknown."""
    if not _finite(x):
        return I16_NONE
    return min(32767, max(-32767, int(round(float(x) * 10))))


def _from_tenths(v: int):
    return None if v == I16_NONE else v / 10


def pack(obj) -> bytes:
    """Compact JSON, raw DEFLATE: what a state or file message carries. Phones inflate it with their own
    zlib (Apple's COMPRESSION_ZLIB, Java's Inflater(nowrap = true)), so no header and no checksum."""
    deflate = zlib.compressobj(9, zlib.DEFLATED, -15)
    return deflate.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8")) + deflate.flush()


def unpack(data: bytes, limit: int):
    """The JSON a packed message holds. Raises FrameError when it is damaged or inflates past `limit` bytes."""
    inflate = zlib.decompressobj(-15)
    try:
        raw = inflate.decompress(bytes(data), limit + 1)
    except zlib.error as exc:
        raise FrameError("not DEFLATE data") from exc
    if len(raw) > limit or inflate.unconsumed_tail or not inflate.eof:
        raise FrameError("too long, or cut short")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FrameError("not JSON") from exc


def split(data: bytes, size: int) -> list:
    """A message in slices of at most `size` bytes; an empty message is one empty slice."""
    slices = [data[i:i + size] for i in range(0, len(data), size)] or [b""]
    if len(slices) > MAX_PIECES:
        raise ValueError(f"a message of {len(data)} bytes needs more than {MAX_PIECES} pieces")
    return slices


class Assembler:
    """Puts the pieces of messages back together, at most `keep` of them at a time.

    Pieces may come in any order and more than once. A message that never completes is dropped once
    `keep` newer ones have started, so a lost piece costs that one message and nothing else.
    """

    def __init__(self, keep: int = 2, limit: int = FILE_MAX_BYTES):
        self.keep, self.limit, self.parts = keep, limit, OrderedDict()

    def add(self, message: int, index: int, count: int, data: bytes):
        """The whole message once its last piece is in, else None."""
        if not 0 < count <= MAX_PIECES or not 0 <= index < count:
            raise FrameError("piece numbers")
        part = self.parts.get(message)
        if part is None or part["count"] != count:
            part = self.parts[message] = {"count": count, "pieces": {}, "bytes": 0}
            while len(self.parts) > self.keep:
                self.parts.popitem(last=False)
        if index not in part["pieces"]:
            part["bytes"] += len(data)
            if part["bytes"] > self.limit:
                del self.parts[message]
                raise FrameError("message too long")
            part["pieces"][index] = bytes(data)
        if len(part["pieces"]) < count:
            return None
        del self.parts[message]
        return b"".join(part["pieces"][i] for i in range(count))


class Encoder:
    """Signs and numbers frames for one start of the link."""

    def __init__(self, key: bytes, boot: int, counter: int = 0):
        if len(key) != KEY_BYTES:
            raise ValueError("the key must be 32 bytes")
        self.key, self.boot, self.counter = bytes(key), int(boot) & U32_MAX, int(counter)
        self.message = 0                # numbers state and file messages within a boot

    def _frame(self, kind: int, body: bytes) -> bytes:
        self.counter = (self.counter + 1) & U32_MAX
        head = HEADER.pack(VERSION, kind, self.boot, self.counter) + body
        frame = head + hmac.new(self.key, head, hashlib.sha256).digest()[:TAG_BYTES]
        if len(frame) > MAX_FRAME:
            raise ValueError(f"a {KINDS[kind]} frame of {len(frame)} bytes is over {MAX_FRAME}")
        return frame

    def status(self, detector: dict) -> bytes:
        """The detector's state, as `Heartbeat.state()` in display/display_publisher.py gives it."""
        state = detector.get("state")
        body = STATUS_BODY.pack(
            DETECTOR_STATES.index(state) if state in DETECTOR_STATES else 0xFF, 0,
            _u32(detector.get("windows")), _u32(detector.get("hits")),
            _u16(detector.get("quiet_windows")), _u16(detector.get("quiet_alarm_windows")),
            _u16(detector.get("whole_band_windows")), _u16(detector.get("heartbeat_unchanged_s"), 10),
            _tenths(detector.get("floor_db")))
        return self._frame(STATUS, body)

    def matches(self, matches: list) -> list:
        """New matches as one or more frames. Each needs id, ch, strength_db, age_s and rule."""
        frames = []
        for start in range(0, len(matches), MATCHES_PER_FRAME):
            chunk = matches[start:start + MATCHES_PER_FRAME]
            rows = b"".join(MATCH_ROW.pack(
                match_key(m["id"]), _u16(m.get("ch")), _tenths(m.get("strength_db")),
                _u16(m.get("age_s"), 1000), RULES.index(m.get("rule")) if m.get("rule") in RULES else 0)
                for m in chunk)
            frames.append(self._frame(MATCHES, bytes([len(chunk)]) + rows))
        return frames

    def channels(self, rows: list) -> bytes:
        """The channel rows of `Channels.snapshot()`, at most CHANNELS_PER_FRAME of them."""
        rows = rows[:CHANNELS_PER_FRAME]
        packed = []
        for r in rows:
            activity = r.get("activity")
            flags = ((1 if r.get("rising") else 0) | (2 if r.get("call") else 0) | (4 if r.get("approx_time") else 0)
                     | ((ACTIVITIES.index(activity) if activity in ACTIVITIES else 0) << 4))
            packed.append(CHANNEL_ROW.pack(
                _u16(r.get("ch")), _u32(round(float(r.get("freq_mhz") or 0) * 1e4)), _tenths(r.get("strength_db")),
                _u16(r.get("age_s"), 10), _u16(r.get("hits_60s")), flags, match_key(r.get("id", ""))[:4]))
        return self._frame(CHANNELS, bytes([len(rows)]) + b"".join(packed))

    def reply(self, action: int, ok: bool, text: str) -> bytes:
        """The answer to a phone's control frame: done or not, and why, in a few words."""
        words = text.encode("utf-8")[:REPLY_TEXT_BYTES].decode("utf-8", "ignore").encode("utf-8")
        return self._frame(REPLY, bytes([action, 1 if ok else 0, len(words)]) + words)

    def pieces(self, kind: int, data: bytes) -> list:
        """One message (a state or a file, already packed) as numbered, signed pieces, to send at once."""
        message, slices = self.slices(data)
        return [self.piece(kind, message, i, len(slices), s) for i, s in enumerate(slices)]

    def slices(self, data: bytes):
        """A message number and the message's slices, for pieces sent a few at a time: each piece is signed
        and numbered only when it goes (`piece`), so a frame sent between two of them cannot make the next
        one look like a replay."""
        slices = split(bytes(data), PIECE_BYTES)
        self.message = (self.message + 1) & U16_MAX
        return self.message, slices

    def piece(self, kind: int, message: int, index: int, count: int, data: bytes) -> bytes:
        if kind not in (STATE, FILE):
            raise ValueError("only states and files go in pieces")
        return self._frame(kind, PIECE.pack(message, index, count) + bytes(data))

    def state(self, doc: dict) -> list:
        """Everything the car computer tells the server, for an app without internet: `{"v": 1, "state", "ops"}`."""
        return self.pieces(STATE, pack(doc))


def control_frame(key: bytes, boot: int, counter: int, action: int, name: str = "") -> bytes:
    """What a phone writes: `boot` and `counter` from the newest frame it has from this car computer."""
    words = name.encode("ascii") if action == RENAME else b""
    return _control(key, boot, counter, action, words)


def _control(key, boot, counter, action, data):
    head = HEADER.pack(VERSION, CONTROL, boot & U32_MAX, counter & U32_MAX) + bytes([action, len(data)]) + data
    return head + hmac.new(key, head, hashlib.sha256).digest()[:TAG_BYTES]


def op_frames(key: bytes, boot: int, counter: int, message: int, op: dict) -> list:
    """An operation from a phone (1.6.0): its JSON, as the app sends it to the server, in control frames.
    `message` is the phone's own number for it, random, so two phones writing at once do not mix."""
    slices = split(json.dumps(op, separators=(",", ":")).encode("utf-8"), CONTROL_PIECE_BYTES)
    return [_control(key, boot, counter, OP, PIECE.pack(message & U16_MAX, i, len(slices)) + s)
            for i, s in enumerate(slices)]


def decode_control(frame: bytes, key: bytes) -> dict:
    """A phone's control frame, after checking its tag. Raises FrameError; checking boot and counter is the
    caller's, since only the car computer knows its own."""
    frame = bytes(frame)
    if len(frame) < HEADER.size + 2 + TAG_BYTES or len(frame) > MAX_FRAME:
        raise FrameError("wrong length")
    head, tag = frame[:-TAG_BYTES], frame[-TAG_BYTES:]
    if not hmac.compare_digest(tag, hmac.new(key, head, hashlib.sha256).digest()[:TAG_BYTES]):
        raise FrameError("not signed with this key")
    version, kind, boot, counter = HEADER.unpack_from(head)
    if version != VERSION or kind != CONTROL:
        raise FrameError("not a control frame")
    action, length = head[HEADER.size], head[HEADER.size + 1]
    if action not in ACTIONS or len(head) != HEADER.size + 2 + length:
        raise FrameError("unknown action or wrong name length")
    data = head[HEADER.size + 2:]
    if action == OP:
        if len(data) < PIECE.size:
            raise FrameError("operation piece length")
        message, index, count = PIECE.unpack_from(data)
        return {"boot": boot, "counter": counter, "action": "op", "message": message, "index": index,
                "count": count, "data": data[PIECE.size:]}
    try:
        name = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise FrameError("the name is not plain letters") from exc
    return {"boot": boot, "counter": counter, "action": ACTIONS[action], "name": name}


def decode(frame: bytes, key: bytes) -> dict:
    """A frame's fields as a dict, after checking its tag. Raises FrameError."""
    frame = bytes(frame)
    if len(frame) < HEADER.size + TAG_BYTES or len(frame) > MAX_FRAME:
        raise FrameError("wrong length")
    head, tag = frame[:-TAG_BYTES], frame[-TAG_BYTES:]
    if not hmac.compare_digest(tag, hmac.new(key, head, hashlib.sha256).digest()[:TAG_BYTES]):
        raise FrameError("not signed with this key")
    version, kind, boot, counter = HEADER.unpack_from(head)
    if version != VERSION:
        raise FrameError(f"version {version}")
    if kind not in KINDS:
        raise FrameError(f"kind {kind}")
    body = head[HEADER.size:]
    out = {"kind": KINDS[kind], "boot": boot, "counter": counter}
    if kind == REPLY:
        if len(body) < 3 or len(body) != 3 + body[2]:
            raise FrameError("reply length")
        out.update(action=ACTIONS.get(body[0]), ok=body[1] == 1, text=body[3:].decode("utf-8", "replace"))
        return out
    if kind in (STATE, FILE):
        if len(body) < PIECE.size:
            raise FrameError("piece length")
        message, index, count = PIECE.unpack_from(body)
        if not 0 <= index < count <= MAX_PIECES:
            raise FrameError("piece numbers")
        out.update(message=message, index=index, count=count, data=body[PIECE.size:].hex())
        return out
    if kind == STATUS:
        if len(body) != STATUS_BODY.size:
            raise FrameError("status length")
        state, _, windows, hits, quiet, alarm, whole, unchanged, floor = STATUS_BODY.unpack(body)
        out["detector"] = {"state": DETECTOR_STATES[state] if state < len(DETECTOR_STATES) else "unknown",
                           "windows": windows, "hits": hits, "quiet_windows": quiet,
                           "quiet_alarm_windows": alarm or None, "whole_band_windows": whole,
                           "heartbeat_unchanged_s": unchanged / 10, "floor_db": _from_tenths(floor)}
        return out
    row = MATCH_ROW if kind == MATCHES else CHANNEL_ROW
    if not body or len(body) != 1 + body[0] * row.size:
        raise FrameError("row count")
    rows = [row.unpack_from(body, 1 + i * row.size) for i in range(body[0])]
    if kind == MATCHES:
        out["matches"] = [{"key": k.hex(), "ch": ch, "strength_db": _from_tenths(s), "age_s": age / 1000,
                           "rule": RULES[rule] if rule < len(RULES) else None}
                          for k, ch, s, age, rule in rows]
    else:
        out["channels"] = [{"ch": ch, "freq_mhz": freq / 1e4, "strength_db": _from_tenths(s), "age_s": age / 10,
                            "hits_60s": hits, "rising": bool(flags & 1), "call": bool(flags & 2),
                            "approx_time": bool(flags & 4),
                            "activity": ACTIVITIES[flags >> 4] if flags >> 4 < len(ACTIVITIES) else None,
                            "burst": burst.hex()}
                           for ch, freq, s, age, hits, flags, burst in rows]
    return out


class Acceptor:
    """Which signed frames an app takes: within a boot only a higher counter, and never an earlier boot again.

    A copy of a frame sent before is refused, so a recording replayed later cannot raise an alert.
    """

    def __init__(self, remember: int = 32):
        self.boot, self.counter, self.old, self.remember = None, -1, [], remember

    def accept(self, boot: int, counter: int) -> bool:
        if boot == self.boot:
            if counter <= self.counter:
                return False
        elif boot in self.old:
            return False
        else:
            if self.boot is not None:
                self.old = (self.old + [self.boot])[-self.remember:]
            self.boot = boot
        self.counter = counter
        return True


PARITY_STATE = {
    "v": 1,
    "state": {"source": "bluetooth", "session": "20261122-150122-000000Z_watch_382.5MHz", "snapshot_age_s": 0.0,
              "detector": {"state": "stopped", "reasons": ["StreamStall: receiver sample stream stalled"]},
              "channels": [], "matches": [],
              "features": ["mark", "poweroff", "logs", "notify", "doctor", "bluetooth", "support"],
              "pi": {"temp_c": 48.2, "uptime_s": 612.0, "load_1m": 0.8, "disk_free_gb": 401.3},
              "link": {"via": "none", "networks": []}, "notify": {"pushover": True, "configured": True},
              "software": "1.6.0",
              "bluetooth": {"on": True, "state": "connected", "name": "Slot17", "subscribed": True, "key_id": "630dcd29"},
              "doctor": {"items": [["Bluetooth", "ok", "on, as Slot17"], ["Network", "PROBLEM", "no network"]],
                         "age_s": 12.5}},
    "ops": [{"id": "a" * 32, "kind": "mark", "state": "done", "detail": "Saved on the Pi"},
            {"id": "b" * 32, "kind": "poweroff", "state": "sent", "detail": ""}],
}
PARITY_FILE = {"v": 1, "op": "c" * 32, "kind": "pi-log",
               "text": "== journal ==\nbluetooth: starting on 2C:CF:67:93:A4:8E as 'Slot17'\n"
                       "Detector failed: StreamStall: receiver sample stream stalled\n"}
# PARITY_STATE packed once at level 9 (zlib 1.3.1.zlib-ng). A constant, because zlib and zlib-ng make different
# bytes for the same input: the fixture must be the same on every computer, and the apps must inflate real
# compressed data. bluetooth_frames.unpack(PARITY_STATE_Z) == PARITY_STATE is tested.
PARITY_STATE_Z = bytes.fromhex(
    "85524b8f9b3010fe2b68ce140169d294e34a957ae8b6abe6d628420e0cc1023c963d24daaef8ef1d1392664feb8b1fe3f95e"
    "f61b9ca1c862f0ac18a178034fa3ab6405c77e4426e216a48ade6b32729aa7f926cbf2fc53b64ec394cee34f79515cb5e56a"
    "9b27ebe7ef7f438b51d6b7c4a53a61e9a1489334861a192b2637135d1965266bb1961687ca9391bb7bd8b16c861dabbe2f22"
    "8715ea33bac8abc1f618f9b9289354a5f130c550b5ca18ec43ef21862188c165d3a0e2d1cd3b29b84e882c5dd051d3c8b2a7"
    "9397c910ebe6551635cdfae2f7f6476bc931089ad5413be360cb0a8acfdb248f61b4ac87d9e426cb83cd9e545d664330bd15"
    "48edbbb27188e5e9282d6996ac4471af4d17a0ce5a4908860c0619c81772ddac7cbaab925b76942c250228d88d287ec934fa"
    "24b6eaeb895cf6d4f045b91069966c92f49d078108cf776dbe252f20469e63cedea8211ced7ae2eccb6cf9e82ba78f378218"
    "3a7c2db5ec60b34aebaacebfc2748f4be0b5641274efe1e921390a71938923e5a305fb10efe1e7d5a7d45e7eff7afaf1ed79"
    "7e8268b10f07097af93612e87a1222b2015c688202f5c110b44e9b707379f09be3fa1ab37c43a5fb60579db18ec844dc62f4"
    "a2c5d1c270fc60fc6778f84bf71f8d861f59603a4cff00")


def stored(data: bytes) -> bytes:
    """`data` as raw DEFLATE stored blocks (no compression): the same bytes on every computer."""
    out = bytearray()
    for start in range(0, max(1, len(data)), 0xFFFF):
        block = data[start:start + 0xFFFF]
        last = start + 0xFFFF >= len(data)
        out += bytes([1 if last else 0]) + struct.pack("<HH", len(block), len(block) ^ 0xFFFF) + block
    return bytes(out)


def parity_fixture() -> dict:
    """The frames in shared/parity/ble-frames.json, made with a fixed key so every app can check them."""
    key = bytes(range(KEY_BYTES))
    enc = Encoder(key, boot=0x5107B007)
    detector = {"state": "running", "windows": 14400, "hits": 37, "quiet_windows": 0, "quiet_alarm_windows": 240,
                "whole_band_windows": 12, "heartbeat_unchanged_s": 0.4, "floor_db": -1.6}
    matches = [{"id": "20261122-150122-000000Z_watch_382.5MHz/9f2c", "ch": 154, "strength_db": 55.2, "age_s": 0.12,
                "rule": "standard"},
               {"id": "20261122-150122-000000Z_watch_382.5MHz/a03e", "ch": 9, "strength_db": None, "age_s": 1.5,
                "rule": "coherent"}]
    channels = [{"ch": 154, "id": matches[0]["id"], "freq_mhz": 383.8625, "strength_db": 55.2, "age_s": 0.1,
                 "hits_60s": 3, "activity": "active", "approx_time": False, "rising": True, "call": False},
                {"ch": 9, "id": matches[1]["id"], "freq_mhz": 380.2375, "strength_db": None, "age_s": 1.5,
                 "hits_60s": 1, "activity": None, "approx_time": True, "rising": False, "call": True}]
    frames = [("status", enc.status(detector)), ("matches", enc.matches(matches)[0]), ("channels", enc.channels(channels)),
              ("reply", enc.reply(RENAME, True, "Bluetooth name: Car 2"))]
    controls = [{"action": "rename", "name": "Car 2", "boot": enc.boot, "counter": 3,
                 "hex": control_frame(key, enc.boot, 3, RENAME, "Car 2").hex()},
                {"action": "off", "name": "", "boot": enc.boot, "counter": 4, "hex": control_frame(key, enc.boot, 4, OFF).hex()}]
    tampered = bytearray(frames[1][1])
    tampered[12] ^= 0x01
    if unpack(PARITY_STATE_Z, STATE_MAX_BYTES) != PARITY_STATE:
        raise AssertionError("PARITY_STATE_Z no longer holds PARITY_STATE: make it again")
    messages = [("state", STATE, PARITY_STATE_Z, PARITY_STATE), ("file", FILE, stored(json.dumps(
        PARITY_FILE, separators=(",", ":")).encode()), PARITY_FILE)]
    op = {"kind": "mark", "id": "d" * 32, "label": "police", "age_s": 1.5, "phone_utc": 1795434100.25,
          "note": "two cars, blue lights, on the A12 towards Arnhem; a note long enough for a second piece"}
    return {
        "about": "Frames of the Bluetooth link (p0/bluetooth_frames.py). Every app must decode each frame to "
                 "`decoded` with `key`, refuse `tampered`, and give each match id its `key`. 1.6.0: each of "
                 "`messages` is pieces that put together and inflated (raw DEFLATE) give `json`; each of "
                 "`operations` is the control frames an app writes for `op`.",
        "key": key.hex(), "key_id": key_id(key), "can": CAN,
        "frames": [{"name": name, "hex": f.hex(), "decoded": decode(f, key)} for name, f in frames],
        "messages": [{"name": name, "kind": KINDS[kind], "frames": [f.hex() for f in enc.pieces(kind, data)],
                      "json": doc} for name, kind, data, doc in messages],
        "operations": [{"op": op, "text": json.dumps(op, separators=(",", ":")), "boot": enc.boot, "counter": 9,
                        "message": 0x2A17,
                        "frames": [f.hex() for f in op_frames(key, enc.boot, 9, 0x2A17, op)]}],
        "controls": controls,
        "tampered": bytes(tampered).hex(),
        "match_keys": [{"id": m["id"], "key": match_key(m["id"]).hex()} for m in matches],
        "sequence": [{"boot": 7, "counter": 1, "accept": True}, {"boot": 7, "counter": 2, "accept": True},
                     {"boot": 7, "counter": 2, "accept": False}, {"boot": 9, "counter": 1, "accept": True},
                     {"boot": 7, "counter": 3, "accept": False}, {"boot": 9, "counter": 2, "accept": True}],
    }


if __name__ == "__main__":
    print(json.dumps(parity_fixture(), indent=2))
