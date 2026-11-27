#!/usr/bin/env python3
"""Continuous receive-only TETRA activity monitoring.

A bounded callback ring feeds 0.5-second analysis windows. Short candidate
bursts require physical-layer training evidence before RF alerts. Event metadata,
compact narrowband evidence and optional per-window power are written by a
separate bounded recorder. Human marks can preserve six seconds of wideband IQ.
The service reports ready only after completed analysis. No RF-derived vehicle
identity, movement or distance is claimed. See README.md.

    python watch.py --band uplink --minutes 20 --save-power
    python watch.py --selftest     # offline, no receiver opened
"""
import argparse
from collections import deque
import datetime as dt
import os
import signal
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import diagnostics
import notify
import failure
import receiver as receivers
import recording
from version import provenance
from detect import load_downlink_channels, BurstHistory

BANDS = {"uplink": 382.5, "downlink": 392.5,
         "civil-uplink": 415.0, "civil-downlink": 425.0}

# The City/Highway idea: higher sensitivity reaches further but alerts more,
# lower sensitivity is calmer in a busy band. These set the detection threshold in
# dB above each channel's own floor.
SENSITIVITY = {"highway": 6.0,   # default energy-candidate threshold
               "city": 10.0,     # calmer - urban, lots of activity
               "max": 4.0}       # bench / weak-signal hunting

# Analysed in one go; >> the 170 ms needed for periodicity. A burst waits on average half a window
# before analysis starts, so this sets most of the alert delay: 3.0 s gave a median of 3.1 s from
# burst to finished check on the 7 November drive. 1.5 s (2026-11-07) halved the
# wait; the confirmation budget below keeps the same share of each window as before.
# 0.5 s (2026-11-12): on that evening's drive the beeps were decided 1.7 to 2.7 s after
# their burst, and the operator asked for the shortest delay possible. The cost: a burst
# lying across the end of a window is cut, which now happens to about 3 % of normal
# bursts instead of 1 %, and less checking time in each whole-band window.
WINDOW_S = 0.25
# 0.25 s (2026-11-15): each window also looks back WINDOW_OVERLAP_S, longer than any envelope the
# detector keeps (4 slots + 2 ms = 58.7 ms), and owns only the bursts that START in its own new
# 0.25 s (detect.detect `own`). So every burst is seen whole, by exactly one window: none is cut at a
# window edge any more (about 3 % of normal bursts were, with 0.5 s windows), and none is counted
# twice. WINDOW_S stays the time a window moves on, so every "windows x WINDOW_S" stays signal time.
WINDOW_OVERLAP_S = 0.064
OWN_MARGIN_S = 0.0015   # a burst starting this close to a window's start belongs to the window before
SNIPPET_S = 6.0         # raw IQ kept around a hit for later re-examination
RING_CAP_S = 15.0       # ring capacity; SPSC, bounded (~360 MB at 6 MSPS)
BYTES_PER_SAMPLE = 4    # int16 I + int16 Q

# Exit code the supervisor reads as "the stream wedged" (as opposed to a clean
# stop or a crash). 75 is the conventional EX_TEMPFAIL - a transient failure the
# caller should retry. See supervise.py.
STALL_EXIT_CODE = 75

# --- analysis can die while reception looks perfectly healthy ---
# `ring.write` is advanced by the USB callback, not by analysis. Both the stall
# check here and supervise.py's heartbeat backstop used to watch it - two
# "independent" watchdogs reading the same counter, neither of which says the
# detector is actually *working*. If `process_window` raised, the worker thread
# died, samples kept arriving, the heartbeat kept updating, and nothing was
# analysed for hours. `windows` is the counter that only moves when a window is
# genuinely processed. A window is seconds long by design, so it gets a longer
# limit than the sample-stream check.
ANALYSIS_STALL_FACTOR = 3.0

# A session summary used to exist only after a clean shutdown, so the 8 September
# drive - cut off by vehicle power during teardown - left a zero-byte file and no
# counters at all. Snapshot it while running as well: an interrupted session then
# loses at most this many seconds of bookkeeping instead of all of it.
SUMMARY_INTERVAL_S = 30.0

# Treat a wall-clock move this large as a step rather than drift. NTP slew
# stays far below it; the car's cold-boot correction is minutes.
CLOCK_STEP_S = 1.0

# --- a full disk must not take the detector down with it ---
# A snippet is ~144 MB and an event line ~400 bytes; measured 4.5 GB per 11
# minutes, so a 3 h drive is ~70 GB. Below this floor snippets stop, loudly, and
# detection and the event log carry on. Detection degrades to "events only",
# never to "silently nothing".
SNIPPET_MIN_FREE_BYTES = 5 * (1 << 30)
SNIPPET_FREE_CHECK_EVERY = 10

# Consecutive `antenna_suspect` windows that make "the receiver is hearing
# nothing" a session fault rather than a passing dip. Calibrated from the Pi's
# own corpus, not guessed: across twelve watch sessions the hearing ones peaked
# at 12.3 % of windows suspect and never ran long (5 consecutive on 12 Sept),
# while the seven deaf ones ran 100 % - 806, 857 and 8208 windows. Those
# windows were 3 s long, so the limit was set at twenty of them: about a minute,
# far above any transient and far below a real fault. It is kept as a time, so
# it stays a minute whatever the window length (120 windows of 0.5 s today), and
# the heartbeat carries the count for display_publisher.py and the phone.
#
# This is honest-but-not-proof, and it stays that way: low input cannot prove a
# disconnected antenna, so the reason says "unverified" and the operator decides.
# Starting the Pi at home with no antenna attached is a normal test and will
# raise it - that is correct behaviour, not a false alarm, and notify.py no
# longer repeats an unchanged state fault, so it costs one message per session.
# 0 disables the fault entirely.
QUIET_INPUT_SECONDS = 60.0
QUIET_INPUT_WINDOWS = int(round(QUIET_INPUT_SECONDS / WINDOW_S))

# Samples lost in the stream's first minute are a start-up note, not a session fault.
# Every loss recorded since a 4G stick shared the receiver's USB controller (14 November 2026)
# came 0.3 to 25 s after the stream opened, while the stick was starting, with one exception
# at 105 s; on 17 November three of four starts lost 0.1 s once or a few times and nothing
# after. Counted as a fault, such a gap marked the whole drive "degraded" and put checking on
# one thread for five minutes (on 17 November 36 % of candidates unchecked against 11 %).
# They are still counted and reported (`startup_dropped`); a loss after this time is a fault
# as before. Settable with SLOT17_STARTUP_GRACE_S; 0 treats every loss as a fault.
def _startup_grace_s():
    try:
        value = float(os.environ.get("SLOT17_STARTUP_GRACE_S", "60"))
    except ValueError:
        return 60.0
    return min(max(value, 0.0), 600.0) if value == value else 60.0


