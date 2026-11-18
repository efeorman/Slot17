"""One analysis window: candidates, fair short-burst confirmation and telemetry."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import os
import time

import numpy as np

import confirmation
import diagnostics
import rfhealth
from channelize import channelise
from detect import detect
import detect as detect_mod

MAX_CONFIRM_SECONDS = 2.0
# A window with this many candidates is a whole-band interference burst: real traffic never puts
# 60 of the 193 channels over the threshold at once. Counted so the phone can show how often it happens.
WHOLE_BAND_CANDIDATES = 60
# Candidates are examined on several threads at once, one candidate per thread. The numerical work
# releases Python's lock, so on the four-core Pi 5 three threads examine 2.2 times as much in the same
# time as one, and four 2.4 times (measured on saved field windows, 13 November 2026, with identical
# decisions). After dropped samples the work goes back to one thread: reception matters more than
# checking interference faster. See DROP_HOLD_S for how long.
#
# How many: 0 means choose from this computer's cores, leaving one free for the receiver's callback
# (1 core: 1 thread, 2 cores: 1, 4 cores: 3), and never more than MAX_AUTO_WORKERS, because the gain
# flattens and more has not been measured. Set it yourself with `--check-threads N` on watch.py or
# the environment variable SLOT17_CHECK_THREADS; that is not capped.
CONFIRM_WORKERS = 0
MAX_AUTO_WORKERS = 4
MAX_WORKERS = 32
_pools = {}


def cores():
    """Cores this process may use, which can be fewer than the computer has."""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):       # not on Linux
        return max(1, os.cpu_count() or 1)


def default_workers():
    """The chosen number of checking threads: the environment's, else from the core count."""
    text = os.environ.get("SLOT17_CHECK_THREADS", "").strip()
    if text.isdigit() and 0 < int(text) <= MAX_WORKERS:
        return int(text)
    return max(1, min(MAX_AUTO_WORKERS, cores() - 1))


def _pool(workers):
    if workers not in _pools:
        _pools[workers] = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="confirm")
    return _pools[workers]


def _examine(iq, ctx, h, region, deadline):
    """One region of one candidate. None when the deadline came first; an exception is returned, not raised."""
    if time.monotonic() >= deadline:
        return None
    try:
        return confirmation.confirm(iq, ctx.fs, ctx.centre * 1e6, h, selected_index=region)
    except Exception as exc:                # noqa: BLE001 - one bad region must cost one candidate
        return exc


# How long checking stays on one thread after the receiver dropped samples (outside the start-up
# grace of watch.STARTUP_GRACE_S, since 1.2.5). Until 14 November 2026
# that was the rest of the session. The losses seen since then all came in a session's first three
# minutes, while a 4G stick on the receiver's USB controller was starting (4, 14 and 18 buffers, then
# none), and one such drive spent its remaining 50 minutes on one thread with 57 % of its candidates
# never reached. So: one thread until this long has passed without further loss, then the chosen
# number again, once. Loss after that return keeps one thread for the rest of the session, because
# the threads may then be the cause.
DROP_HOLD_S = 300.0


def confirm_workers(ctx):
    st = ctx.stats
    wanted = int(getattr(ctx, "confirm_workers", CONFIRM_WORKERS) or 0)
    wanted = min(MAX_WORKERS, wanted) if wanted > 0 else default_workers()
    # Losses in the stream's first minute (watch.STARTUP_GRACE_S) leave the threads alone: they came
    # from a 4G stick starting on the receiver's USB controller, not from the checking threads.
    dropped = max(0, int(getattr(st, "dropped", 0) or 0) - int(getattr(st, "startup_dropped", 0) or 0))
    if not dropped:
        return wanted
    now = time.monotonic()
    if dropped != getattr(st, "drop_noted", 0):
        st.drop_noted, st.drop_noted_mono = dropped, now
        if getattr(st, "threads_resumed", False) and not getattr(st, "threads_given_up", False):
            st.threads_given_up = True
            if wanted > 1 and getattr(ctx, "verbose", False):
                print("  samples dropped again after the checking threads came back: "
                      "one thread for the rest of this session", flush=True)
    noted = getattr(st, "drop_noted_mono", None)
    if getattr(st, "threads_given_up", False) or noted is None or now - noted < DROP_HOLD_S:
        return 1
    if not getattr(st, "threads_resumed", False):
        st.threads_resumed = True
        if wanted > 1 and getattr(ctx, "verbose", False):
            print(f"  no samples dropped for {DROP_HOLD_S:.0f} s: checking candidates on {wanted} thread(s) again",
                  flush=True)
    return wanted
