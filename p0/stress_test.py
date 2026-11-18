#!/usr/bin/env python3
"""Adversarial validation: try hard to make the detector fire when it should not.

A detector is only as trustworthy as the things it *rejects*. This throws four
kinds of input at it and checks the outcome:

  A. REAL off-air captures we already hold - including 40 s of genuine C2000
     TETRA *downlink* (continuous carriers). The uplink burst-detector must stay
     silent on all of them: continuous carriers, an empty band, and
     antenna-disconnected internal spurs are exactly what must NOT raise an alert.

  B. REAL TETRA modulation (the sigidwiki recording), gated to uplink timing -
     the positive control. This MUST fire, or the test rig itself is broken.

  C. SYNTHETIC non-TETRA signals with real-world parameters - CW, FM, a
     continuous digital carrier, DMR-like and GSM-like TDMA, impulsive noise,
     and burst trains with the wrong period or no period. None may fire.
     One deliberately *hard* case (a narrowband beacon on TETRA's exact timing)
     is included and characterised honestly rather than hidden.

  D. BROKEN input - empty, zeros, clipped to the rail, NaN/inf, a DC spike, a
     lone impulse. None may crash the pipeline.

    python stress_test.py            # full suite
    python stress_test.py --quick    # skip the large real-capture reads
"""
import argparse
import glob
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
from channelize import channelise, CHANNEL_HZ
from detect import detect, load_downlink_channels
from validate_real import load_real_tetra, gate_to_bursts, real_noise
from tetra_gen import snr_bandwidth_hz, make_transmission, FRAME_S
from detect import BurstHistory

FS = 6_000_000.0
CENTRE = 382.5e6
TARGET_CH = 100
TARGET_HZ = 380.0125e6 + TARGET_CH * CHANNEL_HZ
OFFSET = TARGET_HZ - CENTRE
NOISE_S = 8.0

_pass = 0
_fail = 0


def result(name, ok, detail=""):
    global _pass, _fail
    _pass += ok
    _fail += not ok
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          f"{'  -  ' + detail if detail else ''}")


# --------------------------------------------------------------------------- #
# signal generators - all return complex baseband centred at 0, length N

def _norm(sig):
    on = sig[np.abs(sig) > 1e-12]
    p = float(np.mean(np.abs(on) ** 2)) if on.size else 1.0
    return sig / np.sqrt(max(p, 1e-30))


def cw(n):
    return np.ones(n, np.complex64)


def fm_voice(n, fs, dev_hz=3000.0, audio_hz=1500.0):
    rng = np.random.default_rng(1)
    m = rng.normal(size=n)
    # crude low-pass to ~audio_hz
    k = max(1, int(fs / audio_hz))
    m = np.convolve(m, np.ones(k) / k, "same")
    ph = 2 * np.pi * dev_hz / fs * np.cumsum(m)
    return np.exp(1j * ph).astype(np.complex64)


def digital_continuous(n, fs, sym_rate=4800.0, seed=2):
    """A continuously-keyed pi/4-ish digital carrier (P25/DMR-carrier-like)."""
    rng = np.random.default_rng(seed)
    nsym = int(n / fs * sym_rate) + 2
    syms = np.exp(1j * (rng.integers(0, 4, nsym) * np.pi / 2 + np.pi / 4))
    sps = int(fs / sym_rate)
    up = np.zeros(nsym * sps, np.complex64)
    up[::sps] = syms
    h = np.ones(sps) / sps                       # rectangular pulse - enough here
    sig = np.convolve(up, h, "same")[:n]
    if len(sig) < n:
        sig = np.pad(sig, (0, n - len(sig)))
    return sig.astype(np.complex64)