STARTUP_GRACE_S = _startup_grace_s()

# How much a channel's peak SNR must move between sightings to call it a trend.
TREND_DELTA_DB = 3.0
TREND_MEMORY_S = 30.0   # forget a channel this long after its last sighting

_stop = threading.Event()


class TrendTracker:
    """Channel signal trend only; power control and fading can mimic motion."""
    def __init__(self):
        self.last = {}      # tetra_channel -> (elapsed_s, snr_db)

    def update(self, tetra_ch, elapsed_s, snr_db):
        prev = self.last.get(tetra_ch)
        self.last[tetra_ch] = (elapsed_s, snr_db)
        for k in [k for k, (t, _) in self.last.items()
                  if elapsed_s - t > TREND_MEMORY_S and k != tetra_ch]:
            del self.last[k]
        if prev is None or elapsed_s - prev[0] > TREND_MEMORY_S:
            return "new"
        d = snr_db - prev[1]
        if d >= TREND_DELTA_DB:
            return "rising"
        if d <= -TREND_DELTA_DB:
            return "falling"
        return "steady"


class Ring:
    """Lock-free single-producer/single-consumer ring of interleaved int16 IQ.

    The producer (capture callback) copies each block in and advances `write`;
    the consumer (analysis) reads windows strictly behind `write`. Only the
    `write` index is guarded, and only while it is updated - never across a data
    copy - so the producer can never be blocked by the consumer's work. Sizing
    the ring far larger than one window keeps the consumer's read region well
    away from where the producer is writing, so the data copy needs no lock.
    """
    def __init__(self, cap_samples):
        self.cap = int(cap_samples)
        self.buf = np.zeros(self.cap * 2, np.int16)
        self._write = 0
        self._reserved_write = 0
        self._lock = threading.Lock()

    def push(self, blk_int16):
        nsamp = blk_int16.shape[0] // 2
        if nsamp <= 0:
            return
        total = nsamp
        self._reserved_write = self._write + total
        if nsamp > self.cap:
            blk_int16 = blk_int16[-self.cap * 2:]
            nsamp = self.cap
        start = (self._write + total - nsamp) % self.cap
        if start + nsamp <= self.cap:
            self.buf[start * 2:(start + nsamp) * 2] = blk_int16[:nsamp * 2]
        else:
            first = self.cap - start
            self.buf[start * 2:] = blk_int16[:first * 2]
            self.buf[:(nsamp - first) * 2] = blk_int16[first * 2:nsamp * 2]
        with self._lock:
            self._write += total

    @property
    def write(self):
        with self._lock:
            return self._write

    def extract(self, read, nsamp):
        """Copy `nsamp` interleaved samples starting at absolute index `read`."""
        if nsamp < 0 or nsamp > self.cap or read < max(0, self._reserved_write - self.cap) or read + nsamp > self.write:
            raise BufferError("requested IQ is unavailable or overwritten")
        start = read % self.cap
        if start + nsamp <= self.cap:
            out = self.buf[start * 2:(start + nsamp) * 2].copy()
        else:
            first = self.cap - start
            out = np.empty(nsamp * 2, np.int16)
            out[:first * 2] = self.buf[start * 2:]
            out[first * 2:] = self.buf[:(nsamp - first) * 2]
        if read < self._reserved_write - self.cap:
            raise BufferError("IQ overwritten during snapshot")
        return out


class Stats:
    def __init__(self):
        self.dropped = 0        # samples libairspy reports lost (callback)
        self.startup_dropped = 0  # of `dropped`: lost in the stream's first STARTUP_GRACE_S
        self.overflow = 0       # windows skipped because analysis fell behind
        self.windows = 0
        self.windows_started = 0
        self.confirm_attempts = self.confirm_errors = self.event_errors = self.notify_errors = 0
        self.partial_confirmations = 0
        self.refinement_failures = 0
        self.nub_training_matches = self.nub_tail_rejections = 0
        self.last_success_utc = self.last_success_mono = None
        self.last_window_seconds = self.max_window_seconds = self.last_channel_seconds = 0.0
        self.stage = "starting"
        self.callback_errors = 0
        self.capture_start_utc = None
        # The Pi did not retain correct RTC time. In the car it cold-boots with a
        # stale time and NTP steps it minutes later, once the phone hotspot is
        # up. `capture_start_mono` gives every timestamp a step-immune partner,
        # and `clock_offset` re-anchors the wall-clock times when a step lands.
        self.capture_start_mono = None
        self.clock_offset = 0.0
        self.clock_steps = 0
        self.discontinuities = deque(maxlen=1024)
        # `discontinuities` is bounded, so evicted gaps have to keep counting
        # somewhere. Without this the drop total behind the read position falls
        # back to zero and RF timestamps walk backwards during heavy loss.
        self.dropped_retired = 0
        self.discontinuous_windows = 0
        self.hits = 0            # every detection detect() produced
        self.confirmed = 0       # hits that passed the examine second stage
        self.unchecked = 0       # hits the per-window confirm budget skipped
        self.unconfirmable = 0   # hits with no burst region short enough to inspect
        self.unchecked_own_burst = 0  # hits whose burst outside a broadband moment was never inspected
        self.call_matches = 0    # of `confirmed`: in a call pattern (detect.call_run)
        self.weak_matches = 0    # of `confirmed`: made by a weak rule (phy.py), the standard one having refused
        self.whole_band_windows = 0   # windows with candidates on 60 or more channels at once
        self.confirm_workers = 0      # threads the last window's candidates were examined on
        # Dropped samples put checking on one thread for a while: see pipeline.DROP_HOLD_S.
        self.drop_noted = 0           # the `dropped` total the checker last saw
        self.drop_noted_mono = None   # when it saw that total change
        self.threads_resumed = False  # the threads came back once after a spell without loss
        self.threads_given_up = False  # samples were lost again after that: one thread until the end
        self.glitches = 0
        self.impulsive = 0       # coincidence advisories; recognition decides
        self.window_errors = 0   # windows lost to an exception
        self.telemetry_errors = self.heartbeat_errors = 0
        self.last_telemetry_error = self.last_heartbeat_error = None
        self.last_telemetry_seconds = 0.0
        self.snippet_errors = 0  # snippets that could not be written (47b)
        self.snippets_skipped = 0  # snippets skipped for lack of disk space
        self.worker_alive = True  # cleared when the analysis thread exits
        # Consecutive analysis windows whose input was too weak for the receiver
        # to be hearing anything. Seven of the twelve sessions on this Pi ran
        # like that - one of them for 6.8 hours - and the detector called every
        # one of them healthy. See QUIET_INPUT_WINDOWS.
        self.quiet_windows = 0
        self.quiet_windows_max = 0
        self._drop_warned = False
        self._last_dropped = 0
        self._last_el = 0.0