# Confirmation may use at most this share of a window's own length, so the per-second budget
# does not grow when windows get shorter (3 s windows: 2.0 s, exactly as before).
CONFIRM_SHARE = 2.0 / 3.0

# A channel recognised this recently is checked first, flagged as a broadband impulse or not: a radio
# that just transmitted is the likeliest to transmit again, and its next burst is the one most worth
# not missing.
#
# Candidates flagged as a broadband impulse are otherwise checked last, within the window's own
# budget only. Letting them be checked after the budget ("catch-up", 1.2.2 and 1.2.3, 15 November
# 2026) was removed before it ran on a drive: on the ten interference windows saved that day,
# checking every candidate with no time limit recognised nothing more, and it delayed the windows
# after an impulse by up to 2 s.
RECENT_MATCH_S = 60.0


def emit(ctx, event):
    writer = getattr(ctx, "recorder", None)
    if writer is not None:
        if not writer.event(event):
            ctx.stats.event_errors += 1
    elif getattr(ctx, "ev_fh", None) is not None:  # offline compatibility only
        try:
            import json
            ctx.ev_fh.write(json.dumps(event, allow_nan=False) + "\n")
            ctx.ev_fh.flush()
        except Exception:
            ctx.stats.event_errors += 1
    if not isinstance(ctx.events, deque):
        ctx.events = deque(ctx.events, maxlen=256)
    ctx.events.append(event)