def gate(base, fs, burst_ms, period_ms, aperiodic=False, jitter_ms=0.0, seed=3):
    """Key `base` on for burst_ms once per period_ms (or at random times)."""
    n = len(base)
    out = np.zeros(n, np.complex64)
    bn = int(burst_ms * 1e-3 * fs)
    ramp = max(1, int(0.0003 * fs))
    w = np.ones(bn)
    w[:ramp] = np.linspace(0, 1, ramp)
    w[-ramp:] = np.linspace(1, 0, ramp)
    rng = np.random.default_rng(seed)
    if aperiodic:
        n_bursts = int(n / (period_ms * 1e-3 * fs))
        starts = np.sort(rng.integers(0, max(1, n - bn), size=n_bursts))
    else:
        pn = int(period_ms * 1e-3 * fs)
        starts = np.arange(0, n - bn, pn)
        if jitter_ms:
            starts = starts + rng.integers(-int(jitter_ms * 1e-3 * fs),
                                           int(jitter_ms * 1e-3 * fs) + 1,
                                           size=len(starts))
    for s in starts:
        s = int(max(0, min(n - bn, s)))
        out[s:s + bn] = base[s:s + bn] * w
    return out


def impulses(n, fs, rate_hz=200.0, width_ms=0.5, seed=5):
    rng = np.random.default_rng(seed)
    out = np.zeros(n, np.complex64)
    bn = max(1, int(width_ms * 1e-3 * fs))
    for _ in range(int(n / fs * rate_hz)):
        s = rng.integers(0, max(1, n - bn))
        out[s:s + bn] = (rng.normal(size=bn) + 1j * rng.normal(size=bn))
    return out


def inject(base, fs, noise, snr_db=22.0):
    """Place `base` at the target channel in real noise at a strong SNR.

    SNR is defined over `snr_bandwidth_hz()` - the signal's own occupied
    bandwidth - the same definition tetra_gen and validate_real use. It used to
    be defined over 25 kHz x 1.35 here, which made every label in this file
    1.4 dB more generous than the same label elsewhere.
    """
    base = _norm(base)
    noise_p = float(np.mean(np.abs(noise) ** 2))
    scale = np.sqrt(noise_p * 10 ** (snr_db / 10.0) * (snr_bandwidth_hz() / fs))
    t = np.arange(len(base)) / fs
    return noise + base * np.exp(2j * np.pi * OFFSET * t).astype(np.complex64) * scale


WINDOW_S = 0.25         # must match watch.WINDOW_S; tests/test_runtime.py checks that it does


def fires_windowed(y, fs=FS, centre=CENTRE, dl=None, threshold_db=6.0):
    """Run the signal through analysis windows sharing one BurstHistory.

    This is what `watch.py` does in the field, and it is the only way to exercise
    the sparse path: a registration pulse every few seconds puts at most one
    burst in a window, so a single-shot `detect()` can never see it.
    """
    hist = BurstHistory()
    want = int(WINDOW_S * fs)
    out = []
    for i in range(0, max(len(y) - want + 1, 0), want):
        ch = channelise(y[i:i + want], fs, centre, apply_cal=False)
        out += [h for h in detect(ch, threshold_db=threshold_db,
                                  downlink_channels=dl, history=hist,
                                  t_abs_s=i / fs)
                if h["tetra_channel"] == TARGET_CH]
    return out


def sparse_signal(noise, interval_s, fs=FS, kind="nub", seed=3):
    """A radio that only stays registered: one burst every `interval_s`."""
    n = len(noise)
    frames = {int(round(t / FRAME_S))
              for t in np.arange(0.4, n / fs, interval_s)}
    tx = make_transmission(n / fs, fs, kind=kind, frames=frames, seed=seed)
    return tx, len(frames)


def fires(y, fs=FS, centre=CENTRE, dl=None):
    """Return (hit_on_target_channel, total_hits) for a signal block."""
    hits = detect(channelise(y, fs, centre, apply_cal=False), downlink_channels=dl)
    on_target = [h for h in hits if h["tetra_channel"] == TARGET_CH]
    return (on_target[0] if on_target else None), len(hits)


# --------------------------------------------------------------------------- #

