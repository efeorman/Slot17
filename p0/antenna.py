#!/usr/bin/env python3
"""Compare antenna placements live, without waiting for an emergency vehicle.

The problem this solves: "where do I mount the antenna for the most range?"
cannot be answered from detection logs. Detections are sparse (a C2000 radio may
be silent for minutes), their distance is unknown,
and the vehicles move. Comparing two placements that way would take hours per
position and still not be conclusive.

**Use the downlink as a reference beacon instead.** The C2000 masts transmit
*continuously* on 390-395 MHz from a fixed location. That is a free, always-on,
stationary reference sitting 2.5 % away in frequency from the 380-385 MHz uplink
we actually detect on - close enough that antenna performance is effectively
identical. Lock onto one mast carrier, watch its SNR, and move the antenna: the
number moves in seconds instead of hours.

**SNR is the metric, not signal strength.** A placement that hears the mast 3 dB
louder but picks up 6 dB more of the car's electrical noise is worse, and only
SNR says so. This prints both parts so you can see which one changed - exactly
the failure seen at 383.2 MHz on the entry-45 drive.

**The number is a figure of merit, not a calibrated SNR.** A narrow carrier
concentrates its power into few FFT bins while the floor is spread across all of
them, so the printed dB reads high in absolute terms (the selftest shows ~65 dB
for a 30 dB tone). That does not matter here: only the *difference between
placements on the same locked carrier* is being compared, and that difference is
faithful. Never quote these dB as a receiver sensitivity figure.

**One `airspy_open`, never stopped** (HARD RULE 3). The stream runs continuously
while you move the antenna; there is no stop/start per position, so this costs
one stream cycle for the whole comparison however many placements you try.

    python3 antenna.py                       # lock strongest downlink carrier, live SNR
    python3 antenna.py --seconds 300         # bounded run
    python3 antenna.py --band uplink         # noise floor in the working band
    python3 antenna.py --lock-hz 392412500   # force a specific carrier
    python3 antenna.py --selftest            # offline, no hardware

Readings are also written to captures/<session>/antenna.jsonl so plateaus can be
compared afterwards - park, hold each placement ~20 s, then read off the levels.

**Do not run this while watch.py holds the radio.** It refuses if it cannot open
the device, which is the same symptom as a wedge; stop the detector first.
"""
import argparse
import ctypes
import datetime as dt
import json
import os
import signal
import sys
import threading
import time

import numpy as np
from scipy.signal import welch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import airspy_ctypes as A

BANDS = {"uplink": 382.5e6, "downlink": 392.5e6}
DEFAULT_RATE = 6_000_000
NPERSEG = 8192
REPORT_S = 1.0
LOCK_SECONDS = 3.0          # averaging used to choose the reference carrier
CARRIER_HALF_HZ = 12_500.0  # half a TETRA channel

_stop = threading.Event()


class Latest:
    """Newest block only. The callback must never block or grow a buffer."""
    def __init__(self):
        self.buf = None
        self.n = 0
        self.dropped = 0


def make_callback(latest):
    def _cb(tp):
        t = tp.contents
        try:
            if t.dropped_samples:
                latest.dropped += t.dropped_samples
            raw = ctypes.string_at(t.samples, t.sample_count * 4)
            latest.buf = np.frombuffer(raw, dtype=np.int16)
            latest.n += t.sample_count
        except Exception:  # noqa: S110 - nothing may escape into the driver's thread
            pass
        return 0
    return A.SAMPLE_BLOCK_CB(_cb)


def spectrum(iq, fs):
    """(freqs_hz_offset, psd_db) of one block, DC-centred."""
    f, p = welch(iq, fs=fs, nperseg=min(NPERSEG, len(iq)),
                 return_onesided=False, detrend=False)
    f = np.fft.fftshift(f)
    p = np.fft.fftshift(p)
    return f, 10.0 * np.log10(np.maximum(p, 1e-20))


def floor_db(psd_db, bin_hz):
    """Robust noise floor: the 10th percentile of the spectrum.

    Entry 47f: this used to compute a window width and then ignore it - both
    branches returned the same global percentile. The windowed version was never
    implemented, so the docstring described code that did not exist.

    It is now deliberately global, and that is the right choice here rather than
    a shortcut: the whole 6 MHz slice is one receiver passband, the measurement
    compares *the same* locked carrier across antenna placements, and a global
    percentile is the more stable reference for exactly that. A rolling floor
    would track a placement's own broadband noise and partly cancel the effect
    being measured. `analyze.rolling_floor` remains the right tool for finding
    unknown carriers across a band; this is not that job.
    """
    return float(np.percentile(psd_db, 10))


