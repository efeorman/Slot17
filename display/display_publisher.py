#!/usr/bin/env python3
"""Push the running detector's live state to the phone display server.

It reads the heartbeat on tmpfs and the lines appended to the current session's
events.jsonl. Its only writes into a session are sighting requests in marks/, the
same files p0/mark.py writes, which the detector picks up itself. It never opens
the receiver, and its name is not one of the receiver tools that preflight's idle
guard looks for.

New matches are pushed as soon as they appear in events.jsonl (checked ten
times a second) over a kept-open HTTP connection; otherwise the state is pushed
every two seconds so the phone can tell a live link from a dead one. Every
channel matched in the last minute and every new match are sent - the phone,
not this process, decides which three bars to show and what counts as red.

Strength is rc3's matched-burst strength in dB above the channel's own noise
floor. It is not distance, and a match is a TETRA radio, not a known vehicle.
Most of the delay between a burst and the phone is the detector itself: it
analyses 0.5 s windows and confirms before an event is written.

    python3 display_publisher.py            # run until stopped
    python3 display_publisher.py --once     # push the current state once (no history)
"""
import argparse
from collections import deque
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode, urlsplit

HEARTBEAT = Path("/run/tetra/watch.heartbeat")
CONFIG = Path.home() / ".config/slot17-display.json"
HOLD_S = 60.0          # a channel stays in the state this long after its last match
MAX_CHANNELS = 8       # sent; the phone shows three
MAX_MATCHES = 20       # new matches carried per push
STALE_S = 10.0         # heartbeat content unchanged this long = detector not progressing
KEEPALIVE_S = 2.0      # push at least this often
RETRY_S = 0.5          # after a failed push, the next try comes this soon...
FAST_RETRIES = 6       # ...this many times; then every KEEPALIVE_S, so an outage is not hammered
LINK_REPORT_S = 60.0   # a 'link:' line in the log this often: pushes, failures, milliseconds
LOOP_S = 0.05          # events.jsonl check interval (0.1 s until 15 November 2026)
HEARTBEAT_EVERY_S = 0.5
STOPPED_EXIT_S = 30.0  # PartOf= does not follow a detector that dies on its own
# A receiver that hears nothing is the one fault that wastes a whole drive, and the
# detector treats it as telemetry only. Warn after a run far longer than the
# transients seen on a working drive and far short of a dead antenna (806 windows).
# The detector reports its own limit. If a heartbeat lacks it, the limit is one minute of
# windows of the length the heartbeat gives; QUIET_ALARM_WINDOWS is only for a detector so
# old that it reports neither, whose windows were 3 s.
QUIET_ALARM_S = 60.0
QUIET_ALARM_WINDOWS = 20
QUIET_REPEAT_S = 900.0
BACKFILL_BYTES = 512 << 10
READ_CAP = 4 << 20
TIMEOUT_S = 2.5
FEATURES = ["mark", "poweroff", "logs", "notify", "doctor", "bluetooth", "support"]
MARK_LABELS = {"police", "ambulance", "fire", "unknown", "noise", "antenna-change"}
MAX_ACKS = 32                 # in one push: the server's limit
MAX_ACKS_WAITING = 256        # held until sent
LOG_FILE_BYTES = 40_000       # of each file in a Pi log
MAX_ANSWER_BYTES = 256 << 10  # from the server; far above any real answer
PENDING_MATCHES = 256         # waiting to be sent, while the link is down or many arrive at once
PI_HEALTH_EVERY_S = 5.0
UPDATE_STATUS_EVERY_S = 30.0  # re-read what the updater last wrote
UPDATE_FIRST_CHECK_S = 20.0   # after the publisher starts, so every start checks once the link is up
UPDATE_CHECK_EVERY_S = 900.0
LOG_UPLOAD_TIMEOUT_S = 60.0
LOG_UPLOAD_CAP = 900 << 10
JOURNAL_LINES = 400
MODEM_LINES = 80               # of the 4G stick check's lines, in a Pi log
# Must match deploy/61-slot17-poweroff.sudoers word for word, or sudo -n refuses.
POWEROFF_ARGV = ["sudo", "-n", "systemctl", "--no-block", "poweroff"]
POWEROFF_MAX_AGE_S = 30.0
# Whatever may hold a credential in a file or journal line. Over-redacting a log costs little.
SECRET_TEXT = re.compile(r"(?i)((?:[a-z0-9_-]*(?:token|password|passwd|secret|api_?key|user_?key|viewer_?key|status_?key)|"
                         r"authorization|cookie|pushover_user|[\"']user[\"']|\bkey)[\"']?)(\s*[:=]\s*[\"']?|\s+)(bearer\s+)?[^\s\"',;&}]+")


def finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def parse_mark_results(data):
    """What the detector reported about sightings: (mark id, state) from complete lines."""
    results = []
    for line in data.split(b"\n"):
        line = line.strip(b"\0 \r\t")
        if not line.startswith(b"{") or b'"mark_' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("event_type") == "mark_recording":
            results.append((str(event.get("mark_id", "")), str(event.get("state", ""))))
        elif isinstance(event, dict) and event.get("event_type") == "mark_error":
            results.append((str(event.get("request", "")).split(".")[0], "error"))
    return results


def parse_lines(data):
    """Confirmed candidate events from complete JSON lines (any JSON formatting)."""
    events = []
    for line in data.split(b"\n"):
        line = line.strip(b"\0 \r\t")       # an unclean stop leaves NUL padding
        # Cheap guard on the key name only; the value is checked after parsing.
        if not line.startswith(b"{") or b'"confirmed"' not in line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if (isinstance(event, dict) and event.get("event_type") == "candidate"
                and event.get("confirmed") is True):
            events.append(event)
    return events


class Tail:
    """Incremental reader of complete JSON lines appended to one file."""

    def __init__(self):
        self.path = None
        self.pos = 0
        self.buf = b""
        self.mark_results = []

    def follow(self, path, from_end):
        self.path, self.buf = path, b""
        try:
            self.pos = path.stat().st_size if from_end else 0
        except OSError:
            self.pos = 0

    def recent(self, nbytes=BACKFILL_BYTES):
        """Confirmed events in the last `nbytes` before the current position."""
        if self.path is None or self.pos == 0:
            return []
        try:
            with open(self.path, "rb") as fh:
                start = max(0, self.pos - nbytes)
                fh.seek(start)
                data = fh.read(self.pos - start)
        except OSError:
            return []
        if start:
            data = data.split(b"\n", 1)[-1]    # drop the partial first line
        return parse_lines(data)

    def read(self):
        if self.path is None:
            return []
        try:
            size = self.path.stat().st_size
            if size < self.pos:                 # replaced or truncated
                self.pos, self.buf = 0, b""
            if size == self.pos:
                return []
            with open(self.path, "rb") as fh:
                fh.seek(self.pos)
                chunk = fh.read(min(size - self.pos, READ_CAP))
        except OSError:
            return []
        self.pos += len(chunk)
        data = self.buf + chunk
        cut = data.rfind(b"\n")
        if cut < 0:                             # no complete line yet
            self.buf = data if len(data) <= READ_CAP else b""
            return []
        self.buf = data[cut + 1:]
        self.mark_results.extend(parse_mark_results(data[:cut]))
        return parse_lines(data[:cut])


# Rising: the newest match on a channel is at least RISE_DB above the weakest of the last minute's, and
# the strongest of them, with RISE_MIN_MATCHES or more. Matched strength is not distance (a radio sets
# its power by its own mast), so this is "getting louder", not "getting closer".
RISE_DB = 6.0
RISE_MIN_MATCHES = 3
MATCH_RULES = ("standard", "weak", "coherent")   # p0/phy.py's recognition rules
CALL_HOLD_S = 10.0     # a channel shows "in a call" this long after its last call-pattern match


def rising(strengths, newest):
    """True when `newest` rises RISE_DB above the weakest of `strengths` (which include it) and tops them."""
    if newest is None or len(strengths) < RISE_MIN_MATCHES:
        return False
    return newest - min(strengths) >= RISE_DB and newest >= max(strengths) - 0.05


class Channels:
    """Recent confirmed matches per TETRA channel, aged on the Pi's monotonic clock."""

    def __init__(self):
        self.state = {}

    def add(self, event, session, now_mono):
        ch = event.get("tetra_channel")
        if not isinstance(ch, int):
            return None
        reliable = event.get("strength_reliable", True) is True and finite(event.get("peak_snr_db"))
        seen = float(event["burst_mono"]) if finite(event.get("burst_mono")) else now_mono
        rule = (event.get("matched_burst") or {}).get("rule")
        match = {"id": f"{session}/{event.get('event_id', '')}", "ch": ch, "seen": seen,
                 "strength_db": round(float(event["peak_snr_db"]), 1) if reliable else None,
                 "freq_mhz": round(float(event.get("frequency_hz") or 0) / 1e6, 4),
                 "activity": event.get("activity") if isinstance(event.get("activity"), str) else None,
                 "approx_time": event.get("matched_timing_refined") is False,
                 "delay_s": round(max(0.0, now_mono - seen), 2),
                 "call": event.get("call_like") is True,
                 # Which recognition rule matched (1.2.9): a record of how, never closer or "police".
                 "rule": rule if rule in MATCH_RULES else None}
        row = self.state.get(ch)
        history = row["strengths"] if row else deque()
        while history and seen - history[0][0] > HOLD_S:
            history.popleft()
        if match["strength_db"] is not None:
            history.append((seen, match["strength_db"]))
        match["rising"] = rising([x for _, x in history], match["strength_db"])
        last_call = row["last_call"] if row else None
        if match["call"]:
            last_call = seen
        if row is None or seen >= row["seen"]:
            hits = row["hits"] if row else deque()
            row = dict(match, hits=hits)
            self.state[ch] = row
        row["hits"].append(seen)
        row["strengths"], row["last_call"] = history, last_call
        return match

    def snapshot(self, now_mono):
        for ch in list(self.state):
            row = self.state[ch]
            while row["hits"] and now_mono - row["hits"][0] > HOLD_S:
                row["hits"].popleft()
            if now_mono - row["seen"] > HOLD_S:
                del self.state[ch]
        rows = sorted(self.state.values(), key=lambda r: -r["seen"])[:MAX_CHANNELS]
        return [{"ch": r["ch"], "id": r["id"], "freq_mhz": r["freq_mhz"],
                 "strength_db": r["strength_db"], "age_s": round(max(0.0, now_mono - r["seen"]), 2),
                 "hits_60s": len(r["hits"]), "activity": r["activity"],
                 "approx_time": r["approx_time"], "rising": bool(r.get("rising")),
                 "call": r.get("last_call") is not None and now_mono - r["last_call"] <= CALL_HOLD_S}
                for r in rows]