def part_a_real_captures(dl):
    print("\nA. REAL off-air captures - the uplink detector must stay silent")
    sidecars = sorted(glob.glob(os.path.join(paths.IQ_DIR, "*.json")))
    sidecars = [s for s in sidecars if "_carriers" not in s]
    if not sidecars:
        print("  (no local captures found)")
        return
    tested = 0
    for sc in sidecars:
        try:
            meta = json.load(open(sc, encoding="utf-8"))
            fs = float(meta["sample_rate_hz"])
            centre = float(meta["centre_hz"])
        except (OSError, ValueError, KeyError):
            continue
        iqp = sc[:-5] + ".iq"
        if not os.path.exists(iqp):
            continue
        want = int(min(NOISE_S, 8.0) * fs)
        raw = np.fromfile(iqp, dtype=np.int16, count=want * 2)
        if raw.size < 1000:
            continue
        iq = (raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)
              ).astype(np.complex64)
        try:
            hits = detect(channelise(iq, fs, centre, apply_cal=False),
                          downlink_channels=dl)
        except Exception as e:                       # noqa: BLE001
            result(os.path.basename(iqp)[:46], False, f"CRASHED: {e}")
            continue
        tag = os.path.basename(iqp).split("_")[1]
        kind = ("downlink (continuous TETRA carriers)" if centre > 388e6 else
                "uplink / no-antenna")
        result(f"{tag:22s} {kind}", len(hits) == 0,
               f"{len(hits)} uplink-style hit(s) in {want/fs:.0f}s"
               + (f" -> {[h['tetra_channel'] for h in hits][:6]}" if hits else ""))
        tested += 1
    if not tested:
        print("  (no readable captures)")


def part_b_positive(noise, dl):
    print("\nB. REAL TETRA modulation, gated to uplink timing - MUST fire")
    try:
        real = load_real_tetra(FS, max_seconds=NOISE_S)  # trim before upsample (Pi RAM)
    except Exception as e:                           # noqa: BLE001
        result("load real TETRA recording", False, str(e))
        return
    real = real[:len(noise)] if len(real) >= len(noise) else np.pad(
        real, (0, len(noise) - len(real)))
    bursts = gate_to_bursts(real, FS)
    # Assert detection only where it is deterministic on EVERY noise bed.
    #
    # This floor was 8 dB, and 8 dB passed on the bench while failing on the Pi.
    # Two things were hiding behind each other. First, "SNR" meant two different
    # things in this project - 25 kHz x 1.35 here, the signal's own 24.3 kHz
    # occupied bandwidth in tetra_gen - so every label in this file used to be
    # 1.4 dB more generous than the same label elsewhere. That is now unified on
    # tetra_gen.snr_bandwidth_hz(). Second, the bench has real recorded off-air
    # noise and the Pi falls back to white Gaussian, and the real bed is the
    # kinder of the two. Sweeping the honest definition across six beds:
    #
    #     bed                      8 dB   9 dB  10 dB  11 dB  12 dB
    #     synthetic seeds 0-4        no    YES    YES    YES    YES
    #     real uplink-baseline      YES    YES    YES    YES    YES
    #
    # 9 dB is where every bed agrees; 10 dB is that with a dB of margin. Anything
    # lower is asserting on the noise realisation, not on the detector.
    #
    for snr in (10.0, 12.0, 20.0):
        y = inject(bursts, FS, noise, snr_db=snr)
        hit, total = fires(y, dl=dl)
        result(f"real TETRA @ {snr:.0f} dB detected", hit is not None,
               (f"perio {hit['periodicity']:.2f}, period "
                f"{hit['measured_period_ms']}ms, {hit['proximity']}"
                if hit else "MISS - rig or detector broken"))
    # The marginal region itself: reported, never asserted (bed-dependent by
    # design). 6 dB is detect()'s own 'highway' threshold; 8 dB is where the real
    # and synthetic beds part company.
    for snr in (6.0, 8.0):
        hitm, _ = fires(inject(bursts, FS, noise, snr_db=snr), dl=dl)
        print(f"    (info) at {snr:.0f} dB: {'fired' if hitm else 'missed'}"
              f" - marginal, depends on the noise bed, not asserted")