def evaluate_candidates(iq, hits, ctx, deadline, window, on_final=None):
    """Give each candidate one region before spending more time on any one.

    A sampled rejection is not evidence that unexamined regions contain no TETRA.
    Record coverage explicitly. Confirmed candidates need no further regions.

    `on_final(hit, recording)` is called the moment a candidate is recognised, so its event can be
    written before the window's other candidates are checked (14 November 2026: in a busy window a
    match waited up to the whole checking time for unrelated candidates). Every other candidate is
    finished at the end, as before.
    """
    state = {}
    st = ctx.stats
    for h in hits:
        regions = confirmation.intervals(h, ctx.fs, len(iq))
        # Regions away from a broadband moment come first within a flagged candidate.
        shared = set(h.get("coincident_burst_starts_s") or ()) if h.get("coincident_channels") else set()
        starts = [a for a, _ in confirmation.burst_pairs(h)]
        order = sorted(range(len(regions)), key=lambda i: starts[i] in shared)
        clean = sum(starts[i] not in shared for i in order)
        state[id(h)] = {"parts": [], "available": len(regions), "reports": [], "order": order, "clean": clean}
        # An envelope longer than four slots yields no region at all. That is a
        # different fact from "the deadline arrived first", and neither one is a
        # completed inspection; say which, so `null` is never read as a rejection.
        reason = ("no burst region within four slots" if not regions
                  else "broadband impulse: deprioritised; window processing deadline"
                  if h.get("coincident_channels")
                  else "window processing deadline")
        h.update(confirmed=None, confirm={"skipped": reason})
    if getattr(ctx, "confirm", True):
        # Three passes. A hit flagged as a broadband impulse gets budget only after
        # every other candidate has had its regions. impulse.py says this budget
        # must not be spent on interference first, and detect.py's comment says
        # watch.py can refuse it - but nothing acted on the flag, so during an
        # in-car noise storm the 2 s was spread over ~180 noise channels and
        # genuine bursts went unexamined (2026-09-09: coverage fell to 33 %, and
        # 70 % of candidates carried the flag).
        #
        # Within the flagged hits, bursts at a moment of their own go before the
        # bursts inside the broadband moment (11 November 2026): on 3 and 4 November
        # 680 of 2002 and 362 of 4636 unchecked candidates held such a burst, and it
        # waited behind about 180 channels' worth of the impulse itself.
        #
        # Deliberately a priority and NOT a rejection. Uplink bursts are
        # slot-synchronised to the network, so several real radios on separated
        # channels can legitimately key up in the same instant and trip the
        # coincidence test - test_coincident_channels_are_not_hard_rejected
        # exists for exactly that case. Leftover budget still inspects them.
        workers = confirm_workers(ctx)
        st.confirm_workers = workers
        for tier in (0, 1, 2):
            for step in range(confirmation.MAX_BURSTS):
                # One round: at most one region of each candidate that is due. The candidates of a
                # round are independent of each other, so they can be examined at the same time; the
                # order of the rounds, which is the priority, stays exactly as it was.
                batch = []
                for h in hits:
                    row = state[id(h)]
                    flagged = bool(h.get("coincident_channels"))
                    if flagged is not (tier > 0):
                        continue
                    position = step + (row['clean'] if tier == 2 else 0)
                    limit = row['clean'] if tier == 1 else row['available']
                    if h.get("confirmed") or row.get("error") or position >= limit:
                        continue
                    batch.append((h, row, position, row['order'][position]))
                if not batch or time.monotonic() >= deadline:
                    continue
                if workers == 1 or len(batch) == 1:
                    results = [_examine(iq, ctx, h, region, deadline) for h, _, _, region in batch]
                else:
                    results = list(_pool(workers).map(lambda item: _examine(iq, ctx, item[0], item[3], deadline), batch))
                for (h, row, position, _), result in zip(batch, results):
                    if result is None:
                        continue                # the deadline came before this one was started
                    ctx.last_confirmed_attempt[h['tetra_channel']] = window
                    st.confirm_attempts += 1
                    if isinstance(result, Exception):
                        h.update(confirmed=None, confirm={"error": f"{type(result).__name__}: {result}"[:200]})
                        row['error'] = True
                        st.confirm_errors += 1
                        continue
                    row['parts'].append(result)
                    row['reports'].append(h['confirm'])
                    row['clean_tested'] = row.get('clean_tested', 0) + (position < row['clean'])
                    for report in h['confirm'].get('phy', []):
                        st.nub_training_matches += report.get('nub_training_matches', 0)
                        st.nub_tail_rejections += report.get('nub_tail_rejections', 0)
                    if h.get('confirmed') and on_final is not None and not row.get('final'):
                        row['final'] = True
                        on_final(h, _finish_candidate(h, row, ctx))
    recordings = {}
    for h in hits:
        row = state[id(h)]
        if row.get('final'):
            continue
        recording = _finish_candidate(h, row, ctx)
        if recording is not None:
            recordings[id(h)] = recording
    return recordings


