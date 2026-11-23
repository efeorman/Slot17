# Losing power at the ignition

The detector is expected to die by having its power removed, not by being asked
to stop. This document says exactly what survives that, what does not, and what
it costs. Nothing here is a substitute for a clean stop; it is what happens when
there isn't one.

Related: [README.md](../../README.md) for the routine, [FIELD-TEST.md](FIELD-TEST.md)
for measurement design.

## What survives, and the worst case for each

| Evidence | Durability | Worst case lost |
|---|---|---|
| `events.jsonl` | Written and flushed per line on its own thread; made durable at most 1 s later (since 1.5.1 by the recordings thread's `syncfs` on Linux, so the events thread never waits for a disk) | ~1 s of events |
| `recordings.jsonl` | Written by the recordings thread after each batch, durable within 1 s | ~1 s of outcome lines |
| `.c64` / `.power` / `.iq` recordings | Since 1.5.1 in batches: everything queued is written to `.tmp`, made durable together (one `syncfs` on Linux, one `fsync` per file elsewhere), then renamed; the new names are durable within 1 s | the batch still in flight, at most about 1 s of recordings |
| `summary.json` | Rebuilt and written durably every 30 s | 30 s of counters |
| `session.json` | Written once at start, durably | nothing |
| `watch.heartbeat` | On `/run` (tmpfs) | all of it, by design |

A binary recording is therefore **either complete or absent — never truncated**.
The `.tmp` file is fully written and flushed before the rename, so a cut during
the write leaves a `.tmp` that no index line refers to, and `recordings.jsonl`
never claims a file that does not exist.

The heartbeat is deliberately not evidence. It lives on tmpfs so a once-a-second
write never touches the disk, and it describes liveness, not history. Everything
it carries that matters afterwards is also in `summary.json`.

## What you only get from a clean stop

- `"final": true`. An interrupted session's summary says `false`, and its
  `recorder_drained` and `host_at_end` are `null`.
- Confirmation that the recorder drained its queue rather than being cut mid-write.
- Any notification still queued in the outbox at the moment power went.

Pressing the Argon One case button before switching the car off gives all of
this: it runs `shutdown now -h`, the unit allows 45 s to stop, and the detector's
teardown needs about 40. It is better, but the system no longer depends on it.

## The clock, and why it corrupts timestamps

This Pi has **no battery-backed real-time clock**. Cold-booted in the car it comes
up on a stale time restored from the last shutdown, and `systemd-timesyncd` steps
it forward once the phone hotspot connects. Measured on 9 September: the kernel
started believing it was 16:10:33, and NTP corrected to 16:19:51 — a **+9 min 18 s
step, nine minutes into the session.**

Two things used to break at that moment, both now fixed:

**Every RF alert went silent for the rest of the session.** Session timestamps are
anchored to the wall clock captured at the first sample. `Notifier.alert_hit`
compared that stale stamp against a corrected `time.time()`, concluded every match
was minutes old, and discarded it under the 30-second freshness rule. Freshness is
now measured on the sample clock (`burst_mono`), which a step cannot move.

**Elapsed time was inflated by the size of the step.** The 9 September session
reported 850 s elapsed against 107 completed windows — 321 s of real signal.
Elapsed is now taken from `time.monotonic()`.

What happens now when the clock steps:

- The wall-clock anchor is corrected, so windows after the step carry real times.
- A `clock_step` event is written to `events.jsonl` with `delta_s`, the cumulative
  `clock_offset_s` and the monotonic `elapsed_s` at which it happened.
- `clock_steps` and `clock_offset_s` appear in the heartbeat and in `summary.json`.

**Events written before the step keep the stale timestamp.** They cannot be
rewritten — they are already flushed to disk, which is the property that makes
them survive a power cut in the first place. The `clock_step` event is what makes
them interpretable: every event before it is early by the offset recorded there.
When matching an alert against a sighting, check `clock_steps` in the summary
first, and correct anything before the step.

Elapsed seconds, window indices and sample counts are on the sample clock and are
correct throughout, step or no step.

## Residual risk this does not remove

**Filesystem.** The root filesystem is ext4 mounted `rw,noatime` with the default
5-second journal commit. File contents we care about are fsynced within a second,
and the journal keeps the filesystem structurally consistent, so a cut should not
corrupt the volume. Repeated hard power cuts still carry some risk over time;
`commit=1` as a mount option would narrow the window at some write cost, and a UPS
would remove the question. Neither is configured. The Argon case supports a UPS
add-on (`argonone-upsconfig.sh` is present); nothing is attached.

**The last second.** Anything produced in the final second before power goes is
lost. This is a design choice: fsyncing every event would put a disk flush in the
path of every candidate, and this rig produced 7178 candidates in 17 minutes on
8 September.

**Slow storage (1.5.1).** Until 1.5.0 every recording cost two `fsync` calls, its
file and its sidecar, and ext4 commits its journal on each. A user's drive at 42
candidates a second on an SD card skipped 161 of 16,652 snippets, and a match could
wait behind a recording before the publisher saw it. Now one `syncfs` makes a whole
batch durable, and events have their own thread. `scripts/bench_recorder.py` runs
both recorders with each sync made as slow as a card's: at 50 ms per sync the old
one skipped 126 of 461 recordings and delivered matches up to 241 ms late; the new
one skipped none, with matches at most 8 ms late. Skipped recordings no longer mark
the detector degraded (only research snippets are missing); `setup.sh doctor` names
them under Storage.

**A cut during the very first second** can leave a session directory containing
`session.json` and little else. That is a real session that recorded nothing, not
a corrupt one.

## Checking a session after an interrupted drive

```bash
cd /home/pi/Slot17
python3 - <<'PY'
import json, glob, os
d = sorted(glob.glob('captures/*/'), key=os.path.getmtime)[-1]
s = json.load(open(d + 'summary.json'))
print(d)
print('final          :', s['final'], '(false = power was cut)')
# The window length is in the summary from version 1.1.3; before that, in its last health record.
w = s.get('window_s') or round((s.get('rf_health') or {}).get('window_duration_s') or 3, 1)
print('windows        :', s['windows'], '->', round(s['windows'] * w), 's of signal')
print('confirmed hits :', s['confirmed_hits'])
print('dropped/overflow:', s['dropped_samples'], s['overflow_windows'])
print('degraded       :', s['degraded_reasons'])
PY
```

If `final` is `false`, the counters are up to 30 seconds stale and the session
ended by power removal. That is expected in this installation and is not a fault.