def dropped_before(gaps, retired, read, floor=0):
    """Cumulative dropped samples at or before absolute position `read`.

    `read` only ever advances, so this must never decrease; a decrease would
    move an RF timestamp backwards. `floor` carries the previous answer and
    `retired` carries gaps the bounded deque has already discarded.
    """
    return max(floor, retired, max((n for pos, n in gaps if pos <= read), default=0))


def count_drop(stats, ring, lost):
    """Record `lost` samples reported by the driver before the block now arriving at `ring.write`."""
    stats.dropped += lost          # atomic under GIL
    if ring.write < STARTUP_GRACE_S * float(getattr(stats, "fs", 6_000_000)):
        stats.startup_dropped += lost
    gaps = stats.discontinuities
    if len(gaps) == gaps.maxlen:
        # About to be evicted, and far enough behind the write head
        # that no readable window can precede it: carry its total.
        stats.dropped_retired = max(stats.dropped_retired, gaps[0][1])
    gaps.append((ring.write, int(stats.dropped)))


class Sink:
    """Where every receiver's samples go: the ring, with the session's start time and every loss as a gap.

    Each receiver's callback calls these in this order, from the driver's own thread: first(n) with the
    block's length, drop(lost) for samples the driver says it lost before this block, push(int16 I/Q) with
    the block itself; and lost(count) when a block could not be read at all.
    """

    def __init__(self, ring, stats):
        self.ring, self.stats = ring, stats

    def first(self, n, delay_s=0.0):
        """delay_s: how far a receiver's output lags its input (the RTL pair's join: 0.5 ms), so the first
        sample is dated when it reached the antenna, not when it left the join (found in review, 24 November 2026)."""
        stats = self.stats
        if stats.capture_start_utc is None:
            back = n / float(getattr(stats, "fs", 6_000_000)) + float(delay_s)
            stats.capture_start_utc = time.time() - back
            stats.capture_start_mono = time.monotonic() - back

    def drop(self, lost):
        count_drop(self.stats, self.ring, lost)

    def push(self, iq):
        self.ring.push(iq)

    def lost(self, count):
        """These samples are gone: mark the gap, like any other drop, so no window is treated as continuous
        across it. `count` may be a function, read here, so a block that can't be read can't escape either."""
        stats = self.stats
        stats.callback_errors += 1
        try:
            stats.dropped += count() if callable(count) else count
            stats.discontinuities.append((self.ring.write, int(stats.dropped)))
        except Exception:  # noqa: BLE001, S110 - nothing may escape into the driver's thread
            pass


def make_callback(ring, stats):
    """The Airspy's block callback into this ring (receiver_airspy.py has it; kept here for its tests)."""
    import receiver_airspy
    return receiver_airspy.make_callback(Sink(ring, stats))


def note_metadata_error(stats, what, exc):
    """A status file could not be written (a full disk, say). Reception carries on and reports it."""
    stats.metadata_errors = getattr(stats, "metadata_errors", 0) + 1
    stats.last_metadata_error = f"{what}: {type(exc).__name__}: {exc}"[:200]


class Ctx:
    """Everything the analysis worker needs, gathered in one place."""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _elapsed(ctx, now_t):
    """Seconds of session, on a clock an NTP step cannot inflate."""
    start_mono = getattr(ctx, "t_start_mono", None)
    if start_mono is not None:
        return time.monotonic() - start_mono
    return now_t - ctx.t_start


def check_clock_step(ctx):
    """Re-anchor wall-clock timestamps when the system clock is stepped.

    This Pi did not retain RTC time. Cold-booted in the car it starts on a
    stale time and `systemd-timesyncd` steps it forward once the hotspot
    connects - 9 min 18 s on 9 September. Session timestamps are anchored to
    the wall clock captured at the first callback, so without this every event
    in the drive keeps the stale time, and `Notifier.alert_hit` compares a stale
    event stamp against a corrected `time.time()`, decides every match is more
    than 30 seconds old, and silently drops **every RF alert for the rest of
    the session**.

    The sample clock and `time.monotonic()` are both immune to the step, so the
    difference between them and the wall clock is the correction to apply.
    Returns the step size in seconds, or 0.0 when the clock is behaving.
    """
    st = ctx.stats
    if st.capture_start_mono is None:
        return 0.0
    expected = st.capture_start_utc + st.clock_offset + (time.monotonic() - st.capture_start_mono)
    delta = time.time() - expected
    if abs(delta) < CLOCK_STEP_S:
        return 0.0
    st.clock_offset += delta
    st.clock_steps += 1
    from pipeline import emit
    emit(ctx, {"schema_version": 2, "event_type": "clock_step",
               "delta_s": round(delta, 3), "steps": st.clock_steps,
               "clock_offset_s": round(st.clock_offset, 3),
               "elapsed_s": round(time.monotonic() - st.capture_start_mono, 3),
               "utc": time.time(),
               "note": "wall clock corrected; events written before this line are "
                       "earlier than real time by the cumulative offset"})
    print(f"  clock stepped {delta:+.1f}s; timestamps re-anchored "
          f"(total offset {st.clock_offset:+.1f}s)", flush=True)
    mark_session_time(ctx)
    return delta


def mark_session_time(ctx):
    """Leave the session's real start time inside the session, and next to it.

    The folder name is stamped from the wall clock at startup, and on this Pi
    that clock is always wrong at startup: there is no RTC and there cannot be
    one - the ignition removes power outright, so nothing keeps time across a
    cold boot. Every session in the corpus is misnamed, by between ten minutes
    and 47 hours, and one 12 September folder claims a start two days before it
    happened. Renaming the live directory would break the open recorder and the
    paths already written into events, so instead:

      * `session-time.json` inside the session states both times and the offset;
      * a sibling symlink under the corrected name makes `ls` tell the truth.

    Neither touches a byte of recorded evidence. Failure here is never allowed
    to disturb a capture - a wrong folder name is a nuisance, a lost drive is not.
    """
    st = ctx.stats
    outdir = getattr(ctx, "outdir", None)
    if not outdir or st.capture_start_mono is None:
        return None
    try:
        started = st.capture_start_utc + st.clock_offset
        real = dt.datetime.fromtimestamp(started, dt.timezone.utc)
        corrected = real.strftime("%Y%m%d-%H%M%S-%fZ")
        name = os.path.basename(outdir)
        suffix = name.split("Z_", 1)[1] if "Z_" in name else name
        recording.atomic_json(os.path.join(outdir, "session-time.json"), {
            "schema_version": 1,
            "directory_name_utc": name.split("_", 1)[0],
            "corrected_start_utc": started,
            "corrected_start_iso": real.isoformat(),
            "clock_offset_s": round(st.clock_offset, 3),
            "clock_steps": st.clock_steps,
            "note": "The directory name was stamped from the pre-NTP boot clock and is "
                    "wrong by clock_offset_s. This Pi has no RTC by design (power is cut "
                    "at the ignition). Use corrected_start_utc for anything time-based.",
        })
        link = os.path.join(os.path.dirname(outdir), f"{corrected}_{suffix}")
        if os.name == "posix" and not os.path.lexists(link):
            os.symlink(name, link)
        return link
    except Exception as exc:                            # noqa: BLE001
        print(f"  WARN could not record corrected session time "
              f"({type(exc).__name__}: {exc})", flush=True)
        return None


