#!/usr/bin/env python3
"""Phone display for the Slot17 detector: accepts pushes from the car, streams them to the phone.

Standard library only; state lives in memory. Two secrets in config.json (mode
0600): `pi_token` authorises pushes from the Pi, `viewer_key` authorises the
phone. Opening `/?key=<viewer_key>` once stores a cookie, so the address can be
bookmarked without the key. The key is never logged.

The phone receives each push the moment it arrives over Server-Sent Events
(`/api/stream`), plus a repeat every two seconds so it can tell a live link from
a dead one. `/api/state` returns the same message once (fallback and auth probe).

The operator currently uses plain HTTP; both secrets travel unencrypted.

    python3 server.py                 # listen on the configured port
    python3 server.py --init          # create config.json with fresh secrets
"""
import argparse
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
CONFIG = Path(os.environ.get("SLOT17_DISPLAY_CONFIG", HERE / "config.json"))
PAGE = HERE / "page.html"
# Home-screen app: name, colours and icons. Public, like any site's icon: none of it is private.
MANIFEST = json.dumps({
    "name": "Slot17", "short_name": "Slot17", "start_url": "/", "scope": "/", "display": "standalone",
    "background_color": "#0b0e13", "theme_color": "#0b0e13",
    "icons": [{"src": "/icon-180.png", "sizes": "180x180", "type": "image/png"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}]})
