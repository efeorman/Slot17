#!/usr/bin/env python3
"""Propose TETRA uplink *candidates* - deliberately sensitive, never an alert.

This file does not decide that a TETRA radio transmitted. `phy.py` does, by
recognising a public training word in an individual burst, and `pipeline.py`
promotes only that to an alert. Everything here exists to hand phy.py a short
list of places worth looking, cheaply enough to do it on every analysis window.

  1. **Energy.** A run of frames in one 25 kHz channel above that channel's own
     quiet-time floor. This is the gate that actually selects candidates.
  2. **Duration.** A TETRA burst fills most of a 14.167 ms timeslot, or about
     half of one for a control burst. Anything much shorter is a spur or a click;
     anything much longer is a continuous carrier or interference, not a radio
     keying up. Still a hard gate: it costs nothing and removes clicks.
  3. **Occupied bandwidth.** A tone or narrow spur reads one fine bin; a TETRA
     carrier reads several. Also a hard gate - it can only remove signals too
     narrow to be TETRA at all.

**Periodicity is scored, not enforced, in the live detector.** `pipeline.py`
calls `detect(..., require_periodic=False)` on purpose: a car passing at speed
may key up exactly once inside a window, and an energy candidate containing ONE
burst is precisely what phy.py is built to judge. The score travels with the hit
as supporting metadata. `require_periodic=True` remains the default for offline
callers and for `--selftest`, which therefore exercises a stricter chain than
the field product - read selftest results with that in mind.

`downlink_paired` is **static inventory membership**, not an observation. Both
bands share the same channel numbering (380.0125 and 390.0125 MHz bases, 25 kHz
raster), so the flag says only that channel n appears in a carrier list measured
at some earlier time. Nothing here listens to the downlink while an uplink burst
is in progress, and the emitted event says so in `downlink_pairing_basis`. It is
metadata; it must never be described as a live pairing, and it cannot promote a
candidate on its own.

    python detect.py --selftest        # includes a detection-probability sweep
    python detect.py <capture.iq>
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import impulse
import rangemodel
from analyze import load
import channelize
from channelize import channelise, CHANNEL_HZ

SLOT_S = 255 / 18000
FRAME_S = 4 * SLOT_S
MIN_BURST_S = 4.0e-3         # below this it is not a TETRA burst
MAX_BURST_S = 4 * SLOT_S + 0.002  # adjacent allocated slots may stay keyed
DEFAULT_THRESHOLD_DB = 8.0
# Burst starts are tested against the half-slot grid, not the 56.67 ms frame:
# network scheduling moves a radio between slots and subslots, so the frame is
# too coarse a ruler. PERIOD_TOL_S (4 ms, sized for the frame grid) became dead
# when the grid was tightened and has been removed; these two are the live pair.
# Note the resolution this is measured at: ch.frame_s is about 0.64 ms at 6 MSPS,
# so a gap carries about +/-0.64 ms of quantisation and the tolerance below is
# only ~1.25 frames wider than the noise in its own input. Re-derive both if the
# channeliser frame length ever changes.
SLOT_GRID_S = SLOT_S / 2
SLOT_GRID_TOL_S = 8.0e-4
MIN_BURSTS_FOR_PERIOD = 3
PERIODIC_MIN_SCORE = 0.6     # fraction of gaps that must land on the frame grid

# How far back the cross-window burst history reaches, and how much of it we keep
# per channel. 45 s spans about fifteen 3 s windows - enough to see three pulses
# from a radio that only re-registers every few seconds, and short enough that a
# vehicle that has driven away is forgotten quickly.
HISTORY_S = 45.0
HISTORY_MAX_BURSTS = 256     # per channel; bounds memory on a busy band

# A TETRA carrier occupies ~18-24 kHz. Measured on the channeliser's 3.125 kHz
# fine bins, a real burst reads >=6.2 kHz and a real off-air carrier ~18.8 kHz,
# while a pure tone or narrow spur reads 3.1 kHz (one bin). 4.6 kHz sits between
# them with a full fine-bin of margin either side, and because noise spreads
# across all bins (reading *wide*), this gate only ever removes genuinely narrow
# signals - it cannot reject a weak carrier. This closes the narrowband-beacon
# false alarm found by stress_test.py.
MIN_OCCUPANCY_HZ = 4600.0

# Internal spurs never to be reported, as ABSOLUTE frequencies in Hz. Absolute is
# correct and deliberate: 384.000 MHz is 8 x 48 MHz, the LPC4370's initialisation
# clock harmonic, so it sits at the same frequency whatever the receiver is tuned
# to and whatever the sample rate. (It is a coincidence that it also equals
# 382.5 MHz + 6 MSPS/4; it is not an fs/4 image, and computing it from the tuning
# would be wrong.) It is the only 48 MHz harmonic that lands in any band we watch.
SPUR_BLACKLIST_HZ = [384_000_000.0]
# The spur is ~730 Hz wide and sits exactly halfway between uplink channels 159
# and 160, 12.5 kHz from each. A 15 kHz guard therefore blinded BOTH of them
# permanently - 1 % of the band, in a detector whose whole job is not to miss a
# vehicle. stress_test.py part F shows the occupied-bandwidth and periodicity
# gates reject the real spur on their own at +55 dB with the blacklist disabled
# entirely, so this guard is belt-and-braces, not the load-bearing defence. 6 kHz
# still covers the line eight times over while leaving 159/160 usable.
SPUR_GUARD_HZ = 6_000.0

# --- Strength class, range and activity: the telemetry -------- #
# Distance is not recoverable from one receiver: with one antenna, no knowledge of
# the radio's transmit power, and TETRA power control changing that power on the
# network's command, a strong signal can be a near radio sending softly or a far one
# sending loudly. The classes below describe the signal strength only. The metres
# figure is a labelled research scenario with a wide span, never a measurement, and
# it is never shown to the user.

# SNR thresholds for the three-level bar, in dB above the channel's own floor.
PROXIMITY_LEVELS = [
    (22.0, "red", "strong"),
    (12.0, "yellow", "moderate"),
    (0.0, "green", "weak"),
]

# Range now comes from rangemodel.py, which works the actual link budget with a
# two-ray propagation model. The constants that used to live here -
# RANGE_REF_M = 700 (an advertised figure for this kind of device, rounded down) and a hand-picked
# exponent of 3 - were never checked against a vehicle at a known distance, and
# they made a strong signal print as a small number: a 57 dB hit on a
# downlink-paired channel came out as "14 m".
RANGE_MIN_M, RANGE_MAX_M = 5.0, 20000.0

# A radio in an active call/data exchange keys up most frames; a bare
# registration ping does not. Ratio of observed bursts to the frames that could
# have held one, over the analysed window.
ACTIVITY_ACTIVE_RATIO = 0.30

# The air-ground-air channels (1.8.0): ECC/DEC/(06)05 sets 384.800-385.000 MHz aside for emergency services'
# aircraft, and 384.750-384.800 MHz as extension; channels 190-199 on our grid. Above about 150 m an aircraft's radio
# uses only these (ETSI TR 102 300-6), so a match there is an aircraft, almost always a police or ambulance
# helicopter. Checked against their ADS-B tracks on 27 November 2026: in minutes with a match on these channels one
# flew within 20 km half the time, against 4 % in other minutes of the same drives.
AIR_BAND_HZ = (384.750e6, 385.000e6)


def air_channel(f_hz):
    """The aircraft channels, by frequency: the same in every country that follows ECC/DEC/(06)05."""
    return AIR_BAND_HZ[0] <= f_hz < AIR_BAND_HZ[1]


BAND_LABELS = [
    (380.0e6, 385.0e6, "C2000-uplink", "L"),
    (390.0e6, 395.0e6, "C2000-downlink", "H"),
    (410.0e6, 420.0e6, "civil-uplink", "L"),
    (420.0e6, 430.0e6, "civil-downlink", "H"),
]


def proximity(snr_db):
    """(level, label) three-level bar from SNR - the reliable indicator."""
    for thr, level, label in PROXIMITY_LEVELS:
        if snr_db >= thr:
            return level, label
    return "green", "weak"


def approx_range_m(snr_db):
    """A ROUGH range estimate in metres. Returns (estimate, low, high).

    Still not a measurement, and the bracket is still wide - but it is now wide
    for a stated physical reason (TETRA adaptive power control spans ~25 dB, which
    at 40 dB/decade is a factor of ~4 in distance) rather than because a spread
    was invented to look humble.
    """
    d, lo, hi = rangemodel.range_m(float(snr_db))
    clip = lambda x: float(min(RANGE_MAX_M, max(RANGE_MIN_M, x)))
    return clip(d), clip(lo), clip(hi)


def activity_class(bursts, window_s):
    """'active' (a call/data exchange) vs 'intermittent' (registration-like)."""
    possible = max(1.0, window_s / FRAME_S)
    ratio = bursts / possible
    return ("active" if ratio > ACTIVITY_ACTIVE_RATIO else "intermittent"), round(ratio, 3)


def band_label(f_hz):
    """(name, 'L'/'H') for a frequency, as a low/high-band tag."""
    for lo, hi, name, lh in BAND_LABELS:
        if lo <= f_hz < hi:
            return name, lh
    return "unknown", "?"


class BurstHistory:
    """Burst start times per channel, remembered **across** analysis windows.

    This exists because of the single most important thing the detector was
    getting wrong. A radio in an active call keys up
    once per 56.67 ms frame, so a 3 s window holds ~50 bursts and the periodicity
    test has plenty to work with. But the signal this device is actually built to
    catch is the other one: a radio doing nothing but *staying registered on the
    network* sends one short burst every few seconds. A 3 s
    window can hold at most one such burst, `periodicity_score` needs three, and
    so every idle-but-present vehicle was rejected before any other test ran - at
    any signal strength.

    Keeping the burst times across windows lets the *same* periodicity test work
    on a sparse signal, without widening any tolerance or lowering any threshold:
    a TETRA radio is slot-synchronised to the network, so even a burst three
    seconds later still lands on an exact multiple of the 56.67 ms frame. That is
    a real, checkable fingerprint, not a relaxation.

    Times are absolute seconds since the watch started, so they stay meaningful
    across windows even when `analysis_worker` skips a stale one.
    """

    def __init__(self, memory_s=HISTORY_S, max_bursts=HISTORY_MAX_BURSTS):
        self.memory_s = float(memory_s)
        self.max_bursts = int(max_bursts)
        self.by_channel = {}          # tetra_ch -> np.ndarray of absolute times

    def add(self, tetra_ch, times_s, now_s):
        """Record this window's burst starts and forget anything too old."""
        prev = self.by_channel.get(tetra_ch)
        t = (np.asarray(times_s, float) if prev is None
             else np.concatenate([prev, np.asarray(times_s, float)]))
        t = t[t >= now_s - self.memory_s]
        if t.size > self.max_bursts:
            t = t[-self.max_bursts:]
        self.by_channel[tetra_ch] = t
        return t

    def prune(self, now_s):
        """Drop channels with nothing left inside the memory window."""
        for k in [k for k, t in self.by_channel.items()
                  if t.size == 0 or t[-1] < now_s - self.memory_s]:
            del self.by_channel[k]

    def times(self, tetra_ch):
        t = self.by_channel.get(tetra_ch)
        return np.empty(0) if t is None else t


