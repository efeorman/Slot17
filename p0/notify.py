#!/usr/bin/env python3
"""Pushover delivery with bounded queues, transactional expiry and live retries.

Only training-confirmed RF activity produces alerts. Credentials are injected at
send time. RF send attempts stop at a burst age of 120 seconds; the phone's
display lifetime is independently 900 seconds. All HTTP 4xx are permanent; quota exhaustion also
suspends sends until the advertised reset (or one hour if unavailable).

--selftest sends nothing. --test / --boot / --message send actual messages.
"""
import argparse
import json
import math
import os
import platform
import queue
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

API_URL = "https://api.pushover.net/1/messages.json"
HERE = os.path.dirname(os.path.abspath(__file__))
USER_CONFIG = os.path.expanduser("~/.config/tetra-pushover.json")
REPO_CONFIG = os.path.join(HERE, "pushover.json")
SPOOL_PATH = os.path.expanduser("~/.cache/tetra-notify-outbox.sqlite3")
# Whether Pushover messages go out at all, set from the app (operation `notify`, carried out by the
# display publisher) and kept beside the credentials, not in them. Missing or unreadable means on,
# as before 1.2.9. Off stops every Pushover message from this computer: faults, detections and the
# start-up message. The app can then show the faults itself while it runs.
SWITCH_PATH = os.path.expanduser("~/.config/slot17-notify.json")

TIMEOUT_S = 12.0
UNSAVED_LIMIT = 64
RETRY_MIN_S = 5.0            # Pushover: never retry a transient failure sooner
RETRY_MAX_S = 300.0
SPOOL_MAX = 200              # bound the spool; oldest dropped first

# Pushover limits, enforced locally so we never send a request that must 4xx.
MAX_MESSAGE = 1024
MAX_TITLE = 250

# Priority 1 (High) for a detection: bypasses quiet hours, always sounds. NOT 2
# (Emergency) - that repeats until acknowledged, which is the wrong thing to
# demand of someone who is driving.
PRIORITY_ALERT = 1
PRIORITY_INFO = 0

# Limit repeated phone alerts on the same channel. The first recognised activity
# still alerts immediately; detection and recording continue during the cooldown.
# A channel is not a vehicle identity: another radio on it can also be muted.
# Restore the pre-rc1 cadence after the operator reported intrusive drive alerts.
ALERT_COOLDOWN_S = 60.0

# Retain the operator's deployed escalation policy, now comparing matched-burst
# strength against the last alerted match. A stronger burst does not identify
# another radio or vehicle, or show that anything is approaching.
ALERT_ESCALATION_DB = 6.0
ALERT_ESCALATION_MIN_S = 20.0

# A degraded session is worth interrupting a drive for, but not repeatedly: a new
# kind of fault pushes at once, otherwise the driver hears about it every five
# minutes with the running totals. Health messages outlive RF ones on purpose -
# a detection three minutes stale is useless, a broken recorder is not.
HEALTH_REMINDER_S = 300.0
HEALTH_TTL_S = 600

# Operational status can wait longer for the hotspot than an RF detection.
STATUS_TTL_S = 3600

# Pushover display lifetime (counted from API receipt) and independent eligibility
# for every local send attempt, including retries/restarts. Never shorten the
# display lifetime just because an alert spent time waiting for a network.
ALERT_TTL_S = 900
ALERT_MAX_AGE_S = 120


def _mask(s):
    """Show enough of a credential to identify it, never enough to use it."""
    if not s:
        return "(unset)"
    return f"{s[:4]}...{s[-2:]} ({len(s)} chars)"


# --------------------------------------------------------------------------- #
# configuration

def load_config(path=None):
    """Credentials from env, then the user config, then the repo config."""
    cfg = {}
    for p in ([path] if path else [USER_CONFIG, REPO_CONFIG]):
        if p and os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if not isinstance(loaded, dict):
                    continue
                cfg = {k: v for k, v in loaded.items() if k not in ("token", "user", "device") or isinstance(v, str)}
                cfg["_source"] = p
                break
            except (OSError, ValueError):
                continue
    env = {"token": os.environ.get("PUSHOVER_TOKEN"),
           "user": os.environ.get("PUSHOVER_USER"),
           "device": os.environ.get("PUSHOVER_DEVICE")}
    for k, v in env.items():
        if v:
            cfg[k] = v
            cfg["_source"] = "environment"
    return cfg