ICONS = {"/icon-180.png": "icon-180.png", "/apple-touch-icon.png": "icon-180.png", "/icon-512.png": "icon-512.png"}
# A home-screen app on iPhone keeps its own cookies, apart from Safari's, so it starts without the
# private link's cookie. This page lets the person paste the link (or only its key) there.
LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0e13"><meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="Slot17">
<link rel="manifest" href="/manifest.webmanifest"><link rel="apple-touch-icon" href="/icon-180.png">
<link rel="icon" type="image/png" href="/icon-180.png"><title>Slot17</title><style nonce="__NONCE__">
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0e13;color:#e8edf4;
font:16px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:24px;box-sizing:border-box}
form{max-width:380px;width:100%;display:grid;gap:12px}h1{margin:0;font-size:26px}p{color:#7d8896;margin:0}
input,button{font:inherit;padding:14px;border-radius:14px;border:1px solid #232b38;background:#131821;color:#e8edf4}
button{background:#2fd26b;color:#0b0e13;font-weight:700;border:0}.bad{color:#ff4d4f}</style></head>
<body><form method="get" action="/"><h1>Slot17</h1><p>__NOTE__</p>
<input name="link" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="http://…/?key=…"
 aria-label="Private link"><button type="submit">Open</button>
<p>This device keeps it for a year. Treat the link like a password.</p></form></body></html>"""
MAX_BODY = 64 * 1024
MAX_LOG_BODY = 1024 * 1024  # one uploaded app or Pi log
LOG_BODY_DEADLINE_S = 45.0  # a hotspot upload of a whole log is slower than a push
LOG_KEEP_FILES = 200                # of each kind
LOG_KEEP_BYTES = 100 * 1024 * 1024  # of each kind
LOG_NAME = re.compile(r"^(app|pi|report)-[0-9]{8}-[0-9]{6}Z-[0-9a-f]{8}\.log$")
OP_ID = re.compile(r"^[0-9a-f]{32}$")
# Settings the app reads from here, so sounds, rules and timings change without an app update.
APP_CONFIG_MAX = 64 * 1024
SOUND_NAME = re.compile(r"^[a-z0-9_-]{1,40}\.wav$")
SOUND_MAX = 2 * 1024 * 1024


class AppConfig:
    """The app's settings file, re-read when it changes. A broken file is reported, never served."""

    def __init__(self, path):
        self.path = Path(path)
        self.stamp = None
        self.body = None
        self.error = ""
        self.lock = threading.Lock()

    def load(self):
        with self.lock:
            try:
                stat = self.path.stat()
            except OSError:
                self.stamp, self.body, self.error = None, None, "no settings file"
                return None
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp != self.stamp:
                self.stamp = stamp
                try:
                    if stat.st_size > APP_CONFIG_MAX:
                        raise ValueError("settings file too large")
                    raw = self.path.read_bytes()
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise ValueError("settings must be a JSON object")
                    version = hashlib.sha256(raw).hexdigest()[:12]
                    self.body = json.dumps({"version": version, "config": data}, separators=(",", ":")).encode()
                    self.error = ""
                except OSError as exc:          # may be a moment's trouble: read it again next time
                    self.stamp, self.body, self.error = None, None, f"{type(exc).__name__}: {exc}"[:200]
                except (ValueError, RecursionError) as exc:
                    self.body, self.error = None, f"{type(exc).__name__}: {exc}"[:200]
            return self.body

# How long a queued operation may wait for the Pi. A power-off or stop that outlives
# its moment must never fire at the start of some later drive; a sighting stays
# useful, and the phone keeps its own copy until it hears the Pi saved it.
# A Pushover switch made while the car is off waits for the next drive, like a sighting.
OP_TTL_S = {"poweroff": 30.0, "logs": 120.0, "mark": 86400.0, "notify": 86400.0, "doctor": 120.0,
            "bluetooth": 86400.0}
STOP_TTL_S = 60.0
OP_RESEND_S = 30.0          # a sighting the Pi took but never answered is offered again
MAX_OPS = 64
MAX_OP_BATCH = 16           # handed to the Pi at once; the rest wait for its next request
OP_ANSWER_S = 180.0         # a command handed over and never answered is reported, not left "sent"
# The Pi loses power with the ignition, so it can never send a log at the end of a drive. The
# server asks for one by itself while the Pi is on: the newest then covers the drive to within
# this long of its end. The first comes early, so even a short drive leaves one.
AUTO_PI_LOG_S = 600.0
AUTO_PI_LOG_FIRST_S = 120.0
# A signed software release for the car computer, put here by the owner. The server only stores and
# hands it out: it holds no signing key, and the car computer checks the signature itself.
RELEASE_MAX = 20 << 20
UPDATE_STATES = {"off", "current", "staged", "installed", "rejected", "error"}
TERMINAL_OP_STATES = {"done", "failed", "expired"}
MARK_LABELS = {"police", "ambulance", "fire", "unknown", "noise", "antenna-change"}
COOKIE = "s17"
# Release check (1.2.9): which published version is the newest, so the app and page can say "update
# available". Only the public release is asked for; nothing about this server or its car is sent.
RELEASE_REPO = "efeorman/Slot17"          # config "release_repo" for a fork; "release_check": false turns it off
RELEASE_FIRST_CHECK_S = 60.0
RELEASE_CHECK_EVERY_S = 6 * 3600.0
RELEASE_MAX_BYTES = 256 * 1024
# Updates for everyone (1.3.1): the newest GitHub release carries the car computer update the Slot17 team
# signed. With "release_updates": true (new servers) this server copies it into release/, where the car
# computer fetches it. The server checks only size and checksum; the car computer checks the signature
# against the public key it was given at setup, so this server can't supply code of its own.
UPDATE_JSON, UPDATE_BUNDLE = "slot17-update.json", "slot17-update.tar.gz"
MATCH_RULES = ("standard", "weak", "coherent")   # how a match was recognised; anything else is dropped
PI_ON_WITHIN_S = 30
SOCKET_TIMEOUT_S = 15.0     # idle limit for any read or write on a connection
BODY_DEADLINE_S = 10.0      # a request body must arrive completely within this
HEADER_DEADLINE_S = 10.0    # a complete request line and headers must arrive within this
MAX_THREADS = 32            # concurrent connections of any kind
MAX_PER_ADDRESS = 12        # of those, from one address: the Pi and phone often share one
MAX_STREAMS = 8             # concurrent phone streams
STREAM_REPEAT_S = 2.0
TICKET_TTL_S = 6.0
AGE_MARGIN_S = 0.02         # covers field rounding and short clock-rate differences
STATES = {"running", "starting", "degraded", "stale", "stopped"}


class Store:
    def __init__(self):
        self.cond = threading.Condition()
        self.boot = secrets.token_hex(4)       # lets the phone detect a server restart
        self.release = None                    # the newest published release, from ReleaseCheck
        self.seq = 0
        self.latest = None
        self.doctor = None                      # (verdicts, monotonic time received)
        self.received_mono = None
        self.source = None
        self.source_seq = -1
        self.retired = set()
        self.tickets = {}
        # One pending operator command, collected by the Pi on its next clock
        # challenge. Deliberately a single slot and deliberately stop-only: the
        # Pi is an outbound client with no inbound path, and the publisher only
        # exists while the detector does - so there is nothing here that could
        # start a receiver, and nothing that could cycle the Airspy, which is
        # the failure mode rule 3 in CONTRIBUTING.md exists to prevent.
        self.command = None
        self.command_at = None
        self.command_state = "none"
        # Operations from the app, handed to the Pi the same way. Delivered only to a
        # publisher that declared it understands them, so an old one simply never
        # receives them and the phone can say why.
        self.ops = {}
        self.features = []
        self.auto_log_s = AUTO_PI_LOG_S     # 0 turns the server's own log requests off
        self.auto_log_due = None            # monotonic time of the next one

    def add_op(self, kind, op_id, payload):
        """Queue an operation. Idempotent by id: the phone retries until it hears back."""
        with self.cond:
            now = time.monotonic()
            self._expire_ops(now)
            if op_id in self.ops:
                return self._op_public(self.ops[op_id], now)
            self._queue(kind, op_id, payload, now)
            self.cond.notify_all()
            return self._op_public(self.ops[op_id], now)

    def _queue(self, kind, op_id, payload, now):
        if len(self.ops) >= MAX_OPS:
            finished = [k for k, v in self.ops.items() if v["state"] in TERMINAL_OP_STATES]
            if not finished:
                raise ValueError("too many queued operations")
            del self.ops[finished[0]]
        self.ops[op_id] = {"id": op_id, "kind": kind, "payload": payload, "state": "pending",
                           "created": now, "sent": None, "detail": ""}

    def _auto_log(self, now, was_on):
        """Ask the Pi for its log now and then, so every drive leaves one. Called with the lock held."""
        if not self.auto_log_s or "logs" not in self.features:
            return
        if not was_on or self.auto_log_due is None:      # the Pi has just come on: a new drive
            self.auto_log_due = now + min(AUTO_PI_LOG_FIRST_S, self.auto_log_s)
        if now < self.auto_log_due:
            return
        self.auto_log_due = now + self.auto_log_s
        if any(op["kind"] == "logs" and op["state"] not in TERMINAL_OP_STATES for op in self.ops.values()):
            return                                       # the phone asked for one already
        try:
            self._queue("logs", secrets.token_hex(16), {"auto": True}, now)
        except ValueError:
            pass                                         # a full queue of the phone's requests comes first

    def take_ops(self):
        with self.cond:
            now = time.monotonic()
            self._expire_ops(now)
            out = []
            for op in self.ops.values():
                if len(out) >= MAX_OP_BATCH:
                    break
                due = op["state"] == "pending" or (op["kind"] == "mark" and op["state"] == "sent"
                                                   and now - op["sent"] > OP_RESEND_S)
                if due and op["kind"] in self.features:
                    op["state"], op["sent"] = "sent", now
                    out.append(dict({k: v for k, v in op["payload"].items() if k != "auto"}, id=op["id"], kind=op["kind"],
                                    age_s=round(now - op["created"] + op["payload"].get("age_s", 0.0), 3)))
            return out

    def apply_acks(self, acks):
        with self.cond:
            for ack in acks:
                op = self.ops.get(ack["id"])
                if op is not None:
                    op["state"], op["detail"] = ack["state"], ack.get("detail", "")
            self.cond.notify_all()

    def _expire_ops(self, now):
        for op in self.ops.values():
            if op["state"] == "pending" and now - op["created"] > OP_TTL_S[op["kind"]]:
                op["state"], op["detail"] = "expired", "The Pi did not collect it in time"
            # Not resent: a stop, log or power off must not run twice. A sighting is resent instead.
            elif op["state"] == "sent" and op["kind"] != "mark" and now - op["sent"] > OP_ANSWER_S:
                op["state"], op["detail"] = "failed", "The Pi did not answer"

    @staticmethod
    def _op_public(op, now):
        return {"id": op["id"], "kind": op["kind"], "state": op["state"],
                "detail": op["detail"], "age_s": round(now - op["created"], 1)}

    def request_stop(self, note=""):
        """Queue the operator's stop. Idempotent: asking twice changes nothing."""
        with self.cond:
            if self.command_state != "pending":
                self.command = {"action": "stop", "note": str(note)[:120],
                                "id": secrets.token_hex(8)}
                self.command_at = time.monotonic()
                self.command_state = "pending"
            return dict(self.command, state=self.command_state)

    def take_command(self):
        """Hand the pending command to the Pi, exactly once."""
        with self.cond:
            self._expire_stop(time.monotonic())
            if self.command_state != "pending":
                return None
            self.command_state = "sent"
            self.command_at = time.monotonic()
            return dict(self.command)

    def command_status(self):
        with self.cond:
            return self._command_status(time.monotonic())

    def _expire_stop(self, now):
        """A stop nobody collected must not end the next drive's session when the Pi returns."""
        if self.command_state == "pending" and now - self.command_at > STOP_TTL_S:
            self.command_state = "expired"

    def ticket(self, source):
        """Fence publisher instances and timestamp a one-use upload challenge.

        State stays in memory. Retired identities are never reactivated during
        this server boot; fail closed at the cap rather than evict that fence.
        """
        with self.cond:
            now = time.monotonic()
            self.tickets = {k: v for k, v in self.tickets.items() if now - v[1] <= TICKET_TTL_S}
            if source != self.source:
                if source in self.retired or len(self.retired) >= 4096:
                    raise ValueError("publisher retired")
                if self.source is not None:
                    self.retired.add(self.source)
                self.source, self.source_seq = source, -1
                self.tickets.clear()
            if len(self.tickets) >= 128:
                raise ValueError("too many tickets")
            nonce = secrets.token_urlsafe(24)
            self.tickets[nonce] = (source, now)
            return nonce

    def put(self, state):
        with self.cond:
            now = time.monotonic()
            ticket = self.tickets.pop(state.pop("ticket"), None)
            if (ticket is None or ticket[0] != state["source"] or state["source"] != self.source
                    or now - ticket[1] > TICKET_TTL_S):
                raise ValueError("expired or superseded upload")
            if state["seq"] <= self.source_seq:
                return False                # retry/out-of-order POST cannot refresh the snapshot
            self.source_seq = state["seq"]
            self.features = state.get("features", [])
            self._auto_log(now, self.received_mono is not None and now - self.received_mono < PI_ON_WITHIN_S)
            self.apply_acks(state.pop("acks", []))
            if state["detector"]["state"] == "stopped" and self.command_state == "sent":
                self.command_state = "done"
            # The publisher aged its snapshot AFTER receiving this challenge.
            # Counting the whole challenge round trip safely includes upload
            # transit (and overcounts the challenge's return leg).
            upper = now - ticket[1] + AGE_MARGIN_S
            state["snapshot_age_s"] += upper
            for row in state["channels"] + state["matches"]:
                row["age_s"] += upper
            # The doctor's verdicts come now and then, not with every push: keep the last ones.
            if "doctor" in state:
                self.doctor = (state["doctor"], now)
            elif self.doctor is not None:
                verdicts, at = self.doctor
                state["doctor"] = dict(verdicts, age_s=round(verdicts["age_s"] + now - at, 1))
            self.seq += 1
            self.latest, self.received_mono = state, now
            self.cond.notify_all()
            return True

    def pi_on(self, within_s=PI_ON_WITHIN_S):
        """Whether the Pi pushed recently. Says nothing about the detector or what it hears."""
        with self.cond:
            return self.received_mono is not None and time.monotonic() - self.received_mono < within_s

    def message(self):
        with self.cond:
            return self._message()

    def _message(self):
        now = time.monotonic()
        age = None if self.received_mono is None else now - self.received_mono
        out = {"boot": self.boot, "seq": self.seq, "server_mono_s": now,
               "server_age_s": age, "state": self.latest}
        if self.release:
            out["release"] = dict(self.release)
        status = self._command_status(now)
        if status:
            out["command"] = status
        self._expire_ops(now)
        # The server's own log requests are not the phone's business: it never asked for them.
        shown = [self._op_public(op, now) for op in self.ops.values() if not op["payload"].get("auto")]
        if shown:
            out["ops"] = shown
        return out

    def _command_status(self, now):
        self._expire_stop(now)
        if self.command_state == "none":
            return None
        return {"action": (self.command or {}).get("action"), "state": self.command_state,
                "age_s": round(now - (self.command_at or now), 1)}

    def wait(self, after_seq, timeout):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != after_seq, timeout=timeout)
            return self._message()


# ---------------------------------------------------------------- validation
def _num(x, lo, hi, nd=2):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or not lo <= x <= hi:
        raise ValueError
    return round(float(x), nd)


def _int(x, lo, hi):
    if isinstance(x, bool) or not isinstance(x, int) or not lo <= x <= hi:
        raise ValueError
    return x


def _str(x, n):
    if not isinstance(x, str) or len(x) > n:
        raise ValueError
    return x


def _opt(fn, x, *a):
    return None if x is None else fn(x, *a)


def clean_state(p):
    """Rebuild an authenticated push from known, typed, bounded fields; ValueError if malformed."""
    if not isinstance(p, dict) or p.get("v") != 3:
        raise ValueError
    d = p.get("detector")
    if not isinstance(d, dict) or not isinstance(d.get("state"), str) or d["state"] not in STATES:
        raise ValueError
    det = {"state": d["state"]}
    if "heartbeat_unchanged_s" in d:
        det["heartbeat_unchanged_s"] = _num(d["heartbeat_unchanged_s"], 0, 1e7, 1)
    if "reasons" in d:
        if not isinstance(d["reasons"], list) or len(d["reasons"]) > 6:
            raise ValueError
        det["reasons"] = [_str(r, 60) for r in d["reasons"]]
    if "floor_db" in d:
        det["floor_db"] = _num(d["floor_db"], -300, 300, 1)
    for key in ("windows", "hits", "unchecked", "partial", "quiet_windows", "whole_band_windows"):
        if key in d:
            det[key] = _int(d[key], 0, 10**9)
    if "quiet_alarm_windows" in d:
        det["quiet_alarm_windows"] = _int(d["quiet_alarm_windows"], 1, 10**6)
    if "window_s" in d:
        det["window_s"] = _num(d["window_s"], 0.1, 60, 3)
    if "input_peak_counts" in d:
        det["input_peak_counts"] = _num(d["input_peak_counts"], 0, 1e9, 1)
    channels, matches = p.get("channels"), p.get("matches", [])
    if not isinstance(channels, list) or len(channels) > 8 or not isinstance(matches, list) or len(matches) > 20:
        raise ValueError
    out_ch = []
    for c in channels:
        if not isinstance(c, dict):
            raise ValueError
        out_ch.append({"ch": _int(c.get("ch"), 0, 4000), "id": _str(c.get("id", ""), 160),
                       "freq_mhz": _num(c.get("freq_mhz"), 0, 1000, 4),
                       "strength_db": _opt(_num, c.get("strength_db"), -20, 150, 1),
                       "age_s": _num(c.get("age_s"), 0, 3600), "hits_60s": _int(c.get("hits_60s"), 0, 10**6),
                       "activity": _opt(_str, c.get("activity"), 20),
                       "approx_time": bool(c.get("approx_time", False)),
                       "rising": c.get("rising") is True, "call": c.get("call") is True})
    out_m = []
    for m in matches:
        if not isinstance(m, dict):
            raise ValueError
        out_m.append({"id": _str(m.get("id"), 160), "ch": _int(m.get("ch"), 0, 4000),
                      "strength_db": _opt(_num, m.get("strength_db"), -20, 150, 1),
                      "age_s": _num(m.get("age_s"), 0, 3600), "delay_s": _num(m.get("delay_s"), 0, 3600),
                      "rising": m.get("rising") is True, "call": m.get("call") is True,
                      **({"rule": m["rule"]} if m.get("rule") in MATCH_RULES else {})})
    source, ticket = _str(p.get("source"), 64), _str(p.get("ticket"), 64)
    if not source or not ticket:
        raise ValueError
    out = {"v": 3, "source": source, "ticket": ticket,
           "snapshot_age_s": _num(p.get("snapshot_age_s"), 0, 3600, 6),
           "seq": _int(p.get("seq"), 0, 10**12), "session": _opt(_str, p.get("session"), 80),
           "detector": det, "channels": out_ch, "matches": out_m}
    # Optional, so an older publisher keeps working unchanged.
    if "features" in p:
        if not isinstance(p["features"], list) or len(p["features"]) > 8:
            raise ValueError
        out["features"] = [_str(f, 20) for f in p["features"]]
    if "acks" in p:
        if not isinstance(p["acks"], list) or len(p["acks"]) > 32:
            raise ValueError
        out["acks"] = []
        for ack in p["acks"]:
            if not isinstance(ack, dict) or not OP_ID.match(_str(ack.get("id"), 32)):
                raise ValueError
            if not isinstance(ack.get("state"), str) or ack["state"] not in ("done", "failed"):
                raise ValueError
            out["acks"].append({"id": ack["id"], "state": ack["state"], "detail": _str(ack.get("detail", ""), 160)})
    if "pi" in p:
        if not isinstance(p["pi"], dict):
            raise ValueError
        ranges = {"uptime_s": (0, 1e9), "temp_c": (-40, 150), "load_1m": (0, 1e4), "disk_free_gb": (0, 1e6),
                  "mem_free_mb": (0, 1e7), "session_s": (0, 1e9), "dropped_samples": (0, 1e15),
                  "throttled": (0, 2**32)}
        out["pi"] = {k: _num(p["pi"][k], *ranges[k], 2) for k in ranges if p["pi"].get(k) is not None}
    if "link" in p:
        try:
            out["link"] = clean_link(p["link"])
        except ValueError:
            pass                                # an odd detail never refuses the push it came with
    if isinstance(p.get("software"), str) and re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", p["software"]):
        out["software"] = p["software"]
    if isinstance(p.get("notify"), dict) and isinstance(p["notify"].get("pushover"), bool):
        out["notify"] = {"pushover": p["notify"]["pushover"], "configured": p["notify"].get("configured") is True}
    if "bluetooth" in p:
        try:
            out["bluetooth"] = clean_bluetooth(p["bluetooth"])
        except ValueError:
            pass                                # an odd detail never refuses the push it came with
    if "doctor" in p:
        try:
            out["doctor"] = clean_doctor(p["doctor"])
        except ValueError:
            pass                                # an odd detail never refuses the push it came with
    if "update" in p:
        u = p["update"]
        if not isinstance(u, dict) or not isinstance(u.get("state"), str) or u["state"] not in UPDATE_STATES:
            raise ValueError
        out["update"] = {"state": u["state"], "commit": _str(u.get("commit", ""), 12),
                         "seq": _int(u.get("seq", 0), 0, 10**9), "detail": _str(u.get("detail", ""), 120)}
    return out


LINK_KINDS = {"cable", "4G stick", "Wi-Fi", "none"}
DOCTOR_LEVELS = {"ok", "note", "PROBLEM"}
BLUETOOTH_STATES = {"off", "starting", "advertising", "connected", "error", "unavailable"}
# The same rule as p0/bluetooth_config.py: what every phone shows the same way.
BLUETOOTH_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,19}")
KEY_ID = re.compile(r"[0-9a-f]{8}")


def clean_bluetooth(b):
    """The car computer's Bluetooth link (1.4.0): its switch, its name and how it is doing; bounded."""
    if not isinstance(b, dict) or not isinstance(b.get("on"), bool):
        raise ValueError
    out = {"on": b["on"], "state": b["state"] if b.get("state") in BLUETOOTH_STATES else "off",
           "subscribed": b.get("subscribed") is True}
    name = b.get("name")
    if isinstance(name, str) and BLUETOOTH_NAME.fullmatch(name) and name == name.strip():
        out["name"] = name
    if isinstance(b.get("key_id"), str) and KEY_ID.fullmatch(b["key_id"]):
        out["key_id"] = b["key_id"]
    for key, lo, hi in (("frames", 0, 10**12), ("matches", 0, 10**12), ("mtu", 23, 517)):
        if b.get(key) is not None:
            try:
                out[key] = _int(b[key], lo, hi)
            except (ValueError, TypeError):
                pass
    if b.get("since_s") is not None:
        try:
            out["since_s"] = _num(b["since_s"], 0, 1e9, 1)
        except (ValueError, TypeError):
            pass
    if isinstance(b.get("error"), str) and b["error"]:
        out["error"] = b["error"][:120]
    return out


def clean_doctor(d):
    """The car computer's `setup.sh doctor` verdicts (1.3.1): a few lines of plain words, bounded."""
    if not isinstance(d, dict) or not isinstance(d.get("items"), list) or len(d["items"]) > 16:
        raise ValueError
    items = []
    for row in d["items"]:
        if not isinstance(row, list) or len(row) != 3 or row[1] not in DOCTOR_LEVELS:
            raise ValueError
        items.append([_str(row[0], 40), row[1], _str(row[2], 240)])
    return {"items": items, "age_s": _num(d.get("age_s", 0), 0, 86400 * 30, 1)}


def clean_link(link):
    """The car computer's connection details, rebuilt from known fields only.

    A detail for the app: a field that is wrong is dropped or trimmed here, never allowed to refuse
    the push it came with.
    """
    if not isinstance(link, dict):
        raise ValueError

    def text(value, n):
        return value[:n] if isinstance(value, str) else None

    def num(value, lo, hi):
        try:
            return _num(value, lo, hi, 1)
        except (ValueError, TypeError):
            return None

    def kind(value):
        return value if value in LINK_KINDS else "other"
    out = {"via": kind(link.get("via", "none"))}
    if isinstance(link.get("networks"), list):
        out["networks"] = [kind(x) for x in link["networks"][:4]]
    if text(link.get("device"), 12):
        out["device"] = text(link["device"], 12)
    for key, lo, hi in (("pushes", 0, 10**6), ("failed", 0, 10**6), ("failovers", 0, 10**6),
                        ("median_ms", 0, 600_000), ("slowest_ms", 0, 600_000), ("wifi_dbm", -150, 0)):
        value = num(link.get(key), lo, hi) if link.get(key) is not None else None
        if value is not None:
            out[key] = value
    if "clock_synced" in link:
        out["clock_synced"] = link["clock_synced"] is True
    stick = link.get("stick")
    if isinstance(stick, dict):
        s = {"connected": stick.get("connected") is True}
        for key in ("network", "provider", "cell"):
            if text(stick.get(key), 24):
                s[key] = text(stick[key], 24)
        if "roaming" in stick:
            s["roaming"] = stick["roaming"] is True
        for key, lo, hi in (("bars", 0, 10), ("rsrp", -200, 0), ("rsrq", -50, 10), ("snr", -50, 60),
                            ("ping_ms", 0, 600_000), ("ping_worst_ms", 0, 600_000), ("ping_lost", 0, 1000),
                            ("down_s", 0, 1e9)):
            value = num(stick.get(key), lo, hi) if stick.get(key) is not None else None
            if value is not None:
                s[key] = value
        out["stick"] = s
    return out


def clean_op(wanted):
    """Validate one operation from the app; returns (kind, id, payload) or raises ValueError."""
    if not isinstance(wanted, dict) or not isinstance(wanted.get("kind"), str) or wanted["kind"] not in OP_TTL_S:
        raise ValueError
    op_id = _str(wanted.get("id"), 32)
    if not OP_ID.match(op_id):
        raise ValueError
    payload = {}
    if wanted["kind"] == "mark":
        if not isinstance(wanted.get("label"), str) or wanted["label"] not in MARK_LABELS:
            raise ValueError
        payload = {"label": wanted["label"], "note": _str(wanted.get("note", ""), 500),
                   "age_s": _num(wanted.get("age_s", 0), 0, 86400, 3),
                   "phone_utc": _num(wanted.get("phone_utc"), 1e9, 1e11, 3)}
    elif wanted["kind"] == "notify":
        if not isinstance(wanted.get("pushover"), bool):
            raise ValueError
        payload = {"pushover": wanted["pushover"]}
    elif wanted["kind"] == "bluetooth":
        # Exactly one of: the switch, a new name, or the app asking for the link's key.
        asked = [k for k in ("on", "name", "pair") if k in wanted]
        if len(asked) != 1:
            raise ValueError
        if asked == ["on"] and isinstance(wanted["on"], bool):
            payload = {"on": wanted["on"]}
        elif asked == ["name"] and isinstance(wanted["name"], str) and BLUETOOTH_NAME.fullmatch(wanted["name"])                 and wanted["name"] == wanted["name"].strip():
            payload = {"name": wanted["name"]}
        elif asked == ["pair"] and wanted["pair"] is True:
            payload = {"pair": True}
        else:
            raise ValueError
    return wanted["kind"], op_id, payload


def save_log(folder, kind, body):
    """Store one uploaded log under a name the server chooses; keep the folder bounded."""
    folder = Path(folder)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%SZ", time.gmtime())
    name = f"{kind}-{stamp}-{secrets.token_hex(4)}.log"
    # Written under a name no reader accepts, then renamed: a partial upload is never listed. Synced
    # first: the phone treats a success as stored, and will not send those records again.
    partial = folder / (name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(body)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(partial, folder / name)
    # The new log always counts first; files saved in the same instant can share a timestamp.
    # Counted per kind, so a flood of one kind cannot delete the other's records.
    others = sorted(((s.st_mtime_ns, p.name, s.st_size) for p, s in _log_stats(folder)
                     if p.name != name and p.name.startswith(kind + "-")), reverse=True)
    total = len(body)
    for index, (_, other, size) in enumerate(others, start=1):
        total += size
        if index >= LOG_KEEP_FILES or total > LOG_KEEP_BYTES:
            try:
                (folder / other).unlink(missing_ok=True)
            except OSError:
                pass                            # tidying up must never fail an upload that succeeded
    return name


def make_drive_report(log_dir, name):
    """After the phone's drive data arrives: a report of that drive beside it (drive_report.py), in the background.

    Any failure is printed and dropped; an upload that succeeded must never be undone by its report.
    """
    def run():
        try:
            import drive_report
            saved = drive_report.write_report(log_dir, name, save=save_log)
            if saved:
                print(f"drive report saved as {saved}", flush=True)
        except Exception as exc:            # noqa: BLE001 - see the docstring
            print(f"drive report failed: {type(exc).__name__}: {exc}"[:200], flush=True)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _log_stats(folder):
    """Stored logs with their details, skipping any that another request removed meanwhile."""
    for path in Path(folder).iterdir():
        if LOG_NAME.match(path.name):
            try:
                yield path, path.stat()
            except OSError:
                continue


def list_logs(folder):
    folder = Path(folder)
    if not folder.is_dir():
        return []
    rows = [{"name": p.name, "size": s.st_size, "age_s": round(time.time() - s.st_mtime)} for p, s in _log_stats(folder)]
    return sorted(rows, key=lambda r: r["age_s"])[:2 * LOG_KEEP_FILES]


# ---------------------------------------------------------------- server
class CappedServer(ThreadingHTTPServer):
    """One thread per connection, with limits on how many and how long.

    At most MAX_THREADS connections, MAX_PER_ADDRESS from one address, and HEADER_DEADLINE_S for a
    request's headers. The socket timeout alone cannot do this: it restarts with every byte, so a
    client sending one header byte at a time could hold every connection before any key is checked.
    """
    daemon_threads = True

    def __init__(self, addr, handler, max_threads=MAX_THREADS, max_per_address=MAX_PER_ADDRESS,
                 header_deadline=HEADER_DEADLINE_S):
        super().__init__(addr, handler)
        self.slots = threading.BoundedSemaphore(max_threads)
        self.max_per_address = max_per_address
        self.header_deadline = header_deadline
        self.conn_lock = threading.Lock()
        self.per_address = {}
        self.header_deadlines = {}              # connections still waiting for a complete request
        self.closing = threading.Event()
        threading.Thread(target=self._enforce_deadlines, daemon=True).start()

    def _enforce_deadlines(self):
        while not self.closing.wait(0.5):
            now = time.monotonic()
            with self.conn_lock:
                late = [c for c, deadline in self.header_deadlines.items() if now >= deadline]
                for conn in late:
                    del self.header_deadlines[conn]
            for conn in late:
                try:
                    conn.shutdown(socket.SHUT_RDWR)  # the waiting read ends, and its thread with it
                except OSError:
                    pass

    def await_request(self, conn):
        with self.conn_lock:
            self.header_deadlines[conn] = time.monotonic() + self.header_deadline

    def request_arrived(self, conn):
        with self.conn_lock:
            self.header_deadlines.pop(conn, None)

    def server_close(self):
        self.closing.set()
        super().server_close()

    def process_request(self, request, client_address):
        address = client_address[0]
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)       # over capacity: drop, do not queue
            return
        with self.conn_lock:
            refused = self.per_address.get(address, 0) >= self.max_per_address
            if not refused:
                self.per_address[address] = self.per_address.get(address, 0) + 1
        if refused:
            self.slots.release()
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._release(request, address)
            raise

    def _release(self, request, address):
        with self.conn_lock:
            self.header_deadlines.pop(request, None)
            left = self.per_address.get(address, 1) - 1
            if left > 0:
                self.per_address[address] = left
            else:
                self.per_address.pop(address, None)
        self.slots.release()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(request, client_address[0])


def make_handler(cfg, store, page_bytes, streams=None, log_dir=None, config_path=None, sound_dir=None,
                 release_dir=None):
    log_dir = Path(log_dir) if log_dir else HERE / "logs"
    release_dir = Path(release_dir) if release_dir else HERE / "release"
    app_config = AppConfig(config_path or HERE / "app-config.json")
    sound_dir = Path(sound_dir) if sound_dir else HERE / "sounds"
    pi_token = cfg["pi_token"].encode()
    viewer_key = cfg["viewer_key"].encode()
    # For a phone shortcut: answers only on or off, and opens nothing else. No key set means no answer.
    pi_status_key = cfg.get("pi_status_key", "").encode()
    streams = streams or threading.BoundedSemaphore(MAX_STREAMS)

    class Handler(BaseHTTPRequestHandler):
        server_version = "Slot17Display/3"
        sys_version = ""
        protocol_version = "HTTP/1.1"          # keep-alive for the Pi's pushes
        timeout = SOCKET_TIMEOUT_S             # applied to the socket by StreamRequestHandler

        def handle_one_request(self):
            self.server.await_request(self.connection)
            super().handle_one_request()

        def parse_request(self):
            result = super().parse_request()    # this reads the headers
            self.server.request_arrived(self.connection)
            return result

        def handle(self):
            try:
                super().handle()
            except (ConnectionError, TimeoutError):
                # A phone closing a stream/probe is a normal disconnect.
                self.close_connection = True

        # Never log query strings: the first visit carries the viewer key.
        def log_message(self, fmt, *args):
            pass

        def log_request(self, code="-", size="-"):
            path = urlsplit(getattr(self, "path", "")).path     # none yet on a malformed request line
            if path not in ("/api/state", "/api/push", "/api/stream", "/api/clock"):
                sys.stderr.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.address_string()} "
                                 f"{self.command} {path} {code}\n")

        def proved(self):
            """Marks this connection as holding a key. Only such a connection is kept open."""
            self.trusted = True
            return True

        def send(self, code, body, ctype="application/json", extra=None):
            if isinstance(body, str):
                body = body.encode()
            if not getattr(self, "trusted", False):
                self.close_connection = True
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def viewer(self):
            jar = cookies.SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie", ""))
            except cookies.CookieError:
                return False
            got = jar[COOKIE].value.encode() if COOKIE in jar else b""
            return hmac.compare_digest(got, viewer_key) and self.proved()

        def same_origin(self):
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            try:
                return urlsplit(origin).netloc == self.headers.get("Host", "")
            except ValueError:
                return False

        def read_body(self, length, limit_s=BODY_DEADLINE_S):
            """Read exactly `length` bytes within the time limit, or None."""
            deadline, parts, got = time.monotonic() + limit_s, [], 0
            try:
                while got < length:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        return None
                    self.connection.settimeout(left)   # one read cannot outlive the deadline
                    chunk = self.rfile.read1(min(65536, length - got))
                    if not chunk:
                        return None
                    parts.append(chunk)
                    got += len(chunk)
                return b"".join(parts)
            except OSError:
                return None
            finally:
                try:
                    self.connection.settimeout(SOCKET_TIMEOUT_S)
                except OSError:
                    pass

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/api/clock":
                if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), b"Bearer " + pi_token):
                    return self.send(401, '{"error":"unauthorised"}')
                self.proved()
                source = self.headers.get("X-Display-Source", "")
                if not source or len(source) > 64:
                    return self.send(400, '{"error":"invalid source"}')
                try:
                    ticket = store.ticket(source)
                except ValueError:
                    return self.send(409, '{"error":"publisher superseded or ticket capacity"}')
                # The Pi has no inbound path, so an operator command can only
                # travel on a request the Pi itself makes. It already makes this
                # one before every push, which is why the stop rides here rather
                # than needing a port opened towards the car.
                body = {"ticket": ticket}
                if self.headers.get("X-Display-Commands") != "none":
                    pending = store.take_command()
                    if pending:
                        body["command"] = pending
                    ops = store.take_ops()
                    if ops:
                        body["ops"] = ops
                return self.send(200, json.dumps(body))
            if url.path == "/healthz":
                return self.send(200, "ok\n", "text/plain")
            if url.path in ("/api/release", "/api/release/bundle"):
                if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), b"Bearer " + pi_token):
                    return self.send(401, '{"error":"unauthorised"}')
                self.proved()
                name, limit, ctype = (("release.json", 4096, "application/json") if url.path == "/api/release"
                                      else ("bundle.tar.gz", RELEASE_MAX, "application/gzip"))
                try:
                    with open(release_dir / name, "rb") as fh:
                        body = fh.read(limit + 1)
                except OSError:
                    return self.send(404, '{"error":"no release"}')
                if len(body) > limit:
                    return self.send(404, '{"error":"no release"}')
                return self.send(200, body, ctype)
            if url.path == "/api/pi-on":
                token = parse_qs(url.query).get("token", [""])[0].encode()
                if not pi_status_key or not hmac.compare_digest(token, pi_status_key):
                    return self.send(401, "unauthorised\n", "text/plain")
                return self.send(200, "on\n" if store.pi_on() else "off\n", "text/plain")
            if url.path == "/manifest.webmanifest":
                return self.send(200, MANIFEST, "application/manifest+json")
            if url.path in ICONS:
                try:
                    return self.send(200, (HERE / ICONS[url.path]).read_bytes(), "image/png")
                except OSError:
                    return self.send(404, "not found\n", "text/plain")
            if url.path == "/":
                query = parse_qs(url.query)
                key = query.get("key", [""])[0]
                pasted = query.get("link", [""])[0].strip()[:512]
                if pasted and not key:
                    # The whole private link, or only its key.
                    key = parse_qs(urlsplit(pasted).query).get("key", [""])[0] if "key=" in pasted else pasted
                key = key.encode()
                if key and hmac.compare_digest(key, viewer_key):
                    return self.send(303, "", "text/plain", {
                        "Location": "/",
                        "Set-Cookie": f"{COOKIE}={cfg['viewer_key']}; Path=/; HttpOnly; "
                                      "SameSite=Strict; Max-Age=31536000"})
                if not self.viewer():
                    nonce = secrets.token_urlsafe(16)
                    note = ("<span class=bad>That is not this server's private link.</span>" if pasted else
                            "Open the private link once on this phone, or paste it here.")
                    return self.send(401, LOGIN_PAGE.replace("__NONCE__", nonce).replace("__NOTE__", note),
                                     "text/html; charset=utf-8", {
                                         "Content-Security-Policy":
                                             f"default-src 'none'; style-src 'nonce-{nonce}'; img-src 'self'; "
                                             "manifest-src 'self'; form-action 'self'; base-uri 'none'; "
                                             "frame-ancestors 'none'"})
                nonce = secrets.token_urlsafe(16)
                return self.send(200, page_bytes.replace(b"__NONCE__", nonce.encode()),
                                 "text/html; charset=utf-8", {
                                     # media-src blob: is required, not cosmetic: the page
                                     # builds a short silent WAV as a Blob and loops it to
                                     # hold an audible audio session open on iOS. Under
                                     # default-src 'none' that element is blocked and the
                                     # phone stays silent with nothing in the UI to say why.
                                     "Content-Security-Policy":
                                         f"default-src 'none'; script-src 'nonce-{nonce}'; "
                                         f"style-src 'nonce-{nonce}'; connect-src 'self'; img-src 'self' data:; "
                                         "media-src blob:; manifest-src 'self'; "
                                         "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"})
            if url.path == "/api/state":
                if not self.viewer():
                    return self.send(401, '{"error":"unauthorised"}')
                return self.send(200, json.dumps(store.message(), separators=(",", ":")))
            if url.path == "/api/stream":
                return self.stream()
            if url.path == "/api/config":
                if not self.viewer():
                    return self.send(401, '{"error":"unauthorised"}')
                body = app_config.load()
                if body is None:
                    return self.send(404, json.dumps({"error": app_config.error or "no settings"}))
                return self.send(200, body)
            if url.path.startswith("/api/sounds/"):
                if not self.viewer():
                    return self.send(401, '{"error":"unauthorised"}')
                name = url.path[len("/api/sounds/"):]
                if not SOUND_NAME.match(name):
                    return self.send(404, '{"error":"not found"}')
                try:
                    if (sound_dir / name).stat().st_size > SOUND_MAX:
                        raise FileNotFoundError
                    return self.send(200, (sound_dir / name).read_bytes(), "audio/wav")
                except (FileNotFoundError, IsADirectoryError, PermissionError):
                    return self.send(404, '{"error":"not found"}')
            if url.path == "/api/logs":
                if not self.viewer():
                    return self.send(401, '{"error":"unauthorised"}')
                return self.send(200, json.dumps({"logs": list_logs(log_dir)}))
            if url.path.startswith("/api/logs/"):
                if not self.viewer():
                    return self.send(401, '{"error":"unauthorised"}')
                name = url.path[len("/api/logs/"):]
                if not LOG_NAME.match(name):
                    return self.send(404, '{"error":"not found"}')
                try:
                    return self.send(200, (log_dir / name).read_bytes(), "text/plain; charset=utf-8")
                except FileNotFoundError:
                    return self.send(404, '{"error":"not found"}')
            return self.send(404, '{"error":"not found"}')

        def stream(self):
            if not self.viewer():
                return self.send(401, '{"error":"unauthorised"}')
            if not streams.acquire(blocking=False):
                self.close_connection = True
                return self.send(503, '{"error":"too many streams"}')
            self.close_connection = True           # an event stream ends when the socket does
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"retry: 2000\n\n")
                msg = store.message()
                while True:
                    self.wfile.write(b"data: " + json.dumps(msg, separators=(",", ":")).encode() + b"\n\n")
                    self.wfile.flush()
                    msg = store.wait(msg["seq"], STREAM_REPEAT_S)
            except OSError:                        # phone went away or stalled past the timeout
                pass
            finally:
                streams.release()

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            # Consume a bounded body before answering, even to refuse it: closing a
            # socket with unread request bytes makes the OS send a reset and the
            # client never sees the status. Larger or slower uploads are cut off.
            path = urlsplit(self.path).path
            uploading = path in ("/api/app-log", "/api/pi-log")
            limit = MAX_LOG_BODY if uploading else MAX_BODY
            if not self.same_origin():
                self.close_connection = True
                if 0 < length <= MAX_BODY:
                    self.read_body(length)          # so the client sees the 403, not a reset
                return self.send(403, '{"error":"another site"}')
            if not 0 < length <= limit:
                self.close_connection = True
                # Read off a modestly oversized body so the client sees the 413, not a reset.
                if limit < length <= limit + 16 * MAX_BODY:
                    self.read_body(length)          # bounded in size and time
                return self.send(413, '{"error":"size"}')
            # Check who is asking before accepting a large, slow body.
            if path == "/api/app-log" and not self.viewer():
                self.close_connection = True
                return self.send(401, '{"error":"unauthorised"}')
            if path == "/api/pi-log" and not hmac.compare_digest(
                    self.headers.get("Authorization", "").encode(), b"Bearer " + pi_token):
                self.close_connection = True
                return self.send(401, '{"error":"unauthorised"}')
            body = self.read_body(length, LOG_BODY_DEADLINE_S if uploading else BODY_DEADLINE_S)
            if body is None:
                self.close_connection = True
                return self.send(408, '{"error":"incomplete body"}')
            if uploading:
                try:
                    name = save_log(log_dir, "app" if path == "/api/app-log" else "pi", body)
                except OSError:
                    return self.send(507, '{"error":"could not store the log"}')
                if path == "/api/app-log" and b'"slot17-drive-data"' in body:
                    make_drive_report(log_dir, name)
                return self.send(200, json.dumps({"name": name}))
            if path == "/api/op":
                if not self.viewer():
                    return self.send(401, '{"error":"unauthorised"}')
                try:
                    kind, op_id, payload = clean_op(json.loads(body))
                    return self.send(200, json.dumps(store.add_op(kind, op_id, payload)))
                except (ValueError, RecursionError):
                    return self.send(400, '{"error":"invalid operation"}')
            if path == "/api/command":
                # Authorised by the viewer cookie/key, like every other phone
                # endpoint. Stop is the only verb: see Store.__init__ for why
                # there is deliberately no start here.
                if not self.viewer():
                    return self.send(401, '{"error":"unauthorised"}')
                try:
                    wanted = json.loads(body)
                    if not isinstance(wanted, dict) or wanted.get("action") != "stop":
                        raise ValueError
                    note = _str(wanted.get("note", ""), 120)
                except (ValueError, RecursionError):
                    return self.send(400, '{"error":"only {\\"action\\":\\"stop\\"} is accepted"}')
                return self.send(200, json.dumps(store.request_stop(note)))
            if path != "/api/push":
                return self.send(404, '{"error":"not found"}')
            auth = self.headers.get("Authorization", "").encode()
            if not hmac.compare_digest(auth, b"Bearer " + pi_token):
                return self.send(401, '{"error":"unauthorised"}')
            self.proved()
            try:
                state = clean_state(json.loads(body))
            except (ValueError, RecursionError):   # includes JSON errors and bad fields
                return self.send(400, '{"error":"invalid state"}')
            try:
                store.put(state)
            except ValueError:
                return self.send(409, '{"error":"expired or superseded upload"}')
            return self.send(204, b"")

    return Handler


