#!/usr/bin/env python3
"""Synthetic TETRA physical-layer burst fixtures; no transmission support.

Models pi/4-DQPSK at 18 ksymbol/s, RRC shaping (alpha .35), CUB/NUB1 training,
useful burst lengths and exact 255-symbol slots. Random surrounding data is not
channel-coded traffic. The generated fixtures are supplemented by real saved
drive recordings; passing synthetic tests does not establish field range.
"""
import argparse

import numpy as np
from scipy.signal import resample_poly

SYMBOL_RATE = 18_000.0          # TETRA symbol rate
ROLLOFF = 0.35                  # RRC alpha
SLOT_S = 255 / SYMBOL_RATE      # exact slot duration, no accumulated rounding drift
FRAME_S = 4 * SLOT_S            # 56.667 ms - 4 slots per frame
NUB_SYMBOLS = 231               # normal uplink burst content, of 255 in a slot
CUB_SYMBOLS = 103               # useful CUB symbols; guard/ramp is separate
RAMP_SYMBOLS = 4                # transmitter power ramp at each end

# pi/4-DQPSK phase changes per dibit, per ETSI EN 300 392-2.
DIBIT_PHASE = {(0, 0): +np.pi / 4, (0, 1): +3 * np.pi / 4,
               (1, 0): -np.pi / 4, (1, 1): -3 * np.pi / 4}


def rrc_taps(sps, span_symbols=8, alpha=ROLLOFF):
    """Root-raised-cosine impulse response, `span_symbols` long at `sps`."""
    n = np.arange(-span_symbols * sps / 2, span_symbols * sps / 2 + 1)
    t = n / sps
    taps = np.zeros_like(t)
    for i, ti in enumerate(t):
        if abs(ti) < 1e-12:
            taps[i] = 1 - alpha + 4 * alpha / np.pi
        elif abs(abs(ti) - 1 / (4 * alpha)) < 1e-12:
            taps[i] = (alpha / np.sqrt(2)) * (
                (1 + 2 / np.pi) * np.sin(np.pi / (4 * alpha))
                + (1 - 2 / np.pi) * np.cos(np.pi / (4 * alpha)))
        else:
            num = (np.sin(np.pi * ti * (1 - alpha))
                   + 4 * alpha * ti * np.cos(np.pi * ti * (1 + alpha)))
            den = np.pi * ti * (1 - (4 * alpha * ti) ** 2)
            taps[i] = num / den
    return taps / np.sqrt(np.sum(taps ** 2))


def modulate(n_symbols, sps, rng, training=None):
    """pi/4-DQPSK symbols, RRC-shaped, at `sps` samples per symbol."""
    bits = rng.integers(0, 2, size=(n_symbols, 2))
    phase = 0.0
    if training:
        from phy import TRAINING_BITS
        word = np.array([int(b) for b in TRAINING_BITS[training]]).reshape(-1, 2)
        centre = (n_symbols - len(word)) // 2
        bits[centre:centre + len(word)] = word
        # Public tail bits, ETSI equation 9.12. Older synthetic fixtures inserted
        # only the centre word and left even these known bits random.
        # Preserve the arbitrary phase of the payload when making old fixtures
        # standards-compliant; this keeps their noise realization comparable.
        phase = sum(DIBIT_PHASE[tuple(pair)] for pair in bits[:2])
        bits[:2] = bits[-2:] = [[1, 1], [0, 0]]
        phase -= sum(DIBIT_PHASE[tuple(pair)] for pair in bits[:2])
    syms = np.empty(n_symbols, dtype=np.complex128)
    for i, (b0, b1) in enumerate(bits):
        phase += DIBIT_PHASE[(int(b0), int(b1))]
        syms[i] = np.exp(1j * phase)

    up = np.zeros(n_symbols * sps, dtype=np.complex128)
    up[::sps] = syms
    return np.convolve(up, rrc_taps(sps), mode="same")


def burst(kind, sps, rng, training=True):
    """One shaped burst with power ramps at both ends."""
    n = {"nub": NUB_SYMBOLS, "cub": CUB_SYMBOLS}[kind]
    x = modulate(n, sps, rng, ("cub" if kind == "cub" else "nub1") if training else None)
    # Separate ramps from the useful symbols, rather than shortening the data.
    x = np.pad(x, (RAMP_SYMBOLS * sps, RAMP_SYMBOLS * sps), mode="edge")
    ramp = RAMP_SYMBOLS * sps
    if ramp * 2 < len(x):
        w = np.ones(len(x))
        w[:ramp] = np.linspace(0, 1, ramp)
        w[-ramp:] = np.linspace(1, 0, ramp)
        x = x * w
    return x


