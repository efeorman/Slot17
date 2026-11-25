"""Human observations and bounded pre/post IQ snapshots, independent of detections."""
import json
import math
from pathlib import Path
import time

from pipeline import emit


def boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        return None


BOOT_ID = boot_id()


def age_of(mark, utc):
    """How long ago the sighting was. On the monotonic clock when the writer put its reading there on this same
    boot: a UTC step between writing and reading (the Pi boots on a stale clock) made a fresh sighting look old
    and lose its recording (found in review, 24 November 2026). Else from UTC, as before."""
    mono = mark.get("mono")
    if (BOOT_ID is not None and mark.get("boot") == BOOT_ID and isinstance(mono, (int, float))
            and math.isfinite(mono)):
        return time.monotonic() - float(mono)
    return time.time() - utc


class Marks:
    def __init__(self, ctx, ring):
        self.ctx, self.ring = ctx, ring
        self.inbox = Path(ctx.outdir) / "marks"
        self.inbox.mkdir(exist_ok=True)
        self.pending = []
        self.last_poll = self.last_capture = -1e30
        self.unrenamed = set()      # handled, but the disk would not let them be marked as handled

    def poll(self):
        now = time.monotonic()
        if now - self.last_poll < .5:
            return
        self.last_poll = now
        waiting = [p for p in sorted(self.inbox.glob("*.request.json")) if p.name not in self.unrenamed]
        for path in waiting[:16]:
            try:
                if path.stat().st_size > 8192:
                    raise ValueError("mark request exceeds 8 KiB")
                mark = json.loads(path.read_text(encoding="utf-8"))
                utc = float(mark["utc"])
                if not math.isfinite(utc):
                    raise ValueError("mark time is not a finite number")
                age = age_of(mark, utc)
                event = {"schema_version": 2, "event_type": "human_observation", "mark_id": path.stem,
                         "utc": utc, "label": str(mark.get("label", "unknown"))[:80],
                         "note": str(mark.get("note", ""))[:2000], "source": "human; not inferred from RF"}
                emit(self.ctx, event)
                if 0 <= age <= 5 and now - self.last_capture >= 10 and len(self.pending) < 2:
                    pos = max(0, self.ring.write - int(age * self.ctx.fs))
                    self.pending.append((pos, now + 5, event))
                    self.last_capture = now
                else:
                    emit(self.ctx, {"event_type": "mark_recording", "mark_id": event["mark_id"],
                                    "state": "observation_only", "reason": "stale mark or capture rate limit"})
            except Exception as exc:
                emit(self.ctx, {"event_type": "mark_error", "request": path.name, "error": type(exc).__name__})
            try:
                path.replace(path.with_suffix(".processed"))
            except OSError as exc:
                self.unrenamed.add(path.name)
                emit(self.ctx, {"event_type": "mark_error", "request": path.name, "error": type(exc).__name__})
        for pos, deadline, event in list(self.pending):
            if self.ring.write < pos + 3 * self.ctx.fs and now < deadline:
                continue
            self.pending.remove((pos, deadline, event))
            start, stop = max(0, pos - 3 * self.ctx.fs), min(self.ring.write, pos + 3 * self.ctx.fs)
            try:
                samples = self.ring.extract(start, stop - start)
                name = event["mark_id"] + ".iq"
                state = self.ctx.recorder.samples(name, samples,
                    {"format": "int16_iq_le", "sample_rate_hz": self.ctx.fs,
                     "centre_hz": self.ctx.centre * 1e6, "sample_start": start, "sample_stop": stop,
                     "mark": event, "dropped_sample_boundaries": list(self.ctx.stats.discontinuities),
                     "timestamp_basis": "host request time aligned approximately to sample clock"})
            except BufferError:
                state, name = "overwritten", None
            emit(self.ctx, {"schema_version": 2, "event_type": "mark_recording",
                            "mark_id": event["mark_id"], "name": name, "state": state})

    def finish(self):
        for _, _, event in self.pending:
            emit(self.ctx, {"event_type": "mark_recording", "mark_id": event["mark_id"],
                            "state": "stopped_before_postroll"})
        self.pending.clear()