def init_config(path=CONFIG, port=5732):
    if path.exists():
        raise SystemExit(f"{path} exists; refusing to overwrite its secrets")
    cfg = {"host": "0.0.0.0", "port": port,  # noqa: S104 - the phone and the Pi must reach it
           "pi_token": secrets.token_urlsafe(32), "viewer_key": secrets.token_urlsafe(18),
           "pi_status_key": secrets.token_urlsafe(18),
           # Pass the team's signed car computer updates on (see relay_update); "false" stops that.
           "release_updates": True}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=1)
    print(f"wrote {path} (0600)")


def load_config(path=CONFIG):
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if len(cfg.get("pi_token", "")) < 32 or len(cfg.get("viewer_key", "")) < 20:
        raise SystemExit(f"{path}: pi_token (>=32 chars) and viewer_key (>=20 chars) required")
    cfg.setdefault("host", "0.0.0.0")  # noqa: S104 - the phone and the Pi must reach it
    cfg.setdefault("port", 5732)
    return cfg


def release_version(text):
    """'1.2.9' or 'v1.2.9' as '1.2.9'; anything else None."""
    found = re.fullmatch(r"v?(\d{1,3}\.\d{1,3}\.\d{1,3})", str(text or "").strip())
    return found.group(1) if found else None


def _https_get(url, accept="application/vnd.github+json", limit=RELEASE_MAX_BYTES):
    if not url.startswith(("https://api.github.com/", "https://raw.githubusercontent.com/")):
        raise ValueError("only GitHub's own https addresses")
    req = urllib.request.Request(url, headers={"User-Agent": "Slot17-release-check",  # noqa: S310 - checked above
                                               "Accept": accept})
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed https hosts only
        body = resp.read(limit + 1)
    if len(body) > limit:
        raise ValueError("answer too large")
    return body


