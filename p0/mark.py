#!/usr/bin/env python3
"""Record a human observation. Run from a passenger's SSH session or while parked."""
import argparse
import datetime as dt
import json
from pathlib import Path
import time
import uuid

import paths


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("label", choices=("police", "ambulance", "fire", "unknown", "noise", "antenna-change"))
    ap.add_argument("--note", default="")
    ap.add_argument("--session", type=Path, help="explicit session, including a completed session for retrospective notes")
    ap.add_argument("--utc", help="observation ISO timestamp with timezone; defaults to now")
    ap.add_argument("--heartbeat", type=Path, default=Path(paths.ROOT) / "runtime/watch.heartbeat")
    args = ap.parse_args()
    stamp = time.time()
    if args.utc:
        value = dt.datetime.fromisoformat(args.utc)
        if value.tzinfo is None:
            ap.error("--utc must include a timezone, e.g. 2026-09-09T18:02:00+02:00")
        stamp = value.timestamp()
    session = args.session
    if session is None:
        hb = json.loads(args.heartbeat.read_text(encoding="utf-8"))
        age = (time.monotonic() - hb["t_mono"]) if hb.get("t_mono") is not None else time.time() - hb.get("t", 0)
        if not hb.get("ready") or not 0 <= age < 5:
            ap.error("no fresh ready heartbeat; use --session for retrospective annotation")
        session = Path(hb["session"])
    if not session.is_dir() or not (session / "session.json").is_file():
        ap.error("session must be an existing v2 capture directory")
    mark = {"utc": stamp, "label": args.label, "note": args.note[:2000], "entered_utc": time.time()}
    if not args.utc:                 # now: the detector measures its age on the clock a UTC step can't move
        try:
            mark.update(mono=time.monotonic(), boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip())
        except OSError:
            pass
    inbox = session / "marks"
    inbox.mkdir(exist_ok=True)
    name = uuid.uuid4().hex + ".request.json"
    tmp = inbox / (name + ".tmp")
    tmp.write_text(json.dumps(mark), encoding="utf-8")
    tmp.replace(inbox / name)
    print(f"Observation saved: {inbox / name}")
    print("A running detector can capture 3 seconds before and after a fresh mark. Retrospective notes retain no new IQ.")


if __name__ == "__main__":
    main()
