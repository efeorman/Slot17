#!/usr/bin/env python3
"""Find narrow internal spurs - the lines the carrier detector deliberately ignores.

analyze.py gates on width (>=6 kHz) because a TETRA channel is ~20 kHz wide and a
single-bin spike is not a signal. Spurs are exactly what that gate throws away, so
finding them needs the opposite test: narrow, persistent lines standing above the
noise floor.

Run this on a capture taken with the **antenna disconnected**. Anything it finds is
generated inside the receiver or the laptop, and belongs in the detector's permanent
blacklist.

    python spurs.py <no-antenna-capture.iq>
    python spurs.py <a.iq> <b.iq>      # e.g. on battery vs on charger
"""
import argparse
import json
import os
import sys

import numpy as np
from scipy.signal import welch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze import PLOT_DIR, load, rolling_floor, load_calibration

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

FINE_N = 32768        # ~183 Hz bins at 6 MSPS
MAX_SPUR_WIDTH_HZ = 6_000   # wider than this is a signal, not a spur
DC_GUARD_HZ = 25_000


def spectrum(path):
    iq, meta = load(path)
    fs, fc = float(meta["sample_rate_hz"]), float(meta["centre_hz"])
    f, pxx = welch(iq, fs=fs, nperseg=FINE_N, noverlap=FINE_N // 2,
                   detrend=False, return_onesided=False)
    order = np.argsort(f)
    f, pxx = f[order], pxx[order]
    db = 10 * np.log10(pxx + 1e-30)
    freqs = f + fc
    cal = load_calibration()
    if cal:
        freqs = freqs - freqs * cal * 1e-6
    floor = rolling_floor(db, win_bins=int(400_000 / (fs / FINE_N)))
    return freqs, db, floor, fs, fc, meta


def find_spurs(freqs, db, floor, fc, snr_db):
    above = (db > floor + snr_db) & (np.abs(freqs - fc) > DC_GUARD_HZ)
    # Restrict to the flat passband; the roll-off manufactures false positives.
    mid = np.abs(freqs - fc) < 0.20 * (freqs[-1] - freqs[0])
    ref = float(np.median(floor[mid]))
    ok = np.where(floor >= ref - 3.0)[0]
    keep = np.zeros(len(freqs), bool)
    keep[ok[0]:ok[-1] + 1] = True
    above &= keep

    out, i, n = [], 0, len(above)
    while i < n:
        if not above[i]:
            i += 1
            continue
        j = i
        while j < n and above[j]:
            j += 1
        width = freqs[j - 1] - freqs[i]
        if width <= MAX_SPUR_WIDTH_HZ:
            k = i + int(np.argmax(db[i:j]))
            out.append({"freq_hz": float(freqs[k]), "width_hz": float(width),
                        "snr_db": float(db[k] - floor[k])})
        i = j
    return sorted(out, key=lambda s: -s["snr_db"])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--snr", type=float, default=6.0)
    args = ap.parse_args()

    # Windows shells do not expand wildcards into argv, so do it here - otherwise
    # a literal "*.iq" reaches open() and raises a confusing OSError.
    import glob as _glob
    paths = []
    for a in args.files:
        hits = _glob.glob(a)
        paths.extend(sorted(hits) if hits else [a])

    results = {}
    for path in paths:
        freqs, db, floor, fs, fc, meta = spectrum(path)
        spurs = find_spurs(freqs, db, floor, fc, args.snr)
        name = os.path.basename(path)
        results[name] = spurs
        print(f"\n{name}")
        print(f"  centre {fc/1e6:g} MHz, {fs/1e6:g} MSPS, "
              f"bin {fs/FINE_N:.0f} Hz, note: {meta.get('note','')}")
        print(f"  {len(spurs)} narrow line(s) >{args.snr:g} dB above floor")
        if spurs:
            print(f"    {'frequency MHz':>14}  {'width kHz':>9}  {'SNR dB':>7}")
            for s in spurs[:40]:
                print(f"    {s['freq_hz']/1e6:14.5f}  {s['width_hz']/1e3:9.2f}  "
                      f"{s['snr_db']:7.1f}")
            if len(spurs) > 40:
                print(f"    ... and {len(spurs)-40} more")

        png = os.path.join(PLOT_DIR, os.path.splitext(name)[0] + "_spurs.png")
        fig, ax = plt.subplots(figsize=(15, 5))
        ax.plot(freqs / 1e6, db, lw=0.5, color="#1a3a5c")
        ax.plot(freqs / 1e6, floor, lw=1.0, color="#c05621")
        for s in spurs:
            ax.axvline(s["freq_hz"] / 1e6, color="#9b2c2c", lw=0.6, alpha=0.6)
        ax.set_xlabel("Frequency (MHz)")
        ax.set_ylabel("Power (dB, uncalibrated)")
        ax.set_title(f"{name} - {len(spurs)} narrow line(s), antenna disconnected")
        ax.grid(alpha=0.25)
        fig.tight_layout()
        fig.savefig(png, dpi=130)
        print(f"  plot: {png}")

    out = os.path.join(PLOT_DIR, "spur-map.json")
    json.dump({"snr_threshold_db": args.snr, "results": results},
              open(out, "w", encoding="utf-8"), indent=2)
    print(f"\nSpur map: {out}")


if __name__ == "__main__":
    main()