def quiet_limit(hb):
    """Quiet windows in a row that make the alarm, from what the heartbeat says; None if it says nothing."""
    limit = hb.get("quiet_input_alarm_windows")
    if finite(limit) and limit > 0:
        return int(limit)
    window_s = hb.get("window_s")
    if finite(window_s) and 0 < window_s <= 60:
        return max(1, int(round(QUIET_ALARM_S / window_s)))
    return None


def read_heartbeat(path=HEARTBEAT):
    try:
        hb = json.loads(path.read_text(encoding="utf-8"))
        return hb if isinstance(hb, dict) else None
    except (OSError, ValueError):
        return None


class Heartbeat:
    """Detector health from heartbeat *progress* on the monotonic clock.

    A present heartbeat file is not health: the detector must keep rewriting it.
    Wall-clock `t` is only used as a change marker, so NTP steps cannot make a
    live detector look stale or a stuck one look fresh.
    """

    def __init__(self, path):
        self.path = path
        self.hb = None
        self.sig = None
        self.progress_mono = None
        self.quiet_window = None      # last analysis window counted
        self.quiet_windows = 0        # consecutive windows with a suspiciously low input

    def reset_input(self):
        self.quiet_window, self.quiet_windows = None, 0

    def poll(self, now_mono):
        hb = read_heartbeat(self.path)
        if hb is None:
            self.hb = self.sig = self.progress_mono = None
            self.reset_input()
            return None
        sig = (hb.get("pid"), hb.get("t"), hb.get("samples_in"), hb.get("windows_started"))
        if sig != self.sig:
            self.sig, self.progress_mono = sig, now_mono
        self._count_input(hb)
        self.hb = hb
        return hb

    def _count_input(self, hb):
        """Count consecutive analysis windows whose input looks too weak to hear anything.

        The detector's own `antenna_suspect` heuristic, counted per window id so a
        heartbeat read twice cannot inflate the run. A brief run is normal (the
        12 September working drives peaked at 5 windows); a disconnected antenna
        ran for the whole session (806 and 857 windows).
        """
        if finite(hb.get("quiet_windows")) and hb["quiet_windows"] >= 0:
            self.quiet_windows = int(hb["quiet_windows"])   # the detector saw every window; this loop may not
            return
        rf = hb.get("rf_health")
        if not isinstance(rf, dict):
            return
        window = rf.get("window_id")
        if not isinstance(window, str) or window == self.quiet_window:
            return
        self.quiet_window = window
        suspect = (rf.get("input") or {}).get("antenna_suspect") is True
        self.quiet_windows = self.quiet_windows + 1 if suspect else 0

    def state(self, now_mono):
        hb = self.hb
        if hb is None:
            return {"state": "stopped"}
        unchanged = now_mono - self.progress_mono
        reasons = [str(r.get("fault"))[:60] for r in (hb.get("degraded_reasons") or [])
                   if isinstance(r, dict)][:6]
        if unchanged > STALE_S:
            state = "stale"
        elif not hb.get("ready"):
            state = "starting"
        elif hb.get("degraded"):
            state = "degraded"
        else:
            state = "running"
        out = {"state": state, "heartbeat_unchanged_s": round(unchanged, 1), "reasons": reasons,
               "quiet_windows": self.quiet_windows}
        limit = quiet_limit(hb)
        if limit is not None:
            out["quiet_alarm_windows"] = limit
        if finite(hb.get("window_s")) and 0 < hb["window_s"] <= 60:
            out["window_s"] = float(hb["window_s"])
        peak = ((hb.get("rf_health") or {}).get("input") or {}).get("peak_counts")
        if finite(peak):
            out["input_peak_counts"] = round(float(peak), 1)
        # The quiet level of the band in the detector's own dB. Every dB it rises is a dB of reach lost,
        # so the phone shows it live: for trying another place for the antenna, or unplugging a charger.
        floor = (hb.get("rf_health") or {}).get("median_floor_db")
        if finite(floor):
            out["floor_db"] = round(float(floor), 1)
        for key, src in (("windows", "windows"), ("hits", "hits"), ("unchecked", "unchecked"),
                         ("partial", "partial_confirmations"), ("whole_band_windows", "whole_band_windows")):
            if isinstance(hb.get(src), int) and not isinstance(hb.get(src), bool):
                out[key] = hb[src]
        return out


def read_pi_health(session_dir=None):
    """Cheap readings from /proc and /sys for the app. Anything unavailable is left out."""
    health = {}

    def text(path):
        return Path(path).read_text(encoding="utf-8")
    try:
        health["uptime_s"] = float(text("/proc/uptime").split()[0])
    except (OSError, ValueError, IndexError):
        pass
    try:
        health["temp_c"] = int(text("/sys/class/thermal/thermal_zone0/temp").strip()) / 1000
    except (OSError, ValueError):
        pass
    try:
        health["load_1m"] = float(text("/proc/loadavg").split()[0])
    except (OSError, ValueError, IndexError):
        pass
    try:
        for line in text("/proc/meminfo").splitlines():
            if line.startswith("MemAvailable:"):
                health["mem_free_mb"] = int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        stat = os.statvfs(session_dir or Path.home())
        health["disk_free_gb"] = stat.f_bavail * stat.f_frsize / 1e9
    except (OSError, AttributeError):       # AttributeError: no statvfs on Windows test machines
        pass
    return health


MODEM_STATE = Path("/run/slot17-modem.json")
MODEM_FRESH_S = 120.0       # the stick check runs every 30 s; an older reading is not shown


