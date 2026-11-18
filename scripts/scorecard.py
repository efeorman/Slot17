#!/usr/bin/env python3
"""Encounter scorecard: did a beep come before the emergency vehicles that were seen?

Reads the drive data the iPhone app uploads to the display server (files whose content starts with
{"kind":"slot17-drive-data"...}) and prints a Markdown report.

    python scripts/scorecard.py path/to/logs [more paths] [--lead 60] [--out SCORECARD.md]

What it can and cannot show. A sighting marked because a beep made the driver look says nothing about
whether beeps predict vehicles. Only sightings logged independently of the beeps do: a passenger
marking every emergency vehicle they see, beep or not. The report therefore prints, next to the
share of sightings that had a beep before them, the share of ordinary moments that had one too.
If the two are close, the beeps are not telling you anything.
"""
import argparse
import bisect
import datetime as dt
import json
from pathlib import Path
import sys

VEHICLES = {"police", "ambulance", "fire", "unknown"}
NOTHING = "noise"
REPEAT_S = 3.0          # the same button again within this long is one sighting
DRIVE_GAP_S = 600.0     # a longer silence in the records starts a new drive
CONTROL_STEP_S = 10.0


def when(text):
    return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def clock(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load(paths):
    """All alerts, sightings and track points from the given files and folders, each counted once."""
    alerts, sightings, track, files = {}, {}, {}, 0
    names = []
    for path in map(Path, paths):
        names += sorted(path.iterdir()) if path.is_dir() else [path]
    for name in names:
        try:
            with open(name, "rb") as fh:
                if not fh.read(64).lstrip().startswith(b"{"):
                    continue
            data = json.loads(name.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("kind") != "slot17-drive-data":
            continue
        files += 1
        for a in data.get("alerts", []):
            try:
                alerts[a["id"]] = {"t": when(a["at"]), "db": float(a["strength"]), "channel": a.get("channel"),
                                   "sound": a.get("sound")}
            except (KeyError, TypeError, ValueError):
                continue
        for s in data.get("sightings", []):
            try:
                sightings[s["id"]] = {"t": when(s["seenAt"]), "label": s["label"], "note": s.get("note", ""),
                                      "recording_request": bool(s.get("isRecordingRequest"))}
            except (KeyError, TypeError, ValueError):
                continue
        for p in data.get("track", []):
            try:
                track[when(p["at"])] = float(p.get("speedKmh", 0.0))
            except (KeyError, TypeError, ValueError):
                continue
    return (sorted(alerts.values(), key=lambda a: a["t"]), sorted(sightings.values(), key=lambda s: s["t"]),
            sorted(track.items()), files)


def merge_repeats(sightings, within=REPEAT_S):
    """A double tap is one sighting. Returns (kept, number merged away)."""
    kept, last = [], {}
    for s in sightings:
        if s["recording_request"]:
            continue
        previous = last.get(s["label"])
        if previous is not None and s["t"] - previous < within:
            last[s["label"]] = s["t"]
            continue
        last[s["label"]] = s["t"]
        kept.append(s)
    return kept, sum(not s["recording_request"] for s in sightings) - len(kept)


def drives(times, gap=DRIVE_GAP_S):
    out = []
    for t in sorted(times):
        if out and t - out[-1][1] <= gap:
            out[-1][1] = t
        else:
            out.append([t, t])
    return [(a, b) for a, b in out if b > a]


def lead_before(beeps, t, lead):
    """Seconds from the newest beep at or before `t` to `t`, if within `lead`; else None."""
    i = bisect.bisect_right(beeps, t)
    if i and t - beeps[i - 1] <= lead:
        return t - beeps[i - 1]
    return None


def score(alerts, sightings, track, lead=60.0, excluded=0):
    sightings, merged = merge_repeats(sightings)
    beeps = [a["t"] for a in alerts if a["sound"]]
    spans = drives([a["t"] for a in alerts] + [s["t"] for s in sightings] + [t for t, _ in track])
    rows = []
    for start, stop in spans:
        own_beeps = [t for t in beeps if start <= t <= stop]
        seen = [s for s in sightings if start <= s["t"] <= stop]
        vehicles = [s for s in seen if s["label"] in VEHICLES]
        leads = [lead_before(beeps, s["t"], lead) for s in vehicles]
        nothing = [s for s in seen if s["label"] == NOTHING]
        moments = [start + k * CONTROL_STEP_S for k in range(int((stop - start) / CONTROL_STEP_S) + 1)]
        covered = sum(lead_before(beeps, t, lead) is not None for t in moments)
        vehicle_times = [s["t"] for s in vehicles]
        # A beep is "explained" if a vehicle was marked from `lead` seconds after it down to 10 s before it.
        alone = sum(not any(-10 <= v - b <= lead for v in vehicle_times) for b in own_beeps)
        rows.append({"start": start, "stop": stop, "minutes": (stop - start) / 60,
                     "matches": sum(start <= a["t"] <= stop for a in alerts), "beeps": len(own_beeps),
                     "vehicles": len(vehicles), "warned": sum(x is not None for x in leads),
                     "leads": [x for x in leads if x is not None], "nothing": len(nothing),
                     "nothing_after_beep": sum(lead_before(beeps, s["t"], lead) is not None for s in nothing),
                     "beeps_alone": alone, "moments": len(moments), "moments_covered": covered,
                     "vehicle_rows": [(s, x) for s, x in zip(vehicles, leads)]})
    return {"rows": rows, "merged": merged, "lead": lead, "excluded": excluded}


def pct(part, whole):
    return "n/a" if not whole else f"{100 * part / whole:.0f}%"


def report(result, files):
    rows, lead = result["rows"], result["lead"]
    total = {k: sum(r[k] for r in rows) for k in ("minutes", "matches", "beeps", "vehicles", "warned", "nothing",
                                                 "nothing_after_beep", "beeps_alone", "moments", "moments_covered")}
    leads = sorted(x for r in rows for x in r["leads"])
    out = ["# Slot17 encounter scorecard", "",
           f"From {files} drive-data upload(s): {len(rows)} drive(s), {total['minutes'] / 60:.1f} hours. "
           f"A sighting counts as warned when a beep sounded in the {lead:.0f} s before it. "
           f"{result['merged']} repeated tap(s) merged, {result['excluded']} sighting(s) left out on request.", "",
           "## The answer so far", ""]
    hit, base = pct(total["warned"], total["vehicles"]), pct(total["moments_covered"], total["moments"])
    out += ["| Measure | Value |", "|---|---|",
            f"| Emergency vehicles marked | {total['vehicles']} |",
            f"| ...with a beep in the {lead:.0f} s before | {total['warned']} ({hit}) |",
            f"| ...with no beep before (missed) | {total['vehicles'] - total['warned']} |",
            f"| Ordinary moments with a beep in the {lead:.0f} s before | {base} |",
            f"| Seconds from beep to sighting, median | {leads[len(leads) // 2]:.0f} |" if leads else "| Seconds from beep to sighting, median | n/a |",
            f"| Beeps | {total['beeps']} ({total['beeps'] / max(total['minutes'], 1e-9) * 60:.1f} an hour) |",
            f"| Beeps with no vehicle marked near them | {total['beeps_alone']} ({pct(total['beeps_alone'], total['beeps'])}) |",
            f"| 'Saw nothing' marks | {total['nothing']}, of which {total['nothing_after_beep']} after a beep |", ""]
    if total["vehicles"] < 20:
        out += [f"**Too few sightings to conclude anything ({total['vehicles']}; aim for 20 or more).** "
                "The first row must clearly exceed the ordinary-moments row before the beeps mean something, "
                "and only sightings marked independently of the beeps count: a mark made because a beep "
                "made you look proves nothing.", ""]
    out += ["## Each drive", "",
            "| Start (UTC) | Minutes | Matches | Beeps | Beeps/hour | Vehicles | Warned | Missed | Saw nothing | Ordinary moments with a beep before |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {clock(r['start'])} | {r['minutes']:.0f} | {r['matches']} | {r['beeps']} | "
                   f"{r['beeps'] / max(r['minutes'], 1e-9) * 60:.1f} | {r['vehicles']} | {r['warned']} | "
                   f"{r['vehicles'] - r['warned']} | {r['nothing']} | {pct(r['moments_covered'], r['moments'])} |")
    out += ["", "## Each vehicle sighting", "", "| Time (UTC) | What | Beep before | Note |", "|---|---|---|---|"]
    for r in rows:
        for s, x in r["vehicle_rows"]:
            out.append(f"| {clock(s['t'])} | {s['label']} | {'none' if x is None else f'{x:.0f} s before'} | {s['note']} |")
    if not total["vehicles"]:
        out.append("| none | | | |")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="drive-data files, or folders holding them")
    ap.add_argument("--lead", type=float, default=60.0, help="seconds before a sighting in which a beep counts (default 60)")
    ap.add_argument("--exclude", action="append", default=[], metavar="UTC",
                    help="leave out sightings at this time, for test taps: 2026-11-11T19:54:04Z (repeatable)")
    ap.add_argument("--out", help="write the report here as well as printing it")
    args = ap.parse_args()
    alerts, sightings, track, files = load(args.paths)
    if not files:
        print("no drive-data uploads found", file=sys.stderr)
        return 1
    drop = [when(t) for t in args.exclude]
    kept = [s for s in sightings if not any(abs(s["t"] - t) < 1.0 for t in drop)]
    text = report(score(alerts, kept, track, args.lead, excluded=len(sightings) - len(kept)), files)
    sys.stdout.buffer.write(text.encode("utf-8"))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