def host_faults(host):
    """Power and thermal states the Pi reports about itself, right now.

    Only the "_now" bits raise a fault. The "_since_boot" flags stay set for the
    whole boot, so alerting on those would mean one brief undervoltage blip at
    switch-on marked the entire drive degraded. They remain in `host_health` for
    anyone reading the session afterwards.

    This matters for a glovebox install: the fan moves air inside the case but
    cannot cool the enclosure, and a throttling Pi is analysing less of the band
    than its window count suggests.
    """
    if not host:
        return []
    power = host.get("power") or {}
    faults = []
    for label, key in (("supply undervoltage", "undervoltage_now"),
                       ("CPU frequency capped", "frequency_capped_now"),
                       ("thermal throttling", "throttled_now"),
                       ("soft temperature limit reached", "soft_temperature_limit_now")):
        if power.get(key):
            faults.append({"fault": label, "total": host.get("cpu_temp_c")})
    return faults


def faulty_drops(st):
    """Dropped samples that count as a fault: all of them except those of the start-up grace."""
    return max(0, int(getattr(st, "dropped", 0) or 0) - int(getattr(st, "startup_dropped", 0) or 0))


def degraded_reasons(st, recorder, host=None):
    return _recorded_faults(st, recorder, host) + _reporting_faults(st)


def _reporting_faults(st):
    """Faults in reporting, not reception. A status file or a notice that cannot be written is news."""
    faults = []
    if getattr(st, "metadata_errors", 0):
        faults.append({"fault": "status file errors", "total": st.metadata_errors})
    problem = getattr(st, "notify_problem", None)
    if problem:
        faults.append({"fault": problem, "total": None})
    return faults


def _recorded_faults(st, recorder, host=None):
    """Name what is wrong right now, so a phone message can say more than "degraded".

    Each entry carries its running total, because these counters only ever grow:
    a reader comparing two heartbeats can tell a single early glitch from damage
    that is still accumulating. A recorder that has stopped is only a fault while
    the worker is still running - at shutdown it is what stopping looks like, and
    reporting it there is what made the 8 September drive's final heartbeat claim
    a degraded session with every error counter at zero.
    """
    reasons = []
    for label, value in (("dropped samples", faulty_drops(st)), ("overflow windows", st.overflow),
                         ("callback errors", st.callback_errors), ("window errors", st.window_errors),
                         ("event errors", st.event_errors), ("confirmation errors", st.confirm_errors),
                         ("telemetry errors", st.telemetry_errors),
                         ("heartbeat errors", st.heartbeat_errors)):
        if value:
            reasons.append({"fault": label, "total": int(value)})
    if recorder:
        # Recordings skipped because the storage was slower than a busy band, or full, are not on this list
        # (1.5.1): detection, matches and events are untouched, only research snippets are missing. They are
        # still counted in the heartbeat and summary (recorder.skipped_data), and the doctor names them. Events
        # dropped or a recording that failed to write still are faults.
        for label, key in (("recording events dropped", "dropped_events"),
                           ("recording data errors", "data_errors"),
                           ("recording event errors", "event_errors")):
            if recorder[key]:
                reasons.append({"fault": label, "total": int(recorder[key])})
        if st.worker_alive and not recorder["alive"]:
            reasons.append({"fault": "recorder stopped", "total": None})
    # A receiver that hears nothing makes the whole session worthless, and until
    # now it was the one fault nobody was told about: `antenna_suspect` was
    # telemetry only, so a 6.8-hour deaf session reported degraded=false and
    # ready=true throughout. The total is the run length, so a reader can tell a
    # minute of quiet from six hours of it.
    if QUIET_INPUT_WINDOWS and getattr(st, "quiet_windows", 0) >= QUIET_INPUT_WINDOWS:
        reasons.append({"fault": "receiver input very low - antenna connection unverified",
                        "total": int(st.quiet_windows), "steady": True})
    reasons.extend(host_faults(host))
    return reasons


def describe_fault(reason):
    """One reason as a line of text; a state has no total to report."""
    return (reason["fault"] if reason.get("total") is None
            else f"{reason['fault']} {reason['total']}")


def build_summary(final, *, stats, ring, ctx, recorder, started_mono,
                  receiver, failed, failure_reason, recorder_drained):
    """Session counters, built repeatedly so an interrupted drive keeps them.

    `final` separates a completed teardown from a snapshot taken while running:
    only the final one has drained the recorder or sampled the host at the end,
    and a reader must never mistake a mid-drive snapshot for the end of a session.
    """
    snap = recorder.snapshot()
    return {"schema_version": 2, "final": bool(final),
            "utc": time.time(), "seconds": round(time.monotonic() - started_mono, 3),
            "windows": stats.windows, "windows_started": stats.windows_started,
            "hits": stats.hits, "confirmed_hits": stats.confirmed, "unchecked_hits": stats.unchecked,
            "unconfirmable_hits": stats.unconfirmable,
            "unchecked_own_burst_hits": stats.unchecked_own_burst,
            "weak_matches": stats.weak_matches,
            "call_matches": stats.call_matches,
            "whole_band_windows": stats.whole_band_windows,
            "partial_confirmations": stats.partial_confirmations,
            "coverage_notice_policy": "status and summary only; no coverage phone alerts",
            "refinement_failures": stats.refinement_failures,
            "nub_training_matches": stats.nub_training_matches,
            "nub_tail_rejections": stats.nub_tail_rejections,
            "confirm_errors": stats.confirm_errors, "window_errors": stats.window_errors,
            "dropped_samples": int(stats.dropped),
            "startup_dropped_samples": int(getattr(stats, "startup_dropped", 0)),
            "startup_grace_s": STARTUP_GRACE_S, "overflow_windows": stats.overflow,
            "discontinuous_windows": stats.discontinuous_windows, "callback_errors": stats.callback_errors,
            "max_window_seconds": stats.max_window_seconds, "recorder": snap,
            "samples_received": ring.write,
            "samples_after_last_completed_window": max(0, ring.write - getattr(stats, "last_sample_end", 0)),
            "recorder_drained": recorder_drained, "failed": failed,
            "failure_reason": failure_reason, 'rf_health': getattr(ctx, 'rf_health', None),
            "failure_category": getattr(stats, 'failure_category', None),
            "degraded_reasons": degraded_reasons(
                stats, snap,
                ctx.host_sampler.latest if getattr(ctx, "host_sampler", None) else None),
            "quiet_windows": int(getattr(stats, "quiet_windows", 0)),
            "quiet_windows_max": int(getattr(stats, "quiet_windows_max", 0)),
            "window_s": WINDOW_S, "confirm_workers": stats.confirm_workers,
            "clock_steps": stats.clock_steps, "clock_offset_s": round(stats.clock_offset, 3),
            "telemetry_errors": stats.telemetry_errors, "heartbeat_errors": stats.heartbeat_errors,
            "notifications": ctx.notifier.snapshot() if getattr(ctx, "notifier", None) else None,
            "receiver": receiver, "software": provenance(),
            "host_at_end": diagnostics.host_health() if final else None}


