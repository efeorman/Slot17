#!/usr/bin/env python3
"""The car computer's own screen: the display page, served to this computer's own browser only.

A touchscreen on the car computer (a Raspberry Pi case with an HDMI screen, for example) shows the same
page as a phone, fed straight from the car computer: no server, no internet, nothing between the detector
and the screen but a file in memory. The publisher (display/display_publisher.py) writes its whole state to
STATE_DIR/state.json whenever something changes and at least twice a second, as long as STATE_DIR/ops
exists, which this creates. This serves it the way the display server does, so the page needs no special
case beyond /api/screen.

The screen's requests (a sighting, a recording, stop, power off...) go back as files in STATE_DIR/ops. The
publisher picks them up within 50 ms and carries them out exactly as it does the app's, by id, so one is
never done twice. The screen opens in drive mode at every start, whatever it was left on (the operator's
choice, 26 November 2026). Whether drive mode shows one bar or three is kept in ~/.config/slot17-screen.json,
written so a power cut keeps it.

Only this computer can reach it: it listens on 127.0.0.1 and refuses any other Host header (a web page in
the kiosk browser cannot point a name of its own at it).

    python3 -B p0/screen.py                  serve on 127.0.0.1:5733 (deploy/install-screen.sh makes it a service)
    python3 -B p0/screen.py --port 5733
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
PORT = 5733
SETTINGS = Path(os.environ.get("SLOT17_SCREEN_SETTINGS", Path.home() / ".config" / "slot17-screen.json"))
# The page a signed update brings (p0/web/, made from display/page.html when a release is built), else the
# installed one: signed updates replace p0/ but not display/page.html.
PAGES = (ROOT / "p0" / "web" / "page.html", ROOT / "display" / "page.html")
STALE_S = 4.0               # the publisher writes at least twice a second; this long without, it isn't running
STREAM_POLL_S = 0.05        # how often a stream looks for a new state: the publisher's own loop
STREAM_REPEAT_S = 2.0       # a repeat at least this often, so the page can tell a live link from a dead one
MAX_BODY = 8192
MAX_WAITING_OPS = 32
OP_KINDS = {"mark", "notify", "doctor", "bluetooth", "support", "logs", "poweroff"}
MARK_LABELS = {"police", "ambulance", "fire", "unknown", "noise", "antenna-change"}
OP_ID = re.compile(r"[0-9a-f]{32}")
SESSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z_.-]{0,79}")
BLUETOOTH_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,19}")
BARS = (1, 3)                # drive mode: the strongest channel's bar, or the three strongest channels' bars
DETECTOR_UNIT = "tetra-detector.service"
# The same policy as the display server's page (display/server.py), with a fresh nonce per page.
PAGE_CSP = ("default-src 'none'; script-src 'nonce-{n}'; style-src 'nonce-{n}'; connect-src 'self'; "
            "img-src 'self' data:; font-src data:; media-src blob:; manifest-src 'self'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")


def screen_dir():
    """Per user, in memory. Checked to be this user's own before use (owned), so another account on the
    computer can neither read the state nor plant a request."""
    if os.environ.get("SLOT17_SCREEN_DIR"):
        return Path(os.environ["SLOT17_SCREEN_DIR"])
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return Path("/dev/shm") / f"slot17-screen-{uid}"    # noqa: S108 - per user, and checked by owned()


def owned(folder):
    """A real folder (not a link), this user's, closed to everyone else."""
    try:
        st = os.lstat(folder)
    except OSError:
        return False
    import stat as _stat
    if not _stat.S_ISDIR(st.st_mode):
        return False
    if hasattr(os, "getuid") and (st.st_uid != os.getuid() or st.st_mode & 0o077):
        return False
    return True


STATE_DIR = screen_dir()


