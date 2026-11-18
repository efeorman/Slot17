#!/usr/bin/env python3
"""Measure the receiver's frequency error against live TETRA carriers.

C2000 base stations are GPS-disciplined and sit on an exact 25 kHz raster, so a
downlink capture is a free frequency standard.

The method matters: carriers are first *detected* with the normal analysis chain
(passband-limited, width-gated), and only then refined at fine resolution. An
earlier version instead scanned every raster point and reported the strongest bin
near each, which happily locked onto the neighbouring carrier 15 kHz away and
produced nonsense. Never calibrate against a peak you have not first confirmed
is a carrier.

    python calibrate.py                 # newest downlink captures
    python calibrate.py <file.iq> ...   # specific ones
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys

import numpy as np
from scipy.signal import welch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze import IQ_DIR, load
from survey import spectrum

HERE = os.path.dirname(os.path.abspath(__file__))
# This machine's own result. It takes precedence over the shipped calibration.json, an example
# measured on one receiver, and Git ignores it, so an update never overwrites it.
CAL_PATH = os.path.join(HERE, "calibration.local.json")
CHANNELS_PATH = os.path.join(HERE, "downlink_channels.local.json")

FINE_N = 65536       # ~46 Hz bins at 3 MSPS
REFINE_HZ = 8_000    # refine the peak only this far around a detected carrier
MAX_OFFSET_HZ = 10_000   # beyond this we are not looking at the raster point we think
RASTER_HZ = 25_000
BASE_DL_HZ = 390.0125e6
BASE_UL_HZ = 380.0125e6


def nearest_raster(hz):
    base = BASE_DL_HZ if hz > 387e6 else BASE_UL_HZ
    n = int(round((hz - base) / RASTER_HZ))
    return n, base + n * RASTER_HZ


def refine(path, carriers):
    """Locate each detected carrier's peak at ~46 Hz resolution."""
    iq, meta = load(path)
    fs, fc = float(meta["sample_rate_hz"]), float(meta["centre_hz"])
    f, pxx = welch(iq, fs=fs, nperseg=FINE_N, noverlap=FINE_N // 2,
                   return_onesided=False, detrend=False)
    order = np.argsort(f)
    f, pxx = f[order], pxx[order]
    db = 10 * np.log10(pxx + 1e-30)
    freqs = f + fc

    out = []
    for c in carriers:
        win = np.abs(freqs - c["centre_hz"]) < REFINE_HZ
        if win.sum() < 8:
            continue
        k = int(np.argmax(np.where(win, db, -1e9)))
        peak = float(freqs[k])
        n, target = nearest_raster(peak)
        off = peak - target
        if abs(off) > MAX_OFFSET_HZ:
            print(f"   skipped {peak/1e6:.5f} MHz - {off:+.0f} Hz from raster, "
                  f"too far to trust")
            continue
        out.append({"channel": n, "raster_hz": target, "measured_hz": peak,
                    "offset_hz": off, "ppm": off / target * 1e6,
                    "snr_db": c["peak_snr_db"]})
    return out, fc


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="IQ captures (default: newest downlink)")
    ap.add_argument("--snr", type=float, default=8.0,
                    help="dB above floor for a carrier to be used")
    args = ap.parse_args()

    files = args.files
    if not files:
        cands = sorted(glob.glob(os.path.join(IQ_DIR, "*downlink*.iq")),
                       key=os.path.getmtime)
        if not cands:
            raise SystemExit("No downlink captures - run survey.py --band downlink")
        files = cands[-3:]

    rows = []
    for path in files:
        if os.path.getsize(path) < 1_000_000:
            print(f"{os.path.basename(path)}  -  skipped, too small to analyse")
            continue
        # Always measure on the RAW axis: applying the existing calibration
        # here would measure the residual and then store it as if it were
        # the absolute error, shrinking the correction on every run.
        _, _, _, _, carriers, _ = spectrum(path, args.snr, apply_cal=False)
        hits, fc = refine(path, carriers)
        print(f"{os.path.basename(path)}  -  {len(hits)} usable of "
              f"{len(carriers)} detected, LO {fc/1e6:g} MHz")
        for h in hits:
            print(f"   ch {h['channel']:4d}  {h['raster_hz']/1e6:11.4f} MHz  "
                  f"measured {h['measured_hz']/1e6:11.5f}  "
                  f"offset {h['offset_hz']:+6.0f} Hz  ({h['ppm']:+5.2f} ppm)  "
                  f"SNR {h['snr_db']:.1f} dB")
        # One carrier can be detected as two fragments if its centre dips
        # below threshold; both refine to the same peak, so keep the best.
        for h in hits:
            prev = next((r for r in rows if r["channel"] == h["channel"]), None)
            if prev is None:
                rows.append(h)
            elif h["snr_db"] > prev["snr_db"]:
                rows[rows.index(prev)] = h

    if not rows:
        raise SystemExit("No confirmed carriers to calibrate against.")

    offs = np.array([r["offset_hz"] for r in rows])
    ppms = np.array([r["ppm"] for r in rows])
    med_off, med_ppm = float(np.median(offs)), float(np.median(ppms))

    print(f"\n{len(rows)} measurement(s) across {len(files)} capture(s)")
    print(f"  median offset : {med_off:+.0f} Hz   ({med_ppm:+.2f} ppm)")
    print(f"  spread        : {offs.std():.0f} Hz std, "
          f"{offs.min():+.0f} .. {offs.max():+.0f} Hz")

    # These captures span ~2 MHz, over which a ppm error and a fixed-Hz error
    # differ by well under the measurement spread, so the data cannot separate
    # them. Both are recorded; the ppm form is applied, because it stays right
    # if we later work in the uplink band 10 MHz away.
    cal = {"measured_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "median_offset_hz": med_off, "median_ppm": med_ppm,
           "std_hz": float(offs.std()), "n": len(rows),
           "note": "receiver reads high by this much; analyze.py subtracts it",
           "measurements": rows}
    with open(CAL_PATH, "w", encoding="utf-8") as fh:
        json.dump(cal, fh, indent=2)
    print(f"\nWritten: {CAL_PATH}")
    # The carriers just measured are the local network's downlink channels. Stored for the
    # advisory downlink_paired label only; detection never depends on this list.
    channels = sorted({int(r["channel"]) for r in rows})
    with open(CHANNELS_PATH, "w", encoding="utf-8") as fh:
        json.dump({"measured_utc": cal["measured_utc"], "channels": channels,
                   "note": "Downlink carriers measured by calibrate.py on this machine. Advisory label only."},
                  fh, indent=2)
    print(f"Written: {CHANNELS_PATH} ({len(channels)} channels)")
    print("analyze.py and survey.py now correct for this automatically.")


if __name__ == "__main__":
    main()