def _finish_candidate(h, row, ctx):
    """Coverage, counters and the narrowband recording of one candidate. Returns (samples, metadata) or None."""
    st = ctx.stats
    parts = row['parts']
    tested = len(parts)
    # `complete` claims the decision was finished. Zero regions tested out of
    # zero available is not a finished decision, so it must not satisfy it.
    h['confirmation_coverage'] = {"regions_tested": tested, "regions_available": row['available'],
                                  "complete": bool(h.get('confirmed'))
                                  or (row['available'] > 0 and tested == row['available'])}
    # The count that matters: a burst at a moment of its own that nobody looked at. The
    # rest of `unchecked` is the broadband moment itself, seen on about 180 channels.
    if (row['clean'] and not row.get('clean_tested') and not h.get('confirmed')
            and not row.get('error') and getattr(ctx, 'confirm', True)):
        st.unchecked_own_burst = getattr(st, 'unchecked_own_burst', 0) + 1
        h['confirmation_coverage']['own_burst_unchecked'] = True
    if not parts:
        st.unchecked += 1
        if not row['available']:
            st.unconfirmable += 1
        if not getattr(ctx, 'confirm', True):
            h['confirm'] = {"skipped": "confirmation disabled"}
        return None
    if not h['confirmation_coverage']['complete']:
        st.partial_confirmations += 1
    metadata = dict(parts[0][1])
    metadata['segments'] = []
    offset = 0
    for samples, meta in parts:
        metadata['segments'].extend({**seg, 'offset': seg['offset'] + offset} for seg in meta['segments'])
        offset += len(samples)
    metadata['confirmation_coverage'] = h['confirmation_coverage']
    if not row.get('error'):
        h['confirm'] = {"method": "short-burst-phy-v2", "accepted": bool(h.get('confirmed')),
                        "seconds": round(sum(r['seconds'] for r in row['reports']), 4),
                        "segments": len(metadata['segments']),
                        "phy": [e for r in row['reports'] for e in r['phy']]}
    return np.concatenate([part[0] for part in parts]), metadata