def atomic_write(path, data, durable):
    """The old file or the whole new one, never part of one. `durable`: through a power cut too."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        if durable:
            fh.flush()
            os.fsync(fh.fileno())
    os.replace(tmp, path)
    if durable and hasattr(os, "O_DIRECTORY"):
        fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def read_bars(path=None):
    """One bar or three; one when nothing (or anything else, such as an earlier release's {"mode": ...}) is kept."""
    try:
        bars = json.loads(Path(path or SETTINGS).read_text(encoding="utf-8")).get("bars")
    except (OSError, ValueError, AttributeError):
        return 1
    return bars if bars in BARS and not isinstance(bars, bool) else 1


def save_bars(bars, path=None):
    if bars not in BARS or isinstance(bars, bool):
        raise ValueError("bars must be 1 or 3")
    atomic_write(Path(path or SETTINGS), json.dumps({"bars": bars}).encode(), durable=True)


def finite(x, lo, hi):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and lo <= x <= hi


def clean_op(wanted):
    """The screen's request, from known fields only, or ValueError: the same rules as the display server's."""
    if not isinstance(wanted, dict) or wanted.get("kind") not in OP_KINDS or not OP_ID.fullmatch(str(wanted.get("id"))):
        raise ValueError("unknown request")
    kind = wanted["kind"]
    op = {"kind": kind, "id": wanted["id"]}
    if kind == "mark":
        if wanted.get("label") not in MARK_LABELS:
            raise ValueError("unknown label")
        note, age = wanted.get("note", ""), wanted.get("age_s", 0)
        if not isinstance(note, str) or len(note) > 500 or not finite(age, 0, 86400):
            raise ValueError("bad sighting")
        op.update(label=wanted["label"], note=note, age_s=float(age))
        if finite(wanted.get("phone_utc"), 1e9, 1e11):
            op["phone_utc"] = float(wanted["phone_utc"])
    elif kind == "notify":
        if not isinstance(wanted.get("pushover"), bool):
            raise ValueError("bad switch")
        op["pushover"] = wanted["pushover"]
    elif kind == "bluetooth":
        # The switch or a new name. Never the link's key: that only ever goes through the server.
        asked = [k for k in ("on", "name", "pair") if k in wanted]
        if asked == ["on"] and isinstance(wanted["on"], bool):
            op["on"] = wanted["on"]
        elif asked == ["name"] and isinstance(wanted["name"], str) and BLUETOOTH_NAME.fullmatch(wanted["name"]) \
                and wanted["name"] == wanted["name"].strip():
            op["name"] = wanted["name"]
        else:
            raise ValueError("bad Bluetooth request")
    elif kind == "support":
        choice = wanted.get("choice")
        if choice not in ("quick", "drive", "everything"):
            raise ValueError("bad choice")
        op.update(choice=choice, phone_log=wanted.get("phone_log") is True, places=wanted.get("places") is True)
        if choice != "quick":
            if not isinstance(wanted.get("session"), str) or not SESSION.fullmatch(wanted["session"]):
                raise ValueError("bad session")
            op["session"] = wanted["session"]
            op["recordings"] = choice == "everything" and wanted.get("recordings") is True
    return op


class Screen:
    """What the page reads: the publisher's newest state, and the requests it hasn't answered yet."""

    def __init__(self, state_dir=STATE_DIR, settings=SETTINGS, systemctl=None):
        self.dir = Path(state_dir)
        self.settings = Path(settings)
        self.boot = "screen-" + secrets.token_hex(6)
        self.seq = 0
        self.lock = threading.Lock()
        self.seen = None            # (mtime_ns, size) of the state last read
        self.doc = None
        self.stop = None            # {"id", "mono"} of the screen's stop, until it is answered
        self.systemctl = systemctl or self._systemctl
        self.unit = (None, -1e9)    # (why the detector isn't reporting, when that was asked)
        self.said_at = -1e9         # when a state of this service's own (no publisher) last counted as new
        # Its presence tells the publisher to write. Only this user may hand it requests (power off among them).
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not owned(self.dir):
            raise SystemExit(f"{self.dir} is not this user's own private folder; not using it")
        (self.dir / "ops").mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.dir / "ops", 0o700)

    @staticmethod
    def _systemctl():
        done = subprocess.run(["systemctl", "show", DETECTOR_UNIT, "-p", "ActiveState", "-p", "Result"],
                              capture_output=True, text=True, timeout=3)
        return dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)

    def _read(self):
        path = self.dir / "state.json"
        try:
            st = path.stat()
            key = (st.st_mtime_ns, st.st_size)
            if key != self.seen:
                doc = json.loads(path.read_bytes())
                if isinstance(doc, dict) and isinstance(doc.get("state"), dict) and finite(doc.get("mono"), 0, 1e12):
                    self.doc, self.seen = doc, key
                    self.seq += 1
        except (OSError, ValueError):
            pass
        return self.doc

    def _why_quiet(self, now):
        """Why there is no fresh state: from systemd, asked at most every 3 s."""
        why, at = self.unit
        if now - at < 3.0 and why is not None:
            return why
        try:
            unit = self.systemctl()
            active, result = unit.get("ActiveState", "unknown"), unit.get("Result", "")
            if active in ("active", "activating", "reloading"):
                # Found on the Pi, 25 November 2026: this said the publisher was not running while an older
                # publisher ran that did not feed this screen yet. Both are possible; say so.
                why = "The detector is starting" if active == "activating" else \
                    ("The detector runs, but no data reaches this screen: its display publisher is not running, "
                     "or is older than this screen and is updated at the next power-up.")
            elif active == "failed":
                why = f"The detector stopped with a fault ({result}). It starts again at the next power-up."
            else:
                why = "The detector is not running. It starts at the next power-up."
        except (OSError, subprocess.SubprocessError, ValueError):
            why = "No state from the car computer yet"
        self.unit = (why, now)
        return why

    def message(self):
        with self.lock:
            now = time.monotonic()
            doc = self._read()
            age = None if doc is None else max(0.0, now - doc["mono"])
            if doc is None or age > STALE_S:
                # This service's own word, fresh each second: the page must not take it for one old snapshot
                # from the car, aging (found on the Pi, 25 November 2026: "No data from the car, 30 s ago").
                if now - self.said_at >= 1.0:
                    self.seq, self.said_at = self.seq + 1, now
                why = self._why_quiet(now)
                state = {"v": 3, "source": "screen", "session": None, "snapshot_age_s": 0.0,
                         "detector": {"state": "starting" if why.startswith("The detector is starting")
                                      else "no-data" if why.startswith("The detector runs") else "stopped",
                                      "reasons": [why]}, "channels": [], "matches": []}
                if doc is not None:     # what it knew last, so the car computer section still has something
                    for key in ("pi", "software", "features", "doctor", "link", "update"):
                        if key in doc["state"]:
                            state[key] = doc["state"][key]
                out = {"boot": self.boot, "seq": self.seq, "server_mono_s": now, "server_age_s": 0.0, "state": state}
            else:
                out = {"boot": self.boot, "seq": self.seq, "server_mono_s": now, "server_age_s": age, "state": doc["state"]}
            ops = doc.get("ops") if doc is not None and isinstance(doc.get("ops"), list) else []
            out["ops"] = ops
            if self.stop is not None:
                answer = next((o for o in ops if o.get("id") == self.stop["id"]), None)
                state = {"done": "done", "failed": "failed", "sent": "sent"}.get((answer or {}).get("state"), "pending")
                out["command"] = {"action": "stop", "state": state, "age_s": round(now - self.stop["mono"], 1)}
            return out

    def add_op(self, op):
        """Hand a request to the publisher. Its answer comes back in the state's `ops`."""
        folder = self.dir / "ops"
        if len(list(folder.glob("*.json"))) >= MAX_WAITING_OPS:
            raise OverflowError("too many requests waiting")
        op = dict(op, mono=time.monotonic())
        atomic_write(folder / f"{op['id']}.json", json.dumps(op).encode(), durable=False)
        if op["kind"] == "stop":
            with self.lock:
                self.stop = {"id": op["id"], "mono": op["mono"]}
        return {"id": op["id"], "kind": op["kind"], "state": "pending", "detail": ""}