def relay_update(repo, release_dir, get=_https_get):
    """Copy the newest release's signed car computer update into release_dir. Returns a short word for the log.

    Only a release asset of `repo` itself is fetched (GitHub hands it out through its own download
    servers). The bundle must have exactly the size and SHA-256 its description states; the older or
    same number already here is left alone. Both files are swapped in whole, the description last, so
    the car computer never sees a description without its bundle.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}", repo or ""):
        raise ValueError("release_repo must look like owner/name")
    info = json.loads(get(f"https://api.github.com/repos/{repo}/releases/latest"))
    if not isinstance(info, dict) or info.get("draft") or info.get("prerelease"):
        return "no release"
    assets = {a.get("name"): str(a.get("url") or "") for a in info.get("assets") or [] if isinstance(a, dict)}
    if UPDATE_JSON not in assets or UPDATE_BUNDLE not in assets:
        return "the newest release has no car computer update"
    for url in (assets[UPDATE_JSON], assets[UPDATE_BUNDLE]):
        if not re.fullmatch(rf"https://api\.github\.com/repos/{re.escape(repo)}/releases/assets/\d{{1,15}}", url):
            raise ValueError("odd asset address")
    raw = get(assets[UPDATE_JSON], accept="application/octet-stream", limit=4096)
    meta = json.loads(raw)
    seq, size, sha = int(meta["seq"]), int(meta["size"]), str(meta["sha256"])
    if not re.fullmatch(r"[0-9a-f]{128}", str(meta.get("signature", ""))) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ValueError("update description malformed")
    release_dir = Path(release_dir)
    try:
        have = int(json.loads((release_dir / "release.json").read_text(encoding="utf-8")).get("seq", 0))
    except (OSError, ValueError, TypeError):
        have = 0
    if have >= seq:
        return f"release {have} is current"
    if not 0 < size <= RELEASE_MAX:
        raise ValueError("update too large")
    bundle = get(assets[UPDATE_BUNDLE], accept="application/octet-stream", limit=RELEASE_MAX)
    if len(bundle) != size or hashlib.sha256(bundle).hexdigest() != sha:
        raise ValueError("the bundle differs from its description")
    release_dir.mkdir(parents=True, exist_ok=True)
    for name, data in (("bundle.tar.gz", bundle), ("release.json", raw)):
        tmp = release_dir / f".{name}.new"
        tmp.write_bytes(data)
        os.replace(tmp, release_dir / name)
    return f"release {seq} is ready for the car computer"


def fetch_latest_release(repo, get=_https_get):
    """The newest published release of `repo` on GitHub: {software, app, tag, url}, or None.

    `software` is the tag (v1.2.9). `app` comes from versions.json at that tag, which releases from 1.2.9
    on carry; older releases have none, and then the app's own update notice simply stays silent.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,39}/[A-Za-z0-9_.-]{1,100}", repo or ""):
        raise ValueError("release_repo must look like owner/name")
    info = json.loads(get(f"https://api.github.com/repos/{repo}/releases/latest"))
    if not isinstance(info, dict) or info.get("draft") or info.get("prerelease"):
        return None
    software = release_version(info.get("tag_name"))
    if software is None:
        return None
    tag = "v" + software if str(info["tag_name"]).startswith("v") else software
    url = str(info.get("html_url") or "")
    if not url.startswith(f"https://github.com/{repo}/") or len(url) > 200:
        url = f"https://github.com/{repo}/releases/latest"
    app = android = None
    try:
        versions = json.loads(get(f"https://raw.githubusercontent.com/{repo}/{tag}/versions.json"))
        if isinstance(versions, dict):
            app = release_version(versions.get("app"))
            android = release_version(versions.get("android"))      # releases from 1.3.1 on
            software = release_version(versions.get("software")) or software
    except (OSError, ValueError):
        pass
    return {"software": software, "app": app, "android": android, "tag": tag, "url": url}


