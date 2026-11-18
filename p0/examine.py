#!/usr/bin/env python3
"""Examine a saved detection snippet: is it really TETRA, or a false alarm?

`watch.py` writes a 6 s raw-IQ snippet next to every hit. `detect.py` already
decided it was interesting; this tool goes deeper on that one channel and asks
the question the detector cannot fully answer in real time: does the signal carry
the *fingerprints* of pi/4-DQPSK TETRA, or is it a tone / analog voice / noise
that merely passed the gates?

It tunes to the channel, extracts a ~75 kHz baseband slice, and measures three
independent signatures, each of which a false alarm typically fails:

  1. **Occupied bandwidth** - TETRA fills ~18 kHz (~24 kHz at -20 dB). A CW tone is
     a spike; a stray carrier is far narrower or wider.
  2. **Digital, not a spike** - a digitally modulated block is noise-like and flat
     across its band (high spectral flatness, no single dominant bin). A tone is
     the opposite.
  3. **Burst structure on the TETRA grid** - energy comes in bursts whose spacing
     lands on the 56.67 ms frame (or 14.17 ms slot) grid, not at random.

None of these alone is proof, and the tool says so: it reports each signature and
a plain count, never a bare yes. A 6 s snippet with one brief burst can only ever
be "consistent with", which is the honest ceiling here.

    python examine.py <snippet.iq> --freq 383416250        # a specific carrier
    python examine.py <snippet.iq> --channel 136            # by TETRA channel
    python examine.py <snippet.iq> --channel 136 --png out.png
    python examine.py --selftest                            # offline, no file
"""
import argparse
import os
import sys

import numpy as np
from scipy.signal import resample_poly, upfirdn, welch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from channelize import CHANNEL_HZ, BASE_UL_HZ, BASE_DL_HZ
from tetra_gen import FRAME_S, burst_edges

CHANNEL_FS = 75_000.0            # baseband rate after tuning (Nyquist 37.5 kHz)
TETRA_SLOT_MS = 14.167
TETRA_FRAME_MS = FRAME_S * 1e3   # 56.67 ms

# A TETRA uplink burst fills a known fraction of the 14.167 ms slot: 231 of 255
# symbols for a normal burst, 103 for a control burst (ETSI EN 300 392-2 Table 7;
# 1,409 field bursts measured 103.9 and 231.1 symbols above half power, 17 November
# 2026; this said 111 until then). That length is itself a
# fingerprint - DMR keys up for 27.5 ms, GSM for 0.577 ms, P25 for 180 ms, and
# FM/CW never stop - so a lone burst of the right length is real evidence even
# when there is no second burst to measure a grid against.
TETRA_NUB_MS = TETRA_SLOT_MS * 231.0 / 255.0     # ~12.8 ms
TETRA_CUB_MS = TETRA_SLOT_MS * 103.0 / 255.0     # ~5.7 ms
BURST_LEN_TOL_MS = 2.5


def channel_to_hz(ch, centre_hz):
    base = BASE_DL_HZ if centre_hz > 387e6 else BASE_UL_HZ
    return base + ch * CHANNEL_HZ


def load_iq(path, max_seconds=None, fs=6_000_000.0):
    raw = np.fromfile(path, dtype=np.int16)
    if max_seconds:
        raw = raw[:int(max_seconds * fs) * 2]
    return (raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32))


MIX_CHUNK = 1 << 20               # samples per mixing chunk (~8 MB of complex64)

# The first of two decimation stages: a short low-pass, shifted to the channel through its taps,
# that also divides the rate by STAGE1_DOWN (6 MSPS -> 750 kHz). Shifting through the taps is
# exact - sum h[k] x[n-k] e^{-jw(n-k)} = e^{-jwn} sum (h[k] e^{jwk}) x[n-k] - so no oscillator is
# evaluated at the full rate. Its band (passing the 75 kHz kept, stopping well before the 712 kHz
# that would fold onto it) leaves the final anti-alias filter to the second stage, which is the
# same resample_poly design as before at a tenth of the rate.
STAGE1_TAPS = 31
STAGE1_DOWN = 8
STAGE1_CUTOFF_HZ = 200e3