def save_config(token, user, device=None, path=USER_CONFIG):
    """Write credentials 0600. They are secrets; treat them like the OAuth token."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {"token": token, "user": user}
    if device:
        data["device"] = device
    if os.path.islink(path):
        raise OSError(f"refusing to write credentials through a symlink: {path}")
    # Never truncated in place: a power cut leaves the old file or the new one, whole.
    partial = f"{path}.{os.getpid()}.partial"
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(partial, path)
    return path


def configured(cfg=None):
    cfg = load_config() if cfg is None else cfg
    return bool(cfg.get("token") and cfg.get("user"))


def pushover_enabled(path=None):
    """False only when the switch file says exactly {"pushover": false}; anything else means on."""
    try:
        with open(path or SWITCH_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return True
    return not (isinstance(data, dict) and data.get("pushover") is False)


def set_pushover(enabled, path=None):
    """Write the switch atomically (a power cut leaves the old file or the new one, whole)."""
    path = path or SWITCH_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    partial = f"{path}.{os.getpid()}.partial"
    with open(partial, "w", encoding="utf-8") as fh:
        json.dump({"pushover": bool(enabled)}, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(partial, path)
    return bool(enabled)


# --------------------------------------------------------------------------- #
# the transport

class Permanent(Exception):
    """The request is invalid. Never retry it - Pushover blocks repeat 4xx."""


class Quota(Permanent):
    def __init__(self, message, reset=None):
        super().__init__(message)
        try:
            self.wait = max(60, float(reset) - time.time())
        except (TypeError, ValueError):
            self.wait = 3600


class Transient(Exception):
    """Server-side or network problem. Retry, but not sooner than RETRY_MIN_S."""


def _post(fields, url=API_URL, timeout=TIMEOUT_S):
    """One POST. Returns the parsed JSON body, or raises Permanent/Transient."""
    body = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 - a fixed HTTPS address
        url, data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": "Slot17-TETRA-detector/2.0"})
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:  # noqa: S310 - a fixed HTTPS address
            raw = r.read().decode("utf-8", "replace")
        out = json.loads(raw)
        if out.get("status") == 1:
            return out
        raise Permanent(f"API rejected request: status={out.get('status')}")
    except urllib.error.HTTPError as e:
        if e.code == 429:                             # quota exhausted for the month
            raise Quota("HTTP 429 quota exhausted", e.headers.get("X-Limit-App-Reset")) from e
        if 400 <= e.code < 500:
            raise Permanent(f"HTTP {e.code}") from e
        raise Transient(f"HTTP {e.code}") from e
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as e:
        raise Transient(f"{type(e).__name__}: {e}") from e
    except ValueError as e:                           # unparseable body
        raise Transient(f"bad response: {e}") from e


def build(cfg, message, title=None, priority=PRIORITY_INFO, **extra):
    """A validated field dict. Truncates locally so we never earn a 4xx."""
    if not cfg.get("token") or not cfg.get("user"):
        raise Permanent("pushover token/user not configured")
    if int(priority) not in (-2, -1, 0, 1):
        raise Permanent("supported priority values are -2, -1, 0 and 1")
    f = {"token": cfg["token"], "user": cfg["user"],
         "message": (message or " ")[:MAX_MESSAGE], "priority": int(priority)}
    if title:
        f["title"] = title[:MAX_TITLE]
    if cfg.get("device"):
        f["device"] = cfg["device"]
    f["timestamp"] = int(extra.pop("timestamp", time.time()))
    for k in ("sound", "url", "url_title", "ttl", "html", "monospace"):
        if k in extra and extra[k] is not None:
            f[k] = extra[k]
    return f


def send_now(message, title=None, priority=PRIORITY_INFO, cfg=None, **extra):
    """Blocking single send. For the CLI and the boot message, never for watch.

    Returns None without sending when Pushover is switched off (`pushover_enabled`)."""
    if not pushover_enabled():
        return None
    cfg = load_config() if cfg is None else cfg
    return _post(build(cfg, message, title, priority, **extra))


# --------------------------------------------------------------------------- #
# the spool - offline is normal in a car

def spool_add(fields, path=SPOOL_PATH, cap=SPOOL_MAX):
    from outbox import Outbox
    try:
        box = Outbox(path)
        try:
            box.add(fields, cap)
        finally:
            box.close()
        return True
    except Exception:
        return False


def spool_count(path=SPOOL_PATH):
    from outbox import Outbox
    if not os.path.isfile(path):
        return 0
    box = Outbox(path)
    try:
        return box.count()
    finally:
        box.close()


class Notifier:
    """Bounded producer queue; one process owns delivery for each SQLite outbox.

    API success means Pushover accepted it, not that the phone displayed it.
    A crash between API acceptance and local commit can duplicate a message;
    the API supplies no idempotency key. Expiry limits that residual risk.
    """
    def __init__(self, cfg=None, log=None, spool=SPOOL_PATH, transport=_post):
        self.cfg = load_config() if cfg is None else cfg
        self.log = log or (lambda m: None)
        self.spool = spool
        self._transport = transport
        self.q = queue.Queue(maxsize=256)
        self.sent = self.failed = self.spooled = self.expired = self.storage_errors = 0
        self.rf_alerts_queued = self.rf_alerts_suppressed_cooldown = 0
        self.rf_alerts_escalated = 0
        self.rf_alerts_turned_off = 0
        self.health_alerts_queued = self.health_alerts_suppressed = 0
        self.switched_off = 0        # messages not queued because Pushover is switched off in the app
        self.pending = 0
        self.last_error = None
        self._last_alert = {}
        self._last_alert_snr = {}   # level at which each channel last alerted
        self._health_reasons = frozenset()
        self._health_totals = {}
        self._last_health = -1e30
        self._stop = threading.Event()
        self._thread = None
        self._owner = None

    def start(self):
        if not configured(self.cfg):
            return False
        from instance import acquire
        try:
            self._owner = acquire(self.spool + ".lock")
        except (OSError, RuntimeError):
            self.log("notify: outbox already owned or unavailable")
            return False
        self._thread = threading.Thread(target=self._run, name="notifications", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout=15):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        return not self._thread or not self._thread.is_alive()

    def snapshot(self):
        return {"sent": self.sent, "failed": self.failed, "spooled": self.spooled,
                "expired": self.expired, "pending": self.pending, "queued": self.q.qsize(),
                "unfinished": self.q.unfinished_tasks,
                "alert_cooldown_s": ALERT_COOLDOWN_S,
                "alert_display_ttl_s": ALERT_TTL_S,
                "alert_max_send_age_s": ALERT_MAX_AGE_S,
                "rf_alerts_queued": self.rf_alerts_queued,
                "rf_alerts_suppressed_cooldown": self.rf_alerts_suppressed_cooldown,
                "alert_escalation_db": ALERT_ESCALATION_DB,
                "rf_alerts_escalated": self.rf_alerts_escalated,
                "health_reminder_s": HEALTH_REMINDER_S,
                "health_alerts_queued": self.health_alerts_queued,
                "health_alerts_suppressed": self.health_alerts_suppressed,
                "pushover_enabled": pushover_enabled(), "switched_off": self.switched_off,
                "storage_errors": self.storage_errors, "last_error": self.last_error,
                "alive": bool(self._thread and self._thread.is_alive())}

    def problem(self):
        """Why notices cannot get through, in words, or None. Asked with every heartbeat."""
        if self._stop.is_set():
            return None
        if self._thread is not None and not self._thread.is_alive():
            return "notification worker stopped"
        if self.storage_errors:
            return "notification storage errors"
        return None

    def post(self, message, title=None, priority=PRIORITY_INFO, **extra):
        if self._stop.is_set() or (self._thread is not None and not self._thread.is_alive()):
            return False
        if not pushover_enabled():
            self.switched_off += 1
            return False
        try:
            freshness = extra.pop('_freshness', None)
            fields = build(self.cfg, message, title, priority, **extra)
            # Credentials exist only at the HTTPS boundary, never in queues/storage.
            for key in ("token", "user", "device"):
                fields.pop(key, None)
            fields.setdefault("ttl", STATUS_TTL_S)
            # This Pi boots on a stale clock. A deadline from the wall clock would delete
            # every queued notice the moment the clock corrects, so measure it monotonically.
            if freshness is None:
                from outbox import fresh_until
                freshness = fresh_until(time.monotonic() + fields["ttl"])
            fields['_freshness'] = freshness
            self.q.put_nowait(fields)
            return True
        except (Permanent, queue.Full, ValueError, TypeError):
            self.failed += 1
            return False

    def alert_hit(self, h, band="uplink"):
        if h.get("confirmed") is not True:
            return False
        # The operator hears detections from the app and dashboard, so a saved
        # "rf_alerts": false stops only these messages. Detection, recording and
        # fault messages carry on; anything but exactly false leaves alerts on.
        if self.cfg.get("rf_alerts") is False:
            self.rf_alerts_turned_off += 1
            return False
        ch = h.get("tetra_channel")
        now = time.monotonic()
        snr = float(h.get("peak_snr_db") or 0.0)
        strength_reliable = h.get('strength_reliable', True) and math.isfinite(snr)
        since = now - self._last_alert.get(ch, -1e30)
        alerted_at = self._last_alert_snr.get(ch)
        escalated = (strength_reliable and since < ALERT_COOLDOWN_S and alerted_at is not None and snr >= alerted_at + ALERT_ESCALATION_DB
                     and since >= ALERT_ESCALATION_MIN_S)
        if since < ALERT_COOLDOWN_S and not escalated:
            self.rf_alerts_suppressed_cooldown += 1
            return False
        # No trend word: it compares peak SNR between consecutive 3 s windows on a
        # 3 dB threshold, and the window-to-window spread is larger than that, so
        # "rising"/"falling" came out ~50/50 and read to a driver as approaching.
        # No proximity or metres either: the range model is uncalibrated.
        cover = h.get("confirmation_coverage") or {}
        tested, avail = cover.get("regions_tested"), cover.get("regions_available")
        checked = (f"{tested} of {avail} burst regions checked"
                   if tested is not None and avail else "coverage unrecorded")
        strength = (f"{snr:.1f} dB above this channel's noise (not a distance)."
                    if strength_reliable else 'Matched-burst strength unavailable.')
        clock_label = '' if h.get('matched_timing_refined', True) else 'Approximate time: '
        message = (f"{clock_label}{h.get('local_time', '')}  {h.get('frequency_hz', 0)/1e6:.4f} MHz\n"
                   f"{strength}\n"
                   f"TETRA training pattern recognised; {checked}.\n"
                   "Not vehicle type, identity or distance.")
        stamp = time.time()
        try:
            import datetime
            stamp = datetime.datetime.fromisoformat(h['utc']).timestamp()
        except (KeyError, ValueError, TypeError):
            pass
        # Age the detection on a clock that cannot be stepped. The Pi cold-boots
        # in the car with a stale time and NTP corrects it minutes later; judging
        # freshness on the wall clock made every match afterwards look minutes
        # old and silently dropped every RF alert for the rest of the session.
        burst_mono = h.get("burst_mono")
        age = (time.monotonic() - burst_mono) if burst_mono is not None else (time.time() - stamp)
        if not math.isfinite(age) or age < -1 or age >= ALERT_MAX_AGE_S:
            self.expired += 1
            return False
        from outbox import fresh_until
        queued = self.post(message, title=f"TETRA activity - ch {ch}",
                           priority=PRIORITY_ALERT, timestamp=int(stamp), ttl=ALERT_TTL_S,
                           _freshness=fresh_until(now + ALERT_MAX_AGE_S - max(0, age)))
        if queued:
            self._last_alert[ch] = now
            self._last_alert_snr[ch] = snr if strength_reliable else None
            self.rf_alerts_queued += 1
            if escalated:
                self.rf_alerts_escalated += 1
        return queued

    def alert_health(self, reasons):
        """Tell the driver the detector itself is in trouble, while it still matters.

        `reasons` are the heartbeat's `degraded_reasons`. A reason that was not
        in the last message pushes immediately, because a second kind of fault
        is news. Otherwise it waits for HEALTH_REMINDER_S *and* a total that has
        actually grown, so a fault still accumulating stays visible while a
        steady state says its piece once and then shuts up.

        There is deliberately no "recovered" message. Every counter behind these
        reasons is cumulative, so a session that has degraded once stays degraded
        and a recovery claim would be false comfort.
        """
        reasons = list(reasons or ())
        # Key on the fault, never on its running total: the total moves every
        # heartbeat, and treating that as a new fault would push once a second.
        current = frozenset(r["fault"] for r in reasons)
        # A fault that has ended is forgotten, so its return counts as a new episode.
        # Cumulative counters never end, so they are unaffected.
        self._health_reasons = self._health_reasons & current
        self._health_totals = {f: t for f, t in self._health_totals.items() if f in current}
        if not reasons:
            return False
        # A reason marked `steady` carries a total for the reader's benefit but
        # is one condition, not an accumulating fault: "the receiver hears
        # nothing" counts the windows it has lasted, and that number rises every
        # three seconds without anything changing. Repeating it on every reminder
        # is exactly the intrusiveness the 9 September alert review objected to,
        # and at home - where the antenna is deliberately off while testing - it
        # would buzz all evening. So a steady fault says its piece once.
        totals = {r["fault"]: (None if r.get("steady") else r.get("total")) for r in reasons}
        now = time.monotonic()
        grew = any(isinstance(totals.get(f), (int, float))
                   and isinstance(self._health_totals.get(f), (int, float))
                   and totals[f] > self._health_totals[f] for f in current)
        # A new kind of fault is news and pushes at once. Otherwise the reminder
        # window must have passed *and* a total must actually have grown since we
        # last spoke - growth permits the reminder, it does not bypass it.
        if not (current - self._health_reasons) and \
                not (grew and now - self._last_health >= HEALTH_REMINDER_S):
            self.health_alerts_suppressed += 1
            return False
        lines = [r["fault"] if r.get("total") is None else f"{r['fault']} {r['total']}"
                 for r in reasons]
        queued = self.post("Detection is still running, but the session is degraded:\n"
                           + "\n".join(lines)
                           + "\nCheck status.py when parked. Recorded evidence may be incomplete.",
                           title="TETRA detector degraded",
                           priority=PRIORITY_ALERT, ttl=HEALTH_TTL_S)
        if queued:
            self._health_reasons = current
            self._health_totals = totals
            self._last_health = now
            self.health_alerts_queued += 1
        return queued

    def _run(self):
        from outbox import Outbox
        box = None
        try:
            box = Outbox(self.spool)
            backoff, next_try = RETRY_MIN_S, 0.0
            unsaved = []                # accepted, but the store was busy: the only copy, so kept
            while not self._stop.is_set() or not self.q.empty():
                retrying, unsaved = unsaved, []
                for fields in retrying:
                    try:
                        box.add(fields)
                        self.spooled += 1
                    except Exception as exc:
                        self.last_error = type(exc).__name__
                        unsaved.append(fields)
                for _ in range(256):
                    try:
                        fields = self.q.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        box.add(fields)
                        self.spooled += 1
                        self.pending += 1
                    except Exception as exc:
                        self.storage_errors += 1
                        self.last_error = type(exc).__name__
                        if len(unsaved) < UNSAVED_LIMIT:
                            unsaved.append(fields)
                    finally:
                        self.q.task_done()
                self.expired += box.expire(time.time())
                self.pending = box.count()
                if self._stop.is_set():
                    continue  # queued messages are durable; do not send on shutdown
                row = box.next()
                if row and time.monotonic() >= next_try:
                    rowid, fields, expires = row
                    from outbox import eligible
                    if not eligible(fields, expires):
                        box.remove(rowid)
                        self.expired += 1
                        continue
                    fields.pop('_freshness', None)
                    fields.update(token=self.cfg['token'], user=self.cfg['user'])
                    if self.cfg.get('device'):
                        fields['device'] = self.cfg['device']
                    try:
                        self._transport(fields)
                        box.remove(rowid)
                        self.sent += 1
                        self.last_error = None
                        backoff, next_try = RETRY_MIN_S, 0
                    except Permanent as exc:
                        box.remove(rowid)
                        self.failed += 1
                        self.last_error = type(exc).__name__
                        if isinstance(exc, Quota):
                            next_try = time.monotonic() + exc.wait
                    except Exception as exc:
                        # Includes network errors. Do not log transport fields or
                        # server bodies, which can echo request credentials.
                        self.last_error = type(exc).__name__
                        next_try = time.monotonic() + backoff
                        backoff = min(RETRY_MAX_S, backoff * 2)
                self._stop.wait(.05)
        except Exception as exc:
            self.storage_errors += 1
            self.last_error = type(exc).__name__
            self.log("notify: delivery worker stopped; inspect notification health")
        finally:
            if box:
                self.pending = box.count()
                box.close()
            if self._owner:
                self._owner.close()


# --------------------------------------------------------------------------- #
# the boot message

def _run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:                                 # noqa: BLE001
        return ""


def network_info():
    """Hostname, addresses, Wi-Fi SSID and default route - the boot message body."""
    info = {"hostname": socket.gethostname()}
    addrs = []
    out = _run(["ip", "-4", "-o", "addr", "show", "scope", "global"])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            addrs.append(f"{parts[1]} {parts[3]}")
    if not addrs:                                     # non-Linux fallback
        try:
            addrs.append(socket.gethostbyname(socket.gethostname()))
        except OSError:
            pass
    info["addresses"] = addrs
    info["ssid"] = _run(["iwgetid", "-r"]) or None
    route = _run(["ip", "route", "show", "default"])
    info["gateway"] = route.split()[2] if len(route.split()) > 2 else None
    info["online"] = bool(addrs and info["gateway"])
    return info


def wait_for_network(max_wait_s=180.0, poll_s=3.0):
    """Block until there is a default route, or give up. Returns network_info()."""
    deadline = time.monotonic() + max_wait_s
    info = network_info()
    while not info["online"] and time.monotonic() < deadline:
        time.sleep(poll_s)
        info = network_info()
    return info


def airspy_present():
    try:
        import recover
        return recover.find_airspy() is not None
    except Exception:                                 # noqa: BLE001
        return None


def boot_message(info=None):
    import diagnostics
    info = network_info() if info is None else info
    host = diagnostics.host_health()
    a = airspy_present()
    lines = [f"host {info['hostname']}  ({platform.machine()})"]
    for ad in info["addresses"]:
        lines.append(f"ip   {ad}")
    if info.get("ssid"):
        lines.append(f"wifi {info['ssid']}")
    if info.get("gateway"):
        lines.append(f"gw   {info['gateway']}")
    if host.get("cpu_temp_c") is not None:
        lines.append(f"temp {host['cpu_temp_c']:.0f} C")
    p = host.get("power") or {}
    if p.get("raw"):
        lines.append(f"power {p['raw']}"
                     + ("  UNDERVOLTAGE" if p.get("undervoltage_now") else ""))
    lines.append("airspy " + ({True: "present", False: "NOT FOUND",
                               None: "unknown"}[a]))
    lines.append("detector state not checked")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #

def selftest():
    import unittest
    suite = unittest.defaultTestLoader.discover(os.path.join(os.path.dirname(HERE), "tests"), pattern="test_notifications.py")
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--test", action="store_true", help="send one real test message")
    ap.add_argument("--boot", action="store_true",
                    help="wait for the network, then send the start-up message")
    ap.add_argument("--message", help="send this text")
    ap.add_argument("--title")
    ap.add_argument("--priority", type=int, default=PRIORITY_INFO)
    ap.add_argument("--configure", action="store_true",
                    help="write credentials to ~/.config/tetra-pushover.json (0600)")
    ap.add_argument("--credentials-stdin", action="store_true",
                    help="--configure: read the token, then the user key, from standard input")
    ap.add_argument("--token")
    ap.add_argument("--user")
    ap.add_argument("--device")
    ap.add_argument("--wait", type=float, default=180.0,
                    help="--boot: seconds to wait for a network to appear")
    ap.add_argument("--deadline", type=float, default=900.0,
                    help="--boot: keep retrying this long before spooling")
    ap.add_argument("--status", action="store_true",
                    help="show configuration (credentials masked) and spool depth")
    args = ap.parse_args()

    if args.selftest:
        raise SystemExit(selftest())

    if args.configure:
        if args.credentials_stdin:              # not on the command line, where other accounts can read it
            lines = sys.stdin.read().splitlines()
            args.token, args.user = (lines + ["", ""])[:2]
        if not (args.token and args.user):
            ap.error("--configure needs credentials: --credentials-stdin, or --token and --user")
        p = save_config(args.token, args.user, args.device)
        print(f"written {p} (mode 0600)")
        return 0

    cfg = load_config()
    if args.status:
        print(f"source   {cfg.get('_source', 'none')}")
        print(f"token    {_mask(cfg.get('token'))}")
        print(f"user     {_mask(cfg.get('user'))}")
        print(f"device   {cfg.get('device') or '(all devices)'}")
        n = spool_count(SPOOL_PATH)
        print(f"spool    {n} message(s) pending" if n else "spool    empty")
        print("sending  " + ("on" if pushover_enabled() else f"switched off in the app ({SWITCH_PATH})"))
        return 0

    if not pushover_enabled():
        print("Pushover messages are switched off in the Slot17 app; nothing sent. "
              "Switch them on in the app's Settings.")
        return 0

    if not configured(cfg):
        print("No Pushover credentials. Run:\n"
              "  python3 notify.py --configure --token <T> --user <U> --device iphone",
              file=sys.stderr)
        return 2

    if args.boot:
        # Wait for a network, then keep trying until `--deadline`. In a car
        # the hotspot can take minutes to appear and may flap, so a single
        # attempt at network-online.target is not enough - a boot message
        # that never arrives is worse than one that arrives late. Backoff
        # respects Pushover's "no sooner than 5 s" rule, and a 4xx still
        # ends it immediately rather than hammering a rejected request.
        info = wait_for_network(args.wait)
        msg = boot_message(info)
        title = ("TETRA detector online" if info["online"]
                 else "TETRA detector up (no network yet)")
        deadline = time.monotonic() + args.deadline
        wait = RETRY_MIN_S
        while True:
            try:
                send_now(msg, title=title, priority=PRIORITY_INFO, cfg=cfg)
                print("boot message sent:" + chr(10) + msg)
                return 0
            except Permanent as e:
                print(f"rejected, not retrying: {e}", file=sys.stderr)
                return 1
            except Transient as e:
                if time.monotonic() + wait >= deadline:
                    from outbox import fresh_until
                    spool_add(dict(build(cfg, msg, title, PRIORITY_INFO, ttl=STATUS_TTL_S),
                                   _freshness=fresh_until(time.monotonic() + STATUS_TTL_S)))
                    print(f"still offline after {args.deadline:g}s - "
                          f"spooled for the next run ({e})")
                    return 0
                print(f"  offline ({e}); retrying in {wait:.0f}s")
                time.sleep(wait)
                wait = min(RETRY_MAX_S, wait * 2)
                info = network_info()      # refresh the body with the new IP
                msg = boot_message(info)
                if info["online"]:
                    title = "TETRA detector online"

    text = args.message
    title = args.title
    if args.test:
        text = text or (
            "Test message from the TETRA detector.\n"
            f"host {socket.gethostname()} ({platform.system()} {platform.machine()})\n"
            "If you can read this, alerts will reach you.")
        title = title or "TETRA detector - test"
    if not text:
        ap.print_help()
        return 0
    try:
        out = send_now(text, title=title, priority=args.priority, cfg=cfg)
        print(f"sent (request {out.get('request')})")
        return 0
    except (Permanent, Transient) as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