def write_heartbeat(path, ctx, ring, now_t):
    """Atomically write a one-line liveness snapshot for the supervisor.

    Both sample progress and completed analysis matter. If samples stop
    advancing while the process is still alive, the stream has wedged - the
    in-field failure this whole recovery path exists for. Written to a temp file
    and renamed so a reader never sees a half-written line.
    """
    st = ctx.stats
    mono = time.monotonic()
    data = {"schema_version": 2, "t": round(now_t, 2), "pid": os.getpid(),
            # The same moments on the clock a UTC step can't move (24 November 2026, found in review): for the
            # supervisor's freshness check, on the same boot.
            "t_mono": round(mono, 3), "last_success_mono": st.last_success_mono,
            "elapsed_s": round(_elapsed(ctx, now_t), 1),
            "clock_steps": st.clock_steps, "clock_offset_s": round(st.clock_offset, 3),
            "samples_in": int(ring.write), "windows": st.windows,
            "hits": st.hits, "dropped": int(st.dropped),
            "startup_dropped": int(getattr(st, "startup_dropped", 0)),
            "overflow": int(st.overflow), "centre_hz": ctx.centre * 1e6,
            "windows_started": st.windows_started, "last_success_utc": st.last_success_utc,
            "worker_alive": st.worker_alive, "stage": st.stage,
            "callback_errors": st.callback_errors, "event_errors": st.event_errors,
            "confirm_attempts": st.confirm_attempts, "confirm_errors": st.confirm_errors,
            "unchecked": st.unchecked, "unconfirmable": st.unconfirmable,
            "unchecked_own_burst": st.unchecked_own_burst,
            "weak_matches": st.weak_matches,
            "call_matches": st.call_matches,
            "whole_band_windows": st.whole_band_windows,
            "confirm_workers": st.confirm_workers,
            "confirmed": st.confirmed,
            "partial_confirmations": st.partial_confirmations,
            "coverage_notice_policy": "status and summary only; no coverage phone alerts",
            "refinement_failures": st.refinement_failures,
            "nub_training_matches": st.nub_training_matches,
            "nub_tail_rejections": st.nub_tail_rejections,
            "window_errors": st.window_errors, "notify_errors": st.notify_errors,
            "glitch_blocks": st.glitches, "telemetry_errors": st.telemetry_errors,
            "last_telemetry_error": st.last_telemetry_error,
            "heartbeat_errors": st.heartbeat_errors, "last_heartbeat_error": st.last_heartbeat_error,
            "telemetry_seconds": round(st.last_telemetry_seconds, 5),
            "window_seconds": round(st.last_window_seconds, 4),
            "max_window_seconds": round(st.max_window_seconds, 4),
            "session": getattr(ctx, "outdir", None),
            "source_sha256": provenance()["source_sha256"],
            "recorder": ctx.recorder.snapshot() if getattr(ctx, "recorder", None) else None}
    data["notifications"] = ctx.notifier.snapshot() if getattr(ctx, "notifier", None) else None
    data['rf_health'] = getattr(ctx, 'rf_health', None)
    data['host_health'] = ctx.host_sampler.latest if getattr(ctx, 'host_sampler', None) else None
    data['sample_rate_hz'] = getattr(ctx, 'fs', None)
    data['gain_indices'] = getattr(ctx, 'gain', None)
    # The receiver's own counters (an RSP's overloads, gaps, ...); the Airspy has none, so its heartbeat is unchanged.
    receiver_status = getattr(ctx, "receiver_status", None)
    if receiver_status is not None:
        try:
            status = receiver_status()
        except Exception as exc:              # noqa: BLE001 - a counter must not stop the liveness write
            status = {"error": f"{type(exc).__name__}: {exc}"[:200]}
        if status:
            data["receiver"] = status
    data['threshold_db'] = getattr(ctx, 'threshold_db', None)
    data['analysis_backlog_s'] = (max(0, ring.write-getattr(st, 'last_sample_end', 0))/ctx.fs
                                  if getattr(ctx, 'fs', None) else None)
    data['quiet_windows'] = int(getattr(st, 'quiet_windows', 0))
    data['quiet_input_alarm_windows'] = QUIET_INPUT_WINDOWS
    data['window_s'] = WINDOW_S
    data["degraded_reasons"] = degraded_reasons(st, data["recorder"], data["host_health"])
    data["degraded"] = bool(data["degraded_reasons"])
    if st.last_success_mono is not None:
        fresh = 0 <= mono - st.last_success_mono < 10
    else:
        fresh = st.last_success_utc is not None and 0 <= now_t - st.last_success_utc < 10
    data["ready"] = bool(st.worker_alive and fresh)
    try:
        recording.atomic_json(path, data, durable=False)
        st.last_heartbeat_error = None
    except (OSError, ValueError) as exc:
        st.heartbeat_errors += 1
        st.last_heartbeat_error = f'{type(exc).__name__}: {exc}'[:240]
        if st.heartbeat_errors == 1:
            print('Heartbeat write failed: '+st.last_heartbeat_error, flush=True)
    return data