def process_window(iq, ring, read_end, w_now, ctx, t_abs_s=0.0):
    began = time.monotonic()
    fs = ctx.fs
    st = ctx.stats
    st.windows_started += 1
    window = st.windows_started
    sample_start = read_end - len(iq)
    utc_start = getattr(ctx, "window_start_utc", ctx.t_start + t_abs_s)
    window_id = f"w{window:08d}"
    st.stage = "channelisation"
    sig = diagnostics.signal_health(iq[::32], fs / 32, gain=getattr(ctx, "gain", None))
    if sig["glitch_block"]:
        st.glitches += 1
        raise ValueError("corrupt IQ block")
    # Count the run of windows the receiver spent hearing nothing. Only True
    # resets or extends it: None means the check could not be made (unknown gain,
    # corrupt block) and must not be read as either a fault or an all-clear.
    # getattr(): offline callers and tests hand in their own stats objects.
    if sig.get("antenna_suspect") is True:
        st.quiet_windows = getattr(st, "quiet_windows", 0) + 1
        st.quiet_windows_max = max(getattr(st, "quiet_windows_max", 0), st.quiet_windows)
    elif sig.get("antenna_suspect") is False:
        st.quiet_windows = 0
    ch = channelise(iq, float(fs), ctx.centre * 1e6)
    # Energy candidates may contain ONE burst. Only physical-layer recognition
    # can promote these to an alert; frame repetition is supporting metadata.
    hits = detect(ch, threshold_db=ctx.threshold_db, downlink_channels=ctx.dl,
                  require_periodic=False, history=getattr(ctx, "history", None), t_abs_s=t_abs_s,
                  own=getattr(ctx, "own_s", None))
    if getattr(ctx, "history", None) is not None:
        ctx.history.prune(t_abs_s + ch.duration_s)
    if len(hits) >= WHOLE_BAND_CANDIDATES:
        st.whole_band_windows = getattr(st, "whole_band_windows", 0) + 1
    channel_seconds = time.monotonic() - began
    writer = getattr(ctx, "recorder", None)
    if getattr(ctx, "save_power", False) and writer:
        db = 10 * np.log10(ch.power + 1e-30)
        baseline = float(np.median(db))
        # With overlapping windows only the new part is kept, so the saved power still tiles the
        # session once, as the replay tools expect.
        lead = int(round(getattr(ctx, "overlap_s", 0.0) / ch.frame_s)) if getattr(ctx, "overlap_s", 0.0) else 0
        lead = min(lead, db.shape[1] - 1) if db.shape[1] else 0
        values = np.clip(np.round(db[:, lead:] - baseline), -128, 127).astype(np.int8)
        state = writer.samples(window_id + ".power", values,
                               {"format": "int8_db", "shape": list(values.shape), "baseline_db": baseline,
                                "channel_hz": ch.channel_hz.tolist(), "frame_s": ch.frame_s,
                                "window_id": window_id, "sample_start": sample_start + int(round(lead * ch.frame_s * fs)),
                                "utc_start": utc_start + lead * ch.frame_s, "skipped_overlap_frames": lead})
        if state != "queued":
            writer.event({"schema_version": 1, "event_type": "recording_refused", "name": window_id + ".power",
                          "state": state, "window_id": window_id})

    if not hasattr(ctx, "last_confirmed_attempt"):
        ctx.last_confirmed_attempt = {}
    if not hasattr(ctx, "last_match_s"):
        ctx.last_match_s = {}

    def recent(h):
        return t_abs_s - ctx.last_match_s.get(h["tetra_channel"], -1e30) <= RECENT_MATCH_S
    hits.sort(key=lambda h: (not recent(h), ctx.last_confirmed_attempt.get(h["tetra_channel"], -1),
                            bool(h.get("coincident_channels")), -h["peak_snr_db"]))
    # Real time is set by how far each window moves on, not by its length: overlapping windows
    # hold more samples than they add.
    span = getattr(ctx, "hop_s", None) or len(iq) / fs
    deadline = min(began + span * 0.9,
                   time.monotonic() + min(MAX_CONFIRM_SECONDS, CONFIRM_SHARE * span))
    st.stage = "confirmation"
    floors_cache = []

    def floors():
        if not floors_cache:
            try:
                floors_cache.append(ch.noise_floor())
            except Exception:
                # Optional attribution must not discard an already-confirmed window.
                floors_cache.append(None)
        return floors_cache[0]

    def finish(h, recording):
        if h.get('confirmed'):
            ctx.last_match_s[h['tetra_channel']] = t_abs_s
        _finish_event(h, recording, ch, floors() if h.get('confirmed') else None, ctx, writer,
                      window_id, sample_start, utc_start, t_abs_s)

    early = set()

    def on_final(h, recording):
        early.add(id(h))
        finish(h, recording)

    recordings = evaluate_candidates(iq, hits, ctx, deadline, window, on_final=on_final)
    for h in hits:
        if id(h) not in early:
            finish(h, recordings.get(id(h)))

    # One shared full-band window for every candidate not reached in time. This
    # closes the old late-hit snippet loss without repeating a 144 MB write.
    if writer and getattr(ctx, "save_snippets", False) and any(h.get("confirmed") is None or not h["confirmation_coverage"]["complete"] for h in hits):
        raw_allowed = time.monotonic() - getattr(ctx, "last_backlog_recording", -1e30) >= 30
        if not raw_allowed:
            emit(ctx, {"schema_version": 2, "event_type": "window_recording", "window_id": window_id,
                       "state": "rate_limited", "reason": "backlog IQ capped at one window per 30 seconds"})
        else:
            ctx.last_backlog_recording = time.monotonic()
        wide = np.empty(len(iq) * 2, dtype="<i2") if raw_allowed else None
        if raw_allowed:
            wide[0::2] = np.clip(iq.real, -32768, 32767).astype(np.int16)
            wide[1::2] = np.clip(iq.imag, -32768, 32767).astype(np.int16)
            name = window_id + ".iq"
            emit(ctx, {"schema_version": 2, "event_type": "window_recording", "window_id": window_id,
                       "name": name, "state": "requested"})
            state = writer.samples(name, wide, {"format": "int16_iq_le", "sample_rate_hz": fs,
                                               "centre_hz": ctx.centre * 1e6, "window_id": window_id,
                                               "sample_start": sample_start, "utc_start": utc_start})
            emit(ctx, {"schema_version": 2, "event_type": "window_recording", "window_id": window_id,
                       "name": name, "state": state})

    # Reuse the existing channelisation AFTER confirmation, so diagnostics
    # cannot consume its deadline. A telemetry error must not discard RF work.
    telemetry_start = time.monotonic()
    try:
        if not hasattr(ctx, 'rf_monitor'):
            ctx.rf_monitor = rfhealth.Monitor()
        reading = ctx.rf_monitor.update(ch, sig, hits, window_id=window_id, utc_start=utc_start,
                                        gain=getattr(ctx, 'gain', None), threshold_db=ctx.threshold_db,
                                        processing_seconds=telemetry_start-began,
                                        channel_seconds=channel_seconds)
        emit(ctx, reading)
        ctx.rf_health = rfhealth.compact(reading)
        st.last_telemetry_error = None
    except Exception as exc:
        st.telemetry_errors += 1
        st.last_telemetry_error = f'{type(exc).__name__}: {exc}'[:240]
        emit(ctx, {'schema_version': 1, 'event_type': 'telemetry_error',
                   'window_id': window_id, 'error': st.last_telemetry_error})
    st.last_telemetry_seconds = time.monotonic()-telemetry_start
    st.windows += 1  # completion, including confirmation and event submission
    st.last_success_utc = time.time()
    st.last_success_mono = time.monotonic()
    st.last_window_seconds = st.last_success_mono - began
    st.max_window_seconds = max(st.max_window_seconds, st.last_window_seconds)
    st.last_channel_seconds = channel_seconds
    st.last_sample_end = read_end
    st.stage = "waiting"



