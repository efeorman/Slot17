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

A pair of RTL-SDRs has two crystals, so one correction can't fit both. For captures made with the pair, the
carriers below the handover (the capture's centre) measure stick A and those above it stick B; each stick's
error becomes its own ppm setting in the receiver settings (ppm_a, ppm_b: librtlsdr corrects the tuner and the
sample rate from it, in whole ppm), and what whole numbers leave over becomes the shared correction here.
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


def write_json(path, data):
    """Write a file whole, on the disk, before it replaces the old one: a power cut leaves one or the other."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    if hasattr(os, "O_DIRECTORY"):                   # and the rename itself, where the system allows it
        fd = os.open(os.path.dirname(os.path.abspath(path)), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def capture_receiver(path):
    """What made a capture, from its sidecar: (group, pair). The group is what captures must share to be measured
    together: the receiver's kind, and for the RTL-SDR pair also its sticks' serials, the ppm settings they had and
    where one stick's half ends and the other's begins (a carrier is put in the half its own capture had it in; until
    24 November 2026 two captures tuned apart were split at the newest one's handover, found in review).
    pair is (applied ppm, handover Hz) for a pair's capture, else None. A capture whose sidecar names no receiver
    was made with the Airspy (every capture before the receivers came in); one without a sidecar too."""
    try:
        with open(os.path.splitext(path)[0] + ".json", encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return ("airspy",), None
    rx = meta.get("receiver") if isinstance(meta, dict) else None
    kind = rx.get("kind") if isinstance(rx, dict) and rx.get("kind") else "airspy"
    if kind != "rtlsdr2":
        return (kind,), None
    info = rx.get("info") or {}
    sticks = info.get("sticks") or []
    if len(sticks) != 2:
        return ("rtlsdr2", "unknown sticks"), None
    applied = tuple(int(s.get("ppm") or 0) for s in sticks)
    serials = tuple(str(s.get("serial")) for s in sticks)
    handover = float(info.get("handover_hz") or meta["centre_hz"])
    return ("rtlsdr2", serials, applied, handover), (list(applied), handover)


def choose_captures(files, target):
    """Of `files` (oldest first), the ones to measure for this computer's receiver `target`: the newest of its
    captures and every other made the same way (same receiver; for the pair, the same sticks with the same ppm
    settings). Returns (chosen, pair, left out): pair is {"files", "applied_ppm", "handover_hz"} or None.

    Until 24 November 2026 the presence of any pair capture made the newest pair capture win, so an older pair
    capture could override the Airspy just captured (found in review)."""
    groups = {f: capture_receiver(f) for f in files}
    mine = [f for f in files if groups[f][0][0] == target and (target != "rtlsdr2" or groups[f][1] is not None)]
    if not mine:
        return [], None, list(files)
    newest = groups[mine[-1]][0]
    chosen = [f for f in mine if groups[f][0] == newest]
    pair = None
    if target == "rtlsdr2" and groups[chosen[-1]][1] is not None:
        applied, handover = groups[chosen[-1]][1]
        pair = {"files": chosen, "applied_ppm": applied, "handover_hz": handover}
    return chosen, pair, [f for f in files if f not in chosen]


def pair_captures(files):
    """The pair's captures among `files` measured together (the newest pair capture's group); None if none."""
    _, pair, _ = choose_captures(files, "rtlsdr2")
    return pair


def calibrate_pair(pair, rows, settings=None):
    """Each stick's error from the carriers in its half, its new ppm setting, and the correction left to share.

    A stick reads low by (its crystal's error - its ppm setting): with ppm p applied and carriers reading high by
    m ppm, the crystal is p - m ppm fast, and the new setting is that in whole ppm. The settings are written when
    this computer's receiver is the pair; otherwise they are only suggested."""
    import receiver
    out = {"applied_ppm": pair["applied_ppm"], "handover_hz": pair["handover_hz"], "measured_ppm": [],
           "carriers": [], "error_ppm": [], "setting_ppm": [], "left_ppm": []}
    halves = ([r for r in rows if r["raster_hz"] < pair["handover_hz"]],
              [r for r in rows if r["raster_hz"] > pair["handover_hz"]])
    for label, applied, half in zip("AB", pair["applied_ppm"], halves):
        out["carriers"].append(len(half))
        if not half:
            print(f"  stick {label}: no carrier in its half, so its ppm stays {applied}")
            out["measured_ppm"].append(None)
            out["error_ppm"].append(None)
            out["setting_ppm"].append(applied)
            out["left_ppm"].append(None)
            continue
        m = float(np.median([r["ppm"] for r in half]))
        error = applied - m
        setting = int(round(error))
        out["measured_ppm"].append(round(m, 3))
        out["error_ppm"].append(round(error, 3))
        out["setting_ppm"].append(setting)
        out["left_ppm"].append(round(setting - error, 3))       # it will read high by this much
        print(f"  stick {label}: {len(half)} carrier(s) read {m:+.2f} ppm with ppm {applied} set: "
              f"its crystal is {error:+.2f} ppm off, so ppm_{label.lower()} = {setting}")
    left = [x for x in out["left_ppm"] if x is not None]
    try:
        written = receiver.configured(settings) == "rtlsdr2"
    except receiver.ReceiverError:
        written = False
    if written:
        receiver.update_gain("rtlsdr2", {"ppm_a": out["setting_ppm"][0], "ppm_b": out["setting_ppm"][1]}, settings)
        print(f"  saved ppm_a = {out['setting_ppm'][0]}, ppm_b = {out['setting_ppm'][1]} in the receiver settings "
              f"({receiver.SETTINGS if settings is None else settings}); they apply from the next start")
        out["shared_ppm"] = float(np.mean(left)) if left else 0.0
    else:
        print(f"  this computer's receiver isn't the RTL-SDR pair, so nothing was saved; the pair would want "
              f"ppm_a = {out['setting_ppm'][0]}, ppm_b = {out['setting_ppm'][1]}")
        measured = [x for x in out["measured_ppm"] if x is not None]
        out["shared_ppm"] = float(np.mean(measured)) if measured else 0.0
    out["settings_written"] = written
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*", help="IQ captures (default: newest downlink)")
    ap.add_argument("--snr", type=float, default=8.0,
                    help="dB above floor for a carrier to be used")
    args = ap.parse_args()

    import receiver
    try:
        target = receiver.configured()
    except receiver.ReceiverError as exc:
        raise SystemExit(str(exc)) from exc
    files = args.files
    explicit = bool(files)
    if not files:
        files = sorted(glob.glob(os.path.join(IQ_DIR, "*downlink*.iq")), key=os.path.getmtime)
        if not files:
            raise SystemExit("No downlink captures - run survey.py --band downlink")

    # Only captures made the way this computer's receiver is set up now: one from another receiver, or from the
    # pair with other sticks or other ppm settings, measures something else. Chosen before measuring, because a
    # channel is kept once, from whichever capture heard it best.
    chosen, pair, others = choose_captures(files, target)
    if not chosen:
        pair = choose_captures(files, "rtlsdr2")[1] if explicit and target != "rtlsdr2" else None
        if pair is None:
            raise SystemExit(f"No downlink captures made with the {receiver.name(target)}: run setup.sh car "
                             "--calibrate, or capture.py --freq 392.5 --secs 4 --rate 3 --tag downlink-calibration")
        chosen = pair["files"]
        print(f"  these captures were made with the RTL-SDR pair, and this computer's receiver is the "
              f"{receiver.name(target)}: what they measure is shown, and nothing is saved")
    else:
        if not explicit:
            chosen = chosen[-3:]             # the newest three
            if pair:
                pair["files"] = chosen
        if others and explicit:
            print(f"  left out {len(others)} capture(s) not made with the {receiver.name(target)} as it is set "
                  "up now: " + ", ".join(os.path.basename(f) for f in others))
    files = chosen

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
        for h in hits:
            h["file"] = os.path.basename(path)
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
    if pair:
        cal["rtl_pair"] = calibrate_pair(pair, rows)
        if not cal["rtl_pair"]["settings_written"]:
            # Another receiver's computer: the pair's measurement says nothing about this receiver.
            print("\nNothing written: these captures are the RTL-SDR pair's, and this computer's receiver isn't.")
            return
        cal["median_ppm"] = cal["rtl_pair"]["shared_ppm"]
        cal["median_offset_hz"] = None
        cal["note"] += "; for the RTL-SDR pair, what is left after each stick's own ppm setting (rtl_pair)"
    # Whole or not at all: the start-up check reads these first, and a file a power cut left half-written would
    # keep the detector from starting (until 24 November 2026 they were written in place; found in review).
    write_json(CAL_PATH, cal)
    print(f"\nWritten: {CAL_PATH}")
    # The carriers just measured are the local network's downlink channels. Stored for the
    # advisory downlink_paired label only; detection never depends on this list.
    channels = sorted({int(r["channel"]) for r in rows})
    write_json(CHANNELS_PATH, {"measured_utc": cal["measured_utc"], "channels": channels,
                               "note": "Downlink carriers measured by calibrate.py on this machine. Advisory label only."})
    print(f"Written: {CHANNELS_PATH} ({len(channels)} channels)")
    print("analyze.py and survey.py now correct for this automatically.")
    if pair:
        rp = cal["rtl_pair"]
        print(f"RTL-SDR pair: ppm_a = {rp['setting_ppm'][0]}, ppm_b = {rp['setting_ppm'][1]}"
              + (" saved; they apply from the next start" if rp["settings_written"] else " (not saved: see above)"))


if __name__ == "__main__":
    main()