def free_bytes(path):
    """Free space on the filesystem holding `path`, or None if unknowable."""
    try:
        st = os.statvfs(path)                      # POSIX
        return st.f_bavail * st.f_frsize
    except AttributeError:
        try:
            import ctypes
            free = ctypes.c_ulonglong(0)
            ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(str(path)), None, None, ctypes.byref(free))
            # A FAILED query means unknown, not zero. Returning 0 here
            # reads as 'disk full' and would suspend snippets for the
            # whole run - found by stress_test part G.
            return free.value if ok else None
        except Exception:                          # noqa: BLE001
            return None
    except OSError:
        return None


def _iq_from_int16(seg16):
    return (seg16[0::2].astype(np.float32)
            + 1j * seg16[1::2].astype(np.float32)).astype(np.complex64)


def confirm_hit(iq, fs, centre_hz, h, min_sig=None):
    """Compatibility entry point; physical-layer recognition is mandatory."""
    import confirmation
    try:
        confirmation.confirm(iq, fs, centre_hz, h)
    except Exception as exc:
        h.update(confirmed=None, confirm={"error": str(exc)[:200]})
    return h["confirmed"]


def process_window(iq, ring, read_end, w_now, ctx, t_abs_s=0.0):
    from pipeline import process_window as process
    return process(iq, ring, read_end, w_now, ctx, t_abs_s)


def analysis_worker(ring, ctx):
    """Wrapper whose `finally` records that analysis has stopped.

    ENTRY 47a. Without this the thread could die and every watchdog still
    reported healthy, because they all watched `ring.write`, which the USB
    callback advances regardless.
    """
    try:
        _analysis_worker(ring, ctx)
    except Exception as e:                          # noqa: BLE001
        print(f"  ANALYSIS WORKER DIED: {type(e).__name__}: {e}", flush=True)
    finally:
        ctx.stats.worker_alive = False


def _analysis_worker(ring, ctx):
    """Consume windows from the ring, strictly behind the producer's write.

    Each window holds `lead` samples from before `read` (the overlap) and `want` new ones; `read`
    moves on by `want`. The first window has no overlap to look back on.
    """
    want = ctx.want_samples
    lead = int(round(getattr(ctx, "overlap_s", 0.0) * ctx.fs))
    read = ring.write + lead                # skip whatever pre-roll exists
    first = True
    prior_dropped = 0
    last_report = 0
    max_lag = ring.cap - 2 * want           # stay this far from the write head
    while not _stop.is_set():
        w = ring.write
        avail = w - read
        if avail < want:
            time.sleep(0.02)
            continue
        if avail > max_lag:                 # analysis fell behind - skip stale
            skip = avail - max_lag
            read += skip
            ctx.stats.overflow += 1
            if getattr(ctx, "history", None):
                ctx.history = BurstHistory()
        gaps = list(ctx.stats.discontinuities)
        prior_dropped = dropped_before(gaps, ctx.stats.dropped_retired, read, prior_dropped)
        crossed_gap = any(read - lead < pos < read + want for pos, _ in gaps)
        try:
            seg16 = ring.extract(read - lead, want + lead)
        except BufferError:
            ctx.stats.overflow += 1
            read = max(read, ring.write - want)
            ctx.history = BurstHistory()
            first = True
            continue
        # The bursts this window owns: those starting in its new part (see WINDOW_OVERLAP_S).
        # Relative to the window's start: [margin, new length + margin); the first window from 0.
        ctx.own_s = ((0.0 if first else OWN_MARGIN_S), want / ctx.fs + OWN_MARGIN_S) if lead else None
        first = False
        t_abs = (read - lead + prior_dropped) / float(ctx.fs)
        ctx.window_start_utc = ((ctx.stats.capture_start_utc or ctx.t_start)
                                + ctx.stats.clock_offset + t_abs)
        read += want
        if crossed_gap:
            ctx.stats.discontinuous_windows += 1
            ctx.history = BurstHistory()
            from pipeline import emit
            emit(ctx, {"schema_version": 2, "event_type": "input_discontinuity",
                       "sample_stop": read, "dropped_samples": int(ctx.stats.dropped),
                       "utc": ctx.window_start_utc})
            first = True
            continue
        # ENTRY 47a: one bad window must cost one window, not the whole run.
        try:
            process_window(_iq_from_int16(seg16), ring, read, ring.write, ctx,
                           t_abs_s=t_abs)
        except Exception as e:                      # noqa: BLE001
            ctx.stats.window_errors += 1
            if ctx.stats.window_errors % 10 == 1:
                print(f"  WARN window failed ({type(e).__name__}: {e}) - "
                      f"{ctx.stats.window_errors} so far", flush=True)

        st = ctx.stats
        el = _elapsed(ctx, time.time())
        if st.dropped and not st._drop_warned:
            st._drop_warned = True
            if ctx.verbose:
                print(f"  {dt.datetime.now().strftime('%H:%M:%S')}  WARN host began "
                      f"dropping samples at {el/60:.1f} min "
                      f"(USB/device layer)", flush=True)
        if ctx.verbose and st.windows and st.windows != last_report \
                and st.windows % 20 == 0:
            last_report = st.windows
            host = diagnostics.host_health()
            extra = ""
            if host.get("cpu_temp_c") is not None:
                extra += f", {host['cpu_temp_c']:.0f}C"
                if host.get("temp_warn"):
                    extra += " **TEMP**"
            p = host.get("power") or {}
            if p.get("undervoltage_now"):
                extra += " **UNDERVOLT**"
            elif p.get("throttled_now"):
                extra += " **THROTTLED**"
            rate = (st.dropped - st._last_dropped) / max(el - st._last_el, 1e-3)
            st._last_dropped, st._last_el = st.dropped, el
            if st.dropped:
                extra += f", drop {rate/1e3:.0f} k/s"
            print(f"  ... {el/60:6.1f} min, {st.windows:5d} windows "
                  f"({st.windows*WINDOW_S/60:.0f} min signal), "
                  f"{st.confirmed}/{st.hits} confirmed hit(s), "
                  f"{st.overflow} overflow, {st.dropped} dropped{extra}", flush=True)


READY_WAIT_S = 30.0              # at power-up, how long a receiver may take to appear on USB


def wait_for_receiver(kind, limit=READY_WAIT_S, present=None, clock=time.monotonic, sleep=time.sleep):
    """Wait until every device this receiver needs is on USB, up to `limit` seconds; returns the seconds waited.

    At a car's power-up a USB device can appear seconds late (our 4G stick needed four tries on 17 November 2026),
    and the detector gets one attempt per power-up, so opening too early cost the whole drive (found in review,
    26 November 2026). This only reads the USB list, which opens nothing (rule 3). When the list can't be read (not
    Linux), or the time runs out, the receiver is opened as before and says what it found."""
    present = present or receivers.present
    need = receivers.KINDS[kind].get("devices", 1)
    start = clock()
    while True:
        count = present(kind)
        if count is None or count >= need or clock() - start >= limit or _stop.is_set():
            return clock() - start
        sleep(0.5)


