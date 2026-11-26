#!/usr/bin/env python3
"""Capture IQ from the Airspy Mini into a raw file plus a JSON sidecar.

Every capture is self-describing: the sidecar records centre frequency, sample
rate, every individual gain stage and the measured ADC headroom, so a capture
can still be interpreted months later.

Usage:
    python capture.py --freq 392.5 --secs 5 --tag downlink-survey
    python capture.py --freq 382.5 --secs 10 --lna 8 --mixer 8 --vga 12
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

import numpy as np

BIN = paths.AIRSPY_RX
IQ_DIR = paths.IQ_DIR

# airspy_rx -t 2 = INT16_IQ: interleaved int16 I,Q -> 4 bytes per complex sample.
BYTES_PER_SAMPLE = 4
# The ADC is 12-bit, but airspy_rx -t 2 emits the DDC output, and that filter
# chain has gain - measured peaks reach ~11000 with no sign of clipping. So the
# rail that matters is int16, and clipping is judged by a pile-up near it rather
# than by any assumed 12-bit limit.
FULL_SCALE = 32767.0
NEAR_RAIL = 32000


def levels(x):
    """Peak, RMS, headroom (dB) and the share near the digital output's rails, of interleaved int16 samples.
    Widened first: in int16, abs(-32768) is -32768 again, so a capture clipped on the negative rail read as
    having no peak and no clipping (found in review, 24 November 2026)."""
    a = np.abs(x.astype(np.int32))
    peak = float(a.max()) if a.size else 0.0
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) if a.size else 0.0
    return peak, rms, 20 * np.log10(FULL_SCALE / max(peak, 1.0)), float(np.mean(a >= NEAR_RAIL)) if a.size else 0.0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--freq", type=float, required=True, help="centre frequency in MHz")
    p.add_argument("--rate", type=float, default=6.0,
                   help="sample rate in MSPS, 6 or 3. 6 gives ~5.03 MHz of "
                        "usable window and works from firmware rc10-6 onward; "
                        "it failed on the 2016 rc10-0 firmware.")
    p.add_argument("--secs", type=float, default=5.0, help="capture length in seconds")
    p.add_argument("--lna", type=int, default=6, help="LNA gain 0-14")
    p.add_argument("--mixer", type=int, default=6, help="mixer gain 0-15")
    p.add_argument("--vga", type=int, default=10, help="VGA/IF gain 0-15")
    p.add_argument("--biast", type=int, default=0, help="bias tee 1=on 0=off")
    p.add_argument("--packing", type=int, default=0,
                   help="1 = 12-bit packing on the USB link, which cuts bus "
                        "bandwidth by 25%% (24 -> 18 MB/s at 6 MSPS). The host "
                        "unpacks, so the samples are identical either way.")
    p.add_argument("--tag", default="capture", help="short label used in the filename")
    p.add_argument("--note", default="", help="free-text note stored in the sidecar")
    p.add_argument("--receiver", help="which receiver; by default the one setup chose (p0/receiver.py). "
                                      "Anything but the Airspy records at 6 MSPS through its own driver")
    args = p.parse_args()

    paths.ensure_dirs()          # captures/iq does not exist on a new install
    import receiver
    kind = args.receiver or receiver.configured()
    if kind != "airspy":
        return capture_with_backend(kind, args)
    rate_hz = int(args.rate * 1e6)
    nsamples = int(rate_hz * args.secs)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}_{args.tag}_{args.freq:.4f}MHz_{args.rate:g}Msps"
    raw = os.path.join(IQ_DIR, base + ".iq")
    meta_path = os.path.join(IQ_DIR, base + ".json")

    cmd = [BIN, "-r", raw, "-f", f"{args.freq}", "-a", str(rate_hz), "-t", "2",
           "-l", str(args.lna), "-m", str(args.mixer), "-v", str(args.vga),
           "-b", str(args.biast), "-p", str(args.packing), "-n", str(nsamples)]

    mb = nsamples * BYTES_PER_SAMPLE / 1e6
    print(f"Capturing {args.secs:g}s @ {args.freq} MHz, {args.rate:g} MSPS "
          f"({mb:.0f} MB), gains lna={args.lna} mixer={args.mixer} vga={args.vga}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if not os.path.exists(raw) or os.path.getsize(raw) == 0:
        print(r.stdout, r.stderr, file=sys.stderr)
        # Leave no empty file behind: a zero-byte .iq sorts as the newest
        # capture and then breaks every tool that picks up "the latest one".
        if os.path.exists(raw):
            os.remove(raw)
        raise SystemExit("Capture failed - no data written.")
    if r.returncode != 0:
        print(r.stdout, r.stderr, file=sys.stderr)
        raise SystemExit(f"Capture ended early (exit {r.returncode}). What was captured is kept in {raw}.")

    # Headroom check on a slice from the middle, so the tuner has settled.
    size = os.path.getsize(raw)
    n_have = size // BYTES_PER_SAMPLE
    probe = min(n_have, 2_000_000)
    off = ((n_have - probe) // 2) * BYTES_PER_SAMPLE
    x = np.fromfile(raw, dtype=np.int16, count=probe * 2, offset=off)
    peak, rms, headroom_db, clip_frac = levels(x)

    meta = {
        "file": os.path.basename(raw),
        "captured_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "centre_hz": args.freq * 1e6,
        "sample_rate_hz": rate_hz,
        "format": "int16 interleaved IQ (airspy_rx -t 2)",
        "samples": int(n_have),
        "duration_s": round(n_have / rate_hz, 3),
        "gain": {"lna": args.lna, "mixer": args.mixer, "vga": args.vga},
        "bias_tee": bool(args.biast),
        "packing": bool(args.packing),
        "adc": {"peak": peak, "rms": round(rms, 1),
                "headroom_db": round(headroom_db, 1),
                "clipped_fraction": clip_frac},
        "note": args.note,
    }
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)

    print(f"  -> {raw}  ({size/1e6:.0f} MB, {meta['duration_s']:g}s)")
    print(f"  ADC peak {peak:.0f}/{FULL_SCALE:.0f}  headroom {headroom_db:.1f} dB  "
          f"clipped {clip_frac*100:.4f}%")
    if clip_frac > 1e-6:
        print("  WARNING: clipping - drop VGA or LNA gain and recapture.")
    elif headroom_db > 30:
        print("  NOTE: very quiet - you can raise gain for more resolution.")


def capture_with_backend(kind, args, rate_hz=6_000_000):
    """The same .iq and .json as airspy_rx makes, from any other receiver through its backend (p0/receiver.py).

    6 MSPS: what those receivers run at here, whatever --rate says. The backend is opened once and closed once.
    """
    import threading
    import receiver
    if args.rate * 1e6 != rate_hz:
        print(f"  the {receiver.name(kind)} records at {rate_hz / 1e6:g} MSPS here (not {args.rate:g})")
    want = int(rate_hz * args.secs)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{stamp}_{args.tag}_{args.freq:.4f}MHz_{rate_hz / 1e6:g}Msps"
    raw = os.path.join(IQ_DIR, base + ".iq")
    meta_path = os.path.join(IQ_DIR, base + ".json")
    done = threading.Event()

    class FileSink:
        def __init__(self, fh):
            self.fh, self.got, self.dropped = fh, 0, 0

        def first(self, n, delay_s=0.0):
            pass

        def drop(self, n):
            self.dropped += n

        def push(self, iq):
            take = min(len(iq) // 2, want - self.got)
            if take > 0:
                self.fh.write(iq[:2 * take].tobytes())
                self.got += take
            if self.got >= want:
                done.set()

        def lost(self, count):
            self.dropped += count() if callable(count) else count

    print(f"Capturing {args.secs:g}s @ {args.freq} MHz, {rate_hz / 1e6:g} MSPS with the {receiver.name(kind)}")
    try:
        rx = receiver.backend(kind).Receiver(kind, fs=rate_hz, centre_hz=args.freq * 1e6, gain=receiver.gain_for(kind))
    except receiver.ReceiverError as exc:
        raise SystemExit(f"Capture failed: {exc}") from exc
    with open(raw, "wb") as fh:
        sink = FileSink(fh)
        rx.bind(sink)
        try:
            info = rx.open()
            rx.start()
            done.wait(timeout=args.secs + 20)
        except Exception as exc:              # noqa: BLE001 - reported, and the receiver is still closed below
            info = None
            print(f"  {type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            rx.close()
    if sink.got == 0:
        os.remove(raw)                        # never leave an empty .iq as "the newest capture"
        raise SystemExit("Capture failed - no data written.")
    x = np.fromfile(raw, dtype=np.int16, count=min(sink.got, 2_000_000) * 2,
                    offset=max(0, (sink.got - 2_000_000) // 2) * 4)
    peak, rms, headroom_db, clipped = levels(x)
    meta = {
        "file": os.path.basename(raw),
        "captured_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "centre_hz": args.freq * 1e6, "sample_rate_hz": rate_hz,
        "format": f"int16 interleaved IQ ({receiver.name(kind)})",
        "samples": int(sink.got), "duration_s": round(sink.got / rate_hz, 3),
        "receiver": {"kind": kind, "name": receiver.name(kind), "info": info},
        "gain": rx.gain_record(), "dropped_samples": int(sink.dropped),
        "adc": {"peak": peak, "rms": round(rms, 1), "headroom_db": round(20 * np.log10(FULL_SCALE / max(peak, 1.0)), 1),
                "clipped_fraction": clipped},
        "note": args.note,
    }
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)
    print(f"  -> {raw}  ({sink.got} samples, {sink.dropped} lost)")
    if sink.got < want:
        raise SystemExit(f"Capture ended early: {sink.got} of {want} samples. What was captured is kept in {raw}.")
    return 0


if __name__ == "__main__":
    main()
