#!/usr/bin/env python3
"""Sweep a whole TETRA band and produce one combined carrier list.

At 3 MSPS a single capture only sees ~1.8 MHz, so a 5 MHz band needs several
centre frequencies. This walks them, analyses each, merges the results onto the
TETRA raster and draws one stitched spectrum for the band.

    python survey.py --band downlink
    python survey.py --band uplink --secs 10
    python survey.py --centres 390.9 392.7 394.5 --tag rooftop
"""
import argparse
import json
import os
import subprocess
import sys
import datetime as dt

import numpy as np
from scipy.signal import welch

# matplotlib is imported only where a plot is drawn, so calibrate.py can use spectrum() on a
# headless machine without a plotting stack.

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze import (NPERSEG, PLOT_DIR, load, rolling_floor, find_carriers,
                     load_calibration)

HERE = os.path.dirname(os.path.abspath(__file__))

# Measured usable window is 5.03 MHz at 6 MSPS and 1.85 MHz at 3 MSPS. At the
# full rate one centre covers a whole 5 MHz band; at half rate it takes three.
BANDS = {
    "downlink": {"range": (390.0, 395.0), "centres": [392.5],
                 "centres_3msps": [390.9, 392.7, 394.5]},
    "uplink":   {"range": (380.0, 385.0), "centres": [382.5],
                 "centres_3msps": [380.9, 382.7, 384.5]},
}


def run_capture(freq, secs, gains, tag, note, rate):
    """Capture one centre. Returns the .iq path, or None if the board wedged.

    This board stalls its USB endpoint intermittently, so a sweep must survive
    a failure partway through rather than throwing away the centres that did
    succeed.
    """
    cmd = [sys.executable, os.path.join(HERE, "capture.py"),
           "--freq", f"{freq}", "--secs", f"{secs}", "--tag", tag,
           "--rate", str(rate), "--lna", str(gains[0]), "--mixer", str(gains[1]),
           "--vga", str(gains[2]), "--note", note]
    r = subprocess.run(cmd, capture_output=True, text=True)
    sys.stdout.write(r.stdout)
    if r.returncode != 0 or "LIBUSB" in (r.stderr or ""):
        sys.stderr.write(r.stderr)
        return None
    for line in r.stdout.splitlines():
        if "->" in line and ".iq" in line:
            return line.split("->")[1].strip().split("  (")[0]
    return None


