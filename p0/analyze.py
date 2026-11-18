#!/usr/bin/env python3
"""Averaged-spectrum analysis of an Airspy IQ capture.

Produces three things from one capture:
  1. a PNG of the averaged power spectrum across the whole window;
  2. the measured usable passband (the -3 dB points of the receiver response),
     which is the number the build guide leaves open;
  3. a carrier list - narrowband signals standing above a rolling noise floor,
     each snapped to the nearest TETRA channel centre (base + n x 25 kHz, with
     base 380.0125 / 390.0125 MHz) so channels are recognisable at a glance.

Usage:
    python analyze.py                       # newest capture
    python analyze.py <file.iq>             # a specific one
    python analyze.py <file.iq> --snr 8     # looser carrier threshold
"""
import argparse
import glob
import json
import os

import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

import numpy as np
from scipy.signal import welch

# matplotlib is imported lazily, inside main(), so that importing this module for
# its data helpers (load, load_calibration - used by channelize.py / detect.py)
# does not require a plotting stack. The headless car device has no matplotlib.

IQ_DIR = paths.IQ_DIR
PLOT_DIR = paths.PLOT_DIR

NPERSEG = 8192          # 732 Hz bins at 6 MSPS - resolves a 25 kHz channel well
DC_GUARD_HZ = 25_000    # ignore the residual DC/LO artefact at the centre
# TETRA carrier SPACING is 25 kHz. The 12.5 kHz that used to be here is not a
# raster at all - it is the offset of the band base (380.0125 / 390.0125 MHz)
# from a round multiple of 25 kHz. Snapping to multiples of 12,500 put half of
# all carriers on frequencies where no TETRA channel exists, 12.5 kHz off the
# real one. Snap to the real raster instead. (Only the reported `raster_hz` was
# affected; `tetra_channel()` and calibrate.py always did this correctly.)
RASTER_HZ = 25_000
RASTER_BASE_DL_HZ = 390.0125e6
RASTER_BASE_UL_HZ = 380.0125e6
WIDTH_DEPTH_DB = 6.0    # width is measured this far below each peak
MAX_SAMPLES = 60_000_000  # cap the read so a long capture still analyses fast


def _nearest_raster_hz(hz):
    """Nearest real TETRA channel centre: base + n x 25 kHz."""
    base = RASTER_BASE_DL_HZ if hz > 387e6 else RASTER_BASE_UL_HZ
    return base + round((hz - base) / RASTER_HZ) * RASTER_HZ


def newest_capture():
    files = sorted(glob.glob(os.path.join(IQ_DIR, "*.iq")), key=os.path.getmtime)
    if not files:
        raise SystemExit("No captures found in " + IQ_DIR)
    return files[-1]


def load(path):
    meta_path = os.path.splitext(path)[0] + ".json"
    if os.path.exists(meta_path):
        meta = json.load(open(meta_path, encoding="utf-8"))
    else:  # fall back to parsing the filename
        meta = {"centre_hz": 0.0, "sample_rate_hz": 6_000_000, "gain": {}}
    on_disk = os.path.getsize(path) // 4          # 4 bytes per complex sample
    raw = np.fromfile(path, dtype=np.int16, count=MAX_SAMPLES * 2)
    iq = raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)
    # An averaged spectrum saturates long before this cap, so reading further
    # buys nothing - but say so, rather than silently reporting a 40 s capture
    # as though it were 10 s.
    meta["_samples_read"] = len(iq)
    meta["_samples_on_disk"] = on_disk
    return iq, meta


