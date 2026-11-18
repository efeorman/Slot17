#!/usr/bin/env python3
"""A report for each drive, made on the display server from what the phone and the car computer uploaded.

The phone sends its drive data (every match it saw, the sounds it played, the sightings and the route) when
a drive ends; the car computer sends its log every ten minutes. When drive data arrives, this joins it with
the car computer's logs from the same hours and saves a plain-text report next to them, which the app's
Logs screen lists as `report-...`. Standard library only; it never changes anything it reads.

    python3 drive_report.py LOG_FOLDER [DRIVE_DATA_FILE]     write a report (the newest drive data by default)

What the report says is what was measured. A match means a TETRA radio transmitted; nothing here says
which vehicle, or how far.
"""
import json
from pathlib import Path
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

LINK = re.compile(r"link: (\d+) pushes in (\d+) s, (\d+) failed(?:, median (\d+) ms, slowest (\d+) ms)?")
MODEM_UP = re.compile(r"modem: link up \(([^)]*)\); round trip to \S+ (\d+) ms \(worst (\d+) ms\)")
RSRP = re.compile(r"RSRP (-?\d+) dBm")
SUMMARY = re.compile(r"===== summary\.json =====\n(.*?)\n=====", re.S)
SIGHTING_WINDOW_S = 60.0


def parse_time(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def quantile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * (len(values) - 1)))] if values else None