def _finish_event(h, recording, ch, floors, ctx, writer, window_id, sample_start, utc_start, t_abs_s):
    """Strength, time, notification and snippet for one candidate, then its event line."""
    st = ctx.stats
    fs = ctx.fs
    st.hits += 1
    event_s = attribute_match(h, ch, floors)
    if h.get('refinement_warning'):
        st.refinement_failures += 1
    stamp = utc_start + event_s
    h.update(schema_version=2, event_type="candidate", window_id=window_id,
             event_id=f"{window_id}-ch{h['tetra_channel']}",
             window_start_sample=sample_start, window_start_utc=utc_start,
             utc=dt.datetime.fromtimestamp(stamp, dt.timezone.utc).isoformat(timespec="milliseconds"),
             local_time=dt.datetime.fromtimestamp(stamp).strftime("%H:%M:%S"),
             elapsed_s=round(t_abs_s + event_s, 4), centre_hz=ctx.centre * 1e6,
             burst_mono=((ctx.stats.capture_start_mono + t_abs_s + event_s)
                         if getattr(ctx.stats, "capture_start_mono", None) is not None else None),
             sample_rate_hz=fs, timestamp_basis="sample clock anchored to first host callback",
             processed_utc=time.time())
    h["trend"] = (ctx.trend.update(h["tetra_channel"], h["elapsed_s"], h["peak_snr_db"])
                  if h.get('strength_reliable', True) else 'unknown')
    samples, metadata = recording if recording is not None else (None, None)
    run = detect_mod.call_run(h.get("burst_intervals_s") or [])
    h["call_bursts"] = run
    h["call_like"] = bool(h.get("confirmed") and run >= detect_mod.CALL_MIN_RUN)
    if h.get("confirmed"):
        st.confirmed += 1
        if h["call_like"]:
            st.call_matches = getattr(st, "call_matches", 0) + 1
        if (h.get("matched_burst") or {}).get("rule") == "weak":
            st.weak_matches = getattr(st, "weak_matches", 0) + 1
        n = getattr(ctx, "notifier", None)
        if n is not None:
            try:
                suppressed = getattr(n, 'rf_alerts_suppressed_cooldown', 0)
                queued = n.alert_hit(h)
                h['notification_state'] = ('queued' if queued else 'channel_cooldown'
                                           if getattr(n, 'rf_alerts_suppressed_cooldown', 0) > suppressed
                                           else 'not_queued')
            except Exception as exc:
                st.notify_errors += 1
                h['notification_state'] = 'producer_error'
                h['notification_error'] = type(exc).__name__
        else:
            h['notification_state'] = 'disabled_or_unconfigured'
    else:
        h['notification_state'] = 'not_confirmed'
    if h.get("coincident_channels"):
        st.impulsive += 1
    if getattr(ctx, "save_snippets", False) and writer and samples is not None:
        name = h["event_id"] + ".c64"
        h.update(snippet=name, snippet_format="complex64_le", recording_state="requested")
        metadata.update(window_id=window_id, event_id=h["event_id"], utc_start=utc_start,
                        window_start_sample=sample_start)
    emit(ctx, h)
    if h.get("recording_state") == "requested":
        state = writer.samples(name, samples, metadata)
        if state != "queued":
            st.snippets_skipped += 1
            emit(ctx, {"schema_version": 2, "event_type": "recording_unavailable",
                       "event_id": h["event_id"], "state": state})
    if ctx.verbose:
        label = "ALERT" if h.get("confirmed") else "candidate"
        strength = (f"{h['peak_snr_db']:.1f} dB" if h.get('strength_reliable', True)
                    else 'matched strength unavailable')
        print(f"  {h['local_time']} {label} ch {h['tetra_channel']} "
              f"{h['frequency_hz']/1e6:.4f} MHz, {strength}, "
              f"{h['proximity_label']} / {h['trend']}", flush=True)