def spectrum(path, snr_db, apply_cal=True):
    iq, meta = load(path)
    fs, fc = float(meta["sample_rate_hz"]), float(meta["centre_hz"])
    f, pxx = welch(iq, fs=fs, nperseg=NPERSEG, noverlap=NPERSEG // 2,
                   return_onesided=False, detrend=False, scaling="density")
    order = np.argsort(f)
    f, pxx = f[order], pxx[order]
    psd_db = 10 * np.log10(pxx + 1e-30)
    freqs = f + fc
    # Same correction analyze.py applies - without it every carrier here is
    # reported at dial frequency and misses its raster point by ~3.9 kHz.
    cal_ppm = load_calibration() if apply_cal else 0.0
    if cal_ppm:
        freqs = freqs - freqs * cal_ppm * 1e-6
    floor_db = rolling_floor(psd_db, win_bins=int(400_000 / (fs / NPERSEG)))

    mid = np.abs(f) < 0.20 * fs
    ref = float(np.median(floor_db[mid]))
    ok = np.where(floor_db >= ref - 3.0)[0]
    in_band = np.zeros(len(freqs), dtype=bool)
    in_band[ok[0]:ok[-1] + 1] = True

    carriers = find_carriers(freqs, psd_db, floor_db, snr_db, fc, in_band)
    return freqs, psd_db, floor_db, in_band, carriers, meta


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--band", choices=sorted(BANDS), help="preset band to sweep")
    ap.add_argument("--centres", type=float, nargs="+", help="explicit centres in MHz")
    ap.add_argument("--secs", type=float, default=5.0)
    ap.add_argument("--rate", type=float, default=6.0,
                    help="sample rate in MSPS (6 or 3)")
    ap.add_argument("--snr", type=float, default=5.0)
    ap.add_argument("--lna", type=int, default=12)
    ap.add_argument("--mixer", type=int, default=12)
    ap.add_argument("--vga", type=int, default=12)
    ap.add_argument("--tag", default=None)
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    if not args.band and not args.centres:
        raise SystemExit("give --band downlink|uplink or --centres f1 f2 ...")
    if args.centres:
        centres = args.centres
    else:
        key = "centres" if args.rate >= 6 else "centres_3msps"
        centres = BANDS[args.band][key]
    tag = args.tag or (args.band or "survey")
    gains = (args.lna, args.mixer, args.vga)

    print(f"Sweeping {len(centres)} centre(s): "
          + ", ".join(f"{c} MHz" for c in centres))
    print(f"Gain lna={gains[0]} mixer={gains[1]} vga={gains[2]}, "
          f"{args.secs:g}s each\n")

    segments, all_carriers, remaining = [], [], []
    for i, c in enumerate(centres, 1):
        print(f"[{i}/{len(centres)}] {c} MHz")
        path = run_capture(c, args.secs, gains, f"{tag}-{c:g}", args.note, args.rate)
        if path is None:
            remaining = centres[i - 1:]
            print(f"\n  !! The board stopped responding at {c} MHz.")
            print("     Unplug the Airspy, wait 5 s, plug it back in, then resume:")
            print("     python survey.py --centres "
                  + " ".join(f"{f:g}" for f in remaining)
                  + f" --tag {tag} --secs {args.secs:g}")
            break
        freqs, psd_db, floor_db, in_band, carriers, meta = spectrum(path, args.snr)
        hdr = meta.get("adc", {}).get("headroom_db")
        print(f"     {len(carriers)} carrier(s), ADC headroom {hdr} dB")
        segments.append((freqs, psd_db, floor_db, in_band, c))
        all_carriers.extend(carriers)

    if not segments:
        raise SystemExit("\nNo captures completed - nothing to report.")

    # Overlapping windows can see the same carrier twice; keep the stronger sighting.
    merged = {}
    for c in all_carriers:
        key = c["raster_hz"]
        if key not in merged or c["peak_snr_db"] > merged[key]["peak_snr_db"]:
            merged[key] = c
    carriers = sorted(merged.values(), key=lambda c: c["centre_hz"])

    print(f"\n{'=' * 62}")
    print(f"  {len(carriers)} distinct carrier(s) across the band")
    print(f"{'=' * 62}")
    if carriers:
        print(f"  {'centre MHz':>12}  {'raster MHz':>11}  {'width kHz':>9}  "
              f"{'SNR dB':>7}  {'TETRA ch':>9}")
        for c in carriers:
            ch = c["tetra_channel"]
            print(f"  {c['centre_hz']/1e6:12.4f}  {c['raster_hz']/1e6:11.4f}  "
                  f"{c['width_hz']/1e3:9.1f}  {c['peak_snr_db']:7.1f}  "
                  f"{(str(ch) if ch is not None else 'off-raster'):>9}")
    else:
        print("  none found")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = os.path.join(PLOT_DIR, f"{stamp}_{tag}_survey.png")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(15, 6))
    for freqs, psd_db, floor_db, in_band, _c in segments:
        ax.plot(freqs[in_band] / 1e6, psd_db[in_band], lw=0.6, color="#1a3a5c")
        ax.plot(freqs[in_band] / 1e6, floor_db[in_band], lw=1.0, color="#c05621")
    for c in carriers:
        ax.axvline(c["centre_hz"] / 1e6, color="#2f855a", lw=0.8, alpha=0.7)
        ax.annotate(f"ch {c['tetra_channel']}", (c["centre_hz"] / 1e6, ax.get_ylim()[1]),
                    fontsize=7, rotation=90, va="top", ha="right", color="#2f855a")
    ax.set_xlabel("Frequency (MHz)")
    ax.set_ylabel("Power (dB, uncalibrated)")
    ax.set_title(f"{tag} survey - {len(carriers)} carrier(s) "
                 f"(dark = PSD, orange = noise floor)")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    print(f"\nPlot   : {out}")

    js = os.path.join(PLOT_DIR, f"{stamp}_{tag}_survey.json")
    json.dump({"tag": tag, "centres_mhz": centres, "snr_threshold_db": args.snr,
               "gain": {"lna": gains[0], "mixer": gains[1], "vga": gains[2]},
               "carriers": carriers}, open(js, "w", encoding="utf-8"), indent=2)
    print(f"Result : {js}")
    if remaining:
        print("")
        print(f"INCOMPLETE: {len(remaining)} centre(s) not captured: "
              + ", ".join(f"{f:g} MHz" for f in remaining))



if __name__ == "__main__":
    main()