def measure(iq, fs, centre_hz, lock_hz=None):
    """Return (lock_hz, snr_db, peak_db, floor_db_val)."""
    f, psd = spectrum(iq, fs)
    bin_hz = fs / len(f)
    fl = floor_db(psd, bin_hz)
    if lock_hz is None:
        i = int(np.argmax(psd))
        lock_hz = centre_hz + float(f[i])
    off = lock_hz - centre_hz
    sel = np.abs(f - off) <= CARRIER_HALF_HZ
    peak = float(psd[sel].max()) if sel.any() else float(psd.max())
    return lock_hz, peak - fl, peak, fl


def bar(snr_db, lo=0.0, hi=50.0, width=34):
    frac = max(0.0, min(1.0, (snr_db - lo) / (hi - lo)))
    n = int(round(frac * width))
    return "[" + "#" * n + "-" * (width - n) + "]"


def selftest():
    """Offline: a synthetic carrier must be found and its SNR must track."""
    print("antenna.py selftest (offline, no hardware)")
    fs = DEFAULT_RATE
    centre = BANDS["downlink"]
    n = 1 << 17
    rng = np.random.default_rng(7)
    ok = True

    tone_off = 412_500.0          # a carrier 412.5 kHz above centre
    t = np.arange(n) / fs
    for want_snr in (30.0, 20.0, 10.0):
        amp = 10 ** (want_snr / 20.0)
        iq = (rng.normal(size=n) + 1j * rng.normal(size=n)).astype(np.complex128)
        iq += amp * np.exp(2j * np.pi * tone_off * t)
        lock, snr, pk, fl = measure(iq, fs, centre)
        err_hz = abs(lock - (centre + tone_off))
        good = err_hz <= 2 * fs / NPERSEG
        ok &= good
        print(f"  carrier at +{tone_off/1e3:.1f} kHz, injected {want_snr:4.1f} dB "
              f"-> found {(lock-centre)/1e3:+8.1f} kHz (err {err_hz:5.0f} Hz) "
              f"snr {snr:5.1f} dB  {'[PASS]' if good else '[FAIL]'}")

    # SNR must be monotonic in injected amplitude at a locked frequency
    snrs = []
    for want in (10.0, 20.0, 30.0):
        amp = 10 ** (want / 20.0)
        iq = (rng.normal(size=n) + 1j * rng.normal(size=n)).astype(np.complex128)
        iq += amp * np.exp(2j * np.pi * tone_off * t)
        _, s, _, _ = measure(iq, fs, centre, lock_hz=centre + tone_off)
        snrs.append(s)
    mono = all(b > a + 5 for a, b in zip(snrs, snrs[1:]))
    ok &= mono
    print(f"  locked SNR rises with signal: {[round(x,1) for x in snrs]}  "
          f"{'[PASS]' if mono else '[FAIL]'}")

    # A pure-noise input must not report a large SNR
    iq = (rng.normal(size=n) + 1j * rng.normal(size=n)).astype(np.complex128)
    _, s, _, _ = measure(iq, fs, centre)
    quiet = s < 20.0
    ok &= quiet
    print(f"  noise-only input reports {s:.1f} dB (must be < 20)  "
          f"{'[PASS]' if quiet else '[FAIL]'}")

    print("\nALL PASS" if ok else "\nFAILURES")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(
        description="Live antenna-placement meter using a C2000 downlink carrier.")
    ap.add_argument("--band", choices=sorted(BANDS), default="downlink",
                    help="downlink (default) has continuous mast carriers to lock; "
                         "uplink shows the noise floor in the working band")
    ap.add_argument("--freq", type=float, help="explicit centre in MHz")
    ap.add_argument("--lock-hz", type=float,
                    help="lock this carrier instead of the strongest found")
    ap.add_argument("--seconds", type=float, default=0.0, help="0 = until Ctrl-C")
    ap.add_argument("--interval", type=float, default=REPORT_S,
                    help="seconds between readings (default 1.0; lower for short dwells)")
    ap.add_argument("--rate", type=int, default=DEFAULT_RATE)
    ap.add_argument("--lna", type=int, default=12)
    ap.add_argument("--mixer", type=int, default=12)
    ap.add_argument("--vga", type=int, default=12)
    ap.add_argument("--tag", default="antenna")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    centre = args.freq * 1e6 if args.freq else BANDS[args.band]
    fs = args.rate

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(paths.CAPTURES, f"{stamp}_{args.tag}_{centre/1e6:.1f}MHz")
    os.makedirs(outdir, exist_ok=True)
    logpath = os.path.join(outdir, "antenna.jsonl")

    print(f"antenna placement meter - centre {centre/1e6:.4f} MHz, "
          f"{fs/1e6:g} MSPS, gain {args.lna}/{args.mixer}/{args.vga}")
    print(f"log: {logpath}")
    print("\nONE airspy_open for the whole comparison - do not stop and restart it.")
    print("Park, hold each placement ~20 s, and watch SNR. Higher is better.")
    print("Signal alone is not the answer: a placement that gains 3 dB of mast but")
    print("6 dB of car noise is worse, and only SNR shows it.")
    print("Compare placements against each other - the absolute dB is not calibrated.\n")

    dev = ctypes.c_void_p()
    rc = A.lib.airspy_open(ctypes.byref(dev))
    if rc != 0:
        print("could not open the Airspy.\n"
              "  If watch.py/supervise.py is running it holds the device - stop the\n"
              "  detector first. Otherwise the board may be wedged (see recover.py).",
              file=sys.stderr)
        return 2

    latest = Latest()
    cb = make_callback(latest)
    signal.signal(signal.SIGINT, lambda *_: _stop.set())
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())

    lock_hz = args.lock_hz
    t0 = time.time()
    try:
        A.check(A.lib.airspy_set_sample_type(dev, A.SAMPLE_INT16_IQ), "sample_type")
        A.check(A.lib.airspy_set_samplerate(dev, fs), "samplerate")
        A.check(A.lib.airspy_set_lna_gain(dev, args.lna), "lna")
        A.check(A.lib.airspy_set_mixer_gain(dev, args.mixer), "mixer")
        A.check(A.lib.airspy_set_vga_gain(dev, args.vga), "vga")
        A.check(A.lib.airspy_start_rx(dev, cb, None), "start_rx")
        A.check(A.lib.airspy_set_freq(dev, int(centre)), "set_freq")

        # settle, then pick the reference carrier
        deadline = time.time() + LOCK_SECONDS
        while time.time() < deadline and latest.buf is None and not _stop.is_set():
            time.sleep(0.1)
        if latest.buf is None:
            print("no samples arrived - stream did not start", file=sys.stderr)
            return 3

        with open(logpath, "a") as log:
            best = -999.0
            last = 0.0
            while not _stop.is_set():
                if args.seconds and time.time() - t0 > args.seconds:
                    break
                time.sleep(0.05)
                if time.time() - last < args.interval:
                    continue
                last = time.time()
                raw = latest.buf
                if raw is None:
                    continue
                x = raw.astype(np.float32)
                iq = x[0::2] + 1j * x[1::2]
                if len(iq) < NPERSEG:
                    continue
                lock_hz, snr, pk, fl = measure(iq, fs, centre, lock_hz)
                best = max(best, snr)
                rec = {"t": round(time.time(), 2),
                       "local_time": dt.datetime.now().strftime("%H:%M:%S"),
                       "elapsed_s": round(time.time() - t0, 1),
                       "lock_hz": round(lock_hz, 1),
                       "snr_db": round(snr, 2),
                       "carrier_db": round(pk, 2),
                       "floor_db": round(fl, 2),
                       "dropped": latest.dropped}
                log.write(json.dumps(rec) + "\n")
                log.flush()
                print(f"  {rec['local_time']}  {lock_hz/1e6:10.4f} MHz  "
                      f"SNR {snr:5.1f} dB  {bar(snr)}  "
                      f"carrier {pk:7.1f}  floor {fl:7.1f}  best {best:5.1f}",
                      flush=True)
    except A.AirspyError as e:
        print(f"\n{e}", file=sys.stderr)
        return 4
    finally:
        _stop.set()
        try:
            A.lib.airspy_stop_rx(dev)
        except Exception:  # noqa: S110 - best effort while closing
            pass
        try:
            A.lib.airspy_close(dev)
        except Exception:  # noqa: S110 - best effort while closing
            pass
        print(f"\nlog written: {logpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