class ReleaseCheck(threading.Thread):
    """Asks GitHub for the newest release now and then; keeps the last good answer on the store."""

    def __init__(self, store, repo, fetch=fetch_latest_release, relay=None):
        super().__init__(daemon=True, name="release-check")
        self.store, self.repo, self.fetch, self.relay = store, repo, fetch, relay

    def relay_once(self):
        """The signed car computer update, if this server passes them on (release_updates)."""
        if self.relay is None:
            return None
        try:
            said = self.relay()
        except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
            said = f"not passed on: {type(exc).__name__}: {str(exc)[:100]}"
        print(f"car computer update: {said}", flush=True)
        return said

    def check_once(self):
        try:
            found = self.fetch(self.repo)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            # A private repository answers 404 here: there is then simply no update notice.
            print(f"release check: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
            return None
        if found:
            with self.store.cond:
                if found != getattr(self.store, "release", None):
                    print(f"release check: newest is {found['tag']} (app {found['app'] or 'not stated'})", flush=True)
                self.store.release = found
        return found

    def run(self):
        time.sleep(RELEASE_FIRST_CHECK_S)
        while True:
            self.check_once()
            self.relay_once()
            time.sleep(RELEASE_CHECK_EVERY_S)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init", action="store_true", help="create config.json with new secrets")
    ap.add_argument("--port", type=int, default=5732)
    args = ap.parse_args()
    if args.init:
        return init_config(port=args.port)
    cfg = load_config()
    store = Store()
    if cfg.get("release_check", True) is not False:
        repo = str(cfg.get("release_repo") or RELEASE_REPO)
        relay = (lambda: relay_update(repo, HERE / "release")) if cfg.get("release_updates") is True else None
        ReleaseCheck(store, repo, relay=relay).start()
    httpd = CappedServer((cfg["host"], int(cfg["port"])), make_handler(cfg, store, PAGE.read_bytes()))
    print(f"Slot17 display listening on {cfg['host']}:{cfg['port']}", flush=True)
    try:
        httpd.serve_forever(poll_interval=1.0)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