def stream_psd(path, nperseg, chunk_samples=8_000_000, max_seconds=None):
    """Averaged power spectrum over a WHOLE capture, without loading it all.

    `load()` caps reads at MAX_SAMPLES, so a 40 s capture was only ever analysed
    for its first 10 s - fine for a quick look, wrong for anything quantitative.
    Welch's method is just an average of periodograms, so the file can be walked
    in chunks and the periodograms accumulated: mathematically identical to
    running Welch over the whole array, at a fraction of the memory.

    Returns (freqs_hz, psd_db, fs, centre_hz, meta, seconds_used).
    """
    meta_path = os.path.splitext(path)[0] + ".json"
    meta = (json.load(open(meta_path, encoding="utf-8"))
            if os.path.exists(meta_path)
            else {"centre_hz": 0.0, "sample_rate_hz": 6_000_000, "gain": {}})
    fs = float(meta["sample_rate_hz"])
    fc = float(meta["centre_hz"])

    total = os.path.getsize(path) // 4
    if max_seconds:
        total = min(total, int(max_seconds * fs))

    acc = np.zeros(nperseg, dtype=np.float64)
    n_seg = 0
    done = 0
    while done < total:
        want = min(chunk_samples, total - done)
        raw = np.fromfile(path, dtype=np.int16, count=want * 2, offset=done * 4)
        if raw.size < nperseg * 2:
            break
        iq = raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)
        f, pxx = welch(iq, fs=fs, nperseg=nperseg, noverlap=nperseg // 2,
                       return_onesided=False, detrend=False, scaling="density")
        # Weight each chunk by how many segments it contributed, so a short
        # final chunk cannot skew the average.
        segs = max(1, (len(iq) - nperseg) // (nperseg // 2) + 1)
        acc += pxx * segs
        n_seg += segs
        done += want

    if n_seg == 0:
        raise ValueError(f"{os.path.basename(path)} is too short to analyse")
    pxx = acc / n_seg
    f = np.fft.fftfreq(nperseg, 1 / fs)
    order = np.argsort(f)
    return (f[order] + fc, 10 * np.log10(pxx[order] + 1e-30), fs, fc, meta,
            done / fs)


def rolling_floor(psd_db, win_bins):
    """10th-percentile noise floor in a sliding window - robust to carriers."""
    n = len(psd_db)
    if n < 16:
        raise ValueError("spectrum too short to estimate a noise floor - "
                         "is the capture empty?")
    win_bins = max(16, min(win_bins, n))
    step = max(1, win_bins // 8)
    centres, vals = [], []
    for start in range(0, n, step):
        seg = psd_db[max(0, start - win_bins // 2): start + win_bins // 2]
        if len(seg) < 16:
            continue
        centres.append(start)
        vals.append(np.percentile(seg, 10))
    if not centres:  # window covered the whole array in one go
        return np.full(n, float(np.percentile(psd_db, 10)))
    return np.interp(np.arange(n), centres, vals)


def config_file(name):
    """This machine's own measurement (NAME.local.json, written by calibrate.py), else the shipped example."""
    here = os.path.dirname(os.path.abspath(__file__))
    local = os.path.join(here, name.replace(".json", ".local.json"))
    return local if os.path.exists(local) else os.path.join(here, name)


def load_calibration():
    """Frequency correction measured by calibrate.py, in ppm (0 if never run)."""
    path = config_file("calibration.json")
    if not os.path.exists(path):
        return 0.0
    try:
        with open(path, encoding="utf-8") as fh:
            return float(json.load(fh)["median_ppm"])
    except (ValueError, KeyError, OSError):
        return 0.0


def tetra_channel(hz):
    """TETRA carriers sit at 390.0125 + n*25 kHz (downlink) and 10 MHz lower
    on the uplink. Returns the channel number, or None if it is off-raster."""
    base = 390.0125e6 if hz > 387e6 else 380.0125e6
    n = (hz - base) / 25_000.0
    return int(round(n)) if abs(n - round(n)) < 0.2 else None


def width_below_peak(freqs, psd_db, k, depth_db):
    """Width of a peak measured `depth_db` below its own maximum.

    Independent of the detection threshold, so a weak carrier and a strong one
    of the same bandwidth report the same width.
    """
    target = psd_db[k] - depth_db
    n = len(psd_db)
    lo = k
    while lo > 0 and psd_db[lo] > target:
        lo -= 1
    hi = k
    while hi < n - 1 and psd_db[hi] > target:
        hi += 1
    return float(freqs[hi] - freqs[lo])


def find_carriers(freqs, psd_db, floor_db, snr_db, centre_hz, in_band,
                  max_width_hz=80_000):
    above = psd_db > floor_db + snr_db
    # Only inside the flat passband: in the roll-off the floor falls faster than
    # the PSD does, which manufactures carriers hundreds of kHz wide.
    above &= in_band
    # Mask the DC guard band so the LO artefact is never reported as a carrier.
    above &= np.abs(freqs - centre_hz) > DC_GUARD_HZ
    carriers, i, n = [], 0, len(above)
    while i < n:
        if not above[i]:
            i += 1
            continue
        j = i
        while j < n and above[j]:
            j += 1
        seg = slice(i, j)
        width = freqs[j - 1] - freqs[i]
        # Gate on the raw extent first, only to reject single-bin spikes and
        # absurdly wide regions; the real width test uses width10 below.
        if 2_000 <= width <= max_width_hz:
            k = i + int(np.argmax(psd_db[seg]))
            # These carriers have a strong discrete central component, and the
            # peak bin tracks the raster to ~55 Hz across captures. The
            # power-weighted centroid is pulled sideways by asymmetric
            # shoulders - about 2.4 kHz here - so the peak is the frequency
            # estimate, and the centroid is kept only as a shape diagnostic.
            peak_hz = float(freqs[k])
            w = 10 ** (psd_db[seg] / 10.0)
            centroid = float(np.sum(freqs[seg] * w) / np.sum(w))
            # Width measured at the detection threshold tracks SNR, not
            # bandwidth: the same carrier measured 6.6 kHz wide at 9 dB SNR and
            # 20.5 kHz at 16 dB. Measuring at a fixed depth below the peak gives
            # a figure that means the same thing at any signal strength.
            width6 = width_below_peak(freqs, psd_db, k, WIDTH_DEPTH_DB)
            # A TETRA channel's -6 dB width lands around 12-20 kHz whatever its
            # strength, while the internal spurs are under 1 kHz - so this test
            # separates them by more than an order of magnitude, and unlike a
            # threshold-crossing width it does not shrink as SNR falls.
            if not (5_000 <= width6 <= max_width_hz):
                i = j
                continue
            carriers.append({
                "centre_hz": peak_hz,
                "centroid_hz": centroid,
                "raster_hz": _nearest_raster_hz(peak_hz),
                "width_hz": float(width6),
                "threshold_width_hz": float(width),
                "peak_snr_db": float(psd_db[k] - floor_db[k]),
                # Below about depth+3 dB the -6 dB points fall into the noise,
                # so the width figure stops meaning anything.
                "width_reliable": bool(psd_db[k] - floor_db[k] >= WIDTH_DEPTH_DB + 3),
                "tetra_channel": tetra_channel(peak_hz),
            })
        i = j
    return carriers


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="IQ file (default: newest)")
    ap.add_argument("--snr", type=float, default=10.0,
                    help="dB above the rolling floor to call a carrier")
    args = ap.parse_args()

    path = args.file or newest_capture()
    freqs_all, psd_db, fs, fc, meta, used_s = stream_psd(path, NPERSEG)
    print(f"File   : {os.path.basename(path)}")
    print(f"Centre : {fc/1e6:.4f} MHz   Rate: {fs/1e6:g} MSPS   "
          f"Analysed: {used_s:.1f} s (whole capture)")
    if meta.get("gain"):
        g = meta["gain"]
        print(f"Gain   : lna={g.get('lna')} mixer={g.get('mixer')} vga={g.get('vga')}")

    freqs = freqs_all
    f = freqs - fc
    # The receiver reads high by a measured amount; correcting the axis here
    # means every carrier frequency, raster snap and channel number below is
    # reported in true frequency rather than dial frequency.
    cal_ppm = load_calibration()
    if cal_ppm:
        freqs = freqs - freqs * cal_ppm * 1e-6
        print(f"Cal    : {cal_ppm:+.2f} ppm correction applied")
    bin_hz = fs / NPERSEG

    floor_db = rolling_floor(psd_db, win_bins=int(400_000 / bin_hz))

    # --- usable passband: -3 dB points of the receiver response -------------
    # The flat reference is the median floor over the middle 40% of the window.
    mid = np.abs(f) < 0.20 * fs
    ref = float(np.median(floor_db[mid]))
    ok = np.where(floor_db >= ref - 3.0)[0]
    lo_hz, hi_hz = freqs[ok[0]], freqs[ok[-1]]
    usable = hi_hz - lo_hz
    print(f"\nPassband (-3 dB of flat floor, ref {ref:.1f} dB):")
    print(f"  {lo_hz/1e6:.3f} - {hi_hz/1e6:.3f} MHz   usable = {usable/1e6:.3f} MHz")

    in_band = np.zeros(len(freqs), dtype=bool)
    in_band[ok[0]:ok[-1] + 1] = True
    carriers = find_carriers(freqs, psd_db, floor_db, args.snr, fc, in_band)
    print(f"\nCarriers >{args.snr:g} dB above floor: {len(carriers)}")
    if carriers:
        print(f"  {'centre MHz':>12}  {'raster MHz':>11}  {'width kHz':>9}  "
              f"{'SNR dB':>7}  {'TETRA ch':>8}")
        for c in sorted(carriers, key=lambda c: -c["peak_snr_db"]):
            ch = c["tetra_channel"]
            print(f"  {c['centre_hz']/1e6:12.4f}  {c['raster_hz']/1e6:11.4f}  "
                  f"{c['width_hz']/1e3:9.1f}  {c['peak_snr_db']:7.1f}  "
                  f"{(str(ch) if ch is not None else 'off-raster'):>8}"
                  + ("" if c["width_reliable"] else "  (width approx)"))
    else:
        print("  (none - either the band is quiet here, or gain/antenna is wrong)")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    os.makedirs(PLOT_DIR, exist_ok=True)
    png = os.path.join(PLOT_DIR, os.path.splitext(os.path.basename(path))[0] + ".png")
    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(freqs / 1e6, psd_db, lw=0.6, color="#1a3a5c", label="averaged PSD")
    ax.plot(freqs / 1e6, floor_db, lw=1.2, color="#c05621", label="rolling noise floor")
    ax.plot(freqs / 1e6, floor_db + args.snr, lw=0.9, ls="--", color="#9b2c2c",
            label=f"+{args.snr:g} dB detection threshold")
    for c in carriers:
        ax.axvline(c["centre_hz"] / 1e6, color="#2f855a", lw=0.7, alpha=0.55)
    ax.axvspan((fc - DC_GUARD_HZ) / 1e6, (fc + DC_GUARD_HZ) / 1e6,
               color="grey", alpha=0.18, label="DC guard (ignored)")
    ax.set_xlabel("Frequency (MHz)")
    ax.set_ylabel("Power (dB, uncalibrated)")
    ax.set_title(f"{os.path.basename(path)}  -  {len(carriers)} carrier(s), "
                 f"usable passband {usable/1e6:.2f} MHz")
    ax.grid(alpha=0.25)
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(png, dpi=130)
    print(f"\nPlot   : {png}")

    out = os.path.splitext(path)[0] + "_carriers.json"
    json.dump({"file": os.path.basename(path), "centre_hz": fc,
               "passband": {"low_hz": float(lo_hz), "high_hz": float(hi_hz),
                            "usable_hz": float(usable)},
               "snr_threshold_db": args.snr, "carriers": carriers},
              open(out, "w", encoding="utf-8"), indent=2)
    print(f"Result : {out}")


if __name__ == "__main__":
    main()