def tetra_ch_of(ch, row):
    return int(ch.tetra_ch[row])


def find_runs(mask):
    """Start/stop indices of each True run in a 1-D boolean array."""
    if not mask.any():
        return np.empty(0, int), np.empty(0, int)
    d = np.diff(mask.astype(np.int8))
    starts = np.flatnonzero(d == 1) + 1
    stops = np.flatnonzero(d == -1) + 1
    if mask[0]:
        starts = np.r_[0, starts]
    if mask[-1]:
        stops = np.r_[stops, len(mask)]
    return starts, stops


def periodicity_score(start_times):
    """How well a channel's burst starts fit the half-slot grid (SLOT_GRID_S).

    Returns (score 0..1, best_period_s). Intervals are allowed to be an integer
    number of grid steps, because a radio does not transmit in every frame - it
    sends on its assigned timeslot when there is something to send, so gaps of
    2, 3 or many steps are normal and still periodic.

    The grid is half a timeslot (7.08 ms), NOT the 56.67 ms frame: a network can
    move a radio between slots and subslots, and the frame grid missed those.
    Random gaps land inside SLOT_GRID_TOL_S of this grid about 23 % of the time,
    which is the number PERIODIC_MIN_SCORE has to clear - it is a real test, but
    a weaker one than a frame-grid reading of this function would suggest.
    """
    if len(start_times) < MIN_BURSTS_FOR_PERIOD:
        return 0.0, None
    gaps = np.diff(np.sort(start_times))
    gaps = gaps[gaps > 1e-3]
    if gaps.size == 0:
        return 0.0, None
    # Network scheduling can change slots/subslots. Tight slot-grid tolerance
    # avoids using the much wider old frame tolerance on the finer grid.
    base = SLOT_GRID_S
    n = np.maximum(1, np.round(gaps / base))
    good = np.abs(gaps - n * base) <= SLOT_GRID_TOL_S
    return float(np.mean(good)), float(np.median(gaps[good])) if good.any() else None


