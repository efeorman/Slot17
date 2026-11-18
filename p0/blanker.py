#!/usr/bin/env python3
"""Noise blanker - excise impulsive interference from the samples themselves.

    *** NOT ENABLED IN watch.py, ON PURPOSE. Read this before wiring it in. ***

It was written to kill the in-car interference and it does not, because the
interference is not what I assumed. Measured on a real storm snippet (3 s of
`hit_191347_ch50.iq`, the 53-channel event):

    elevated (>10x median) time      3.48 % of the window
    of that, in runs <= 200 us        7.8 %
    of that, in runs <= 5 ms         78.9 %
    longest single run               11.0 ms

**The excursions are milliseconds, not microseconds.** An 11 ms broadband burst is
the same length as a TETRA burst (12.8 ms), so duration cannot separate them - a
blanker set wide enough to catch this interference would destroy the signal it
exists to protect. And it is not receiver overload: peak amplitude reaches only
39 % of the ADC rail (22 % during the events), with zero samples near it.

What actually works on this interference is `impulse.py`'s **cross-channel
coincidence** test - a real radio occupies one channel, this lights up dozens at
the same instant - which flags every storm while losing none of the 21 confirmed
detections. That is already in the detection path.

This module is kept, tested and un-wired: it is correct for genuine microsecond
impulses, which may yet appear from a different source, and the measurement that
ruled it out here is worth not repeating.

--------------------------------------------------------------------------

Every mobile radio ever built has one of these, for exactly the reason this
project needs one: a receiver in a car lives in an electrically filthy
environment - ignition, alternator, the dashboard loom - and that is a normal
operating condition, not a fault to be engineered away.

**Why this, and not more hardware.** Measured from our own logs: **zero**
impulsive events in 35 minutes at home on mains power, **133** in 68.7 minutes in
the car, with the same Pi, the same Airspy and the same software. The vehicle
environment generates it. The antenna is already at the windscreen and the supply
is already a good one, so the remaining honest answer is to make the detector
survive the noise rather than keep trying to remove its source.

**What it does.** Impulsive noise is enormous but *brief*: measured on a real storm
snippet, peak/median = 396x, with 4.5 % of samples above 10x the median. A TETRA
burst is also far above the median - but it lasts 6-14 ms, where an ignition spike
lasts microseconds. So:

    find samples far above the median  ->  keep only the SHORT runs  ->  zero them

Duration is the discriminator, exactly as in `impulse.py`. A long excursion is left
completely alone, so a real transmission is never blanked however strong it is.

**Where it runs.** Before `channelise()`, on the raw window, so everything
downstream - detection, occupancy, confirmation - sees clean samples. Blanking
after channelisation would be far too late: one impulse has already smeared itself
across every one of the 193 channels by then.

    python3 blanker.py --selftest
    python3 blanker.py <snippet.iq>        # score a real capture
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The power envelope is averaged over this long before anything is compared.
# Without it the test is hopeless in both directions: raw |x|^2 of Gaussian noise
# is exponentially distributed, so ~0.1 % of samples exceed 10x the median by pure
# chance, and the instantaneous power *inside* a real burst dips below the
# threshold constantly - shredding the very signal we are protecting. 10 us at
# 6 MSPS averages 60 samples, cutting the fluctuation to ~13 %, while still being
# far shorter than the impulses we want to catch.
BLANK_SMOOTH_S = 10e-6

# Smoothed power this far above the window's median counts as a candidate
# impulse. After smoothing, noise peaks at ~1.7x its median, and a real impulse
# measured 396x - so 10x sits in a very wide gap.
BLANK_FACTOR = 10.0
# Excursions longer than this are NOT blanked - they are signals. A TETRA control
# burst is ~6.2 ms and a normal burst ~12.8 ms, so 200 us leaves a 30x margin.
BLANK_MAX_RUN_S = 200e-6
# Each blanked run is widened by this much at both ends, to catch the shoulders of
# the impulse where it is still above the noise but below the threshold.
BLANK_GUARD_S = 5e-6
# If more than this fraction would be blanked, something is wrong with the
# assumption (a very strong wanted signal, or a stuck front end) - blank nothing
# rather than mangle the window.
BLANK_MAX_FRACTION = 0.25
# Subsample when estimating the median: exact to well under a dB, ~50x faster on
# an 18 M-sample window.
MEDIAN_STRIDE = 64


def blank(iq, fs, factor=BLANK_FACTOR, max_run_s=BLANK_MAX_RUN_S,
          guard_s=BLANK_GUARD_S, max_fraction=BLANK_MAX_FRACTION):
    """Zero the short, huge excursions. Returns (cleaned_iq, fraction_blanked).

    The input is not modified. When nothing qualifies, the original array is
    returned unchanged and the caller pays almost nothing.
    """
    n = len(iq)
    if n < 1024:
        return iq, 0.0
    p = (iq.real.astype(np.float32) ** 2 + iq.imag.astype(np.float32) ** 2)
    # Average before comparing - see BLANK_SMOOTH_S.
    w = max(1, int(round(BLANK_SMOOTH_S * fs)))
    if w > 1:
        k = np.ones(w, np.float32) / w
        p = np.convolve(p, k, mode='same')
    med = float(np.median(p[::MEDIAN_STRIDE]))
    if med <= 0:
        return iq, 0.0

    on = p > factor * med
    if not on.any():
        return iq, 0.0

    # Run-length encode the excursions.
    edges = np.flatnonzero(np.diff(np.r_[0, on.view(np.int8), 0]))
    starts, stops = edges[0::2], edges[1::2]
    lengths = stops - starts
    max_run = max(1, int(round(max_run_s * fs)))
    short = lengths <= max_run
    if not short.any():
        return iq, 0.0                     # only long excursions = real signals

    guard = max(1, int(round(guard_s * fs)))
    s0 = np.maximum(starts[short] - guard, 0)
    s1 = np.minimum(stops[short] + guard, n)

    frac = float((s1 - s0).sum()) / n
    if frac > max_fraction:
        # Refuse to blank a quarter of the window; the premise has failed.
        return iq, 0.0

    out = iq.copy()
    for a, b in zip(s0, s1):
        out[a:b] = 0
    return out, frac


def impulse_score(iq, fs, factor=BLANK_FACTOR):
    """Fraction of samples above `factor` x the median power. Diagnostic only."""
    p = (iq.real.astype(np.float32) ** 2 + iq.imag.astype(np.float32) ** 2)
    w = max(1, int(round(BLANK_SMOOTH_S * fs)))
    if w > 1:
        p = np.convolve(p, np.ones(w, np.float32) / w, mode="same")
    med = float(np.median(p[::MEDIAN_STRIDE]))
    return 0.0 if med <= 0 else float(np.mean(p > factor * med))


# --------------------------------------------------------------------------- #

def selftest():
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{'  ' + detail if detail else ''}")

    print("blanker self-test (offline)")
    fs = 6_000_000.0
    rng = np.random.default_rng(0)
    n = int(fs * 0.5)
    noise = ((rng.normal(size=n) + 1j * rng.normal(size=n))
             / np.sqrt(2)).astype(np.complex64)

    # --- clean noise is left alone ---------------------------------------- #
    out, frac = blank(noise, fs)
    check("clean noise is not blanked", frac == 0.0, f"{frac*100:.3f}% blanked")

    # --- a TETRA-length burst is NEVER blanked, however strong ------------- #
    for amp, label in ((6.0, "moderate"), (60.0, "very strong")):
        tetra = noise.copy()
        bn = int(0.0128 * fs)                      # 12.8 ms normal uplink burst
        for st in range(int(0.02 * fs), n - bn, int(0.05667 * fs)):
            tetra[st:st + bn] *= amp
        out, frac = blank(tetra, fs)
        check(f"a {label} TETRA burst train is untouched", frac == 0.0,
              f"{frac*100:.3f}% blanked")

    # --- ignition-style spikes ARE removed --------------------------------- #
    spiky = noise.copy()
    sn = max(1, int(20e-6 * fs))                   # 20 us spikes
    pos = rng.integers(0, n - sn, size=300)
    for st in pos:
        spiky[st:st + sn] *= 40.0
    before = impulse_score(spiky, fs)
    out, frac = blank(spiky, fs)
    after = impulse_score(out, fs)
    check("ignition spikes are blanked", frac > 0.0, f"{frac*100:.3f}% blanked")
    check("impulsiveness collapses after blanking", after < before / 10,
          f"{before*100:.3f}% -> {after*100:.3f}%")
    check("blanking cost is small", frac < 0.05,
          f"{frac*100:.2f}% of the window zeroed")

    # --- a burst survives a storm ------------------------------------------ #
    mixed = spiky.copy()
    bn = int(0.0128 * fs)
    st = int(0.25 * fs)
    mixed[st:st + bn] += (noise[st:st + bn] * 8.0)
    out, frac = blank(mixed, fs)
    surviving = float(np.mean(np.abs(out[st:st + bn]) > 0))
    check("a real burst survives blanking inside a storm", surviving > 0.90,
          f"{surviving*100:.1f}% of the burst kept")

    # --- refuses to mangle a window it cannot explain ---------------------- #
    pathological = noise.copy()
    idx = rng.integers(0, n, size=int(n * 0.4))
    pathological[idx] *= 50.0
    out, frac = blank(pathological, fs)
    check("refuses to blank when the premise fails", frac == 0.0,
          f"would have blanked >{BLANK_MAX_FRACTION*100:.0f}%")

    # --- input is never modified in place ---------------------------------- #
    orig = spiky.copy()
    _ = blank(spiky, fs)
    check("input array is not modified", np.array_equal(spiky, orig))

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("snippet", nargs="?")
    ap.add_argument("--fs", type=float, default=6_000_000.0)
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    if not args.snippet:
        ap.print_help()
        return 0
    raw = np.fromfile(args.snippet, dtype=np.int16)
    iq = (raw[0::2].astype(np.float32)
          + 1j * raw[1::2].astype(np.float32)).astype(np.complex64)
    before = impulse_score(iq, args.fs)
    out, frac = blank(iq, args.fs)
    after = impulse_score(out, args.fs)
    print(f"{os.path.basename(args.snippet)}")
    print(f"  impulsive samples before : {before*100:.3f}%")
    print(f"  blanked                  : {frac*100:.3f}% of the window")
    print(f"  impulsive samples after  : {after*100:.3f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
