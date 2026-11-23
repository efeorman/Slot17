"""The recorder under a busy band on slow storage: the recorder of a git commit against the working copy's.

    python scripts/bench_recorder.py                     # 1.5.0's recorder against this one, 15 ms per sync
    python scripts/bench_recorder.py --sync-ms 50 --old v1.5.0

Each disk sync (fsync, or syncfs) is made to take --sync-ms longer, which is what a journal commit costs on an
SD card: a few ms on a good one, tens to hundreds on a slow or busy one. The load is a user's drive: 42
candidate snippets of 10.6 KB and 4 power tables of 40 KB a second, and a match every half second whose
delay from the detector to events.jsonl (what the display publisher reads) is measured. Nothing here touches a
receiver; it writes to a temporary folder and deletes it.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np

REPO = Path(__file__).resolve().parents[1]


def load(source, name):
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(module, folder, seconds, sync_ms, candidates, windows):
    real_fsync = os.fsync

    def slow_fsync(fd):
        time.sleep(sync_ms / 1000)
        real_fsync(fd)
    kwargs = {"min_free": 0}
    if "sync" in module.Recorder.__init__.__code__.co_varnames:
        kwargs["sync"] = lambda directory: time.sleep(sync_ms / 1000)   # one journal commit for the batch
    os.fsync = slow_fsync
    try:
        r = module.Recorder(folder, **kwargs).start()
        snippet = np.zeros(1332, np.complex64)                           # 10.6 KB
        table = np.zeros(40_000, np.int8)
        sent, latencies, stop = {}, [], threading.Event()

        def watch_events():
            path, seen = Path(folder) / "events.jsonl", 0
            while not stop.is_set():
                if path.exists():
                    text = path.read_text(encoding="utf-8")
                    for line in text.splitlines()[seen:]:
                        seen += 1
                        e = json.loads(line)
                        if e.get("probe") in sent:
                            latencies.append(time.monotonic() - sent[e["probe"]])
                time.sleep(0.002)
        watcher = threading.Thread(target=watch_events, daemon=True)
        watcher.start()
        start, n, per_s = time.monotonic(), 0, candidates + windows
        while time.monotonic() - start < seconds:
            due = start + n / per_s
            time.sleep(max(0.0, due - time.monotonic()))
            if n % per_s < windows:
                r.samples(f"w{n}.power", table, {"format": "int8_db"})
            else:
                r.samples(f"e{n}.c64", snippet, {"format": "complex64_le"})
            if n % (per_s // 2) == 0:
                sent[n] = time.monotonic()
                r.event({"probe": n, "confirmed": True})
            n += 1
        drained = r.stop(timeout=120)
        time.sleep(0.2)
        stop.set()
        watcher.join()
        snap = r.snapshot()
    finally:
        os.fsync = real_fsync
    return {"offered": n, "written": snap["written_data"], "skipped": snap["skipped_data"], "drained": drained,
            "match_delay_median_ms": round(statistics.median(latencies) * 1000, 1) if latencies else None,
            "match_delay_max_ms": round(max(latencies) * 1000, 1) if latencies else None}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--old", default="v1.5.0", help="git tag or commit of the recorder to compare with (1.5.0 by default)")
    ap.add_argument("--sync-ms", type=float, default=15.0)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--candidates", type=int, default=42)
    ap.add_argument("--windows", type=int, default=4)
    a = ap.parse_args(argv)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:   # an old recorder may still be writing
        old_source = Path(tmp) / "recording_old.py"
        old_source.write_bytes(subprocess.run(["git", "-C", str(REPO), "show", f"{a.old}:p0/recording.py"],
                                              capture_output=True, check=True).stdout)
        for label, source in ((f"recorder of {a.old}", old_source), ("this recorder", REPO / "p0/recording.py")):
            folder = Path(tmp) / label.replace(" ", "-")
            result = run(load(source, label.replace(" ", "_")), folder, a.seconds, a.sync_ms, a.candidates, a.windows)
            print(f"{label:24} {a.sync_ms:g} ms per sync: {json.dumps(result)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