# A radio in a call sends one normal burst (about 14 ms) in its slot of every frame; a radio signing on
# sends single short control bursts. Three normal-length bursts in a row, whole frames apart, is the
# call pattern. On the November drives 224 of about 250 recognised normal bursts sat in such a run and
# almost none of 943 recognised control bursts did (call_pattern.py, 15 November 2026). It is traffic
# on the channel - speech or a data call - and it says nothing about who, or how far.
CALL_BURST_S = (0.011, 0.016)
CALL_MIN_RUN = 3


def call_run(intervals):
    """Longest run of normal-length bursts whose starts are 1 to 3 whole frames apart."""
    starts = [a for a, b in intervals if CALL_BURST_S[0] <= b - a <= CALL_BURST_S[1]]
    best = run = 1 if starts else 0
    for x, y in zip(starts, starts[1:]):
        n = (y - x) / FRAME_S
        run = run + 1 if 0.7 <= n <= 3.3 and abs(n - round(n)) < 0.08 else 1
        best = max(best, run)
    return best


def detect(ch, threshold_db=DEFAULT_THRESHOLD_DB, downlink_channels=None,
           require_periodic=True, history=None, t_abs_s=0.0, own=None):
    """Run the detection chain over a channelised capture. Returns a list of hits.

    `history` (a BurstHistory) and `t_abs_s` (this window's absolute start time,
    in seconds since the watch began) are optional. When given, a channel whose
    bursts are too sparse to test *within* this window is re-tested against its
    own burst times from the last HISTORY_S seconds - which is what makes an
    idle-but-registered radio detectable at all. Without them the function
    behaves exactly as before, so every offline caller is unaffected.

    `own` = (from_s, to_s): only bursts that START in this part of the window count. Overlapping
    windows use it so that each burst belongs to exactly one window, always with the whole burst
    inside it (watch.py, WINDOW_OVERLAP_S). None keeps every burst, as before.
    """
    # Degenerate input (empty or shorter than one FFT frame) yields no frames to
    # test - the correct answer is "nothing detected", not an exception. watch.py
    # always feeds full windows, so this only guards against odd callers.
    if getattr(ch.power, "ndim", 0) != 2 or ch.power.shape[1] == 0:
        return []
    floor = ch.noise_floor()
    thr = floor * 10 ** (threshold_db / 10.0)
    hits = []
    # Burst times per channel, so broadband impulses can be spotted afterwards:
    # a real radio uses ONE channel, an ignition spike lights up dozens at the
    # same instant. See impulse.py.
    channel_bursts = {}

    for row in range(ch.power.shape[0]):
        f_hz = float(ch.channel_hz[row])
        if any(abs(f_hz - s) < SPUR_GUARD_HZ for s in SPUR_BLACKLIST_HZ):
            continue

        starts, stops = find_runs(ch.power[row] > thr[row])
        if starts.size == 0:
            continue

        dur = (stops - starts) * ch.frame_s
        keep = (dur >= MIN_BURST_S) & (dur <= MAX_BURST_S)
        if own is not None:
            begin = starts * ch.frame_s
            keep &= (begin >= own[0]) & (begin < own[1])
        if not keep.any():
            continue
        t0 = starts[keep] * ch.frame_s
        durs = dur[keep]

        score, period = periodicity_score(t0)
        source, hist_n, hist_span = "window", None, None
        if history is not None:
            hist = history.add(tetra_ch_of(ch, row), t_abs_s + t0,
                               t_abs_s + ch.duration_s)
            if score < PERIODIC_MIN_SCORE and hist.size >= MIN_BURSTS_FOR_PERIOD:
                # Too few bursts in this window to judge; ask the history. The
                # test itself is identical - only the span of time it sees grows.
                h_score, h_period = periodicity_score(hist)
                if h_score > score:
                    score, period = h_score, h_period
                    source = "history"
            hist_n = int(hist.size)
            hist_span = round(float(hist[-1] - hist[0]), 1) if hist.size > 1 else 0.0
        if require_periodic and score < PERIODIC_MIN_SCORE:
            continue

        # Occupied-bandwidth gate: reject a signal too narrow to be a TETRA
        # carrier (a tone or spur that happens to share TETRA's timing).
        occ_hz = (None if ch.occupancy_hz is None
                  else float(ch.occupancy_hz[row]))
        if (occ_hz is not None and np.isfinite(occ_hz)
                and occ_hz < MIN_OCCUPANCY_HZ):
            continue

        seg = ch.power[row]
        snr_db = float(10 * np.log10(np.max(seg) / max(floor[row], 1e-30)))
        tetra_ch = int(ch.tetra_ch[row])
        paired = (downlink_channels is not None
                  and tetra_ch in set(downlink_channels))

        n_bursts = int(keep.sum())
        med_burst_ms = float(np.median(durs) * 1e3)
        channel_bursts[tetra_ch] = t0.tolist()
        level, prox_label = proximity(snr_db)
        rng, rng_lo, rng_hi = approx_range_m(snr_db)
        act, act_ratio = activity_class(n_bursts, ch.duration_s)
        band, lh = band_label(f_hz)

        hits.append({
            "tetra_channel": tetra_ch,
            "frequency_hz": f_hz,            # true RF, exactly on the raster
            # Where that carrier sits on THIS receiver's uncorrected axis. Anything
            # mixing against the raw IQ (the examine confirmation stage) must tune
            # here, not to frequency_hz. See earlier testing.
            "tuning_hz": f_hz + getattr(ch, "cal_offset_hz", lambda _f: 0.0)(f_hz),
            "band": band,
            "band_lh": lh,
            # Channels 0-2 and 196-199 sit in the outermost 75 kHz of the 5 MHz kept from 6 MSPS and
            # were not examined before 1.2.6; a strong signal just outside the band could fold onto
            # them, so their matches say so and can be counted apart.
            "band_edge": bool(tetra_ch < channelize.EDGE_LOW or tetra_ch > channelize.EDGE_HIGH),
            # An aircraft's channel (1.8.0): a radio in the air, not a vehicle on the road.
            "air_channel": bool(air_channel(f_hz)),
            "bursts": n_bursts,
            "median_burst_ms": med_burst_ms,
            "burst_intervals_s": [[float(a), float(a + length)] for a, length in zip(t0, durs)],
            "first_s": float(t0[0]),
            "last_s": float(t0[-1]),
            "peak_snr_db": snr_db,
            "occupied_bw_khz": (round(occ_hz / 1e3, 1)
                                if occ_hz is not None else None),
            "periodicity": round(score, 3),
            "periodicity_source": source,       # "window" or "history" (sparse)
            "history_bursts": hist_n,           # bursts seen on this channel
            "history_span_s": hist_span,        # over this much time
            "measured_period_ms": round(period * 1e3, 2) if period else None,
            "downlink_paired": bool(paired),
            "downlink_pairing_basis": "static channel inventory; no simultaneous downlink measurement",
            # telemetry -----------------------------------------
            "proximity": level,                 # green / yellow / red
            "proximity_label": prox_label,      # weak / moderate / strong
            "approx_range_m": round(rng) if rangemodel.CALIBRATED else None,
            "range_span_m": [round(rng_lo), round(rng_hi)] if rangemodel.CALIBRATED else None,
            "model_scenario_m": [round(rng_lo), round(rng), round(rng_hi)],
            # "low" until a calibration drive is done; the dominant unknown is
            # the transmitter's power setting, not our measurement.
            "range_confidence": "calibrated" if rangemodel.CALIBRATED else "unavailable",
            "range_basis": "uncalibrated scenario; transmitter power and propagation unknown",
            "activity": act,                    # active call/data vs intermittent
            "activity_ratio": act_ratio,
            "confidence": "energy_candidate_only",
        })
    # Mark hits that are part of a broadband impulsive event. They are kept in
    # the log (throwing data away hides the interference) but flagged, so
    # watch.py can refuse to spend confirmation budget on them. That budget is
    # exactly what a distant, weak, genuine detection needs.
    impulsive = impulse.flag_coincident(channel_bursts)
    clustered = impulse.coincident_bursts(channel_bursts)
    for h in hits:
        h["impulsive"] = h["tetra_channel"] in impulsive
        h["coincident_channels"] = h["impulsive"]
        # Which of this channel's own bursts sit in a broadband moment. The others are
        # checked first: a real burst elsewhere in the window must not wait behind it.
        if clustered.get(h["tetra_channel"]):
            h["coincident_burst_starts_s"] = sorted(clustered[h["tetra_channel"]])
        h["interference_policy"] = "coincidence is advisory; require physical-layer evidence"
    return sorted(hits, key=lambda h: -h["peak_snr_db"])


