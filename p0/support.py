#!/usr/bin/env python3
"""A support package: one zip with what someone needs to see why this car computer misbehaves.

    python3 support.py --list                        the drives on this car computer, as JSON
    python3 support.py                               the quick check: report, doctor, settings, recent logs
    python3 support.py --drive SESSION [--window 30-50] [--everything] [--recordings]
    python3 support.py ... --out FILE                where to write (default ~/slot17-support-<time>.zip)
    bash setup.sh logs                               asks, then makes one

REPORT.txt comes first in the zip: what happened per drive, in plain words, so a reviewer starts there. A
drive is a session folder; `--window 30-50` keeps its events from minute 30 to 50 after the start only.
`--everything` keeps the per-candidate lines and every channel table; `--recordings` adds the recordings
of that drive (in the window). docs/operations/SUPPORT.md says what each part is for.

It only reads, at the lowest priority and in small steps, because the detector is nearly always capturing
(rule 5); the manifest records the drop counter before and after. Never in it: the display key, Pushover's
tokens, the Bluetooth key, the release key, Wi-Fi passwords, SSH keys, or any line that looks like a password,
token or key. The car computer records no places; the server adds the phone's drive data only when asked.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / "captures"
HOME = Path.home()
QUICK_DRIVES = 5
QUICK_JOURNAL_LINES = 3000
CHANNEL_TABLE_EVERY = 120            # rf_health windows: the 200-channel table every 30 s instead of every 0.25 s
PACE_BYTES = 256 << 10               # pause briefly after this much, so the detector keeps its share
PACE_S = 0.01
# The same rule as display_publisher.SECRET_TEXT: over-redacting a log costs little.
SECRET_TEXT = re.compile(r"(?i)((?:[a-z0-9_-]*(?:token|password|passwd|secret|api_?key|user_?key|viewer_?key|status_?key|psk)|"
                         r"authorization|cookie|pushover_user|(?:bluetooth|pairing)[_ -]?code|[\"']user[\"']|\bkey)[\"']?)(\s*[:=]\s*[\"']?|\s+)(bearer\s+)?[^\s\"',;&}]+")
SERVICES = ("tetra-detector.service", "tetra-display-publisher.service", "slot17-modem.service", "bluetooth.service")


CODE_TEXT = re.compile(r"\b[0-9A-HJKMNP-TV-Z]{4}(?:-[0-9A-HJKMNP-TV-Z]{4}){5}-[0-9A-HJKMNP-TV-Z]{3}\b")      # a Bluetooth pairing code (1.8.0)


def redact(text: str) -> str:
    return CODE_TEXT.sub("[removed]", SECRET_TEXT.sub(lambda m: m.group(1) + m.group(2) + "[removed]", text))


def _json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _run(cmd, timeout=20):
    """A command's output at the lowest priority, or what went wrong. Never raises."""
    try:
        done = subprocess.run(["nice", "-n", "19", *cmd], capture_output=True, text=True, timeout=timeout)
        return (done.stdout or "") + (done.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return f"({' '.join(cmd)} did not run: {type(exc).__name__})\n"


# --- the drives -------------------------------------------------------------------------------------------

def drives(captures=None):
    """Every session folder, newest first, with its corrected start (never the folder name, which can be wrong)."""
    captures = captures or CAPTURES
    out = []
    for d in sorted(captures.glob("*_watch_*")) if captures.is_dir() else []:
        if not d.is_dir() or d.is_symlink():
            continue                                  # the corrected-name links point at the same folders
        t, s = _json(d / "session-time.json") or {}, _json(d / "summary.json") or {}
        start = t.get("corrected_start_utc")
        seconds = s.get("seconds") or (s.get("windows", 0) * s.get("window_s", 0))
        events = d / "events.jsonl"
        out.append({
            "session": d.name, "start_utc": start if isinstance(start, (int, float)) else None,
            "start": dt.datetime.fromtimestamp(start, dt.timezone.utc).isoformat(timespec="seconds")
                     if isinstance(start, (int, float)) else None,
            "minutes": round(seconds / 60, 1) if isinstance(seconds, (int, float)) else None,
            "matches": s.get("confirmed_hits"), "faults": [str(r.get("fault", r))[:80] for r in s.get("degraded_reasons") or []
                                                           if isinstance(r, (dict, str))][:8],
            "failure": s.get("failure_reason"), "dropped_after_start": _after_start(s),
            "whole_band_windows": s.get("whole_band_windows"),
            "events_mb": round(events.stat().st_size / 1e6, 1) if events.exists() else 0.0,
            "recordings": len(list(d.glob("w*.iq"))),
        })
    return sorted(out, key=lambda r: r["start_utc"] or 0, reverse=True)


def _after_start(summary):
    dropped, startup = summary.get("dropped_samples"), summary.get("startup_dropped_samples") or 0
    return dropped - startup if isinstance(dropped, int) and isinstance(startup, int) else None


def report(rows, doctor):
    """REPORT.txt: the doctor's verdicts, then per drive what a reviewer looks at first."""
    lines = ["Slot17 support package", f"made {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC (the car computer's clock)", ""]
    lines.append("Setup check (bash setup.sh doctor):")
    for item in (doctor or {}).get("items", []):
        lines.append(f"  {item[1]:<8} {item[0]}: {item[2]}")
    if not doctor:
        lines.append("  (the doctor did not answer)")
    lines += ["", "Drives, newest first (times in UTC, corrected; folder names can be wrong):"]
    for r in rows:
        lines.append(f"  {r['start'] or 'start unknown'}  {r['minutes'] or '?'} min  {r['matches'] or 0} matches  "
                     f"{r['session']}")
        if r["faults"]:
            lines.append(f"      faults: {'; '.join(r['faults'])}")
        if r["failure"]:
            lines.append(f"      failed: {r['failure']}")
        if r["dropped_after_start"]:
            lines.append(f"      samples dropped after the start: {r['dropped_after_start']:,}")
        if r["whole_band_windows"]:
            lines.append(f"      whole-band interference windows: {r['whole_band_windows']:,}")
    if not rows:
        lines.append("  (no session folders)")
    return "\n".join(lines) + "\n"


# --- the events of a drive ----------------------------------------------------------------------------------

def _event_time(event):
    for key in ("utc", "utc_start", "measured_utc"):
        value = event.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
    return None


def _clock_steps(path):
    """(line number, seconds) of each clock step: events written before a step get its delta added."""
    steps = []
    with open(path, "rb") as fh:
        for n, raw in enumerate(fh):
            if b'"clock_step"' in raw:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(event.get("delta_s"), (int, float)):
                    steps.append((n, float(event["delta_s"])))
    return steps


def events(path, start_utc=None, window=None, everything=False, pace=None, recorded=None):
    """The lines of a drive's events.jsonl worth reading, as text.

    Without `everything`: no per-candidate lines (confirmed matches stay), and the 200-channel table only every
    CHANNEL_TABLE_EVERY windows. `window` = (first, last) minute after the drive's start; it needs `start_utc`.
    `recorded`, a set: the names of the recordings made in the window (all of them without one) are added to it.
    A recording's event has no time of its own, so it counts at the last time before it (found in review,
    24 November 2026: until then a window left the recordings out of account, and every one went in).
    """
    steps = _clock_steps(path) if window else []
    later = sum(d for _, d in steps)                  # what lines before the first step still need added
    step_at = dict(steps)
    health = 0
    at = None                                         # minutes since the start, of the last line with a time
    inside = (lambda m: m is not None and window[0] <= m <= window[1]) if window and start_utc is not None else None
    with open(path, "rb") as fh:
        for n, raw in enumerate(fh):
            if pace:
                pace(len(raw))
            if n in step_at:
                later -= step_at[n]
            try:
                event = json.loads(raw)
            except ValueError:
                continue                              # a line cut short by a power cut
            if not isinstance(event, dict):
                continue
            kind = event.get("event_type")
            if inside is not None:
                t = _event_time(event)
                if t is not None:
                    at = (t + later - start_utc) / 60
            if (recorded is not None and kind in ("window_recording", "mark_recording")
                    and isinstance(event.get("name"), str) and (inside is None or inside(at))):
                recorded.add(event["name"])
            if not everything and kind == "candidate" and event.get("classification") != "tetra_training_match":
                continue
            if inside is not None and t is not None and not inside(at):
                continue
            if kind == "rf_health" and not everything:
                health += 1
                if health % CHANNEL_TABLE_EVERY:
                    event.pop("channels", None)
            yield redact(json.dumps(event, separators=(",", ":"))) + "\n"


# --- the package ---------------------------------------------------------------------------------------------

class Pacer:
    """A short pause after every PACE_BYTES, and the lowest priority, so a capture keeps its share."""

    def __init__(self):
        self.count = 0
        try:
            os.nice(19 - os.nice(0))
        except (AttributeError, OSError):
            pass

    def __call__(self, n):
        self.count += n
        if self.count >= PACE_BYTES:
            self.count = 0
            time.sleep(PACE_S)


def store(z, path, name, pace):
    """A recording into the package uncompressed, in paced pieces like everything else (until 24 November 2026
    it was copied in one go, which a capture running beside it felt; found in review)."""
    info = zipfile.ZipInfo.from_file(path, name)
    info.compress_type = zipfile.ZIP_STORED
    with open(path, "rb") as src, z.open(info, "w", force_zip64=True) as dst:
        for block in iter(lambda: src.read(PACE_BYTES), b""):
            dst.write(block)
            pace(len(block))


def dropped_now():
    """The running session's dropped samples, from its summary: the cost of making the package, measured."""
    rows = drives()
    if not rows:
        return None
    s = _json(CAPTURES / rows[0]["session"] / "summary.json") or {}
    return s.get("dropped_samples")


def system_text(run=_run):
    parts = []
    for title, cmd in (("software", ["cat", str(ROOT / "VERSION")]), ("system", ["uname", "-a"]),
                       ("computer", ["cat", "/proc/device-tree/model"]), ("uptime", ["uptime"]),
                       ("power", ["vcgencmd", "get_throttled"]), ("supply", ["vcgencmd", "pmic_read_adc", "EXT5V_V"]),
                       ("temperature", ["vcgencmd", "measure_temp"]), ("usb", ["lsusb"]), ("usb tree", ["lsusb", "-t"]),
                       ("network", ["ip", "-br", "addr"]), ("routes", ["ip", "route"]), ("disk", ["df", "-h", str(ROOT)]),
                       ("memory", ["free", "-m"]), ("clock", ["timedatectl"]), ("boots", ["journalctl", "--list-boots", "--no-pager"]),
                       ("services", ["systemctl", "is-active", *SERVICES]), ("bluetooth", ["bluetoothctl", "show"])):
        parts.append(f"== {title}: {' '.join(cmd)}\n{run(cmd)}\n")
    return redact("".join(parts))


def settings():
    """What may be shown of the settings: never a key, token or password."""
    out = {}
    for name in ("calibration.local.json", "downlink_channels.local.json"):
        data = _json(ROOT / "p0" / name)
        if data:
            out[name] = data
    sys.path.insert(0, str(ROOT / "p0"))
    try:
        import bluetooth_config
        out["bluetooth"] = bluetooth_config.public(bluetooth_config.load())
    except Exception as exc:                          # noqa: BLE001 - an older p0: say so, carry on
        out["bluetooth"] = f"unreadable: {type(exc).__name__}"
    out["pushover_switch"] = _json(HOME / ".config/slot17-notify.json")
    out["display_server"] = "configured" if (HOME / ".config/slot17-display.json").exists() else "not configured"
    out["signed_updates"] = "on" if (HOME / ".config/slot17-release-key.pub").exists() else "off"
    for unit in ("tetra-detector.service", "tetra-display-publisher.service"):
        try:
            text = Path("/etc/systemd/system", unit).read_text(encoding="utf-8")
            out[unit] = [line for line in text.splitlines() if line.split("=")[0] in ("ExecStart", "ExecStartPre", "Nice", "User")]
        except OSError:
            out[unit] = "not installed"
    return out


def journal(boot, lines=None, run=_run):
    """One boot's journal without the detector's per-candidate lines, and how many there were."""
    cmd = ["journalctl", "-b", str(boot), "-o", "short-monotonic", "--no-pager"] + (["-n", str(lines)] if lines else [])
    text = run(cmd, timeout=120)
    kept, dropped = [], 0
    for line in text.splitlines(True):
        if "candidate ch" in line:
            dropped += 1
        else:
            kept.append(line)
    return redact("".join(kept)) + (f"\n({dropped} candidate lines left out)\n" if dropped else ""), dropped


def boots(run=_run):
    return [line.split()[0] for line in run(["journalctl", "--list-boots", "--no-pager"]).splitlines()
            if line.split() and re.fullmatch(r"-?\d+", line.split()[0])]


def make(out, session=None, window=None, everything=False, recordings=False, run=_run, doctor=None, progress=None):
    """Write the package to `out`. Returns its manifest."""
    pace = Pacer()
    started, before = time.monotonic(), dropped_now()
    rows = drives()
    if doctor is None:
        try:
            doctor = json.loads(run([sys.executable, "-B", str(ROOT / "p0/doctor.py"), "car", "--json"], timeout=40))
        except ValueError:
            doctor = None
    chosen = [r for r in rows if r["session"] == session] if session else []
    if session and not chosen:
        raise ValueError(f"no drive {session}")
    manifest = {"made_utc": time.time(), "software": (ROOT / "VERSION").read_text().strip() if (ROOT / "VERSION").exists() else None,
                "choice": "everything" if everything else "drive" if session else "quick", "session": session,
                "window_minutes": list(window) if window else None, "recordings": recordings, "files": {},
                "left_out": ["display key", "Pushover tokens", "Bluetooth key", "release key", "Wi-Fi passwords", "SSH keys",
                             "lines that look like a password, token or key"]}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        def add(name, text):
            z.writestr(name, text)
            manifest["files"][name] = len(text.encode("utf-8")) if isinstance(text, str) else len(text)
        add("REPORT.txt", report(rows, doctor))
        add("doctor.json", json.dumps(doctor, indent=1))
        add("system.txt", system_text(run))
        add("settings.json", json.dumps(settings(), indent=1, default=str))
        for i, boot in enumerate(boots(run)[-6:][::-1]):
            if progress:
                progress(f"journal of boot {boot}")
            if not session and i >= 2:
                break
            text, dropped = journal(boot, None if session else QUICK_JOURNAL_LINES, run)
            add(f"journal/boot{boot}.txt", text)
        for r in (chosen or rows[:QUICK_DRIVES]):
            folder = CAPTURES / r["session"]
            for name in ("summary.json", "session-time.json", "session.json"):
                if (folder / name).exists():
                    add(f"drives/{r['session']}/{name}", redact((folder / name).read_text(encoding="utf-8", errors="replace")))
            if not chosen:
                continue
            if progress:
                progress("the drive's events")
            recorded = set()
            if (folder / "events.jsonl").exists():
                with z.open(f"drives/{r['session']}/events.jsonl", "w", force_zip64=True) as fh:
                    size = 0
                    for line in events(folder / "events.jsonl", r["start_utc"], window, everything, pace, recorded):
                        data = line.encode("utf-8")
                        fh.write(data)
                        size += len(data)
                manifest["files"][f"drives/{r['session']}/events.jsonl"] = size
            for mark in sorted((folder / "marks").glob("*")) if (folder / "marks").is_dir() else []:
                if mark.is_file() and mark.stat().st_size < 1 << 20:
                    add(f"drives/{r['session']}/marks/{mark.name}", redact(mark.read_text(encoding="utf-8", errors="replace")))
            if recordings and window and r["start_utc"] is None:
                # Without the drive's corrected start the minutes can't be found, so no recording is known to
                # be in them: none goes in, and the manifest says why (fourth review round, 24 November 2026).
                manifest.setdefault("left_out_recordings", []).append(
                    {"drive": r["session"], "why": "the drive has no corrected start time, so the window can't be applied"})
            elif recordings:
                for rec in sorted(folder.glob("w*.iq")):
                    if window and rec.name not in recorded:
                        continue                      # made outside the chosen minutes
                    if progress:
                        progress(f"recording {rec.name}")
                    store(z, rec, f"drives/{r['session']}/{rec.name}", pace)
                    manifest["files"][f"drives/{r['session']}/{rec.name}"] = rec.stat().st_size
        manifest.update(seconds=round(time.monotonic() - started, 1), dropped_samples_before=before,
                        dropped_samples_after=dropped_now())
        z.writestr("manifest.json", json.dumps(manifest, indent=1))
    return manifest


def default_out():
    return HOME / f"slot17-support-{dt.datetime.now(dt.timezone.utc):%Y%m%d-%H%M%S}Z.zip"


def ask(args):
    """`bash setup.sh logs` with no options: which package, and for which drive."""
    print("What should the package hold?")
    print("  1  Quick check: the report, the setup check, settings and recent logs (under 1 MB, recommended)")
    print("  2  One drive in full, without the per-candidate lines (1 to 15 MB)")
    print("  3  Everything from one drive, for deep analysis (100 MB or more)")
    choice = input("  Choose 1, 2 or 3 [1]: ").strip() or "1"
    if choice in ("2", "3"):
        rows = drives()[:12]
        for i, r in enumerate(rows, 1):
            print(f"  {i:2}  {r['start'] or 'start unknown'}  {r['minutes'] or '?'} min  {r['matches'] or 0} matches"
                  + (f"  faults: {'; '.join(r['faults'])}" if r["faults"] else ""))
        pick = input("  Which drive [1]: ").strip() or "1"
        if not pick.isdigit() or not 1 <= int(pick) <= len(rows):
            raise SystemExit("no such drive")
        args.drive, args.everything = rows[int(pick) - 1]["session"], choice == "3"
        window = input("  Only part of it? Minutes after the start, for example 30-50 [all]: ").strip()
        args.window = window or None
        args.recordings = choice == "3" and input("  With its recordings (7.5 MB each)? [y/N]: ").strip().lower().startswith("y")
    return args


def main(argv=None):
    ap = argparse.ArgumentParser(description="One zip with what someone needs to review this car computer.")
    ap.add_argument("--list", action="store_true", help="the drives on this car computer, as JSON")
    ap.add_argument("--drive", metavar="SESSION", help="one drive in full (a session folder's name, see --list)")
    ap.add_argument("--window", metavar="A-B", help="only minutes A to B after the drive's start, for example 30-50")
    ap.add_argument("--everything", action="store_true", help="with --drive: also every candidate line and channel table")
    ap.add_argument("--recordings", action="store_true", help="with --drive: also its recordings (in the window)")
    ap.add_argument("--out", type=Path, help="the zip to write (default ~/slot17-support-<time>.zip)")
    ap.add_argument("--ask", action="store_true", help=argparse.SUPPRESS)      # bash setup.sh logs, with no options
    args = ap.parse_args(argv)
    if args.ask and sys.stdin.isatty():
        args = ask(args)
    if args.list:
        print(json.dumps(drives(), indent=1))
        return 0
    if (args.window or args.everything or args.recordings) and not args.drive:
        ap.error("--window, --everything and --recordings need --drive")
    window = None
    if args.window:
        m = re.fullmatch(r"(\d+)-(\d+)", args.window)
        if not m or int(m[1]) >= int(m[2]):
            ap.error("--window wants two minutes after the start, the first smaller, for example 30-50")
        window = (int(m[1]), int(m[2]))
    out = args.out or default_out()
    try:
        manifest = make(out, args.drive, window, args.everything, args.recordings,
                        progress=lambda what: print(f"  {what}…", file=sys.stderr, flush=True))
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(json.dumps({"file": str(out), "bytes": out.stat().st_size, "seconds": manifest["seconds"],
                      "dropped_samples_before": manifest["dropped_samples_before"],
                      "dropped_samples_after": manifest["dropped_samples_after"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