def part_c_confusers(noise, dl):
    print("\nC. SYNTHETIC non-TETRA signals - none of these may fire")
    n = len(noise)
    cases = [
        ("continuous CW carrier", cw(n)),
        ("FM voice carrier (3 kHz dev)", fm_voice(n, FS)),
        ("continuous digital carrier (4800 sym/s)", digital_continuous(n, FS)),
        ("DMR-like TDMA (27.5 ms slot / 60 ms)",
         gate(digital_continuous(n, FS, sym_rate=4800), FS, 27.5, 60.0)),
        ("GSM-like TDMA (0.577 ms / 4.615 ms)",
         gate(digital_continuous(n, FS, sym_rate=270833), FS, 0.577, 4.615)),
        ("impulsive noise (200/s, 0.5 ms)", impulses(n, FS)),
        ("aperiodic 14 ms bursts (random timing)",
         gate(cw(n), FS, 14.17, 56.67, aperiodic=True)),
        ("14 ms bursts on WRONG period (40 ms)",
         gate(cw(n), FS, 14.17, 40.0)),
        ("narrowband beacon on TETRA's EXACT timing (bandwidth gate)",
         gate(cw(n), FS, 14.17, 56.67)),
        ("real TETRA modulation on WRONG period (40 ms)", None),  # filled below
    ]
    # the wrong-period-real case reuses the real recording
    try:
        real = load_real_tetra(FS, max_seconds=NOISE_S)  # trim before upsample (Pi RAM)
        real = real[:n] if len(real) >= n else np.pad(real, (0, n - len(real)))
        cases[-1] = ("real TETRA modulation on WRONG period (40 ms)",
                     gate(real, FS, 14.17, 40.0))
    except Exception:                                # noqa: BLE001
        cases.pop()

    for name, base in cases:
        try:
            hit, total = fires(inject(base, FS, noise), dl=dl)
        except Exception as e:                       # noqa: BLE001
            result(name, False, f"CRASHED: {e}")
            continue
        result(name, hit is None,
               "silent" if hit is None else
               f"FIRED perio {hit['periodicity']:.2f} "
               f"period {hit['measured_period_ms']}ms bw "
               f"{hit['occupied_bw_khz']}kHz ({total} total)")


SPARSE_S = 12.0         # enough for four pulses of a 3 s registration interval