def _num_or_none(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def link_kind(device, modem_device=None):
    if not device:
        return None
    if device == modem_device:
        return "4G stick"
    if device.startswith(("wlan", "wl")):
        return "Wi-Fi"
    if device.startswith(("eth", "en", "usb")):
        return "cable"
    return device


def wifi_signal(device="wlan0", path="/proc/net/wireless"):
    """dBm from the kernel's own table, or None. Cheap: no programs run."""
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if parts and parts[0].rstrip(":") == device and len(parts) > 3:
                return _num_or_none(parts[3].rstrip("."))
    except OSError:
        pass
    return None


def read_link(stats=None, routes=None, modem_state=MODEM_STATE, now_mono=None, synced=None):
    """What the reports travel over, for the app's Connection card. Anything unknown is left out."""
    now_mono = time.monotonic() if now_mono is None else now_mono
    routes = default_routes() if routes is None else routes
    modem = {}
    try:
        modem = json.loads(Path(modem_state).read_text(encoding="utf-8"))
        if not isinstance(modem, dict) or now_mono - float(modem.get("reading_at", -1e9)) > MODEM_FRESH_S:
            modem = {}
    except (OSError, ValueError, TypeError):
        modem = {}
    modem_device = modem.get("device") if isinstance(modem.get("device"), str) else None
    link = {"via": link_kind(routes[0], modem_device) if routes else "none",
            "networks": [link_kind(d, modem_device) for d in routes][:4]}
    if routes:
        link["device"] = routes[0][:12]
    last = getattr(stats, "last_minute", None) or {}
    for key in ("pushes", "failed", "median_ms", "slowest_ms", "failovers"):
        if isinstance(last.get(key), (int, float)):
            link[key] = last[key]
    if synced is not None:
        link["clock_synced"] = bool(synced)
    if "wlan0" in routes:
        dbm = wifi_signal()
        if dbm is not None:
            link["wifi_dbm"] = dbm
    if modem:
        reading = modem.get("reading") or {}
        stick = {"connected": modem.get("connected") is True,
                 "network": str(reading.get("network_type", ""))[:16] or None,
                 "provider": str(reading.get("network_provider", ""))[:24] or None,
                 "roaming": reading.get("roam_setting_option") == "on" if reading.get("roam_setting_option") else None,
                 "bars": _num_or_none(reading.get("signalbar")), "rsrp": _num_or_none(reading.get("lte_rsrp")),
                 "rsrq": _num_or_none(reading.get("lte_rsrq")), "snr": _num_or_none(reading.get("lte_snr")),
                 "cell": (str(reading.get("cell_id", "")) + "/" + str(reading.get("lte_pci", ""))).strip("/")[:24] or None}
        trip = modem.get("trip")
        if isinstance(trip, list) and len(trip) == 4 and trip[1]:
            stick["ping_ms"], stick["ping_worst_ms"] = trip[2], trip[3]
            stick["ping_lost"] = trip[0] - trip[1]
        if modem.get("down_since") is not None:
            stick["down_s"] = max(0.0, now_mono - float(modem["down_since"]))
        link["stick"] = {k: v for k, v in stick.items() if v is not None}
    return link


def write_mark(session, op, wall=None):
    """Save the app's sighting as the same request file p0/mark.py writes.

    The time comes from how long ago the phone said it happened, applied to this Pi's clock,
    so it agrees with the detector's own clock whatever that is. The Pi has no RTC, so the
    phone's wall clock could be hours from it, which would also lose the IQ snapshot. The
    phone's time goes in the note. Named from the app's id, so a resend cannot save it twice.
    """
    if not isinstance(op.get("label"), str) or op["label"] not in MARK_LABELS or not re.fullmatch(r"[0-9a-f]{32}", str(op.get("id"))):
        raise ValueError("invalid sighting")
    inbox = Path(session) / "marks"
    if not (Path(session) / "session.json").is_file():
        raise FileNotFoundError("no session.json")
    inbox.mkdir(exist_ok=True)
    name = op["id"] + ".request.json"
    if (inbox / name).exists() or (inbox / (op["id"] + ".request.processed")).exists():
        return False
    wall = time.time() if wall is None else wall
    note = f"from the app; phone_utc={op.get('phone_utc')}; {str(op.get('note', ''))[:500]}".strip("; ")
    age = op.get("age_s", 0)
    if not finite(age) or age < 0:
        raise ValueError("invalid sighting age")
    mark = {"utc": wall - float(age), "label": op["label"], "note": note, "entered_utc": wall}
    tmp = inbox / (name + ".tmp")
    tmp.write_text(json.dumps(mark), encoding="utf-8")
    tmp.replace(inbox / name)
    return True


def redact(text):
    return SECRET_TEXT.sub(lambda m: f"{m.group(1)}{m.group(2)}[removed]", text)


def last_bytes(text, limit):
    """The end of a text within a byte limit, without cutting a character in half."""
    return text.encode("utf-8", "replace")[-limit:].decode("utf-8", "ignore").encode("utf-8")


def collect_pi_log(session, heartbeat=HEARTBEAT, runner=None):
    """A bounded text report: session files, heartbeat and recent journal. Light on purpose."""
    runner = runner or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=20))
    parts = [f"Slot17 Pi log. Monotonic {time.monotonic():.1f}; Pi clock {time.time():.1f}.\n"
             "This Pi has no RTC: folder names can be wrong, use session-time.json.\n"]
    files = [heartbeat]
    if session:
        files += [Path(session) / n for n in ("session-time.json", "session.json", "summary.json")]
    for path in files:
        try:
            with open(path, "rb") as fh:       # only what is sent gets read: this runs beside a capture
                text = fh.read(LOG_FILE_BYTES).decode("utf-8", "replace")
            parts.append(f"\n===== {Path(path).name} =====\n{text}\n")
        except OSError as exc:
            parts.append(f"\n===== {Path(path).name}: unavailable ({type(exc).__name__}) =====\n")
    command = ["nice", "-n", "19", "journalctl", "-u", "tetra-detector.service", "-u",
               "tetra-display-publisher.service", "-n", str(JOURNAL_LINES), "--no-pager", "-o", "short-monotonic"]
    try:
        done = runner(command)
        parts.append(f"\n===== journal =====\n{done.stdout or done.stderr}\n")
    except Exception as exc:                # noqa: BLE001 - the rest of the report still helps
        parts.append(f"\n===== journal: unavailable ({type(exc).__name__}) =====\n")
    # The network the reports travel over: routes, and the 4G stick's own lines if there is one.
    for title, cmd in (("network", ["ip", "-4", "-br", "addr"]), ("routes", ["ip", "-4", "route", "show", "default"]),
                       ("4G stick", ["nice", "-n", "19", "journalctl", "-u", "slot17-modem.service", "-b", "-n",
                                     str(MODEM_LINES), "--no-pager", "-o", "short-monotonic", "--grep", "modem:"])):
        try:
            done = runner(cmd)
            parts.append(f"\n===== {title} =====\n{done.stdout or done.stderr}\n")
        except Exception as exc:            # noqa: BLE001 - absent on a car computer without a stick
            parts.append(f"\n===== {title}: unavailable ({type(exc).__name__}) =====\n")
    return last_bytes(redact("".join(parts)), LOG_UPLOAD_CAP)


def load_updater():
    """The detector's updater, if this installation has one and a release key. Otherwise None."""
    folder = str(Path(__file__).resolve().parent.parent / "p0")
    try:
        if folder not in sys.path:
            sys.path.insert(0, folder)
        import update
        updater = update.Updater()
        return updater if updater.key_file.exists() else None
    except Exception as exc:                    # noqa: BLE001 - the display works without it
        print(f"updates unavailable: {type(exc).__name__}", flush=True)
        return None


class PushRejected(OSError):
    pass