def find_page():
    for path in PAGES:
        if path.is_file():
            return path
    raise FileNotFoundError("no display page: looked for " + ", ".join(map(str, PAGES)))


def make_handler(screen, page_path):
    page_cache = {"key": None, "bytes": b""}

    def page_bytes():
        st = page_path.stat()
        key = (st.st_mtime_ns, st.st_size)
        if key != page_cache["key"]:
            page_cache.update(key=key, bytes=page_path.read_bytes())
        return page_cache["bytes"]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "slot17-screen"
        sys_version = ""

        def log_message(self, fmt, *args):           # quiet: the kiosk asks several times a second
            pass

        def send(self, code, body=b"", ctype="application/json", extra=None):
            body = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def hosts(self):
            port = self.server.server_address[1]
            return {f"127.0.0.1:{port}", f"localhost:{port}"}

        def allowed(self):
            if self.headers.get("Host", "") not in self.hosts():
                self.send(403, '{"error":"this screen is for the car computer itself"}')
                return False
            return True

        def read_json(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return None
            origin = self.headers.get("Origin")
            if origin is not None and origin.split("://", 1)[-1] not in self.hosts():
                return None
            if not 0 < length <= MAX_BODY:
                return None
            try:
                return json.loads(self.rfile.read(length))
            except (ValueError, RecursionError):
                return None

        def do_GET(self):
            if not self.allowed():
                return
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self.send(200, "ok\n", "text/plain")
            if path == "/":
                nonce = secrets.token_urlsafe(16)
                return self.send(200, page_bytes().replace(b"__NONCE__", nonce.encode()), "text/html; charset=utf-8",
                                 {"Content-Security-Policy": PAGE_CSP.format(n=nonce)})
            if path == "/api/screen":
                return self.send(200, json.dumps({"screen": True, "bars": read_bars(screen.settings)}))
            if path == "/api/state":
                return self.send(200, json.dumps(screen.message()))
            if path == "/api/stream":
                return self.stream()
            if path == "/api/support":
                return self.send(200, '{"packages":[]}')
            if path == "/api/logs":
                return self.send(200, '{"logs":[]}')
            return self.send(404, '{"error":"not found"}')

        def stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.close_connection = True
            last_seq, last_sent = None, 0.0
            try:
                while True:
                    msg = screen.message()
                    now = time.monotonic()
                    if msg["seq"] != last_seq or now - last_sent >= STREAM_REPEAT_S:
                        self.wfile.write(b"data: " + json.dumps(msg).encode() + b"\n\n")
                        self.wfile.flush()
                        last_seq, last_sent = msg["seq"], now
                    time.sleep(STREAM_POLL_S)
            except OSError:
                return                                   # the page went away

        def do_POST(self):
            if not self.allowed():
                return
            path = self.path.split("?", 1)[0]
            body = self.read_json()
            if body is None:
                return self.send(400, '{"error":"bad request"}')
            if path == "/api/screen":
                try:
                    if not isinstance(body, dict) or set(body) != {"bars"}:
                        raise ValueError("only bars")
                    save_bars(body["bars"], screen.settings)
                except (ValueError, OSError) as exc:
                    return self.send(400, json.dumps({"error": str(exc)[:80]}))
                return self.send(204)
            try:
                if path == "/api/op":
                    return self.send(200, json.dumps(screen.add_op(clean_op(body))))
                if path == "/api/command":
                    if not isinstance(body, dict) or body.get("action") != "stop":
                        raise ValueError("only stop")
                    note = str(body.get("note") or "from the car computer's screen")[:120]
                    return self.send(202, json.dumps(screen.add_op({"kind": "stop", "id": secrets.token_hex(16), "note": note})))
            except ValueError as exc:
                return self.send(400, json.dumps({"error": str(exc)[:80]}))
            except OverflowError as exc:
                return self.send(503, json.dumps({"error": str(exc)}))
            return self.send(404, '{"error":"not found"}')

    return Handler


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args(argv)
    page = find_page()
    screen = Screen()
    httpd = Server(("127.0.0.1", args.port), make_handler(screen, page))
    digest = hashlib.sha256(page.read_bytes()).hexdigest()[:12]
    print(f"screen: serving {page.relative_to(ROOT)} ({digest}) on http://127.0.0.1:{args.port}/, "
          f"state from {screen.dir}", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