def part_e_sparse(dl):
    """The signal the device actually exists to catch, and its confusers.

    A radio staying registered on the network, not one in a call, sends short bursts
    seconds apart. Those land one
    burst (at most) in an analysis window, so they can only be tested through the
    windowed path with a shared burst history. The negative cases here matter as
    much as the positive one: they are what proves the history did not simply
    lower the bar.
    """
    print("\nE. SPARSE registration-style pulses, windowed")
    noise, _ = real_noise(FS, SPARSE_S)
    n = len(noise)

    # --- positive: a registered radio, one burst every 3 s ------------------
    tx, npulse = sparse_signal(noise, 3.0)
    hits = fires_windowed(inject(tx, FS, noise, snr_db=20.0), dl=dl)
    src = hits[0]["periodicity_source"] if hits else "-"
    result(f"registration pulse every 3 s ({npulse} in {SPARSE_S:.0f} s) detected",
           bool(hits), f"{len(hits)} hit(s), periodicity from {src}"
           if hits else "MISS - the sparse path is broken")
    result("...and it was the burst HISTORY that saw it, not one window",
           bool(hits) and src == "history", f"source={src}")

    # --- negative: same cadence, timing NOT on the TETRA frame grid ---------
    rng = np.random.default_rng(11)
    off = np.zeros(n, np.complex64)
    tx_on = make_transmission(n / FS, FS, frames={0}, seed=5)
    blk = tx_on[:int(0.02 * FS)]
    for t0 in np.arange(0.4, SPARSE_S - 0.1, 3.0):
        # deliberately 20-40 ms off the frame grid: a real radio cannot do this,
        # because it is slot-synchronised to the network.
        st = int((t0 + rng.uniform(0.020, 0.040)) * FS)
        off[st:st + len(blk)] = blk[:max(0, min(len(blk), n - st))]
    hit_off = fires_windowed(inject(off, FS, noise, snr_db=20.0), dl=dl)
    result("same cadence but OFF the 56.67 ms frame grid - rejected",
           not hit_off, "silent" if not hit_off
           else f"FIRED perio {hit_off[0]['periodicity']:.2f} - history is too loose")

    # --- negative: narrowband beacon on the same sparse cadence ------------
    beacon = np.zeros(n, np.complex64)
    bn = int(14.17e-3 * FS)
    for t0 in np.arange(0.4, SPARSE_S - 0.1, 3.0):
        st = int(t0 * FS)
        beacon[st:st + bn] = 1.0
    hit_b = fires_windowed(inject(beacon, FS, noise, snr_db=20.0), dl=dl)
    result("narrowband beacon on the same sparse cadence - rejected",
           not hit_b, "silent" if not hit_b else "FIRED - bandwidth gate leaked")

    # --- negative: right cadence, wrong burst length (DMR-like) ------------
    dmr = gate(digital_continuous(n, FS, sym_rate=4800), FS, 27.5, 3000.0)
    hit_d = fires_windowed(inject(dmr, FS, noise, snr_db=20.0), dl=dl)
    result("sparse 27.5 ms (DMR-length) bursts - rejected",
           not hit_d, "silent" if not hit_d else "FIRED - duration gate leaked")

    # --- negative: pure noise through the same windowed path ---------------
    hit_n = fires_windowed(noise.copy(), dl=dl)
    result("pure recorded noise through the windowed path - silent",
           not hit_n, "silent" if not hit_n
           else f"{len(hit_n)} FALSE hit(s) - history invented periodicity")


def part_f_internal_spur(dl):
    """The +45 dB 384.000 MHz internal line (8 x 48 MHz LPC4370 clock harmonic).

    It sits exactly halfway between uplink channels 159 and 160. The blacklist is
    belt-and-braces; this proves the width and raster defences hold on their own,
    which is what justifies narrowing the guard so those two channels are not
    permanently blind.
    """
    print("\nF. The real +45 dB internal 384.000 MHz spur must not alert")
    noise, _ = real_noise(FS, NOISE_S)
    n = len(noise)
    t = np.arange(n) / FS
    # 383.99623 MHz measured, ~730 Hz wide, +45 dB - reproduced as a CW line with
    # a little drift so it is not an unrealistically perfect delta.
    off = 383_996_234.0 - CENTRE
    drift = 350.0 * np.sin(2 * np.pi * 0.3 * t)
    line = np.exp(2j * np.pi * (off * t + np.cumsum(drift) / FS)).astype(np.complex64)
    amp = np.sqrt(float(np.mean(np.abs(noise) ** 2)) * 10 ** (45 / 10.0)
                  * (snr_bandwidth_hz() / FS))
    y = (noise + line * amp).astype(np.complex64)
    hits = detect(channelise(y, FS, CENTRE, apply_cal=False), downlink_channels=dl)
    near = [h for h in hits if h["tetra_channel"] in (158, 159, 160, 161)]
    result("+45 dB internal spur raises no hit on ch 158-161", not near,
           "silent" if not near
           else f"FIRED on {[h['tetra_channel'] for h in near]}")
    result("...and no hit anywhere else in the band", len(hits) == 0,
           f"{len(hits)} hit(s)")

    # The load-bearing question: with the blacklist switched OFF entirely, do the
    # width and raster gates still reject it? If yes, the guard can stay narrow
    # and channels 159/160 stay usable instead of being permanently blind.
    import detect as _d
    saved = _d.SPUR_GUARD_HZ
    try:
        _d.SPUR_GUARD_HZ = 0.0
        for snr in (45, 55):
            amp2 = np.sqrt(float(np.mean(np.abs(noise) ** 2)) * 10 ** (snr / 10.0)
                           * (snr_bandwidth_hz() / FS))
            y2 = (noise + line * amp2).astype(np.complex64)
            h2 = detect(channelise(y2, FS, CENTRE, apply_cal=False),
                        downlink_channels=dl)
            result(f"spur at +{snr} dB rejected with the blacklist DISABLED",
                   not h2, "silent - width/raster gates are enough"
                   if not h2 else f"FIRED on {[h['tetra_channel'] for h in h2]}")
    finally:
        _d.SPUR_GUARD_HZ = saved
    result("channels 159 and 160 are no longer blacklisted",
           all(not any(abs((380.0125e6 + c * CHANNEL_HZ) - sp) < _d.SPUR_GUARD_HZ
                       for sp in _d.SPUR_BLACKLIST_HZ) for c in (159, 160)),
           f"guard {_d.SPUR_GUARD_HZ/1e3:g} kHz")


