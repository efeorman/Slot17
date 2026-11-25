#!/usr/bin/env python3
"""Capture several bands in ONE device session, retuning while streaming.

The board's firmware hangs after a stop/start cycle, so every extra `airspy_rx`
invocation risks a wedge. This opens the device
once, starts the stream once, and changes frequency *mid-stream* — which libairspy
supports and SDR# relies on. A full uplink+downlink survey then costs one device
open instead of two.

    python stream.py --band both --secs 40
    python stream.py --plan 392.5:40 382.5:40 --tag evening
    python stream.py --plan 392.5:60 --tag longwatch --rate 6

Output is byte-identical in format to capture.py — raw int16 IQ plus a JSON
sidecar — so analyze.py, survey.py, spurs.py and calibrate.py all work unchanged.
"""
import argparse
import ctypes
import datetime as dt
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

import numpy as np

import airspy_ctypes as A

IQ_DIR = paths.IQ_DIR
FULL_SCALE = 32767.0
NEAR_RAIL = 32000

# The tuner needs a moment after a retune, and the driver's buffers still hold
# samples from the previous frequency. Discarding a fixed slice is cruder than
# tracking buffer boundaries but is reliable and costs almost nothing.
SETTLE_S = 0.25


class Session:
    """Shared state between the main thread and libairspy's callback thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.fh = None                  # open file, or None while settling/idle
        self.bytes_written = 0
        self.bytes_target = 0
        self.dropped = 0
        self.blocks = 0
        self.done = threading.Event()
        self.error = None


def make_callback(sess):
    def _cb(transfer_ptr):
        t = transfer_ptr.contents
        try:
            n = t.sample_count * A.BYTES_PER_SAMPLE[A.SAMPLE_INT16_IQ]
            with sess.lock:
                sess.blocks += 1
                if t.dropped_samples:
                    sess.dropped += t.dropped_samples
                fh = sess.fh
                if fh is None:
                    return 0                     # settling, or between segments
                remaining = sess.bytes_target - sess.bytes_written
                if remaining <= 0:
                    return 0
                take = min(n, remaining)
                buf = ctypes.string_at(t.samples, take)
                fh.write(buf)
                sess.bytes_written += take
                if sess.bytes_written >= sess.bytes_target:
                    sess.done.set()
        except Exception as e:                   # never let an exception cross into C
            sess.error = e
            sess.done.set()
        return 0
    return A.SAMPLE_BLOCK_CB(_cb)


def parse_plan(args):
    if args.plan:
        out = []
        for item in args.plan:
            freq, _, secs = item.partition(":")
            out.append((float(freq), float(secs) if secs else args.secs))
        return out
    bands = {"downlink": [392.5], "uplink": [382.5],
             "both": [392.5, 382.5]}[args.band]
    return [(f, args.secs) for f in bands]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--band", choices=["downlink", "uplink", "both"], default="both")
    ap.add_argument("--plan", nargs="+", metavar="MHZ[:SECS]",
                    help="explicit segments, e.g. 392.5:40 382.5:40")
    ap.add_argument("--secs", type=float, default=40.0)
    ap.add_argument("--rate", type=float, default=6.0)
    ap.add_argument("--lna", type=int, default=12)
    ap.add_argument("--mixer", type=int, default=12)
    ap.add_argument("--vga", type=int, default=12)
    ap.add_argument("--packing", type=int, default=0)
    ap.add_argument("--biast", type=int, default=0)
    ap.add_argument("--tag", default="stream")
    ap.add_argument("--note", default="")
    ap.add_argument("--close", action="store_true",
                    help="also call airspy_close() at the end. Off by default: it "
                         "races libairspy's own free_transfers() and can crash "
                         "after the data is safely written.")
    args = ap.parse_args()

    plan = parse_plan(args)
    rate_hz = int(args.rate * 1e6)
    bps = A.BYTES_PER_SAMPLE[A.SAMPLE_INT16_IQ]

    print(f"Plan: {len(plan)} segment(s), {args.rate:g} MSPS, "
          f"gain lna={args.lna} mixer={args.mixer} vga={args.vga}")
    for f, s in plan:
        print(f"   {f} MHz for {s:g}s  ({rate_hz*s*bps/1e6:.0f} MB)")
    print()

    A.check(A.lib.airspy_init(), "airspy_init")
    dev = ctypes.c_void_p()
    try:
        A.check(A.lib.airspy_open(ctypes.byref(dev)), "airspy_open")
    except A.AirspyError as e:
        print(f"\n{e}")
        print("The board is wedged or absent. Unplug it, wait 5 s, replug.")
        A.lib.airspy_exit()
        return 1

    sess = Session()
    cb = make_callback(sess)          # keep a reference alive for the C side
    written = []

    try:
        print("Firmware:", A.version_string(dev))
        print("Rates   :", ", ".join(f"{r/1e6:g}" for r in A.samplerates(dev)), "MSPS")

        A.check(A.lib.airspy_set_sample_type(dev, A.SAMPLE_INT16_IQ),
                "airspy_set_sample_type")
        A.check(A.lib.airspy_set_samplerate(dev, rate_hz), "airspy_set_samplerate")
        A.check(A.lib.airspy_set_packing(dev, args.packing), "airspy_set_packing")
        A.check(A.lib.airspy_set_rf_bias(dev, args.biast), "airspy_set_rf_bias")
        A.check(A.lib.airspy_set_lna_gain(dev, args.lna), "airspy_set_lna_gain")
        A.check(A.lib.airspy_set_mixer_gain(dev, args.mixer), "airspy_set_mixer_gain")
        A.check(A.lib.airspy_set_vga_gain(dev, args.vga), "airspy_set_vga_gain")

        # Gains go BEFORE start_rx and the frequency AFTER it - that is the order
        # airspy_rx.c itself uses. Setting the frequency first does not stick:
        # start_rx re-initialises the tuner, so the first segment ends up on some
        # default frequency. That cost one whole session's downlink capture.

        # ---- the whole point: ONE start, never stopped until the end ----------
        A.check(A.lib.airspy_start_rx(dev, cb, None), "airspy_start_rx")
        print("\nStreaming started - the device stays open for the whole session.\n")

        for i, (freq_mhz, secs) in enumerate(plan, 1):
            # Every segment tunes the same way, including the first: after the
            # stream is running. One code path, no special case to get wrong.
            A.check(A.lib.airspy_set_freq(dev, int(freq_mhz * 1e6)),
                    "airspy_set_freq")
            time.sleep(SETTLE_S)          # discard the retune transient

            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            base = (f"{stamp}_{args.tag}-{freq_mhz:g}_"
                    f"{freq_mhz:.4f}MHz_{args.rate:g}Msps")
            raw = os.path.join(IQ_DIR, base + ".iq")
            target = int(rate_hz * secs) * bps

            fh = open(raw, "wb", buffering=1024 * 1024)
            with sess.lock:
                sess.fh = fh
                sess.bytes_written = 0
                sess.bytes_target = target
                sess.done.clear()
            t0 = time.monotonic()
            print(f"[{i}/{len(plan)}] {freq_mhz} MHz, {secs:g}s "
                  f"({target/1e6:.0f} MB) ...", end="", flush=True)

            if not sess.done.wait(timeout=secs * 4 + 30):
                print(" TIMED OUT")
                break
            with sess.lock:
                sess.fh = None
                got = sess.bytes_written
            fh.close()
            elapsed = time.monotonic() - t0

            if sess.error:
                raise sess.error
            if A.lib.airspy_is_streaming(dev) != 1:
                print("  stream stopped unexpectedly")

            meta = {
                "file": os.path.basename(raw),
                "captured_utc": dt.datetime.now(dt.timezone.utc)
                                  .isoformat(timespec="seconds"),
                "centre_hz": freq_mhz * 1e6,
                "sample_rate_hz": rate_hz,
                "format": "int16 interleaved IQ (libairspy, live-retuned stream)",
                "samples": got // bps,
                "duration_s": round(got / bps / rate_hz, 3),
                "gain": {"lna": args.lna, "mixer": args.mixer, "vga": args.vga},
                "bias_tee": bool(args.biast),
                "packing": bool(args.packing),
                "segment": i,
                "note": args.note,
            }
            x = np.fromfile(raw, dtype=np.int16, count=4_000_000)
            peak = float(np.max(np.abs(x))) if x.size else 0.0
            meta["adc"] = {
                "peak": peak,
                "headroom_db": round(20 * np.log10(FULL_SCALE / max(peak, 1.0)), 1),
                "clipped_fraction": float(np.mean(np.abs(x) >= NEAR_RAIL))
                                    if x.size else 0.0,
            }
            with open(os.path.splitext(raw)[0] + ".json", "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)

            print(f" done in {elapsed:.1f}s, ADC peak {peak:.0f}, "
                  f"headroom {meta['adc']['headroom_db']:.1f} dB")
            print(f"        {raw}")
            written.append(raw)

    except A.AirspyError as e:
        print(f"\n{e}")
    finally:
        # Teardown works around a race inside libairspy itself, not in this code.
        # airspy_close() calls free_transfers(), which deallocates the sample
        # queues with NO synchronisation against the libusb transfer callback -
        # that callback can still fire between cancel_transfers() and the free,
        # and then writes into memory that has just been released. That is the
        # "access violation writing 0x28" seen here, and the same race is
        # reported by others through SoapyAirspy.
        #
        # So: stop the hardware properly, wait for streaming to actually end,
        # and then deliberately DO NOT call airspy_close(). Skipping it leaks a
        # device handle and some host memory for the few milliseconds until the
        # process exits, at which point Windows reclaims both. That is a far
        # better trade than calling into a known use-after-free.
        with sess.lock:
            if sess.fh:
                sess.fh.close()
                sess.fh = None

        try:
            A.lib.airspy_stop_rx(dev)
        except OSError as e:
            print(f"airspy_stop_rx raised: {e}")

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                if A.lib.airspy_is_streaming(dev) != 1:
                    break
            except OSError:
                break
            time.sleep(0.05)
        else:
            print("warning: still streaming after 3 s")
        time.sleep(0.3)          # let any in-flight libusb callback finish

        if args.close:
            try:
                A.lib.airspy_close(dev)
                A.lib.airspy_exit()
            except OSError as e:
                print(f"airspy_close raised ({e}) - captured data is complete; "
                      f"this is the known libairspy free_transfers race.")

    print(f"\n{len(written)} segment(s) captured in ONE device session.")
    if sess.dropped:
        print(f"WARNING: {sess.dropped:,} dropped samples reported - "
              f"the host could not keep up.")
    else:
        print("No dropped samples.")
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