def _stage1_taps(fs):
    from scipy.signal import firwin
    return firwin(STAGE1_TAPS, STAGE1_CUTOFF_HZ, fs=fs, window=("kaiser", 6.0))


_STAGE1 = {}


def tune(iq, fs, centre_hz, target_hz, out_fs=CHANNEL_FS):
    """Shift `target_hz` to DC and decimate to `out_fs` (anti-aliased), in two stages.

    Until 1.2.6 this mixed every input sample with an oscillator and then ran one 1,601-tap
    resample_poly, designed afresh on each call: 6.2 of the 10.5 ms of a candidate check on a PC,
    against 0.3 ms for the recogniser itself. The two-stage version gives the same samples to
    within -43 dB, identical decisions on 785 real checks and 240 injected bursts, and is 2.2
    times faster per check (audit/signal-audit-20261117 in the private archive). Rates whose
    decimation is not a multiple of STAGE1_DOWN use the one-stage path, `tune_direct`.
    """
    n = len(iq)
    down = int(round(fs / out_fs))
    if n == 0:
        return np.zeros(0, np.complex64), fs / down
    # Two stages only where they were measured: the first stage's output (750 kHz at 6 MSPS) at
    # least ten times the final rate, so what folds onto the kept band lies deep in its stop band.
    if down % STAGE1_DOWN or fs / STAGE1_DOWN < 10 * out_fs:
        return tune_direct(iq, fs, centre_hz, target_hz, out_fs)
    df = float(target_hz - centre_hz) / float(fs)      # cycles per input sample
    key = float(fs)
    if key not in _STAGE1:
        _STAGE1[key] = _stage1_taps(key)
    h = _STAGE1[key]
    half = (STAGE1_TAPS - 1) // 2
    k = np.arange(STAGE1_TAPS, dtype=np.float64)
    taps = (h * np.exp(2j * np.pi * df * (k - half))).astype(np.complex64)
    x = np.asarray(iq, dtype=np.complex64)
    # Advance by `half` so the filter is centred on each kept sample (no added delay).
    shifted = np.concatenate([x[half:], np.zeros(half, np.complex64)])
    y = upfirdn(taps, shifted, 1, STAGE1_DOWN)[: (n + STAGE1_DOWN - 1) // STAGE1_DOWN]
    m = np.arange(len(y), dtype=np.float64)
    # The rest of the shift at the lower rate; the phase is reduced mod 1 before float32 (see below).
    y *= np.exp(-2j * np.pi * np.mod(df * STAGE1_DOWN * m, 1.0)).astype(np.complex64)
    y = resample_poly(y, 1, down // STAGE1_DOWN)
    return y.astype(np.complex64), fs / down


def tune_direct(iq, fs, centre_hz, target_hz, out_fs=CHANNEL_FS):
    """Shift `target_hz` to DC and decimate to `out_fs` (anti-aliased), mixing every sample.

    Everything stays **complex64**, and the mixer is built in chunks. The obvious
    one-liner - `iq * np.exp(-2j*np.pi*(f)*np.arange(len(iq))/fs)` - is what this
    used to be, and it is a trap on a 3 s window at 6 MSPS: `np.arange(n)/fs` is
    144 MB of float64, `np.exp` of it is 288 MB of complex128, and passing
    complex128 into `resample_poly` makes the whole 1/80 polyphase decimation run
    in double precision. Measured at 0.77 s per hit on the bench - five times the
    cost of channelising the entire 4.8 MHz band - which on a Pi eats most of the
    3 s real-time budget for a single hit, so the device would fall behind exactly
    when several radios are transmitting.

    The phase is accumulated as a fraction of a cycle in float64 and reduced mod 1
    *before* the cast to float32, because raw phase reaches ~5e7 radians over a
    3 s window and float32 cannot hold that; the modulo keeps every value in
    [0, 1) where float32 has plenty of precision.
    """
    n = len(iq)
    down = int(round(fs / out_fs))
    if n == 0:
        return np.zeros(0, np.complex64), fs / down
    df = float(target_hz - centre_hz) / float(fs)      # cycles per sample
    bb = np.empty(n, np.complex64)
    two_pi = np.float32(-2.0 * np.pi)
    for i in range(0, n, MIX_CHUNK):
        k = np.arange(i, min(i + MIX_CHUNK, n), dtype=np.float64)
        ph = np.mod(k * df, 1.0).astype(np.float32)    # [0,1) - float32-safe
        ph *= two_pi
        seg = bb[i:i + len(ph)]
        seg.real = np.cos(ph)
        seg.imag = np.sin(ph)
        seg *= np.asarray(iq[i:i + len(ph)], dtype=np.complex64)
    bb = resample_poly(bb, 1, down)          # polyphase FIR handles the anti-alias
    return bb.astype(np.complex64), fs / down


MIN_BURST_MS = 2.0               # below this is noise fragmentation, not a burst


def find_bursts(bb, fs, min_ms=MIN_BURST_MS):
    """Burst edges, with sub-`min_ms` fragments dropped. A near-noise channel
    shatters into thousands of tiny slivers; a real TETRA burst is >4 ms, so the
    filter keeps signal and discards the noise hash."""
    starts, stops = burst_edges(bb, fs, smooth_ms=1.0, frac=0.25)
    keep = [(a, b) for a, b in zip(starts, stops)
            if (b - a) / fs * 1e3 >= min_ms]
    if not keep:
        return np.array([], int), np.array([], int)
    a, b = zip(*keep)
    return np.array(a), np.array(b)


MIN_ACTIVE_SAMPLES = 128         # short control bursts must not fall back to silence


def active_region(bb, fs):
    """Concatenate the burst samples, so the spectrum reflects the signal, not the
    silence around it. Falls back to the whole slice if no clean bursts.

    The floor was 4096 samples, which at the 75 kHz channel rate is 55 ms - more
    than three TETRA slots. A radio that is merely staying registered on the
    network sends **one** ~14 ms burst (about 1050 samples), so its own burst was
    always discarded and its spectrum measured over 3 s of mostly silence. That
    is precisely the signal this device exists to catch, so the floor is now 512
    samples and `spectral_features` shortens its Welch segment to match.
    """
    starts, stops = find_bursts(bb, fs)
    if len(starts) == 0:
        return bb
    act = np.concatenate([bb[a:b] for a, b in zip(starts, stops)])
    return act if len(act) >= MIN_ACTIVE_SAMPLES else bb


def spectral_features(bb, fs):
    """Noise-floor-relative spectrum stats: occupied bandwidth, peak concentration.

    Bandwidth is measured against the noise floor (estimated from the band edges),
    not against the peak - otherwise the floor itself fills the band. Peak
    concentration (fraction of signal power in the single strongest bin) separates
    a CW tone (~all power in one bin) from a digital block (spread over hundreds).
    """
    # detrend=False so a DC / near-DC carrier is NOT silently removed - a CW spur
    # must stay visible to be flagged. (LO leakage is out-of-band when we tune off
    # the capture centre, as here, so keeping DC costs nothing.)
    # Segment length adapts to what we actually have: a lone registration burst
    # is ~1050 samples at 75 kHz, and a fixed nperseg=2048 would silently make
    # welch analyse zero-padded rubbish (or refuse). 512 still gives ~146 Hz bins
    # at the channel rate - ample to size an 18 kHz carrier.
    if len(bb) == 0:
        return {"occ_bw_hz": 0.0, "peak_bin_frac": 1.0, "peak_snr_db": 0.0,
                "f": np.empty(0), "p": np.empty(0), "floor": 0.0}
    nper = int(min(len(bb), 2048, max(64, 2 ** int(np.floor(np.log2(max(len(bb), 64)))))))
    f, p = welch(bb, fs=fs, nperseg=nper, return_onesided=False, detrend=False)
    f = np.fft.fftshift(f)
    p = np.fft.fftshift(p)
    guard = np.abs(f) > 22_000                # outside a 25 kHz TETRA channel
    floor = float(np.median(p[guard])) if guard.any() else float(np.median(p))
    sig = np.clip(p - floor, 0.0, None)       # signal above the noise floor
    peak = float(sig.max())
    if peak <= 0:
        return {"occ_bw_hz": 0.0, "peak_bin_frac": 1.0, "peak_snr_db": 0.0,
                "f": f, "p": p, "floor": floor}
    mask = sig > 0.1 * peak                    # -10 dB of the noise-subtracted peak
    occ_bw = (f[mask].max() - f[mask].min()) if mask.any() else 0.0
    return {"occ_bw_hz": float(occ_bw),
            "peak_bin_frac": float(peak / (sig.sum() + 1e-30)),
            "peak_snr_db": float(10 * np.log10(peak / floor + 1e-30))
            if floor > 0 else 0.0,
            "f": f, "p": p, "floor": floor}


def burst_structure(bb, fs):
    """Bursts and how their spacing lines up with the TETRA frame/slot grid."""
    starts, stops = find_bursts(bb, fs)
    if len(starts) == 0:
        return {"n": 0, "durations_ms": [], "gaps_ms": [], "grid": None,
                "slot_len": False}
    dur = [(b - a) / fs * 1e3 for a, b in zip(starts, stops)]
    gaps = [(starts[i + 1] - starts[i]) / fs * 1e3 for i in range(len(starts) - 1)]
    # For the LONE-burst case only the normal uplink burst counts. A control
    # burst is ~6.2 ms, and noise fragments cluster at 2-6 ms, so "one 6 ms
    # burst" is not evidence of anything - a live false alarm confirmed at 3/4
    # on a 4.48 ms noise fragment because |4.48 - 6.17| was inside tolerance.
    # With two or more bursts the *timing* carries the evidence and either burst
    # length is fine; that case is handled by `grid` below.
    slot_len = bool(dur) and abs(float(np.median(dur)) - TETRA_NUB_MS) <= BURST_LEN_TOL_MS
    grid = None
    if gaps:
        # nearest match of the median gap to a multiple of the slot period
        g = float(np.median(gaps))
        for base, name in ((TETRA_FRAME_MS, "frame"), (TETRA_SLOT_MS, "slot")):
            k = round(g / base)
            if k >= 1 and abs(g - k * base) <= 0.15 * base:
                grid = f"{name}x{k} ({g:.1f}ms ~= {k}*{base:.2f})"
                break
    return {"n": len(starts), "durations_ms": dur, "gaps_ms": gaps, "grid": grid,
            "slot_len": slot_len}


def symbol_rate_line(bb, fs):
    """Look for a cyclostationary line near 18 kHz in the |x|^2 spectrum.

    A linearly modulated burst has a timing tone at the symbol rate; raising to
    the envelope's second power and taking the spectrum exposes it. Returns
    (peak_freq_hz, prominence) of the strongest line in 12-24 kHz, or (None, 0).
    """
    x = np.abs(bb) ** 2
    x = x - x.mean()
    if len(x) < 4096:
        return None, 0.0
    f, p = welch(x, fs=fs, nperseg=min(8192, len(x)), return_onesided=True)
    band = (f > 12_000) & (f < 24_000)
    if not band.any():
        return None, 0.0
    i = np.flatnonzero(band)[np.argmax(p[band])]
    prom = float(p[i] / (np.median(p[(f > 4000) & (f < 30000)]) + 1e-30))
    return float(f[i]), prom


def analyse(bb, fs):
    import phy
    bs = burst_structure(bb, fs)
    act = active_region(bb, fs)
    sf = spectral_features(act, fs)
    sr_hz, sr_prom = symbol_rate_line(act, fs)
    occ_bw = sf["occ_bw_hz"]
    # four honest, independent signatures - each one a typical false alarm fails
    sig_bw = 12_000 <= occ_bw <= 30_000                     # TETRA fills ~18-24 kHz
    # digital block: power spread across the band (not a CW spike) AND actually
    # above the noise floor - low peak-concentration alone also describes noise.
    # A digital block is flat across ITS OWN band. Broadband noise is also flat -
    # and it fills the whole 75 kHz slice. Without an upper bound this test fires
    # on pure noise, which is exactly how a live false alarm reached 3/4 with a
    # measured occupied bandwidth of 68.4 kHz. TETRA is 18-24 kHz; 35 kHz leaves
    # generous margin for a mistuned or noisy measurement while still excluding
    # "the entire slice is lit up".
    sig_digital = (sf["peak_bin_frac"] < 0.15) and (5_000 < occ_bw < 35_000) \
        and (sf["peak_snr_db"] > 3.0)
    # bursts on the grid: a real periodic frame/slot match, not a coincidence among
    # noise fragments (those are filtered, and thousands would never be periodic).
    # Either several bursts whose spacing lands on the frame/slot grid, or a
    # single burst of exactly TETRA slot length. The second case is what a radio
    # that is only staying registered on the network ever produces, and rejecting
    # it was demoting every sparse hit to "unconfirmed".
    sig_grid = ((bs["grid"] is not None and 2 <= bs["n"] <= 120)
                or (bs["n"] == 1 and bs.get("slot_len", False)))
    # Nothing is "consistent with TETRA" while the channel measures wider than a
    # TETRA channel can be: that is the receiver looking at noise. This is a
    # backstop, not a signature - it cannot add confidence, only withhold it.
    if occ_bw > 35_000:
        sig_grid = sig_grid and False
    sig_symrate = sr_hz is not None and 15_000 <= sr_hz <= 21_000 and sr_prom > 3.0
    starts, stops = find_bursts(bb, fs)
    physical = phy.analyse(bb, fs, starts, stops)
    return {
        "accepted": bool(physical["accepted"]),
        "phy": physical,
        "occ_bw_khz": round(occ_bw / 1e3, 1),
        "peak_bin_frac": round(sf["peak_bin_frac"], 3),
        "peak_snr_db": round(sf["peak_snr_db"], 1),
        "bursts": bs, "symrate_hz": None if sr_hz is None else round(sr_hz),
        "symrate_prominence": round(sr_prom, 1),
        "sig_bandwidth": sig_bw, "sig_digital": sig_digital,
        "sig_grid": sig_grid, "sig_symrate": sig_symrate,
        "signatures_present": int(sig_bw) + int(sig_digital) + int(sig_grid)
                              + int(sig_symrate),
        "_occ_bw_hz": occ_bw,
    }


def report(r, title=""):
    print(f"\n=== examine {title} ===")
    print(f"  occupied bandwidth : {r['occ_bw_khz']} kHz (above noise floor)   "
          f"[TETRA ~18-24 kHz]")
    print(f"  peak concentration : {r['peak_bin_frac']}  (tone ~1, digital <<1); "
          f"in-band SNR {r['peak_snr_db']} dB")
    b = r["bursts"]
    dur = ", ".join(f"{d:.1f}" for d in b["durations_ms"][:6])
    print(f"  bursts             : {b['n']}  durations[ms]: {dur or '-'}")
    print(f"  burst grid         : {b['grid'] or 'no clear frame/slot match'}")
    print(f"  symbol-rate line   : {r['symrate_hz']} Hz "
          f"(prominence {r['symrate_prominence']})   [TETRA 18000]")
    print("  signatures:")
    for k, lab in (("sig_bandwidth", "TETRA-width band (~18-24 kHz)"),
                   ("sig_digital", "digital block, not a tone"),
                   ("sig_grid", "burst timing/length on the TETRA slot structure"),
                   ("sig_symrate", "~18 kHz symbol-rate line")):
        print(f"    [{'YES' if r[k] else 'no '}] {lab}")
    n = r["signatures_present"]
    print(f"  legacy envelope/spectrum score: {n}/4 (diagnostic only)")
    print("  physical-layer decision: " + ("TETRA training match" if r["accepted"] else "unconfirmed"))
    print(f"  training matches: {r['phy']['matched_bursts']}/{r['phy']['tested_bursts']} bursts; vehicle identity unknown")


def make_png(bb, fs, r, path, title=""):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    w = max(1, int(1e-3 * fs))
    env = np.convolve(np.abs(bb) ** 2, np.ones(w) / w, mode="same")
    env_db = 10 * np.log10(env + 1e-12)
    t = np.arange(len(bb)) / fs
    f, p = welch(bb, fs=fs, nperseg=2048, return_onesided=False)
    f = np.fft.fftshift(f); p = 10 * np.log10(np.fft.fftshift(p) + 1e-30)

    fig, ax = plt.subplots(2, 1, figsize=(12, 7))
    ax[0].plot(t, env_db, lw=0.5, color="#1a3a5c")
    ax[0].set(title=f"Power envelope - {title}", xlabel="time (s)",
              ylabel="dB (rel)")
    for a, b in zip(*find_bursts(bb, fs)):
        ax[0].axvspan(a / fs, b / fs, color="#c05621", alpha=0.25)
    ax[1].plot(f / 1e3, p, lw=0.7, color="#1a3a5c")
    ax[1].axvspan(-r["occ_bw_khz"] / 2, r["occ_bw_khz"] / 2, color="#2f855a",
                  alpha=0.12, label=f"occupied bw {r['occ_bw_khz']} kHz")
    ax[1].set(title="Channel spectrum", xlabel="offset from carrier (kHz)",
              ylabel="dB", xlim=(-40, 40))
    ax[1].legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


# --------------------------------------------------------------------------- #

def selftest():
    from tetra_gen import inject
    fs = 6_000_000.0
    centre = 382.5e6
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{'  ' + detail if detail else ''}")

    print("examine self-test (offline, synthetic truth)")
    rng = np.random.default_rng(0)
    n = int(1.0 * fs)
    noise = ((rng.normal(size=n) + 1j * rng.normal(size=n)) / np.sqrt(2)).astype(np.complex64)
    off = 916_250.0                         # same offset as the real ch-136 hit

    # (1) real TETRA injected -> should light up the signatures
    y = inject(noise.copy(), fs, offset_hz=off, snr_db=16.0, seed=7)
    bb, f2 = tune(y, fs, centre, centre + off)
    r = analyse(bb, f2)
    report(r, "synthetic TETRA @16 dB")
    check("TETRA: bandwidth signature", r["sig_bandwidth"],
          f"{r['occ_bw_khz']} kHz")
    check("TETRA: digital (not a tone)", r["sig_digital"],
          f"peak_frac {r['peak_bin_frac']}")
    check("TETRA: burst structure found", r["bursts"]["n"] >= 3,
          f"{r['bursts']['n']} bursts, grid={r['bursts']['grid']}")
    check("TETRA: >=3/4 signatures -> consistent", r["signatures_present"] >= 3,
          f"{r['signatures_present']}/4")

    # (2) a pure CW tone, 6 kHz inside the channel -> must be rejected as narrow
    # and tone-like (not at DC, so it is a realistic in-channel interferer)
    t = np.arange(n) / fs
    tone = noise + 6.0 * np.exp(2j * np.pi * (off + 6_000) * t).astype(np.complex64)
    bb2, f3 = tune(tone, fs, centre, centre + off)
    rt = analyse(bb2, f3)
    report(rt, "pure CW tone")
    check("CW tone: NOT flagged digital", not rt["sig_digital"],
          f"peak_frac {rt['peak_bin_frac']}")
    check("CW tone: NOT TETRA-width", not rt["sig_bandwidth"],
          f"{rt['occ_bw_khz']} kHz")
    check("CW tone: fewer than 3 signatures", rt["signatures_present"] < 3,
          f"{rt['signatures_present']}/4")

    # (3) noise only -> no bandwidth block, not TETRA
    bb3, f4 = tune(noise.copy(), fs, centre, centre + off)
    rn = analyse(bb3, f4)
    check("noise only: not >=3 signatures", rn["signatures_present"] < 3,
          f"{rn['signatures_present']}/4")

    # (4) THE LIVE FALSE ALARM, as a regression test.
    # A real 20-minute run produced one CONFIRMED hit at 3/4 that was not TETRA.
    # Re-examining its saved snippet scored 1/4, and the disagreement came from
    # two signatures that noise could satisfy:
    #   * sig_digital had no upper bandwidth bound, so noise filling the whole
    #     75 kHz slice (measured 68.4 kHz) read as "a flat digital block";
    #   * the lone-burst grid test accepted control-burst length, and a 4.48 ms
    #     noise fragment sits within tolerance of 6.17 ms.
    # Both must stay closed. Broadband noise is the exact shape that broke it.
    check("noise is NOT called a digital block (needs a bounded bandwidth)",
          not rn["sig_digital"],
          f"occ_bw {rn['occ_bw_khz']} kHz, peak_frac {rn['peak_bin_frac']}")
    check("noise gets no burst-grid signature", not rn["sig_grid"],
          f"{rn['bursts']['n']} fragments, grid={rn['bursts']['grid']}")

    # A lone burst of CONTROL length is not evidence: noise fragments live there.
    for ms, want, why in ((TETRA_NUB_MS, True, "normal uplink burst - is evidence"),
                          (TETRA_CUB_MS, False, "control length - too close to noise"),
                          (4.48, False, "the exact fragment that caused the live "
                                        "false alarm")):
        one = np.zeros(int(0.5 * fs), np.complex64)
        st = int(0.2 * fs)
        ln = int(ms * 1e-3 * fs)
        one[st:st + ln] = inject(np.zeros(ln, np.complex64), fs, offset_hz=0.0,
                                 snr_db=30.0, seed=2)[:ln] + 1.0
        bs = burst_structure(one, fs)
        check(f"lone {ms:.2f} ms burst -> slot_len {want}",
              bs.get("slot_len") is want, why)

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("snippet", nargs="?", help="raw int16 IQ snippet from watch.py")
    ap.add_argument("--fs", type=float, default=6_000_000.0)
    ap.add_argument("--centre", type=float, default=382.5e6,
                    help="capture centre Hz (watch.py uplink default 382.5 MHz)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--freq", type=float, help="carrier to examine, Hz")
    g.add_argument("--channel", type=int, help="TETRA channel number")
    ap.add_argument("--png", help="write a PNG here (default: alongside the snippet)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        raise SystemExit(selftest())
    if not args.snippet:
        ap.error("give a snippet path, or --selftest")

    if args.snippet.endswith(".c64"):
        import json
        import confirmation
        meta_path = args.snippet + ".json"
        if not os.path.isfile(meta_path):
            meta_path = os.path.splitext(args.snippet)[0] + ".json"
        with open(meta_path, encoding="utf-8") as fh:
            metadata = json.load(fh)
        samples = np.fromfile(args.snippet, dtype="<c8")
        accepted = confirmation.replay(samples, metadata)
        print(json.dumps({"accepted": accepted, "method": "short-burst-phy-v1",
                          "segments": len(metadata["segments"]),
                          "meaning": "TETRA training evidence; vehicle and distance unknown"}, indent=2))
        return 0

    target = (args.freq if args.freq else
              channel_to_hz(args.channel, args.centre) if args.channel is not None
              else args.centre)
    iq = load_iq(args.snippet, fs=args.fs)
    print(f"loaded {len(iq)/args.fs:.2f} s from {os.path.basename(args.snippet)}; "
          f"tuning to {target/1e6:.4f} MHz")
    bb, f2 = tune(iq, args.fs, args.centre, target)
    r = analyse(bb, f2)
    title = f"{target/1e6:.4f} MHz"
    report(r, title)
    png = args.png or (os.path.splitext(args.snippet)[0] + "_examine.png")
    try:
        make_png(bb, f2, r, png, title)
        print(f"  plot: {png}")
    except Exception as e:                   # plotting must never sink the analysis
        print(f"  (plot skipped: {e})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