def part_d_robustness(dl):
    print("\nD. BROKEN input - must not crash the pipeline")
    n = int(NOISE_S * FS)
    rng = np.random.default_rng(0)
    base = ((rng.normal(size=n) + 1j * rng.normal(size=n)) * 700).astype(np.complex64)
    cases = {
        "empty array": np.zeros(0, np.complex64),
        "all zeros": np.zeros(n, np.complex64),
        "clipped to the rail": np.full(n, 32767 + 32767j, np.complex64),
        "NaN / inf present": (lambda a: (a.__setitem__(slice(0, 10), np.nan),
                                         a.__setitem__(slice(10, 20), np.inf), a)[-1])(
            base.copy()),
        "constant DC offset": np.full(n, 5000 + 0j, np.complex64),
        "single impulse": (lambda a: (a.__setitem__(n // 2, 30000 + 0j), a)[-1])(
            np.zeros(n, np.complex64)),
        "very short (100 samples)": base[:100].copy(),
    }
    for name, sig in cases.items():
        try:
            _ = detect(channelise(sig, FS, CENTRE, apply_cal=False),
                       downlink_channels=dl)
            result(name, True, "handled")
        except Exception as e:                       # noqa: BLE001
            result(name, False, f"CRASHED: {type(e).__name__}: {e}")


def part_g_failure_containment(noise):
    # The synchronous snippet-writer fault injection is obsolete. Exercise the
    # actual asynchronous recorder and completed-window health contracts.
    import unittest
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    suite = unittest.defaultTestLoader.discover(os.path.join(root, "tests"), pattern="test_runtime.py")
    result("runtime failure containment regression suite",
           unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true",
                    help="skip the large real-capture reads (part A)")
    args = ap.parse_args()

    print("=" * 70)
    print("  STRESS TEST - trying to make the detector fire when it should not")
    print("=" * 70)

    dl = load_downlink_channels()
    noise, src = real_noise(FS, NOISE_S)
    print(f"  noise bed: {src} ({len(noise)/FS:.1f} s)")

    if not args.quick:
        part_a_real_captures(dl)
    part_b_positive(noise, dl)
    part_c_confusers(noise, dl)
    part_d_robustness(dl)
    part_e_sparse(dl)
    part_f_internal_spur(dl)
    part_g_failure_containment(noise)

    print("\n" + "=" * 70)
    print(f"  {_pass} passed, {_fail} failed"
          + ("   ALL PASS" if _fail == 0 else "   *** FAILURES ABOVE ***"))
    print("=" * 70)
    return 1 if _fail else 0


if __name__ == "__main__":
    sys.exit(main())