def main():
    ap = argparse.ArgumentParser(description="Continuous receive-only TETRA physical-layer monitor")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--band", choices=sorted(BANDS), default="uplink")
    g.add_argument("--freq", type=float, help="centre MHz")
    duration_args = ap.add_mutually_exclusive_group()
    duration_args.add_argument("--hours", type=float)
    duration_args.add_argument("--minutes", type=float)
    ap.add_argument("--rate", type=float, default=6.0, choices=(3.0, 6.0))
    ap.add_argument("--receiver", choices=sorted(receivers.KINDS),
                    help="which receiver; by default the one setup.sh chose (receiver.py --show), else the Airspy Mini")
    ap.add_argument("--lna", type=int, default=12, choices=range(15), help="Airspy: LNA gain")
    ap.add_argument("--mixer", type=int, default=12, choices=range(16), help="Airspy: mixer gain")
    ap.add_argument("--vga", type=int, default=12, choices=range(16), help="Airspy: VGA gain")
    ap.add_argument("--lna-state", type=int, choices=range(10), help="RSP1A/RSP1B: LNA state, 0 is the most gain")
    ap.add_argument("--if-gain-reduction", type=int, choices=range(20, 60), metavar="20-59",
                    help="RSP1A/RSP1B: IF gain reduction in dB")
    ap.add_argument("--sdrplay-transfer", choices=("isoch", "bulk"), default="isoch",
                    help="RSP1A/RSP1B: USB transfer mode (the API's default is isoch)")
    ap.add_argument("--rtl-gain", type=int, choices=range(0, 501), metavar="0-500",
                    help="RTL-SDR pair: tuner gain in tenths of a dB (each stick uses the nearest it offers)")
    ap.add_argument("--receiver-wait", type=float, default=0.0, metavar="SECONDS",
                    help="wait up to this long for the receiver to appear on USB before opening it (supervise.py "
                         "asks for 30 at power-up); reading the USB list opens nothing")
    ap.add_argument("--sensitivity", choices=sorted(SENSITIVITY), default="highway")
    ap.add_argument("--threshold-db", type=float)
    ap.add_argument("--save-power", action="store_true")
    ap.add_argument("--no-snippets", action="store_true")
    ap.add_argument("--log-candidates", action="store_true",
                    help="also print every unconfirmed candidate to the log (they are all in events.jsonl); "
                         "matches are always printed")
    ap.add_argument("--no-confirm", action="store_true", help="candidate logging only; no RF alerts")
    ap.add_argument("--check-threads", type=int, default=0, metavar="N",
                    help="threads that check candidates at once; 0 (default) chooses from the number of cores, "
                         "leaving one free. Also settable as SLOT17_CHECK_THREADS")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--tag", default="watch")
    ap.add_argument("--heartbeat", metavar="PATH")
    ap.add_argument("--stall-timeout", type=float, default=8.0)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    import math
    import re
    from instance import acquire
    centre = args.freq if args.freq is not None else BANDS[args.band]
    threshold = args.threshold_db if args.threshold_db is not None else SENSITIVITY[args.sensitivity]
    duration = args.hours * 3600 if args.hours is not None else args.minutes * 60 if args.minutes is not None else None
    if not math.isfinite(centre) or not 24 <= centre <= 1800:
        ap.error("centre must be finite and within 24..1800 MHz")
    if not math.isfinite(threshold) or not 0 < threshold <= 60:
        ap.error("threshold must be finite and in (0, 60] dB")
    if duration is not None and (not math.isfinite(duration) or duration <= 0):
        ap.error("duration must be finite and positive")
    if not math.isfinite(args.stall_timeout) or args.stall_timeout < 3:
        ap.error("stall timeout must be finite and at least 3 seconds")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.tag):
        ap.error("tag must contain 1..64 letters, digits, hyphens or underscores")
    runtime = os.path.join(paths.ROOT, "runtime")
    try:
        instance_lock = acquire(os.path.join(runtime, "watch.lock"))
    except RuntimeError as exc:
        print(exc, flush=True)
        return 1
    fs = int(args.rate * 1e6)
    # The receiver setup.sh chose (receiver.py), loaded where libairspy always was: before the session starts.
    try:
        kind = args.receiver or receivers.configured()
        gain = receivers.gain_for(kind)
        sdrplay = kind in ("rsp1b", "rsp1a")
        if kind == "airspy":
            gain.update(lna=args.lna, mixer=args.mixer, vga=args.vga)
        elif sdrplay:
            gain.update({k: v for k, v in (("lna_state", args.lna_state),
                                           ("if_gain_reduction_db", args.if_gain_reduction)) if v is not None})
        elif kind == "rtlsdr2" and args.rtl_gain is not None:
            gain["tuner_gain_db10"] = args.rtl_gain
        backend = receivers.backend(kind)
        extra = {"transfer": args.sdrplay_transfer} if sdrplay else {}
        rx = backend.Receiver(kind, fs=fs, centre_hz=centre * 1e6, gain=gain, **extra)
    except receivers.ReceiverError as exc:
        print(f"Receiver: {exc}", flush=True)
        instance_lock.close()
        return failure.RECEIVER_UNAVAILABLE
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S-%fZ")
    outdir = os.path.join(paths.CAPTURES, f"{stamp}_{args.tag}_{centre:g}MHz")
    recorder = recording.Recorder(outdir).start()
    stats = Stats()
    stats.fs = fs
    ring = Ring(RING_CAP_S * fs)
    ctx = Ctx(fs=fs, centre=centre, dl=load_downlink_channels(), threshold_db=threshold,
              want_samples=int(WINDOW_S * fs), hop_s=WINDOW_S, overlap_s=WINDOW_OVERLAP_S, outdir=outdir,
              save_snippets=not args.no_snippets, save_power=args.save_power,
              log_candidates=args.log_candidates,
              recorder=recorder, trend=TrendTracker(), history=BurstHistory(),
              gain=rx.gain, stats=stats,
              events=deque(maxlen=256), verbose=True, confirm=not args.no_confirm,
              t_start=time.time(), t_start_mono=time.monotonic(), notifier=None,
              confirm_workers=args.check_threads)
    import pipeline as _pipeline
    print(f"  checking candidates on {_pipeline.confirm_workers(ctx)} thread(s); this computer offers "
          f"{_pipeline.cores()} core(s)" + ("" if args.check_threads else " (chosen automatically)"), flush=True)
    started_mono = time.monotonic()
    heartbeat = args.heartbeat or os.path.join(runtime, "watch.heartbeat")
    os.makedirs(os.path.dirname(os.path.abspath(heartbeat)), exist_ok=True)
    session = {"schema_version": 2, "utc_start": ctx.t_start, "software": provenance(),
               "settings": vars(args), "centre_hz": centre * 1e6, "sample_rate_hz": fs,
               "threshold_db": threshold, "gain": rx.gain_record(), **rx.session_fields(),
               "receiver": {"kind": kind, "name": receivers.name(kind)},
               "classification": "TETRA physical layer; vehicle identity and distance unknown"}
    recording.atomic_json(os.path.join(outdir, 'session.json'), session)

    def write_summary(final, recorder_drained=None):
        recording.atomic_json(
            os.path.join(outdir, 'summary.json'),
            build_summary(final, stats=stats, ring=ring, ctx=ctx, recorder=recorder,
                          started_mono=started_mono, receiver=rx.info, failed=failed,
                          failure_reason=failure_reason, recorder_drained=recorder_drained))

    ctx.host_sampler = diagnostics.HostSampler(recorder).start()
    from marking import Marks
    marks = Marks(ctx, ring)
    print(f"Watching {centre:g} MHz at {args.rate:g} MSPS, threshold {threshold:g} dB")
    print(f"Session: {outdir}")
    print(f"Receiver: {receivers.name(kind)}")
    print("RF alerts require TETRA training recognition; vehicle type and distance are unknown.", flush=True)
    if not args.no_notify and notify.configured():
        ctx.notifier = notify.Notifier(log=lambda m: print(m, flush=True))
        if not ctx.notifier.start():
            ctx.notifier = None
    _stop.clear()
    signal.signal(signal.SIGINT, lambda *_: _stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    worker = threading.Thread(target=analysis_worker, args=(ring, ctx), name="analysis", daemon=True)
    rx.bind(Sink(ring, stats))
    ctx.receiver_status = rx.status
    failed = False
    failure_exit = 0
    failure_reason = None
    ready_announced = False
    try:
        waited = wait_for_receiver(kind, limit=args.receiver_wait) if args.receiver_wait > 0 else 0.0
        if waited >= 1.0:
            print(f"Receiver: on USB after {waited:.1f} s", flush=True)
        rx.open()
        rx.start()
        # Discard pre-tune samples before the worker starts.
        worker.start()
        last_in, last_windows = ring.write, 0
        last_advance = last_window = time.monotonic()
        last_hb = last_summary = 0.0
        while not _stop.is_set():
            now = time.monotonic()
            if duration is not None and now - started_mono >= duration:
                break
            if not stats.worker_alive or not worker.is_alive():
                raise failure.AnalysisFailure("analysis worker stopped")
            if stats.windows != last_windows:
                last_windows, last_window = stats.windows, now
                if not ready_announced:
                    ready_announced = True
                    print("READY: first analysis window completed", flush=True)
                    if ctx.notifier:
                        ctx.notifier.post(
                            f"Monitoring {centre:g} MHz. First analysis window completed.",
                            title="TETRA monitor ready",
                            priority=notify.PRIORITY_INFO, ttl=notify.STATUS_TTL_S)
            if now - last_window > args.stall_timeout * ANALYSIS_STALL_FACTOR:
                raise failure.AnalysisFailure("no analysis window completed within watchdog deadline")
            w = ring.write
            if w != last_in:
                last_in, last_advance = w, now
            elif now - last_advance > args.stall_timeout:
                raise failure.StreamStall("receiver sample stream stalled")
            fault = rx.fault()
            if fault:
                raise failure.StreamStall(fault)
            marks.poll()
            if now - last_hb >= 1:
                last_hb = now
                check_clock_step(ctx)
                if ctx.notifier:
                    stats.notify_problem = ctx.notifier.problem()
                try:
                    beat = write_heartbeat(heartbeat, ctx, ring, time.time())
                except OSError as exc:
                    note_metadata_error(stats, "heartbeat", exc)
                else:
                    # Empty as well: a steady fault that has ended must be able to report a new episode.
                    if ctx.notifier:
                        ctx.notifier.alert_health(beat["degraded_reasons"])
            if now - last_summary >= SUMMARY_INTERVAL_S:
                last_summary = now
                try:
                    write_summary(final=False)
                except OSError as exc:
                    note_metadata_error(stats, "summary", exc)
            time.sleep(0.1)
    except Exception as exc:
        failed = True
        failure_exit = failure.exit_code(exc, receiver_opened=rx.opened)
        stats.failure_category = failure.outcome(failure_exit)
        failure_reason = f'{type(exc).__name__}: {exc}'[:400]
        recorder.event({'schema_version': 1, 'event_type': 'runtime_failure',
                        'utc': time.time(), 'reason': failure_reason, 'stage': stats.stage,
                        'failure_category': failure.outcome(failure_exit), 'exit_code': failure_exit})
        print(f"Detector failed: {type(exc).__name__}: {exc}", flush=True)
    finally:
        _stop.set()
        if worker.ident is not None:
            worker.join(timeout=8)
        if worker.is_alive():
            failed = True
            if not failure_exit:
                failure_exit = failure.ANALYSIS_FAILURE
                stats.failure_category = failure.outcome(failure_exit)
                failure_reason = 'analysis worker did not stop within deadline'
            print("Analysis did not stop within deadline", flush=True)
        stats.worker_alive = False
        stats.stage = "failed" if failed else "stopped"
        rx.close()
        marks.finish()
        if ctx.notifier:
            ctx.notifier.stop(timeout=15)
        ctx.host_sampler.stop()
        recorder_drained = recorder.stop(timeout=15)
        if not recorder_drained:
            print("Recorder did not finish: some events or recordings may be missing", flush=True)
        # Each on its own: the lock must be released whatever happens to these files.
        for what, write in (("heartbeat", lambda: write_heartbeat(heartbeat, ctx, ring, time.time())),
                            ("summary", lambda: write_summary(final=True, recorder_drained=recorder_drained))):
            try:
                write()
            except OSError as exc:
                note_metadata_error(stats, what, exc)
                print(f"Could not write the final {what}: {type(exc).__name__}: {exc}", flush=True)
        instance_lock.close()
    print(f"Completed {stats.windows} windows; {stats.confirmed}/{stats.hits} candidates recognised.")
    return failure_exit if failed else 0


def selftest():
    import unittest
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    suite = unittest.TestSuite()
    for pattern in ("test_runtime.py", "test_detection.py"):
        suite.addTests(unittest.defaultTestLoader.discover(os.path.join(root, "tests"), pattern=pattern))
    return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