def load_downlink_channels(path=None):
    """Static inventory: channel numbers a downlink carrier was measured on once.

    A stored list, not a live observation. Membership dates from whenever the
    survey ran; it says nothing about this second, this location, or whether
    that carrier is still on air.
    """
    if path is None:
        from analyze import config_file
        path = config_file("downlink_channels.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)["channels"]
        except (ValueError, KeyError, OSError):
            pass
    return None


# --------------------------------------------------------------------------- #

def _real_uplink_noise(fs, want_s):
    """A slice of the real recorded uplink baseline, or None if unavailable."""
    import glob
    iq_dir = paths.IQ_DIR
    for pat in ("*uplink-baseline*.iq", "*verify-382.5*.iq", "*uplink*.iq"):
        files = sorted(glob.glob(os.path.join(iq_dir, pat)))
        if files:
            raw = np.fromfile(files[-1], dtype=np.int16,
                              count=int(want_s * fs) * 2)
            if raw.size < 1000:
                continue
            return (raw[0::2].astype(np.float32)
                    + 1j * raw[1::2].astype(np.float32)), files[-1]
    return None, None


def selftest():
    import tetra_gen

    fs, centre = 6_000_000.0, 382.5e6
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{'  ' + detail if detail else ''}")

    print("detect self-test")

    noise, src = _real_uplink_noise(fs, 3.0)
    if noise is None:
        print("  (no recorded uplink capture found - using synthetic noise)")
        rng = np.random.default_rng(0)
        n = int(fs * 3.0)
        noise = ((rng.normal(size=n) + 1j * rng.normal(size=n))
                 / np.sqrt(2)).astype(np.complex64)
    else:
        print(f"  noise source: {os.path.basename(src)} "
              f"({len(noise)/fs:.1f} s of real recorded uplink)")

    target_ch = 100
    target_hz = 380.0125e6 + target_ch * CHANNEL_HZ

    # --- 1. a transmission is detected --------------------------------------
    y = tetra_gen.inject(noise.copy(), fs, offset_hz=target_hz - centre,
                         snr_db=16.0, seed=11)
    hits = detect(channelise(y, fs, centre, apply_cal=False))
    got = [h for h in hits if h["tetra_channel"] == target_ch]
    check("injected transmission detected", bool(got),
          f"{len(hits)} hit(s) total")
    if got:
        h = got[0]
        check("burst duration looks like a TETRA slot",
              8.0 <= h["median_burst_ms"] <= 20.0,
              f"{h['median_burst_ms']:.1f} ms")
        check("frame period recovered",
              h["measured_period_ms"] is not None
              and abs(h["measured_period_ms"] - 56.67) < 5,
              f"{h['measured_period_ms']} ms")
        check("periodicity score high", h["periodicity"] >= 0.8,
              f"{h['periodicity']}")

    # --- 2. the real quiet band produces no alerts --------------------------
    quiet = detect(channelise(noise, fs, centre, apply_cal=False))
    check("no false alerts on the real quiet band", len(quiet) == 0,
          f"{len(quiet)} false hit(s) in {len(noise)/fs:.1f} s")

    # --- 3. aperiodic interference is rejected ------------------------------
    # Same energy and burst length, but at random times rather than on the grid.
    rng2 = np.random.default_rng(3)
    z = noise.copy()
    tx = tetra_gen.make_transmission(len(z) / fs, fs, seed=4)
    scrambled = np.zeros_like(z)
    blk = int(0.02 * fs)
    for start in range(0, len(z) - blk, blk):
        if rng2.random() < 0.25:
            src_i = rng2.integers(0, max(1, len(tx) - blk))
            scrambled[start:start + blk] = tx[src_i:src_i + blk]
    t = np.arange(len(z)) / fs
    p_noise = float(np.mean(np.abs(z) ** 2))
    scale = np.sqrt(p_noise * 10 ** (16 / 10.0) * (18000 * 1.35 / fs))
    z = z + scrambled * np.exp(2j * np.pi * (target_hz - centre) * t) * scale
    aper = [h for h in detect(channelise(z, fs, centre, apply_cal=False))
            if h["tetra_channel"] == target_ch]
    check("aperiodic interference rejected", not aper,
          "periodicity test held" if not aper else "leaked through")

    # --- 4. detection probability versus SNR --------------------------------
    print("\n  detection probability vs SNR (real noise, 3 trials each):")
    print(f"    {'SNR dB':>7}  {'detected':>9}  {'periodicity':>11}")
    for snr in (4, 6, 8, 10, 12, 16):
        n_det, scores = 0, []
        for trial in range(3):
            yy = tetra_gen.inject(noise.copy(), fs,
                                  offset_hz=target_hz - centre,
                                  snr_db=float(snr), seed=100 + trial)
            hh = [h for h in detect(channelise(yy, fs, centre, apply_cal=False))
                  if h["tetra_channel"] == target_ch]
            if hh:
                n_det += 1
                scores.append(hh[0]["periodicity"])
        print(f"    {snr:7d}  {n_det:>6}/3  "
              f"{(np.mean(scores) if scores else 0):11.2f}")
        if snr == 12:
            check("detects reliably at 12 dB SNR", n_det == 3, f"{n_det}/3")

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--threshold-db", type=float, default=DEFAULT_THRESHOLD_DB)
    ap.add_argument("--all", action="store_true",
                    help="also report hits that fail the periodicity test")
    args = ap.parse_args()

    if args.selftest:
        raise SystemExit(selftest())
    if not args.file:
        ap.print_help()
        return

    iq, meta = load(args.file)
    ch = channelise(iq, float(meta["sample_rate_hz"]), float(meta["centre_hz"]))
    hits = detect(ch, threshold_db=args.threshold_db,
                  downlink_channels=load_downlink_channels(),
                  require_periodic=not args.all)

    print(f"{os.path.basename(args.file)}  -  {ch.duration_s:.1f} s, "
          f"{ch.power.shape[0]} channels")
    if not hits:
        print("\nNo energy candidates found. This does not establish radio absence.")
        return
    print(f"\n{len(hits)} energy candidate(s); run confirmation before interpreting as TETRA:")
    print(f"  {'ch':>5}  {'MHz':>11}  {'bursts':>6}  {'burst ms':>8}  "
          f"{'period ms':>9}  {'perio':>5}  {'SNR':>5}  {'paired':>6}  conf")
    for h in hits:
        print(f"  {h['tetra_channel']:5d}  {h['frequency_hz']/1e6:11.4f}  "
              f"{h['bursts']:6d}  {h['median_burst_ms']:8.1f}  "
              f"{str(h['measured_period_ms']):>9}  {h['periodicity']:5.2f}  "
              f"{h['peak_snr_db']:5.1f}  {str(h['downlink_paired']):>6}  "
              f"{h['confidence']}")


if __name__ == "__main__":
    main()