def pi_logs_between(folder, start, end):
    """The car computer's logs saved during the drive, and the first one after it (it sends one every 10 min)."""
    out = []
    for path in sorted(Path(folder).glob("pi-*.log")):
        try:
            saved = datetime.strptime(path.name[3:19], "%Y%m%d-%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if start - timedelta(minutes=5) <= saved <= end + timedelta(minutes=12):
            out.append(path)
    return out


def overlap_s(summary, start, end):
    """Seconds a session's summary overlaps the drive: it was written at `utc` after `seconds` of running."""
    try:
        stop = float(summary["utc"])
        begin = stop - float(summary["seconds"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    return max(0.0, min(stop, end.timestamp()) - max(begin, start.timestamp()))


def car_figures(paths, start=None, end=None):
    """Link, 4G and detector figures from the car computer's logs. Lines repeat across logs; each counts once.

    Of the sessions those logs describe, the summary kept is the newest one of the session that overlaps the
    drive most: a car computer can run several sessions in one drive, and one at home right after it.
    """
    seen, links, trips, rsrp, stalls, restarts = set(), [], [], [], 0, 0
    summary, best = None, -1.0
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        found = SUMMARY.search(text)
        if found:
            try:
                candidate = json.loads(found.group(1))
                score = overlap_s(candidate, start, end) if start and end else 0.0
                if score >= best:
                    summary, best = candidate, score
            except (ValueError, TypeError):
                pass
        for line in text.splitlines():
            if line in seen:
                continue
            seen.add(line)
            if m := LINK.search(line):
                links.append(tuple(int(x) if x else None for x in m.groups()))
            if m := MODEM_UP.search(line):
                trips.append(int(m.group(2)))
                if r := RSRP.search(m.group(1)):
                    rsrp.append(int(r.group(1)))
            if "modem: no data session" in line:
                stalls += 1
            if "asked the stick to restart itself" in line:
                restarts += 1
    return {"links": links, "trips": trips, "rsrp": rsrp, "stalls": stalls, "restarts": restarts, "summary": summary}


def report(data, car, now=None):
    alerts = [a for a in data.get("alerts") or [] if isinstance(a, dict)]
    sightings = [s for s in data.get("sightings") or [] if isinstance(s, dict)]
    track = [t for t in data.get("track") or [] if isinstance(t, dict)]
    lines = [f"Slot17 drive report, made {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now or time.time()))}",
             f"Phone data from {data.get('from')} to {data.get('until')} (app {data.get('app', '?')})", ""]

    ages = [a["age"] for a in alerts if isinstance(a.get("age"), (int, float))]
    sounds = {}
    for a in alerts:
        if a.get("sound"):
            sounds[a["sound"]] = sounds.get(a["sound"], 0) + 1
    channels = {}
    for a in alerts:
        channels[a.get("channel")] = channels.get(a.get("channel"), 0) + 1
    strongest = max((a for a in alerts if isinstance(a.get("strength"), (int, float))),
                    key=lambda a: a["strength"], default=None)
    lines.append("RECOGNISED TETRA (a radio transmitted; not who, not how far)")
    lines.append(f"  {len(alerts)} matches on {len(channels)} channels" +
                 (f"; busiest {', '.join(f'L{c} ({n})' for c, n in sorted(channels.items(), key=lambda x: -x[1])[:4])}" if channels else ""))
    if strongest:
        lines.append(f"  strongest {strongest['strength']:.1f} dB on L{strongest.get('channel')} at {strongest.get('at')}")
    lines.append("  sounds: " + (", ".join(f"{n} {k}" for k, n in sorted(sounds.items())) if sounds else "none"))
    if ages:
        lines.append(f"  burst to phone: median {statistics.median(ages):.2f} s, 9 in 10 within {quantile(ages, 0.9):.2f} s, "
                     f"slowest {max(ages):.1f} s; {sum(a > 2 for a in ages)} over 2 s, {sum(a > 10 for a in ages)} over 10 s")
    lines.append("")

    lines.append("SIGHTINGS (marked by hand)")
    if not sightings:
        lines.append("  none")
    for s in sightings:
        at = parse_time(s.get("seenAt"))
        before = [a for a in alerts if a.get("sound") and at and (lambda t: t and 0 <= (at - t).total_seconds() <= SIGHTING_WINDOW_S)(parse_time(a.get("at")))]
        near = [a for a in alerts if at and (lambda t: t and abs((at - t).total_seconds()) <= SIGHTING_WINDOW_S)(parse_time(a.get("at")))]
        top = max((a.get("strength") or 0 for a in near), default=None)
        lines.append(f"  {s.get('seenAt')} {s.get('label', '?')}: {len(before)} sound(s) in the minute before; "
                     f"{len(near)} match(es) within a minute" + (f", strongest {top:.1f} dB" if near else "") +
                     (f'; note "{str(s.get("note"))[:80]}"' if s.get("note") else ""))
    lines.append("")

    speeds = [t["speedKmh"] for t in track if isinstance(t.get("speedKmh"), (int, float)) and t["speedKmh"] >= 0]
    if speeds:
        lines.append("ROUTE")
        lines.append(f"  {len(track)} places; moving (over 5 km/h) in {sum(v > 5 for v in speeds) * 100 // len(speeds)} % of them, "
                     f"top speed {max(speeds):.0f} km/h")
        lines.append("")

    lines.append("CAR COMPUTER")
    s = car.get("summary") or {}
    if s:
        windows = s.get("windows")
        window_s = s.get("window_s") or (s.get("rf_health") or {}).get("window_duration_s")
        lines.append(f"  last session: {windows} windows" + (f" ({windows * window_s / 60:.0f} min)" if windows and window_s else "") +
                     f", {s.get('confirmed_hits')} matches ({s.get('weak_matches', 0)} by a weak rule, {s.get('call_matches', 0)} in a call pattern), "
                     f"{s.get('dropped_samples')} dropped samples"
                     + (f" ({s['startup_dropped_samples']} of them in the first minute, while starting)"
                        if s.get("startup_dropped_samples") else "")
                     + f", {s.get('unchecked_hits')} candidates unchecked, "
                     f"{s.get('whole_band_windows')} whole-band windows")
        rf = s.get("rf_health") or {}
        if isinstance(rf.get("median_floor_db"), (int, float)):
            lines.append(f"  noise floor {rf['median_floor_db']:.1f} dB (detector units) at the last health record")
        if s.get("degraded_reasons"):
            lines.append("  faults: " + ", ".join(str(r.get("fault")) for r in s["degraded_reasons"] if isinstance(r, dict)))
    else:
        lines.append("  no log from the car computer in these hours")
    links = car.get("links") or []
    if links:
        pushes = sum(x[0] for x in links)
        failed = sum(x[2] for x in links)
        medians = [x[3] for x in links if x[3] is not None]
        slowest = max((x[4] for x in links if x[4] is not None), default=None)
        lines.append(f"  link to the server: {pushes} pushes, {failed} failed ({failed * 100 / max(pushes + failed, 1):.1f} %)" +
                     (f", minute medians {min(medians)} to {max(medians)} ms, slowest {slowest} ms" if medians else ""))
    trips, rsrp = car.get("trips") or [], car.get("rsrp") or []
    if trips or car.get("stalls") or car.get("restarts"):
        lines.append(f"  4G stick: round trip median {statistics.median(trips) if trips else '?'} ms"
                     + (f", signal RSRP {min(rsrp)} to {max(rsrp)} dBm" if rsrp else "")
                     + f"; {car.get('stalls', 0)} checks without a data session, {car.get('restarts', 0)} restarts of the stick")
    lines.append("")
    lines.append("Figures come from the uploads only; a stretch with no upload is not in them.")
    return "\n".join(lines) + "\n"


def write_report(folder, drive_file=None, save=None):
    """Make the report for one drive-data upload and save it. Returns the saved name, or None."""
    folder = Path(folder)
    if drive_file is None:
        candidates = sorted(folder.glob("app-*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    else:
        candidates = [folder / Path(drive_file).name]
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("kind") != "slot17-drive-data":
            continue
        start, end = parse_time(data.get("from")), parse_time(data.get("until"))
        if not start or not end:
            return None
        text = report(data, car_figures(pi_logs_between(folder, start, end), start, end))
        if save is None:
            return text
        return save(folder, "report", text.encode("utf-8"))
    return None


if __name__ == "__main__":
    print(write_report(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