def make_transmission(duration_s, fs, kind="nub", frames=None, seed=0,
                      first_frame_offset_s=0.0, training=True):
    """A radio transmitting: one burst per 56.67 ms frame.

    Returns a complex baseband array of `duration_s` at `fs`, unit RMS over the
    active bursts (silence elsewhere).
    """
    rng = np.random.default_rng(seed)

    # Generate at an integer samples-per-symbol, then resample to fs exactly.
    # At 6 MSPS, fs/SYMBOL_RATE = 333.33, so direct generation is not possible.
    sps = 10
    base_fs = SYMBOL_RATE * sps                     # 180 kHz
    out = np.zeros(int(round(duration_s * fs)), dtype=np.complex128)

    n_frames = int(np.ceil(duration_s / FRAME_S))
    for k in range(n_frames):
        if frames is not None and k not in frames:
            continue
        b = burst(kind, sps, rng, training=training)
        # resample_poly with integer up/down keeps the timing exact:
        # 180 kHz * 100/3 = 6 MHz.
        g = np.gcd(int(fs), int(base_fs))
        b = resample_poly(b, int(fs) // g, int(base_fs) // g)
        start = int(round((first_frame_offset_s + k * FRAME_S) * fs))
        end = min(start + len(b), len(out))
        if start >= len(out):
            break
        out[start:end] += b[:end - start]

    active = np.abs(out) > 0
    if active.any():
        out /= np.sqrt(np.mean(np.abs(out[active]) ** 2))
    return out


def snr_bandwidth_hz():
    """The bandwidth every "injected SNR" figure in this project is defined over.

    One number, in one place, because there were two. `tetra_gen.inject` used the
    signal's own occupied bandwidth (18 kHz x 1.35 = 24.3 kHz) while
    `validate_real` and `stress_test` used the channel width (25 kHz x 1.35 =
    33.75 kHz), so the same label meant 1.4 dB more power in one place than the
    other - and the headline "detects down to 4 dB" came from the generous one
    while `detect.py --selftest` reported 0/3 at 4 dB from the strict one.
    The occupied bandwidth is the physically meaningful choice: it is where the
    signal's power actually is.
    """
    return SYMBOL_RATE * (1.0 + ROLLOFF)


def inject(iq, fs, offset_hz, snr_db, noise_ref=None, **kw):
    """Add a synthetic transmission to a real capture at a given offset and SNR.

    SNR is defined per-sample over the burst, against the measured noise power
    of the host capture in the same bandwidth - so "10 dB" means the same thing
    across captures taken at different gains.
    """
    tx = make_transmission(len(iq) / fs, fs, **kw)
    t = np.arange(len(tx)) / fs
    tx = tx * np.exp(2j * np.pi * offset_hz * t)

    noise_p = float(np.mean(np.abs(iq if noise_ref is None else noise_ref) ** 2))
    # Scale so the *in-channel* burst power sits snr_db above the noise power
    # measured in the same 25 kHz the detector will look at.
    scale = np.sqrt(noise_p * 10 ** (snr_db / 10.0) * (snr_bandwidth_hz() / fs))
    return iq + tx * scale


# --------------------------------------------------------------------------- #

# A burst must stand at least this far above the channel's own quiet floor
# before `burst_edges` will segment anything at all. Below it there is no burst
# structure to find and the honest answer is "none", not a random slicing of the
# noise.
BURST_MIN_SNR_DB = 3.0
FLOOR_MARGIN_DB = 3.0            # threshold never sits nearer the floor than this


def burst_edges(x, fs, smooth_ms=1.0, frac=0.25, min_snr_db=BURST_MIN_SNR_DB):
    """Start/stop sample indices of each burst in a complex signal.

    The instantaneous magnitude of RRC-shaped pi/4-DQPSK swings widely and dips
    near zero inside a burst, so thresholding |x| directly shatters one burst
    into dozens of fragments. Smoothing the *power* over ~1 ms first - which is
    what any real detector does - gives a clean envelope.

    The threshold is placed between the envelope's own **noise floor** (25th
    percentile) and a robust peak (99th percentile), NOT at a fraction of the
    maximum. Thresholding at `frac * p.max()` looks equivalent but is not: it has
    no notion of where the noise sits, so on a moderately weak signal the floor
    itself crosses it and one burst shatters into hundreds of noise fragments.
    That was the cause of the non-monotonic confirmation scores in
    earlier testing - real TETRA scored 3/4 at 8 dB, 2/4 at 10 dB and
    4/4 at 12 dB, because at 10 dB `examine.active_region` was concatenating 108
    slivers of noise and measuring their spliced spectrum. Anchoring to the floor
    makes the segmentation monotonic in SNR, which is what a detector needs.

    On a clean synthetic signal (silence between bursts) the floor is ~0 and this
    reduces to the old `frac * peak` behaviour, so the generator's own self-test
    is unchanged.
    """
    w = max(1, int(smooth_ms * 1e-3 * fs))
    p = np.convolve(np.abs(x) ** 2, np.ones(w) / w, mode="same")
    if p.size == 0:
        return np.empty(0, int), np.empty(0, int)
    floor = float(np.percentile(p, 25))
    # A ROBUST high level, not p.max(). The raw maximum is an outlier: measured
    # against real recorded off-air noise it ran 3-6x the 99.9th percentile at
    # low SNR, which pushed the threshold so high that real bursts stopped
    # crossing it cleanly - 52 clean bursts became 27 fragments and the measured
    # bandwidth collapsed from 18.5 kHz to 0.6 kHz. The 99.9th percentile still
    # lands inside a single sparse burst (one 14 ms burst is ~0.2-0.5 % of a
    # 3-6 s slice, well above the 0.1 % tail) and ignores lone spikes.
    # Underestimating this level is safe - it only lowers the threshold, and the
    # floor margin below stops noise fragmenting. Overestimating is what broke.
    peak = float(np.percentile(p, 99.9))
    # Nothing stands far enough above the floor to be a burst.
    if peak <= floor * 10 ** (min_snr_db / 10.0):
        return np.empty(0, int), np.empty(0, int)
    # `frac` of the way from the floor up to the peak, but never nearer the floor
    # than FLOOR_MARGIN_DB. The margin is what stops noise fragmenting; the
    # floor-to-peak term is what keeps a strong burst cleanly segmented. Using a
    # high percentile instead of the peak fails on a *sparse* signal, where one
    # 14 ms burst in 1.2 s is well under 1 % of the samples and every percentile
    # up to the 99th is still pure noise.
    on = p > max(floor + frac * (peak - floor),
                 floor * 10 ** (FLOOR_MARGIN_DB / 10.0))
    edges = np.diff(on.astype(np.int8))
    starts = np.flatnonzero(edges == 1)
    stops = np.flatnonzero(edges == -1)
    if stops.size and starts.size and stops[0] < starts[0]:
        stops = stops[1:]
    n = min(len(starts), len(stops))
    return starts[:n], stops[:n]


def selftest():
    """Verify the generator produces what the detector expects."""
    fs = 6_000_000.0
    ok = True

    def check(name, got, want, tol, unit=""):
        nonlocal ok
        good = abs(got - want) <= tol
        ok &= good
        print(f"  [{'PASS' if good else 'FAIL'}] {name}: "
              f"{got:.4g}{unit} (expect {want:.4g}{unit} +/- {tol:g}{unit})")

    print("tetra_gen self-test")

    # --- occupied bandwidth --------------------------------------------------
    x = make_transmission(0.2, fs, seed=1)
    seg = x[np.abs(x) > 0.1 * np.max(np.abs(x))][:200_000]
    spec = np.fft.fftshift(np.abs(np.fft.fft(seg, 65536)) ** 2)
    f = np.fft.fftshift(np.fft.fftfreq(65536, 1 / fs))
    p = spec / spec.max()
    inband = f[p > 10 ** (-2.0)]          # -20 dB points
    bw = inband.max() - inband.min()
    check("occupied bandwidth (-20 dB)", bw / 1e3, 24.3, 4.0, " kHz")

    # --- burst duration ------------------------------------------------------
    x2 = make_transmission(0.30, fs, seed=2)
    starts, stops = burst_edges(x2, fs)
    check("bursts found in 300 ms", len(starts), 5, 1.5)
    check("normal burst duration", np.median(stops - starts) / fs * 1e3,
          12.8, 2.0, " ms")

    # --- frame periodicity ---------------------------------------------------
    check("frame period", float(np.median(np.diff(starts))) / fs * 1e3,
          56.67, 1.0, " ms")

    # --- control burst is about half a slot ----------------------------------
    x3 = make_transmission(0.30, fs, kind="cub", seed=3)
    s3, e3 = burst_edges(x3, fs)
    check("control burst duration", np.median(e3 - s3) / fs * 1e3, 6.2, 2.0, " ms")

    # --- injection SNR is honoured -------------------------------------------
    rng = np.random.default_rng(0)
    noise = (rng.normal(size=600_000) + 1j * rng.normal(size=600_000)) / np.sqrt(2)
    for want_snr in (6.0, 12.0, 20.0):
        y = inject(noise.copy(), fs, offset_hz=0.0, snr_db=want_snr, seed=5)
        added = y - noise
        act = np.abs(added) > 0.2 * np.max(np.abs(added))
        if act.sum() < 1000:
            print(f"  [FAIL] injection at {want_snr} dB: no burst found")
            ok = False
            continue
        sig_p = float(np.mean(np.abs(added[act]) ** 2))
        # noise power inside one 25 kHz channel
        n_p = float(np.mean(np.abs(noise) ** 2)) * (snr_bandwidth_hz() / fs)
        check(f"injected SNR @{want_snr:g} dB",
              10 * np.log10(sig_p / n_p), want_snr, 1.5, " dB")

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(selftest())
    ap.print_help()


if __name__ == "__main__":
    main()
