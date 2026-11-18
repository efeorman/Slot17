#!/usr/bin/env python3
"""Reject broadband impulsive interference before it reaches the confirm stage.

The drive tests made this the single most valuable thing left to build. In-car
electrical noise - ignition, alternator, the DC-DC converter feeding the Pi -
produces short broadband spikes that light up dozens of 25 kHz channels in the
same 3 s window:

    drive    21 channels at once, 0.50 MHz span
    drive2   22 channels at once, 2.85 MHz span
    drive3   53 channels at once, 1.32 MHz span

Fifty-three emergency vehicles do not key up simultaneously across 1.3 MHz.

**Why this has to run BEFORE confirmation, not after.** `watch.py` confirms hits
strongest-first under a 1.0 s per-window budget. Impulsive noise is strong, so it
wins the sort, consumes the budget, and a genuine weak detection - the distant
vehicle, the one worth alerting on - is logged `unchecked` and never becomes an
ALERT. On `drive3`, 82 of 104 hits (79 %) were never checked at all. The priority
rule inverts under interference, so the interference has to be removed first.

Two independent tests, because they fail differently:

  1. **Cross-channel coincidence** (`flag_coincident`) - the strong one. A real
     radio occupies ONE channel. An impulse hits many at the same instant. This
     needs no extra signal processing at all: `detect()` already knows every
     burst's start time on every channel.

  2. **Envelope impulsiveness** (`impulsiveness`) - a per-snippet check on the raw
     IQ, for the case where an impulse happens to land on only one channel.

Adjacent channels are deliberately NOT counted as evidence of an impulse: a
strong TETRA carrier leaks into its neighbours (that is how ch165/ch166 came to be
logged as two detections of one signal), and that leakage is a sign of a real
signal, not a fake one.

    python3 impulse.py --selftest
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# --- cross-channel coincidence -------------------------------------------- #
# How close two bursts must be in time to count as simultaneous. One channeliser
# frame is 0.64 ms; an impulse lands in the same frame everywhere, so a tolerance
# of a couple of frames is generous.
COINCIDENCE_TOL_S = 2.0e-3
# How many WELL-SEPARATED channels must share an instant before we call it an
# impulse. Two vehicles can coincidentally transmit in the same window; twenty
# cannot.
MIN_COINCIDENT_CHANNELS = 4
# Channels closer together than this are treated as one signal (leakage), not as
# independent evidence.
ADJACENT_GUARD_CH = 2
# ...but leakage is BOUNDED. A strong carrier spills into a few neighbours, not
# into fifty. A contiguous run longer than this is broadband by definition, and
# is impulsive however contiguous it happens to be. (drive3's storm was 53
# consecutive channels; collapsing that to "one signal" was the first version's
# bug, caught by its own self-test.)
MAX_LEAK_RUN_CH = 5
# A hit is dropped when this fraction of its bursts sit in impulse clusters.
IMPULSIVE_BURST_FRACTION = 0.5

# --- envelope impulsiveness ------------------------------------------------ #
IMPULSE_BLOCK_S = 100e-6          # 100 us - shorter than any TETRA burst
IMPULSE_FACTOR = 12.0             # block power this far above the median
# The discriminator is DURATION, not amplitude. A TETRA burst is 6-14 ms of
# sustained power; an ignition spike is tens of microseconds. Elevated energy
# lasting less than this cannot be a TETRA burst whatever its amplitude.
# (The first version measured "fraction of blocks above the median", which a
# 25 %-duty TETRA burst train trivially fails - it scored 25 % "impulsive".
# Its own self-test caught that.)
IMPULSE_MAX_RUN_S = 1.0e-3
IMPULSE_MAX_FRACTION = 0.005      # >0.5 % of time in sub-ms spikes = impulsive


def coincident_bursts(channel_bursts, tol_s=COINCIDENCE_TOL_S,
                      min_channels=MIN_COINCIDENT_CHANNELS,
                      adjacent_guard=ADJACENT_GUARD_CH):
    """Which individual bursts share their instant with several separated channels?

    `channel_bursts` maps tetra_channel -> sequence of burst start times (s).
    Returns tetra_channel -> list of those of its start times that sit in a
    broadband cluster. A channel can hold both: an impulse at one moment and a
    genuine burst at another, and only the first should wait for leftover time.
    """
    events = []
    for ch, times in channel_bursts.items():
        for t in times:
            events.append((float(t), int(ch)))
    clustered = {int(ch): [] for ch in channel_bursts}
    if not events:
        return clustered
    events.sort()

    # Sweep a tolerance window over the time-sorted bursts and collect clusters.
    i = 0
    n = len(events)
    while i < n:
        j = i
        while j + 1 < n and events[j + 1][0] - events[i][0] <= tol_s:
            j += 1
        members = events[i:j + 1]
        chans = sorted({c for _, c in members})
        # Split into runs of adjacent channels: leakage from one strong carrier
        # is one signal, not many.
        runs, run = [], [chans[0]]
        for a, b in zip(chans, chans[1:]):
            if b - a > adjacent_guard:
                runs.append(run)
                run = [b]
            else:
                run.append(b)
        runs.append(run)
        # A run wider than leakage can be is broadband on its own.
        broadband_run = any(len(r) > MAX_LEAK_RUN_CH for r in runs)
        if len(chans) >= min_channels and (broadband_run
                                           or len(runs) >= min_channels):
            for t, c in members:
                clustered[c].append(t)
        i = j + 1
    return clustered


def flag_coincident(channel_bursts, tol_s=COINCIDENCE_TOL_S,
                    min_channels=MIN_COINCIDENT_CHANNELS,
                    adjacent_guard=ADJACENT_GUARD_CH,
                    burst_fraction=IMPULSIVE_BURST_FRACTION):
    """Which channels are firing because of a broadband impulse?

    `channel_bursts` maps tetra_channel -> sequence of burst start times (s).
    Returns the set of channels whose bursts are mostly simultaneous with bursts
    on several other, well-separated channels.

    Pure and hardware-free, so the policy is unit-testable.
    """
    clustered = coincident_bursts(channel_bursts, tol_s, min_channels, adjacent_guard)
    out = set()
    for ch, times in channel_bursts.items():
        total = len(times)
        if total and len(clustered[int(ch)]) / total >= burst_fraction:
            out.add(int(ch))
    return out


def impulsiveness(x, fs, block_s=IMPULSE_BLOCK_S, factor=IMPULSE_FACTOR,
                  max_run_s=IMPULSE_MAX_RUN_S):
    """Fraction of time spent in energy spikes too SHORT to be a TETRA burst.

    Amplitude alone cannot separate the two: a TETRA burst is also far above the
    median. Duration can. TETRA keys up for 6-14 ms (60-140 blocks at 100 us);
    ignition noise spikes for tens of microseconds (one or two blocks). So find
    the elevated blocks, group them into runs, and count only the runs shorter
    than a TETRA burst could possibly be.
    """
    x = np.asarray(x)
    nb_len = max(1, int(block_s * fs))
    nblocks = len(x) // nb_len
    if nblocks < 16:
        return 0.0
    p = (np.abs(x[:nblocks * nb_len]) ** 2).reshape(nblocks, nb_len).mean(axis=1)
    med = float(np.median(p))
    if med <= 0:
        return 0.0
    on = p > factor * med
    if not on.any():
        return 0.0
    max_run = max(1, int(round(max_run_s / block_s)))
    # run-length encode the elevated mask
    idx = np.flatnonzero(np.diff(np.r_[0, on.view(np.int8), 0]))
    starts, stops = idx[0::2], idx[1::2]
    lengths = stops - starts
    short = int(lengths[lengths <= max_run].sum())
    return float(short / nblocks)


def is_impulsive(x, fs, max_fraction=IMPULSE_MAX_FRACTION, **kw):
    return impulsiveness(x, fs, **kw) > max_fraction


# --------------------------------------------------------------------------- #

def selftest():
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{'  ' + detail if detail else ''}")

    print("impulse self-test (offline)")

    # --- cross-channel coincidence ---------------------------------------- #
    # A real vehicle: one channel, bursts on the frame grid.
    one = {100: [0.0, 0.05667, 0.11333, 0.17]}
    check("a single channel is never impulsive", not flag_coincident(one))

    # Two vehicles on different channels, unrelated timing.
    two = {100: [0.0, 0.05667, 0.11333], 40: [0.021, 0.078, 0.134]}
    check("two independent radios are not impulsive",
          not flag_coincident(two), f"{flag_coincident(two)}")

    # The real drive3 shape: many well-separated channels at the same instants.
    storm = {}
    for ch in range(20, 20 + 53):
        storm[ch] = [0.30, 0.90, 1.50]          # identical times everywhere
    got = flag_coincident(storm)
    check("a 53-channel simultaneous storm is flagged", len(got) == 53,
          f"{len(got)} of 53 channels flagged")

    # Adjacent-channel leakage from ONE strong carrier must NOT be called an
    # impulse - that is a real signal spilling into its neighbours.
    leak = {164: [0.1, 0.157, 0.214], 165: [0.1, 0.157, 0.214],
            166: [0.1, 0.157, 0.214], 167: [0.1, 0.157, 0.214]}
    check("adjacent-channel leakage is NOT called an impulse",
          not flag_coincident(leak), f"{flag_coincident(leak)}")

    # A real radio that happens to transmit during an impulse keeps its identity
    # only if most of its bursts are its own.
    mixed = {100: [0.30, 0.35, 0.41, 0.47, 0.53, 0.59]}      # 6 of its own
    for ch in range(20, 26):
        mixed[ch] = [0.30]
    got = flag_coincident(mixed)
    check("a real radio is kept when only one burst coincides",
          100 not in got, f"flagged={sorted(got)}")

    # --- envelope impulsiveness ------------------------------------------- #
    # Use the REAL capture rate. At 75 kHz a 100 us block holds only 7 samples,
    # so noise variance alone produces isolated spikes and the metric looks far
    # worse than it is; at 6 MSPS a block holds 600 samples and is stable. The
    # first version of this test used 75 kHz and blamed the metric.
    fs = 6_000_000.0
    rng = np.random.default_rng(0)
    n = int(fs * 0.5)
    noise = (rng.normal(size=n) + 1j * rng.normal(size=n)).astype(np.complex64)
    r_noise = impulsiveness(noise, fs)
    check("plain noise is not impulsive", not is_impulsive(noise, fs),
          f"{r_noise*100:.3f}% of time in sub-ms spikes")

    # A TETRA-like burst train: 14 ms on, once per 56.67 ms frame.
    tetra = noise.copy()
    bn = int(0.01417 * fs)
    for start in range(0, n - bn, int(0.05667 * fs)):
        tetra[start:start + bn] *= 6.0
    r_tetra = impulsiveness(tetra, fs)
    check("a TETRA burst train is not called impulsive",
          not is_impulsive(tetra, fs), f"{r_tetra*100:.3f}% of time in sub-ms spikes")

    # Ignition-style spikes: very short, very large.
    spiky = noise.copy()
    sn = max(1, int(50e-6 * fs))
    for start in rng.integers(0, n - sn, size=200):
        spiky[start:start + sn] *= 40.0
    r_spiky = impulsiveness(spiky, fs)
    check("ignition-style spikes ARE impulsive", is_impulsive(spiky, fs),
          f"{r_spiky*100:.2f}% of time in sub-ms spikes")
    check("the two are well separated", r_spiky > 10 * max(r_tetra, 1e-6),
          f"spiky {r_spiky*100:.3f}% vs tetra {r_tetra*100:.3f}%")

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("snippet", nargs="?", help="a hit_*.iq snippet to score")
    ap.add_argument("--fs", type=float, default=6_000_000.0)
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    if not args.snippet:
        ap.print_help()
        return 0
    raw = np.fromfile(args.snippet, dtype=np.int16)
    iq = raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)
    r = impulsiveness(iq, args.fs)
    print(f"{os.path.basename(args.snippet)}: {r*100:.3f}% of time in sub-ms spikes "
          f"-> {'IMPULSIVE' if r > IMPULSE_MAX_FRACTION else 'not impulsive'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
