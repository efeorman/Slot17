"""The Bluetooth link's wire format, version 1: what the car computer sends the phone apps.

Standard library only. The iPhone and Android apps decode the same bytes; `shared/parity/ble-frames.json`
holds frames that all three must read identically (regenerate it with `python -B p0/bluetooth_frames.py`).
docs/design/BLUETOOTH.md describes the link as a whole.

Every frame, little-endian:

    offset  size  field
    0       1     version, 1
    1       1     kind: 1 status, 2 matches, 3 channels, 4 reply; 16 control (from the phone)
    2       4     boot: random for each start of the link (uint32)
    6       4     counter: one higher for every frame within a boot (uint32)
    10      n     body, per kind (below)
    10+n    8     tag: the first 8 bytes of HMAC-SHA256(key, all bytes before the tag)

A frame is at most MAX_FRAME bytes, so it fits in one notification once the phone has asked for a
larger MTU, which iPhones and Android phones do by themselves.

Matches carry exactly what the display server passes on for a match, so a sound never depends on the
route that delivered the match first. Their key is the first 8 bytes of SHA-256 of the match's id;
the apps compute the same key for matches from the server, and play each key once.

A control frame goes the other way, written by a phone to the control characteristic: switch the link
off, or rename it. Its boot and counter are those of a frame the car computer sent lately, and each counter
works once, so a recorded command cannot be played back later. The car computer answers with a reply.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import struct

VERSION = 1
SERVICE_UUID = "f9cb0001-aa93-4349-a576-0e70a736266f"
FRAMES_UUID = "f9cb0002-aa93-4349-a576-0e70a736266f"     # notify: the frames
INFO_UUID = "f9cb0003-aa93-4349-a576-0e70a736266f"       # read: JSON about this car computer
CONTROL_UUID = "f9cb0004-aa93-4349-a576-0e70a736266f"    # write: a control frame from a phone

STATUS, MATCHES, CHANNELS, REPLY, CONTROL = 1, 2, 3, 4, 16
KINDS = {STATUS: "status", MATCHES: "matches", CHANNELS: "channels", REPLY: "reply"}
OFF, RENAME = 1, 2
ACTIONS = {OFF: "off", RENAME: "rename"}
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


class Encoder:
    """Signs and numbers frames for one start of the link."""

    def __init__(self, key: bytes, boot: int, counter: int = 0):
        if len(key) != KEY_BYTES:
            raise ValueError("the key must be 32 bytes")
        self.key, self.boot, self.counter = bytes(key), int(boot) & U32_MAX, int(counter)

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


def control_frame(key: bytes, boot: int, counter: int, action: int, name: str = "") -> bytes:
    """What a phone writes: `boot` and `counter` from the newest frame it has from this car computer."""
    words = name.encode("ascii") if action == RENAME else b""
    head = HEADER.pack(VERSION, CONTROL, boot & U32_MAX, counter & U32_MAX) + bytes([action, len(words)]) + words
    return head + hmac.new(key, head, hashlib.sha256).digest()[:TAG_BYTES]


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
    try:
        name = head[HEADER.size + 2:].decode("ascii")
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
    return {
        "about": "Frames of the Bluetooth link (p0/bluetooth_frames.py). Every app must decode each frame to "
                 "`decoded` with `key`, refuse `tampered`, and give each match id its `key`.",
        "key": key.hex(), "key_id": key_id(key),
        "frames": [{"name": name, "hex": f.hex(), "decoded": decode(f, key)} for name, f in frames],
        "controls": controls,
        "tampered": bytes(tampered).hex(),
        "match_keys": [{"id": m["id"], "key": match_key(m["id"]).hex()} for m in matches],
        "sequence": [{"boot": 7, "counter": 1, "accept": True}, {"boot": 7, "counter": 2, "accept": True},
                     {"boot": 7, "counter": 2, "accept": False}, {"boot": 9, "counter": 1, "accept": True},
                     {"boot": 7, "counter": 3, "accept": False}, {"boot": 9, "counter": 2, "accept": True}],
    }


if __name__ == "__main__":
    print(json.dumps(parity_fixture(), indent=2))