def attribute_match(hit, channels, floors):
    """Attribute a confirmed alert to its matched envelope at channel-power resolution.

    Peak power / quiet floor is a relative strength indicator, not calibrated
    SNR or distance. Preserve the whole-window candidate maximum separately.
    """
    hit.setdefault('candidate_peak_snr_db', hit['peak_snr_db'])
    if not hit.get('confirmed'):
        return hit['first_s']
    event_s = hit['first_s']
    hit.update(strength_reliable=False, matched_timing_refined=False,
               strength_basis='candidate window maximum; matched strength unavailable')
    hit.pop('refinement_warning', None)
    try:
        match = hit['matched_burst']
        start, stop = float(match['start_s']), float(match['stop_s'])
        if not (np.isfinite(start) and np.isfinite(stop) and 0 <= start < stop <= channels.duration_s + channels.frame_s):
            raise ValueError('invalid matched envelope timing')
        event_s = start
        hit['matched_timing_refined'] = True
        row = channels.row_for(hit['tetra_channel'])
        times = (np.arange(channels.power.shape[1]) + .5) * channels.frame_s
        mask = (times >= start) & (times < stop)
        if (row is None or not mask.any() or floors is None
                or not np.isfinite(floors[row]) or floors[row] <= 0):
            raise ValueError('matched burst has no valid channel-power floor')
        peak = float(channels.power[row, mask].max())
        if not np.isfinite(peak) or peak <= 0:
            raise ValueError('matched burst has no valid channel power')
        strength = float(10 * np.log10(peak / floors[row]))
        if not np.isfinite(strength):
            raise ValueError('non-finite matched strength')
        hit.update(peak_snr_db=round(strength, 1), strength_reliable=True,
                   strength_basis='matched envelope peak / window channel 25th-percentile floor',
                   strength_resolution_s=channels.frame_s)
    except Exception as exc:
        hit['refinement_warning'] = f'{type(exc).__name__}: {exc}'[:200]
        if not hit['matched_timing_refined']:
            # Invalid numeric metadata (NaN/Inf) must not make the JSON recorder
            # reject the preserved event. Retain a bounded textual diagnostic.
            hit['invalid_matched_burst'] = repr(hit.get('matched_burst'))[:300]
            hit['matched_burst'] = None
        # Keep the candidate maximum for diagnostics; it must not trigger an
        # escalation or be described as the matched signal's strength.
        hit['peak_snr_db'] = hit['candidate_peak_snr_db']
    # Legacy proximity fields have never measured distance; avoid retaining the
    # unrelated candidate's colour alongside the corrected strength.
    hit['proximity_label'] = 'distance unknown'
    hit['proximity'] = None
    hit['model_scenario_m'] = None
    hit['approx_range_m'] = None
    hit['range_span_m'] = None
    return event_s