def default_routes():
    """Interfaces with a default route, best first (lowest metric), from `ip -4 route show default`."""
    try:
        out = subprocess.run(["ip", "-4", "route", "show", "default"], capture_output=True, text=True,
                             timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        dev = re.search(r"\bdev (\S+)", line)
        metric = re.search(r"\bmetric (\d+)", line)
        if dev:
            found.append((int(metric[1]) if metric else 0, dev[1]))
    return [dev for _, dev in sorted(found)]


class PinnedConnection(http.client.HTTPConnection):
    """An HTTP connection that leaves through one named interface, whatever the routing table prefers.

    So a push that stalls on the 4G stick can be retried straight away over the phone's hotspot,
    instead of waiting for the stick's own check to notice and re-rank the routes.
    """

    def __init__(self, *args, device=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.device = device

    def connect(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if self.device and hasattr(socket, "SO_BINDTODEVICE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, self.device.encode())
            sock.settimeout(self.timeout)
            sock.connect((self.host, self.port))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            sock.close()
            raise
        self.sock = sock


class LinkStats:
    """What the link to the server was like over the last minute, for the Pi log and the app."""

    def __init__(self):
        self.ms = None
        self.last_minute = {}
        self.reset()

    def reset(self):
        if getattr(self, "ms", None) is not None and (self.ms or self.failed):
            ms = sorted(self.ms)
            self.last_minute = {"pushes": len(ms), "failed": self.failed, "failovers": self.failovers,
                                "median_ms": round(ms[len(ms) // 2]) if ms else None,
                                "slowest_ms": round(ms[-1]) if ms else None}
        self.ms, self.failed, self.failovers, self.devices = [], 0, 0, {}

    def ok(self, ms, device):
        self.ms.append(ms)
        self.devices[device or "default"] = self.devices.get(device or "default", 0) + 1

    def line(self, seconds):
        if not self.ms and not self.failed:
            return None
        ms = sorted(self.ms)
        text = f"link: {len(ms)} pushes in {seconds:.0f} s, {self.failed} failed"
        if ms:
            text += f", median {ms[len(ms) // 2]:.0f} ms, slowest {ms[-1]:.0f} ms"
        text += ", via " + ", ".join(f"{d} {n}" for d, n in sorted(self.devices.items())) if self.devices else ""
        if self.failovers:
            text += f", {self.failovers} sent over another network after a failure"
        return text


class Sender:
    """POST over one kept-open connection; on failure, at once over every other network there is.

    Before 15 November 2026 a failed push waited for the next keepalive (2 s) and always took the
    route the kernel preferred, so a few seconds of a 4G cell change at motorway speed were a gap
    on the phone even with the hotspot switched on beside it.
    """
    PREFERRED_AFTER_S = 60.0     # back on the preferred network this long after a failover

    def __init__(self, url, token, take_commands=True, routes=default_routes):
        u = urlsplit(url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise SystemExit(f"bad push url: {url}")
        self.https = u.scheme == "https"
        self.host, self.port = u.hostname, u.port or (443 if self.https else 80)
        self.path = u.path or "/api/push"
        self.headers = {"Content-Type": "application/json", "Authorization": "Bearer " + token,
                        "User-Agent": "Slot17-display-publisher/3", "Connection": "keep-alive",
                        "X-Display-Source": secrets.token_hex(16)}
        if not take_commands:                    # --once ends straight away and would lose them
            self.headers["X-Display-Commands"] = "none"
        self.conn = None
        self.pending_command = None
        self.pending_ops = deque(maxlen=32)
        self.routes = routes
        self.device = None           # the interface the open connection is pinned to; None = the kernel's choice
        self.pinned_since = None
        self.stats = LinkStats()
        self.lock = threading.Lock()     # one push at a time over the one connection, whichever thread sends

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
        self.conn = None

    def _connect(self, device):
        self.device = device
        if self.https:
            self.conn = http.client.HTTPSConnection(self.host, self.port, timeout=TIMEOUT_S)
        else:
            self.conn = PinnedConnection(self.host, self.port, timeout=TIMEOUT_S, device=device)

    def __call__(self, payload):
        """Send one push: over the open connection, a new one, then each other network. Raises if all fail."""
        with self.lock:
            self._send(payload)

    def _send(self, payload):
        started = time.monotonic()
        if self.device is not None and self.pinned_since is not None \
                and started - self.pinned_since > self.PREFERRED_AFTER_S:
            self.close()                         # give the preferred network another chance
            self.device = self.pinned_since = None
        start_device, routes = self.device, []

        def attempts():
            """Where to try, in order: the open connection, a new one, then every other network."""
            if self.conn is not None:
                yield None, start_device                 # None: keep the open connection
            yield "new", start_device
            if self.https:
                return
            routes.extend(self.routes())                 # only asked once something has failed
            for dev in routes:
                # Skip the network just tried; with no pin that was the kernel's first choice.
                if dev != start_device and not (start_device is None and dev == routes[0]):
                    yield "new", dev

        error = None
        for kind, device in attempts():
            if kind == "new":
                self.close()
                self._connect(device)
            try:
                self._exchange(payload)
            except PushRejected:
                self.close()
                raise
            except (OSError, http.client.HTTPException) as exc:
                self.close()
                error = exc
                continue
            if device != start_device and device is not None:
                self.stats.failovers += 1
                self.pinned_since = time.monotonic()
                print(f"push went over {device} after a failure on {start_device or (routes or ['the default route'])[0]}",
                      flush=True)
            self.stats.ok((time.monotonic() - started) * 1000, self.device)
            return
        self.stats.failed += 1
        raise error

    def _exchange(self, payload):
        """The clock challenge and the push itself, over `self.conn`."""
        snapshot = payload.get("snapshot_mono", time.monotonic())
        # A server-timed challenge bounds the outbound trip without
        # comparing Pi/server wall clocks (the Pi's can step at boot).
        self.conn.request("GET", "/api/clock", headers=self.headers)
        resp = self.conn.getresponse()
        data = resp.read(MAX_ANSWER_BYTES + 1)
        if resp.status != 200 or len(data) > MAX_ANSWER_BYTES:
            raise PushRejected(f"clock challenge answered {resp.status}, {len(data)} bytes")
        elapsed = max(0.0, time.monotonic() - snapshot)
        try:
            answer = json.loads(data)
        except ValueError as exc:
            raise PushRejected("clock challenge was not JSON") from exc
        if not isinstance(answer, dict) or not isinstance(answer.get("ticket"), str):
            raise PushRejected("clock challenge had no ticket")
        # The server hands back at most one operator command here. This
        # is the only inbound path that exists: the Pi never listens.
        command = answer.get("command")
        if isinstance(command, dict) and command.get("action") == "stop":
            self.pending_command = command
        ops = answer.get("ops")
        if isinstance(ops, list):
            self.pending_ops.extend(op for op in ops if isinstance(op, dict))
        wire = {k: v for k, v in payload.items() if k != "snapshot_mono"}
        wire.update(v=3, source=self.headers["X-Display-Source"],
                    ticket=answer["ticket"], snapshot_age_s=elapsed)
        for key in ("channels", "matches"):
            wire[key] = [dict(row, age_s=row["age_s"] + elapsed) for row in payload[key]]
        body = json.dumps(wire, separators=(",", ":"), allow_nan=False).encode()
        self.conn.request("POST", self.path, body, self.headers)
        resp = self.conn.getresponse()
        resp.read(MAX_ANSWER_BYTES)
        if resp.status != 204:
            raise PushRejected(f"server answered {resp.status}")
        if resp.getheader("Connection", "").lower() == "close":
            self.close()


def load_config(path=CONFIG):
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not cfg.get("url") or len(cfg.get("token", "")) < 32:
        raise SystemExit(f"{path}: needs 'url' and a 'token' of at least 32 characters")
    return cfg


def phone_sender():
    """Push a short fault notice with the detector's own Pushover settings, or nothing.

    Uses `p0/notify.py` read-only; it never writes to the detector's outbox and
    never sends RF alerts. Returns None when Pushover is not configured here.
    """
    notify = notify_module()
    try:
        if notify is None or not notify.configured():
            return None
    except Exception:                       # noqa: BLE001 - the display must run regardless
        return None

    def send(message, title, priority):
        def run():
            # Log the success too. This only logged failures, so on 16 September
            # a session that was 132/132 windows antenna-suspect left no trace of
            # whether the warning ever reached the phone - success and "Pushover
            # not configured in this process" looked identical afterwards. An
            # alert nobody can audit is not much of an alert.
            try:
                sent = notify.send_now(message, title=title, priority=priority, ttl=notify.STATUS_TTL_S)
                if sent is None:
                    print(f"phone notice not sent, Pushover switched off in the app: {title!r}", flush=True)
                else:
                    print(f"phone notice sent: {title!r} (priority {priority})", flush=True)
            except Exception as exc:        # noqa: BLE001
                print(f"phone notice failed: {title!r}: {type(exc).__name__}: {exc}", flush=True)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread
    return send


def notify_module():
    """`p0/notify.py`, or None where it cannot be loaded (a display-only install)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "p0"))
    try:
        import notify
        return notify
    except Exception:                       # noqa: BLE001 - the display must run regardless
        return None


def software_version():
    """The detector's version, from `p0/version.py` (1.2.9 and later), or None."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "p0"))
    try:
        import version
        return str(version.SOFTWARE)[:16]
    except Exception:                       # noqa: BLE001 - older p0, or a display-only install
        return None


def notify_state():
    """What the app shows for Pushover: whether it is set up here and whether it is switched on."""
    notify = notify_module()
    if notify is None:
        return None
    try:
        return {"pushover": notify.pushover_enabled(), "configured": notify.configured()}
    except Exception:                       # noqa: BLE001 - a detail for the app; the push must go
        return None


def run_notify(op, pub):
    """Switch Pushover messages on or off for this car computer, as asked from the app."""
    notify = notify_module()
    wanted = op.get("pushover")
    if notify is None or not isinstance(wanted, bool):
        pub.ack(op["id"], "failed", "Pushover settings are not available on this car computer")
        return
    try:
        notify.set_pushover(wanted)
    except OSError as exc:
        print(f"Pushover switch failed: {type(exc).__name__}: {exc}", flush=True)
        pub.ack(op["id"], "failed", f"Could not save the setting: {type(exc).__name__}")
        return
    print(f"operator command: Pushover messages {'on' if wanted else 'off'}", flush=True)
    pub.notify_state = notify_state()
    pub.ack(op["id"], "done", f"Pushover messages {'on' if wanted else 'off'}")


BLUETOOTH_RESTART_S = (5.0, 30.0, 120.0, 600.0)  # after the link's process ended by itself, ever more slowly
BLUETOOTH_SETTINGS_EVERY_S = 60.0                # re-read, in case setup.sh changed them
BLUETOOTH_LINE_BYTES = 4096                      # one command: a pipe write this small is all or nothing
BLUETOOTH_STOP_S = 3.0


def bluetooth_module():
    """`p0/bluetooth_config.py`, or None where it cannot be loaded (a display-only install, an older p0)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "p0"))
    try:
        import bluetooth_config
        return bluetooth_config
    except Exception:                       # noqa: BLE001 - the display must run regardless
        return None


class BluetoothLink:
    """The Bluetooth link's own process (`p0/bluetooth_link.py`), run while Bluetooth is switched on.

    It gets the same matches and detector state as the display server, and is never in their way:
    commands go into a non-blocking pipe and are dropped when it is full, and a link that keeps
    ending is started again ever more slowly. Its status lines come back on a reader thread.
    """

    def __init__(self, root=None, config=None, spawn=None):
        self.root = Path(root) if root else Path(__file__).resolve().parents[1]
        self.config = config if config is not None else bluetooth_module()
        self.spawn = spawn or self._spawn
        self.proc = None
        self.status, self.lock = {}, threading.Lock()
        self.failures, self.next_start = 0, 0.0
        self.dropped = 0
        self.last_feed = -1e9
        self.settings, self.settings_at = None, -1e9

    def _spawn(self):
        proc = subprocess.Popen([sys.executable, "-B", str(self.root / "p0/bluetooth_link.py"), "serve"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=str(self.root / "p0"))
        os.set_blocking(proc.stdin.fileno(), False)     # a link that stops reading must never hold us up
        return proc

    def _write(self, data):
        """One whole line into the link's input. BlockingIOError when the pipe is full."""
        os.write(self.proc.stdin.fileno(), data)

    def _settings(self, now, fresh=False):
        if self.config is None:
            return None
        if fresh or self.settings is None or now - self.settings_at >= BLUETOOTH_SETTINGS_EVERY_S:
            try:
                self.settings, self.settings_at = self.config.public(self.config.load()), now
            except Exception as exc:        # noqa: BLE001 - unreadable settings mean off, never a crash
                print(f"bluetooth settings unreadable: {type(exc).__name__}: {exc}", flush=True)
                self.settings, self.settings_at = None, now
        return self.settings

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def poll(self, now):
        """Start the link when it is on and not running; notice when it ended. Never raises."""
        ended = self.proc is not None and self.proc.poll() is not None
        settings = self._settings(now, fresh=ended)     # switched off over Bluetooth: the link ended by itself
        if ended:
            code, self.proc = self.proc.returncode, None
            if settings and settings["on"]:
                self.failures += 1
                wait = BLUETOOTH_RESTART_S[min(self.failures, len(BLUETOOTH_RESTART_S)) - 1]
                self.next_start = now + wait
                print(f"bluetooth link ended (exit {code}); starting it again in {wait:.0f} s", flush=True)
        if not settings or not settings["on"]:
            if self.proc is not None:
                self.stop()
            return
        if self.proc is None and now >= self.next_start:
            try:
                self.proc = self.spawn()
            except Exception as exc:        # noqa: BLE001 - no Bluetooth is a detail, never a crash
                self.proc = None
                self.failures += 1
                self.next_start = now + BLUETOOTH_RESTART_S[-1]
                print(f"bluetooth link did not start: {type(exc).__name__}: {exc}", flush=True)
                return
            threading.Thread(target=self._read, args=(self.proc,), daemon=True, name="bluetooth").start()

    def _read(self, proc):
        for raw in proc.stdout:
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if isinstance(line, dict):
                with self.lock:
                    self.status = line
                if line.get("state") in ("advertising", "connected"):
                    self.failures = 0
                if line.get("settings_changed"):
                    self.settings_at = -1e9     # a phone changed them over Bluetooth: read them again

    def feed(self, command):
        """Hand one command to the link, or drop it when the link is busy. Returns True when written."""
        if not self.running():
            return False
        line = (json.dumps(command, separators=(",", ":")) + "\n").encode()
        if len(line) > BLUETOOTH_LINE_BYTES:
            self.dropped += 1
            return False
        try:
            self._write(line)
            return True
        except BlockingIOError:
            self.dropped += 1
        except OSError:
            pass                            # the link ended; poll() notices and starts it again
        return False

    def reload(self, now=None):
        """The settings changed (the app's switch or name): tell the link, or start or stop it."""
        now = time.monotonic() if now is None else now
        settings = self._settings(now, fresh=True)
        self.failures, self.next_start = 0, 0.0
        if self.running():
            self.feed({"reload": True})     # renames itself, or ends when switched off
        self.poll(now)
        return settings

    def stop(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()              # the link ends when its input does
            proc.wait(timeout=BLUETOOTH_STOP_S)
        except Exception:                   # noqa: BLE001 - make sure it goes, whatever happened
            proc.kill()
        with self.lock:
            self.status = {}

    def state(self, now):
        """For the push: the switch, and what the link last said about itself."""
        settings = self._settings(now)
        if settings is None:
            return None
        with self.lock:
            line = dict(self.status)
        on = settings["on"]
        state = line.get("state") if on and self.running() else "starting" if on else "off"
        out = {"on": on, "state": state if state else "starting", "name": settings["name"],
               "subscribed": bool(on and line.get("subscribed")), "frames": int(line.get("frames") or 0),
               "matches": int(line.get("matches") or 0)}
        if settings.get("key_id"):
            out["key_id"] = settings["key_id"]
        if finite(line.get("since")) and on:
            out["since_s"] = round(max(0.0, now - line["since"]), 1)
        if isinstance(line.get("mtu"), int):
            out["mtu"] = line["mtu"]
        if line.get("error") and on:
            out["error"] = str(line["error"])[:120]
        if on and self._phones_pair(now):
            out["phones_pair"] = True        # the app says: run the setup once more (1.5.1)
        return out

    def _phones_pair(self, now):
        """BlueZ still acts as a GATT client here, so phones are asked to pair at every connection. Read once
        a minute; only the setup (as root) can change it."""
        if now - getattr(self, "_pair_checked", -1e9) >= 60.0:
            self._pair_checked, self._pair_said = now, False
            config = self.config
            try:
                with open(getattr(config, "MAIN_CONF", "/etc/bluetooth/main.conf"), encoding="utf-8") as fh:
                    self._pair_said = bool(config and config.gatt_client(fh.read()))
            except (OSError, AttributeError, UnicodeDecodeError):
                pass
        return self._pair_said


SUPPORT_PIECE_BYTES = 768 * 1024   # under the server's 1 MB per upload
SUPPORT_MAKE_LIMIT_S = 3600.0
SUPPORT_TIMEOUT_S = 60.0
SUPPORT_RETRIES = 5
DRIVES_EVERY_S = 300.0             # the app's list of drives to choose a package from
DRIVES_SHOWN = 12
SUPPORT_CHOICES = ("quick", "drive", "everything")


def support_module():
    """`p0/support.py`, or None where it cannot be loaded (a display-only install, an older p0)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "p0"))
    try:
        import support
        return support
    except Exception:                       # noqa: BLE001 - the display must run regardless
        return None


def drives_state():
    """The newest drives, for the app's package choice: the same rows as `support.py --list`, shortened."""
    support = support_module()
    if support is None:
        return None
    try:
        rows = support.drives()[:DRIVES_SHOWN]
    except Exception:                       # noqa: BLE001 - a detail for the app; the push must go
        return None
    return [{"session": r["session"][:80], "start": r["start"], "minutes": r["minutes"], "matches": r["matches"],
             "faults": len(r["faults"]), "mb": r["events_mb"], "recordings": r["recordings"]} for r in rows]


class SupportJob:
    """One support package at a time, asked for from the app: p0/support.py makes it in its own process at the
    lowest priority, then it goes to the server in pieces, resuming where the server says it got to."""

    def __init__(self, root=None, run=None, opener=None):
        self.root = Path(root) if root else Path(__file__).resolve().parents[1]
        self.made_elsewhere = run is not None
        self.run = run or self._run
        self.opener = opener
        self.busy = threading.Lock()
        self.state = None                   # what the push says about the package on its way

    @property
    def available(self):
        """support.py is here to run (it came with 1.4.0); a display-only install has no p0/."""
        return self.made_elsewhere or (self.root / "p0/support.py").is_file()

    def _run(self, args):
        done = subprocess.run(["nice", "-n", "19", sys.executable, "-B", str(self.root / "p0/support.py"), *args],
                              capture_output=True, text=True, timeout=SUPPORT_MAKE_LIMIT_S)
        if done.returncode != 0:
            raise OSError((done.stderr or done.stdout or "support.py failed").strip().splitlines()[-1][:160])
        return json.loads(done.stdout.strip().splitlines()[-1])

    def start(self, op, pub, cfg):
        if not self.busy.acquire(blocking=False):
            pub.ack(op["id"], "failed", "A package is already being made")
            return None
        thread = threading.Thread(target=self._work, args=(op, pub, cfg), daemon=True, name="support")
        thread.start()
        return thread

    def _work(self, op, pub, cfg):
        out = Path.home() / ".cache" / f"slot17-support-{op['id'][:8]}.zip"
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            args = ["--out", str(out)]
            if op["choice"] != "quick":
                args += ["--drive", op["session"]]
                if op.get("window"):
                    args += ["--window", f"{op['window'][0]}-{op['window'][1]}"]
                if op["choice"] == "everything":
                    args.append("--everything")
                    if op.get("recordings"):
                        args.append("--recordings")
            self.state = {"state": "building"}
            self.run(args)
            name = f"support-{time.strftime('%Y%m%d-%H%M%SZ', time.gmtime())}-{op['id'][:8]}.zip"
            self._upload(out, name, op, cfg)
            print(f"support package uploaded: {name} ({out.stat().st_size} bytes)", flush=True)
            pub.ack(op["id"], "done", name)
        except Exception as exc:            # noqa: BLE001 - report it, keep the display running
            print(f"support package failed: {type(exc).__name__}: {exc}", flush=True)
            pub.ack(op["id"], "failed", f"{type(exc).__name__}: {exc}"[:160])
        finally:
            self.state = None
            out.unlink(missing_ok=True)
            self.busy.release()

    def _upload(self, path, name, op, cfg):
        total = path.stat().st_size
        h = hashlib.sha256()
        with open(path, "rb") as fh:                      # in blocks: a package can be 100 MB and more
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        digest = h.hexdigest()
        parts = urlsplit(cfg["url"])
        cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        offset, failures = 0, 0
        with open(path, "rb") as fh:
            while True:
                fh.seek(offset)
                piece = fh.read(SUPPORT_PIECE_BYTES)
                last = offset + len(piece) == total
                query = {"name": name, "offset": offset, "total": total}
                if last:
                    query.update(sha256=digest, choice=op["choice"], phone_log=int(bool(op.get("phone_log"))),
                                 places=int(bool(op.get("places"))))
                self.state = {"state": "uploading", "sent": offset, "total": total}
                conn = (self.opener or cls)(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                            timeout=SUPPORT_TIMEOUT_S)
                try:
                    conn.request("POST", "/api/support-package?" + urlencode(query), piece,
                                 {"Authorization": "Bearer " + cfg["token"], "Content-Type": "application/octet-stream"})
                    resp = conn.getresponse()
                    answer = json.loads(resp.read() or b"{}")
                    if resp.status != 200:
                        raise OSError(f"server answered {resp.status}: {answer.get('error', '')}")
                except (OSError, ValueError, http.client.HTTPException):
                    failures += 1
                    if failures > SUPPORT_RETRIES:
                        raise
                    time.sleep(min(30, 2 ** failures))
                    continue
                finally:
                    conn.close()
                failures = 0
                if answer.get("done"):
                    return
                offset = int(answer.get("size", 0))      # the server's own count: resumes after a lost piece


def run_support(op, pub, cfg):
    """A support package for the app: checked here as well as on the server, then made in the background."""
    job = getattr(pub, "support", None)
    if job is None or not job.available:
        pub.ack(op["id"], "failed", "Support packages need car computer software 1.5.0")
        return None
    if op.get("choice") not in SUPPORT_CHOICES or (op["choice"] != "quick" and not isinstance(op.get("session"), str)):
        pub.ack(op["id"], "failed", "Unknown package choice")
        return None
    return job.start(op, pub, cfg)


def run_bluetooth(op, pub):
    """Switch the Bluetooth link on or off, rename it, or give the app its key, as asked from the app."""
    config, link = bluetooth_module(), getattr(pub, "bluetooth", None)
    if config is None or link is None:
        pub.ack(op["id"], "failed", "Bluetooth is not available on this car computer")
        return
    try:
        if isinstance(op.get("on"), bool):
            config.save(on=op["on"])
            done = f"Bluetooth {'on' if op['on'] else 'off'}"
        elif isinstance(op.get("name"), str):
            config.save(name=op["name"])
            done = f"Bluetooth name: {op['name']}"
        elif op.get("pair") is True:
            settings = config.load()
            if not settings["on"]:
                pub.ack(op["id"], "failed", "Bluetooth is switched off on the car computer")
                return
            print("operator command: the Bluetooth key was sent to the app", flush=True)
            pub.ack(op["id"], "done", "key:" + settings["key"].hex())
            return
        else:
            pub.ack(op["id"], "failed", "Unknown Bluetooth request")
            return
    except ValueError as exc:
        pub.ack(op["id"], "failed", str(exc)[:160])
        return
    except OSError as exc:
        print(f"Bluetooth setting failed: {type(exc).__name__}: {exc}", flush=True)
        pub.ack(op["id"], "failed", f"Could not save the setting: {type(exc).__name__}")
        return
    link.reload()
    print(f"operator command: {done}", flush=True)
    pub.ack(op["id"], "done", done)


PUSH_WAIT_S = 15.0     # at most this long for a push in flight, before one that must go now (power off, stop)


class Pusher:
    """Sends the pushes on a thread of their own, one at a time.

    A server that does not answer can take a push several seconds: TIMEOUT_S for each network the Sender
    tries. Sent from the main loop, that time held up reading new matches, and so the Bluetooth link that
    needs them most when the internet is gone. The result comes back through `results`, and the
    publisher settles it on its own thread, so its state is only ever changed there.
    """

    def __init__(self, send):
        self.send = send
        self.inbox = queue.Queue(maxsize=1)
        self.results = deque()
        self.idle = threading.Event()
        self.idle.set()
        threading.Thread(target=self._work, daemon=True, name="push").start()

    @property
    def busy(self):
        return not self.idle.is_set()

    def offer(self, payload):
        """Hand over one push, or refuse while one is in flight. Returns True when taken."""
        if self.busy:
            return False
        self.idle.clear()
        self.inbox.put(payload)
        return True

    def wait(self, timeout=PUSH_WAIT_S):
        return self.idle.wait(timeout)

    def _work(self):
        while True:
            payload = self.inbox.get()
            try:
                self.send(payload)
                self.results.append((payload, None))
            except (OSError, http.client.HTTPException, ValueError) as exc:
                self.results.append((payload, exc))
            except Exception as exc:            # noqa: BLE001 - a broken push must never end the thread
                self.results.append((payload, exc))
            finally:
                self.idle.set()


class Publisher:
    def __init__(self, send, heartbeat=HEARTBEAT, phone=None, bluetooth=None):
        self.phone = phone
        self.bluetooth = bluetooth    # a BluetoothLink, or None
        self.pusher = None            # a Pusher in the running service; None sends in line (--once, tests)
        self.support = SupportJob()   # a support package asked for from the app
        self.drives, self.drives_at = None, -1e9
        self.owed = False             # a push was due while one was in flight
        self.quiet_alarm = False
        self.quiet_alerted_at = None
        self._warned_no_phone = False
        self.send = send
        self.heartbeat = Heartbeat(heartbeat)
        self.tail, self.channels = Tail(), Channels()
        self.session = None
        self.pending = deque(maxlen=PENDING_MATCHES)
        self.seq = 0
        self.last_push = -1e9
        self.last_hb_poll = -1e9
        self.failures = 0
        self.stopped_since = None
        self.last_state = None
        # Added to by the log upload thread as well. Never dropped while unsent: only the server's
        # limit on one push applies, and the rest go with the next one.
        self.acks = deque(maxlen=MAX_ACKS_WAITING)
        self.acks_lock = threading.Lock()
        self.log_upload_busy = threading.Lock()
        self.mark_ops = {}            # mark id -> app id, until the detector reports the IQ
        self.pi_health = {}
        self.last_health = -1e9
        self.updater = load_updater()
        self.doctor = Doctor()      # the setup check's verdicts for the apps
        self.update_status = None
        self.software = None
        self.last_update_status = -1e9
        self.next_update_check = None
        self.update_busy = threading.Lock()

    def ack(self, op_id, state, detail=""):
        with self.acks_lock:
            self.acks.append({"id": op_id, "state": state, "detail": str(detail)[:160]})

    def session_path(self):
        session = (self.heartbeat.hb or {}).get("session")
        return Path(session) if isinstance(session, str) and session else None

    def handle_mark(self, op, wall=None):
        """Never raises: a bad sighting must not take the display down."""
        session = self.session_path()
        try:
            if session is None:
                raise FileNotFoundError("no running session")
            created = write_mark(session, op, wall)
        except (OSError, ValueError) as exc:
            print(f"sighting not saved: {type(exc).__name__}: {exc}", flush=True)
            self.ack(op["id"], "failed", f"Not saved on the Pi: {exc}")
            return
        print(f"sighting saved: {op['label']} {op['id']}", flush=True)
        self.mark_ops[f"{op['id']}.request"] = op["id"]
        if not created:
            self.ack(op["id"], "done", "Saved on the Pi")

    def report_marks(self):
        """Tell the phone what the detector did, including whether IQ was captured."""
        results, self.tail.mark_results = self.tail.mark_results, []
        words = {"observation_only": "Saved; no recording (too old, or another was just taken)",
                 "stopped_before_postroll": "Saved; the detector stopped before the recording ended",
                 "overwritten": "Saved; the recording was overwritten", "error": "The detector could not read it"}
        words["queue_full"] = "Saved; no recording (the recorder was busy)"
        for mark_id, state in results:
            op_id = self.mark_ops.pop(mark_id, None) or self.mark_ops.pop(mark_id + ".request", None)
            if op_id:
                self.ack(op_id, "failed" if state == "error" else "done",
                         words.get(state, f"Saved with recording ({state})"))
        while len(self.mark_ops) > 256:
            self.mark_ops.pop(next(iter(self.mark_ops)))

    def finished(self, now_mono):
        """True once the detector has been gone for STOPPED_EXIT_S."""
        return self.stopped_since is not None and now_mono - self.stopped_since >= STOPPED_EXIT_S

    def _follow(self, hb, now_mono):
        session = Path(hb["session"]) if isinstance(hb.get("session"), str) and hb["session"] else None
        if session is None or session.name == self.session:
            return
        joining = self.session is None
        self.session = session.name
        self.channels, self.pending = Channels(), deque(maxlen=PENDING_MATCHES)
        # The input level belongs to the hardware, not the session, so the window
        # count carries over; only the notice re-arms, so a restart with the antenna
        # still unplugged warns again.
        self.quiet_alarm, self.quiet_alerted_at = False, None
        self._warned_no_phone = False
        # Joining a running session: start at the end, but show (never chime for)
        # matches still inside the hold window. A new session: read it from the start.
        self.tail.follow(session / "events.jsonl", from_end=joining)
        if joining:
            for event in self.tail.recent():
                self.channels.add(event, self.session, now_mono)

    def step(self, now_mono=None):
        """Check once; push if something is new or the keepalive is due. Returns pushed (or handed over)?"""
        now_mono = time.monotonic() if now_mono is None else now_mono
        self._settle_pushes(now_mono)
        if now_mono - self.last_hb_poll >= HEARTBEAT_EVERY_S:
            self.last_hb_poll = now_mono
            hb = self.heartbeat.poll(now_mono)
            if hb:
                self.stopped_since = None
                self._follow(hb, now_mono)
                self._antenna_notice(now_mono)
            elif self.stopped_since is None:
                self.stopped_since = now_mono
        new = [m for m in (self.channels.add(e, self.session, now_mono) for e in self.tail.read()) if m]
        self.pending.extend(new)
        state = self.heartbeat.state(now_mono)["state"]
        changed = state != self.last_state            # e.g. running -> stale/stopped: say so at once
        if self.bluetooth is not None:
            self._feed_bluetooth(now_mono, new, changed)
        # After a failed push, try again within half a second rather than at the next keepalive.
        wait = RETRY_S if 0 < self.failures <= FAST_RETRIES else KEEPALIVE_S
        if not new and not changed and not self.owed and now_mono - self.last_push < wait:
            return False
        return self.push(now_mono)

    def _feed_bluetooth(self, now_mono, new, changed):
        """New matches to the Bluetooth link at once; the detector and channels at the server's rhythm.

        A match carries the same fields as in the push to the server, so the phone decides the same
        way whichever route brings it first.
        """
        link = self.bluetooth
        link.poll(now_mono)
        if not link.running():
            return
        if new:
            link.feed({"matches": [{"id": m["id"], "ch": m["ch"], "strength_db": m["strength_db"],
                                    "age_s": round(max(0.0, now_mono - m["seen"]), 3), "rule": m.get("rule")}
                                   for m in new]})
        if new or changed or now_mono - link.last_feed >= KEEPALIVE_S:
            link.last_feed = now_mono
            link.feed({"status": self.heartbeat.state(now_mono), "channels": self.channels.snapshot(now_mono)})

    def _antenna_notice(self, now_mono):
        """Tell the phone once when the receiver stops hearing anything, and once when it recovers."""
        windows = self.heartbeat.quiet_windows
        hb = self.heartbeat.hb or {}
        # The detector says how many of its windows make the alarm and how long one is.
        limit = quiet_limit(hb) or QUIET_ALARM_WINDOWS
        window_s = float(hb["window_s"]) if finite(hb.get("window_s")) and hb["window_s"] > 0 else 3.0
        alarm = windows >= limit
        if alarm and (self.quiet_alerted_at is None or now_mono - self.quiet_alerted_at >= QUIET_REPEAT_S):
            self.quiet_alerted_at = now_mono
            peak = ((self.heartbeat.hb.get("rf_health") or {}).get("input") or {}).get("peak_counts")
            level = f", peak {peak:.0f} counts" if finite(peak) else ""
            self._notify(
                f"Receiver input has been very low for {windows * window_s / 60:.0f} min "
                f"({windows} windows{level}).\n"
                "Check the Airspy antenna and cable - a disconnected antenna looks like this. "
                "A genuinely quiet location can too.",
                "TETRA receiver hears nothing", 1)
        elif not alarm and self.quiet_alarm:
            self._notify("Receiver input is back to a normal level.", "TETRA receiver OK", 0)
            self.quiet_alerted_at = None
        self.quiet_alarm = alarm

    def _notify(self, message, title, priority):
        if self.phone is None:
            # Say so once. Silence here used to be indistinguishable from a
            # delivered message; it is not the same thing at all.
            if not self._warned_no_phone:
                self._warned_no_phone = True
                print("phone notices unavailable: Pushover is not configured for this "
                      "process, so display warnings will not reach the phone", flush=True)
            return
        try:
            return self.phone(message, title, priority)
        except Exception as exc:            # noqa: BLE001 - a failed notice must not stop the display
            print(f"phone notice failed: {type(exc).__name__}: {exc}", flush=True)

    def payload(self, now_mono):
        self.seq += 1
        fresh = [m for m in self.pending if now_mono - m["seen"] <= HOLD_S]
        if len(fresh) > MAX_MATCHES:
            fresh = sorted(fresh, key=lambda m: -(m["strength_db"] if finite(m["strength_db"]) else -1e9))[:MAX_MATCHES]
        matches = [{"id": m["id"], "ch": m["ch"], "strength_db": m["strength_db"],
                    "age_s": round(max(0.0, now_mono - m["seen"]), 2), "delay_s": m["delay_s"],
                    **({"rule": m["rule"]} if m.get("rule") else {})}
                   for m in fresh]
        detector = self.heartbeat.state(now_mono)
        self.last_state = detector["state"]
        if now_mono - self.last_health >= PI_HEALTH_EVERY_S:
            self.last_health = now_mono
            self.pi_health = read_pi_health(self.session_path())
            self.notify_state = notify_state()
            try:
                self.link = read_link(getattr(self, "link_stats", None), synced=getattr(self, "clock_synced", None))
            except Exception:               # noqa: BLE001 - a detail for the app; the push must go
                self.link = None
        hb = self.heartbeat.hb or {}
        pi = dict(self.pi_health)
        if finite(hb.get("dropped")):
            pi["dropped_samples"] = float(hb["dropped"])
        if finite(hb.get("windows")):
            window_s = float(hb["window_s"]) if finite(hb.get("window_s")) and hb["window_s"] > 0 else 3.0
            pi["session_s"] = float(hb["windows"]) * window_s     # never from a clock: see CONTRIBUTING.md
        self.report_marks()
        out = {"v": 3, "snapshot_mono": now_mono, "seq": self.seq, "session": self.session, "detector": detector,
               "channels": self.channels.snapshot(now_mono), "matches": matches,
               "features": FEATURES, "pi": pi, "acks": self._acks_to_send()}
        if getattr(self, "link", None):
            out["link"] = self.link
        if getattr(self, "notify_state", None):
            out["notify"] = self.notify_state
        if self.bluetooth is not None:
            bluetooth = self.bluetooth.state(now_mono)
            if bluetooth:
                out["bluetooth"] = bluetooth
        if now_mono - self.drives_at >= DRIVES_EVERY_S:
            self.drives_at, self.drives = now_mono, drives_state()
        if self.drives is not None:
            out["drives"] = self.drives
        if self.support is not None and self.support.state:
            out["support"] = dict(self.support.state)
        doctor = getattr(self, "doctor", None)
        if doctor is not None:
            if doctor.due(now_mono):
                doctor.start(now_mono)
            verdicts = doctor.for_push(now_mono)
            if verdicts:
                out["doctor"] = verdicts
        if self.updater is not None:
            if now_mono - self.last_update_status >= UPDATE_STATUS_EVERY_S:
                self.last_update_status = now_mono
                try:
                    self.update_status = self.updater.status()
                except Exception as exc:        # noqa: BLE001 - never worth losing a push for
                    self.update_status = {"state": "error", "commit": "", "seq": 0, "detail": type(exc).__name__}
            if self.update_status:
                out["update"] = self.update_status
        if self.software is None:
            self.software = software_version() or ""
        if self.software:
            out["software"] = self.software
        return out

    def check_for_update(self, now_mono):
        """Now and then, fetch a newer signed release in the background. It installs at the next start."""
        if self.updater is None:
            return None
        if self.next_update_check is None:
            self.next_update_check = now_mono + UPDATE_FIRST_CHECK_S
        if now_mono < self.next_update_check or not self.update_busy.acquire(blocking=False):
            return None
        self.next_update_check = now_mono + UPDATE_CHECK_EVERY_S

        def work():
            try:
                print("update check:", self.updater.fetch(), flush=True)
            except Exception as exc:            # noqa: BLE001
                print(f"update check failed: {type(exc).__name__}: {exc}"[:200], flush=True)
            finally:
                self.last_update_status = -1e9  # show the result with the next push
                self.update_busy.release()
        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        return thread

    def _acks_to_send(self):
        with self.acks_lock:
            return list(self.acks)[:MAX_ACKS]

    def push(self, now_mono):
        """Send the state: handed to the pusher, or in line without one. Returns True when sent or handed over."""
        if self.pusher is not None and self.pusher.busy:
            # One at a time. Owed, so the next step sends it the moment the one in flight is back, with
            # whatever arrived meanwhile, instead of at the next keepalive.
            self.owed = True
            return False
        return self._push(now_mono, in_line=self.pusher is None)

    def push_now(self, now_mono):
        """Send the state in line, after any push in flight: for what must reach the phone before this process
        ends (power off). Returns True when it was delivered."""
        if self.pusher is not None:
            self.pusher.wait()
            self._settle_pushes(now_mono)
        return self._push(now_mono, in_line=True)

    def _push(self, now_mono, in_line):
        self.owed = False
        self.last_push = now_mono              # also paces retries while offline
        payload = self.payload(now_mono)
        if not in_line:
            return self.pusher.offer(payload)
        try:
            self.send(payload)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            return self._settle(payload, exc, now_mono)
        return self._settle(payload, None, now_mono)

    def _settle_pushes(self, now_mono):
        while self.pusher is not None and self.pusher.results:
            payload, error = self.pusher.results.popleft()
            self._settle(payload, error, now_mono)

    def _settle(self, payload, error, now_mono):
        """After a push: count a failure, or drop what it delivered. Returns True when it was delivered."""
        if error is not None:
            self.failures += 1
            if self.failures in (1, 10) or self.failures % 300 == 0:
                print(f"push failed ({self.failures}x): {type(error).__name__}: {error}", flush=True)
            return False
        self.failures = 0
        sent = {m["id"] for m in payload["matches"]}     # delivered; the rest go with the next push
        self.pending = deque((m for m in self.pending if m["id"] not in sent and now_mono - m["seen"] <= HOLD_S),
                             maxlen=PENDING_MATCHES)
        with self.acks_lock:
            for ack in payload["acks"]:        # by item: the log upload thread may have added more
                if ack in self.acks:
                    self.acks.remove(ack)
        return True

    def stopped(self):
        if self.pusher is not None:
            self.pusher.wait()
        try:
            self.seq += 1
            self.send({"v": 3, "snapshot_mono": time.monotonic(), "seq": self.seq, "session": self.session,
                       "detector": {"state": "stopped"}, "channels": [], "matches": [], "acks": self._acks_to_send()})
        except (OSError, http.client.HTTPException, ValueError):
            pass


def publisher_lock(path):
    """Hold a process lock, including --once, so a second publisher cannot fence the service."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+b")
    try:
        if os.name == "nt":
            import msvcrt
            if path.stat().st_size == 0:
                fh.write(b"0")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        raise SystemExit("display publisher already running") from None
    return fh


def run(args):
    cfg = load_config(args.config)
    sender = Sender(cfg["url"], cfg["token"], take_commands=not args.once)
    pub = Publisher(sender, phone=phone_sender(), bluetooth=None if args.once else BluetoothLink())
    if not args.once:
        pub.pusher = Pusher(sender)            # --once sends in line: its first step is its answer
    return run_publisher(args, sender, pub, cfg)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="push the current state once and exit")
    ap.add_argument("--config", type=Path, default=CONFIG)
    args = ap.parse_args()
    with publisher_lock(Path.home() / ".cache/slot17-display.lock"):
        return run(args)


STOP_UNIT = "tetra-detector.service"
# Must match deploy/60-slot17-stop.sudoers word for word, or sudo -n refuses.
STOP_ARGV = ["sudo", "-n", "systemctl", "--no-block", "stop", STOP_UNIT]


def run_stop_command(command, pub, runner=None):
    """Carry out the operator's stop, and say so on the Pi's own journal.

    Stop is the only command that exists, and it is safe in the way start would
    not be: `systemctl stop` closes the Airspy once and leaves it closed, while
    a start button on a phone invites the repeated open/close cycling that wedges
    this particular board (rule 3 in CONTRIBUTING.md). Starting stays with boot autostart
    or SSH, where the operator can see what the receiver did.

    Returns True to keep publishing, False when the publisher should wind up.
    Failure is reported and never raised: a command that cannot run must not take
    the display down with it.
    """
    runner = runner or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=60))
    note = str(command.get("note") or "").strip()
    print(f"operator command: stop{' (' + note + ')' if note else ''}", flush=True)
    try:
        # --no-block: this process is PartOf= the detector, so a blocking stop
        # would wait on a job that SIGTERMs us, and read our own death as failure.
        done = runner(STOP_ARGV)
    except Exception as exc:                        # noqa: BLE001 - never kill the display
        print(f"stop command failed: {type(exc).__name__}: {exc}", flush=True)
        pub._notify(f"Could not stop the detector: {type(exc).__name__}. Stop it over SSH.",
                    "TETRA stop failed", 1)
        return True
    if done.returncode == 0:
        print("detector stop queued by operator command", flush=True)
        # Only now, and waited for: on 16 September "stopping" was sent before
        # the refusal, so the phone said both. The wait matters because systemd
        # is about to SIGTERM this process and the notice thread with it.
        sent = pub._notify("Stopping the detector, as asked from the display.",
                           "TETRA detector stopping", 0)
        if isinstance(sent, threading.Thread):
            sent.join(timeout=8)
        return False
    detail = (done.stderr or done.stdout or "").strip()[:160]
    print(f"stop command refused (rc={done.returncode}): {detail}", flush=True)
    pub._notify(f"Could not stop the detector (rc={done.returncode}). Stop it over SSH.",
                "TETRA stop failed", 1)
    return True


def run_poweroff(op, pub, runner=None):
    """Power the Pi off for the operator. Returns False when the publisher should wind up.

    Not a reboot: rule 3 in CONTRIBUTING.md forbids warm reboots with the Airspy attached, and
    this needs the ignition to come back. systemd stops the detector cleanly first. An old
    request is refused here as well as on the server, so it cannot fire on a later drive.
    """
    op_id = op["id"]
    age = op.get("age_s")
    if not finite(age) or age > POWEROFF_MAX_AGE_S:
        pub.ack(op_id, "failed", "Too old; not powering off")
        return True
    # Report first: once this works there is no publisher left to report anything.
    pub.ack(op_id, "done", "Powering off")
    pub.push_now(time.monotonic())
    runner = runner or (lambda cmd: subprocess.run(cmd, capture_output=True, text=True, timeout=30))
    print("operator command: power off", flush=True)
    try:
        done = runner(POWEROFF_ARGV)
    except Exception as exc:                # noqa: BLE001 - never kill the display
        print(f"power off failed: {type(exc).__name__}: {exc}", flush=True)
        pub.ack(op_id, "failed", f"Could not power off: {type(exc).__name__}")
        return True
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()[:100]
        print(f"power off refused (rc={done.returncode}): {detail}", flush=True)
        pub.ack(op_id, "failed", f"The Pi refused (rc={done.returncode}): {detail}")
        return True
    return False


def upload_pi_log(op, pub, cfg, opener=None, collect=collect_pi_log):
    """Collect and upload in a background thread, so pushes and beeps never wait for it."""
    if not pub.log_upload_busy.acquire(blocking=False):
        # One at a time, so a burst of requests cannot pile work onto a Pi that is capturing.
        pub.ack(op["id"], "failed", "A log is already being sent")
        return None

    def work():
        try:
            body = collect(pub.session_path(), pub.heartbeat.path)
            parts = urlsplit(cfg["url"])
            cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
            conn = (opener or cls)(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                                   timeout=LOG_UPLOAD_TIMEOUT_S)
            try:
                conn.request("POST", "/api/pi-log", body, {"Authorization": "Bearer " + cfg["token"],
                                                           "Content-Type": "text/plain; charset=utf-8"})
                resp = conn.getresponse()
                answer = resp.read()
                if resp.status != 200:
                    raise OSError(f"server answered {resp.status}")
                pub.ack(op["id"], "done", f"Saved as {json.loads(answer)['name']}")
            finally:
                conn.close()
            print(f"Pi log uploaded ({len(body)} bytes)", flush=True)
        except Exception as exc:            # noqa: BLE001 - report it, keep the display running
            print(f"Pi log upload failed: {type(exc).__name__}: {exc}", flush=True)
            pub.ack(op["id"], "failed", f"Upload failed: {type(exc).__name__}")
        finally:
            pub.log_upload_busy.release()
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread


DOCTOR_FIRST_S = 90.0       # after the publisher starts: the detector is up by then
DOCTOR_EVERY_S = 900.0
DOCTOR_LIMIT_S = 25.0


class Doctor:
    """`setup.sh doctor --json` in the background: its verdicts go to the apps' setup check.

    It only looks (it never opens the receiver or touches a service) and reads the heartbeat twice,
    two seconds apart, so it runs in its own thread and never holds up a push.
    """

    def __init__(self, root=None, run=None):
        self.root = Path(root) if root else Path(__file__).resolve().parents[1]
        self.run = run or self._run
        self.result = None          # {"items": [...], "at": monotonic}
        self.sent_at = None         # when the current result last went out with a push
        self.busy = False
        self.started = time.monotonic()
        self.last = None

    def _run(self):
        done = subprocess.run([sys.executable, "-B", str(self.root / "deploy/doctor.py"), "car", "--json"],
                              capture_output=True, text=True, timeout=DOCTOR_LIMIT_S)
        return json.loads(done.stdout)

    def due(self, now):
        if self.busy:
            return False
        if self.last is None:
            return now - self.started >= DOCTOR_FIRST_S
        return now - self.last >= DOCTOR_EVERY_S

    def start(self, now, then=None):
        """Run it in the background; `then(ok, detail)` afterwards, for an op's answer."""
        self.busy, self.last = True, now

        def work():
            try:
                found = self.run()
                items = [list(map(str, row))[:3] for row in found["items"]][:16]
                self.result, self.sent_at = {"items": items, "at": time.monotonic()}, None
                problems = sum(1 for row in items if row[1] == "PROBLEM")
                if then:
                    then(True, f"{problems} problem(s)" if problems else "no problems")
            except Exception as exc:        # noqa: BLE001 - a check that fails is a detail, never a crash
                if then:
                    then(False, f"doctor did not run: {type(exc).__name__}")
            finally:
                self.busy = False
        threading.Thread(target=work, daemon=True, name="doctor").start()

    def for_push(self, now):
        """The verdicts for this push: only when new, and again every 5 minutes, not with every push."""
        if self.result is None or (self.sent_at is not None and now - self.sent_at < 300):
            return None
        self.sent_at = now
        return {"items": self.result["items"], "age_s": round(max(0.0, now - self.result["at"]), 1)}


def handle_ops(sender, pub, cfg):
    """Carry out the app's operations. Returns False when the publisher should wind up."""
    keep_going = True
    while sender.pending_ops:
        op = sender.pending_ops.popleft()
        if not re.fullmatch(r"[0-9a-f]{32}", str(op.get("id"))):
            continue
        if op.get("kind") == "mark":
            pub.handle_mark(op)
        elif op.get("kind") == "logs":
            upload_pi_log(op, pub, cfg)
        elif op.get("kind") == "poweroff":
            keep_going = run_poweroff(op, pub) and keep_going
        elif op.get("kind") == "notify":
            run_notify(op, pub)
        elif op.get("kind") == "bluetooth":
            run_bluetooth(op, pub)
        elif op.get("kind") == "support":
            run_support(op, pub, cfg)
        elif op.get("kind") == "doctor":
            doctor = getattr(pub, "doctor", None)
            if doctor is None or doctor.busy:
                pub.ack(op["id"], "failed", "the check is already running" if doctor else "not available")
            else:
                doctor.start(time.monotonic(), then=lambda ok, detail, i=op["id"]: pub.ack(i, "done" if ok else "failed", detail))
    return keep_going


def clock_synced():
    """True/False from timedatectl, or None when it cannot say. The Pi has no clock of its own."""
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], capture_output=True,
                             text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return {"yes": True, "no": False}.get(out)


def report_link(stats, seconds, synced_before):
    """Print the minute's link line, and the clock's state when it changes. Returns the clock state."""
    line = stats.line(seconds)
    if line:
        print(line, flush=True)
    stats.reset()
    synced = clock_synced()
    if synced is not None and synced != synced_before:
        print("clock: synchronised with the network" if synced else "clock: not synchronised yet (no network time)",
              flush=True)
    return synced


def run_publisher(args, sender, pub, cfg=None):
    if args.once:
        # Current state only: joins at the end of any running session, like a normal
        # start. The first step always pushes, so its result is the answer.
        ok = pub.step()
        print("pushed" if ok else "push failed", flush=True)
        sender.close()
        return 0 if ok else 1
    running = True

    def stop(*_):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"publishing to {sender.host}:{sender.port}", flush=True)
    link_since = time.monotonic()
    synced = clock_synced()
    pub.clock_synced, pub.link_stats = synced, sender.stats
    while running:
        started = time.monotonic()
        pub.step()
        if started - link_since >= LINK_REPORT_S:
            synced = report_link(sender.stats, started - link_since, synced)
            pub.clock_synced = synced
            link_since = started
        command, sender.pending_command = sender.pending_command, None
        if command and not run_stop_command(command, pub):
            break
        if not handle_ops(sender, pub, cfg or {}):
            break
        pub.check_for_update(time.monotonic())
        if pub.finished(time.monotonic()):
            print(f"no detector heartbeat for {STOPPED_EXIT_S:.0f} s; exiting", flush=True)
            break
        time.sleep(max(0.02, LOOP_S - (time.monotonic() - started)))
    pub.stopped()
    if pub.bluetooth is not None:
        pub.bluetooth.stop()
    sender.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
