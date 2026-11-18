#!/usr/bin/env python3
"""Isolate the sustained-streaming sample loss found in earlier testing.

The 20-minute uplink watch dropped an escalating number of samples (0 for ~3 min,
then climbing to ~7 %), all at the device->host layer. This probe strips everything
else away to find out *why* and *when*:

  * It opens the radio once and streams, exactly like the product.
  * Its callback does almost nothing - it only adds up samples received and the
    `dropped_samples` the library reports. No channelising, no detection, no copy.
    So if drops still appear, the cause is the USB link / driver / device, NOT our
    processing.
  * `--copy` adds back the one memcpy `watch.py`'s callback does, so you can test
    whether that copy is the trigger by comparing a plain run with a `--copy` run.
  * Every second it logs: samples received, samples dropped, the drop percentage,
    and the running totals - to the screen and to a CSV - so the exact onset time
    and how fast it grows are captured, not just 60-second snapshots.

    python drop_probe.py --band uplink --minutes 20            # plain (min. work)
    python drop_probe.py --band uplink --minutes 20 --copy     # with the memcpy
    python drop_probe.py --selftest                            # logic, no hardware

Run both the plain and --copy versions after a fresh replug (the board tolerates
~4 opens per plug-in), on this laptop and again on the Pi. If the Pi stays at 0,
the loss is a Windows-bench artefact and the product platform is unaffected.
"""
import argparse
import csv
import ctypes
import datetime as dt
import os
import signal
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

BANDS = {"uplink": 382.5, "downlink": 392.5,
         "civil-uplink": 415.0, "civil-downlink": 425.0}
BYTES_PER_SAMPLE = 4

_stop = threading.Event()


class Counters:
    def __init__(self):
        self.lock = threading.Lock()
        self.recv = 0          # samples handed to the callback
        self.dropped = 0       # samples the library reports as lost
        self.blocks = 0


def make_callback(c, do_copy):
    def _cb(tp):
        t = tp.contents
        n = t.sample_count
        d = t.dropped_samples
        if do_copy:
            # the exact work watch.py's callback does, and nothing more
            _ = ctypes.string_at(t.samples, n * BYTES_PER_SAMPLE)
        with c.lock:
            c.recv += n
            c.dropped += d
            c.blocks += 1
        return 0
    import airspy_ctypes as A
    return A.SAMPLE_BLOCK_CB(_cb)


def row(prev, now, elapsed_s, dt_s, fs):
    """One CSV/console row from two counter snapshots. Pure - unit-tested.

    prev/now are (recv, dropped) tuples; returns a dict of per-interval figures.
    """
    recv_d = now[0] - prev[0]
    drop_d = now[1] - prev[1]
    total = recv_d + drop_d
    return {
        "elapsed_s": round(elapsed_s, 1),
        "recv": recv_d,
        "dropped": drop_d,
        "drop_pct": round(100.0 * drop_d / total, 3) if total else 0.0,
        "recv_rate_msps": round(recv_d / dt_s / 1e6, 3) if dt_s else 0.0,
        "cum_recv": now[0],
        "cum_dropped": now[1],
        "cum_drop_pct": round(100.0 * now[1] / (now[0] + now[1]), 3)
        if (now[0] + now[1]) else 0.0,
    }


def probe(centre_mhz, fs, minutes, gains, do_copy, tag):
    import airspy_ctypes as A

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = os.path.join(paths.CAPTURES,
                            f"{stamp}_{tag}_{centre_mhz:g}MHz_dropprobe.csv")
    os.makedirs(paths.CAPTURES, exist_ok=True)

    print(f"Drop probe: {centre_mhz} MHz, {fs/1e6:g} MSPS, "
          f"gains lna/mixer/vga={gains}, callback={'copy' if do_copy else 'minimal'}")
    print(f"  logging every second to {csv_path}")
    print(f"  {minutes:g} min (Ctrl+C to stop early)\n")

    A.check(A.lib.airspy_init(), "airspy_init")
    dev = ctypes.c_void_p()
    try:
        A.check(A.lib.airspy_open(ctypes.byref(dev)), "airspy_open")
    except A.AirspyError as e:
        print(f"{e}\nBoard wedged or absent - unplug, wait 5 s, replug.")
        A.lib.airspy_exit()
        return 1

    c = Counters()
    cb = make_callback(c, do_copy)
    signal.signal(signal.SIGINT, lambda *_: _stop.set())

    fh = open(csv_path, "w", newline="", encoding="utf-8")
    wr = csv.DictWriter(fh, fieldnames=list(
        row((0, 0), (0, 0), 0, 1, fs).keys()) + ["onset"])
    wr.writeheader()

    onset_s = None
    try:
        A.check(A.lib.airspy_set_sample_type(dev, A.SAMPLE_INT16_IQ), "sample_type")
        A.check(A.lib.airspy_set_samplerate(dev, int(fs)), "samplerate")
        A.check(A.lib.airspy_set_lna_gain(dev, gains[0]), "lna")
        A.check(A.lib.airspy_set_mixer_gain(dev, gains[1]), "mixer")
        A.check(A.lib.airspy_set_vga_gain(dev, gains[2]), "vga")
        A.check(A.lib.airspy_start_rx(dev, cb, None), "start_rx")
        A.check(A.lib.airspy_set_freq(dev, int(centre_mhz * 1e6)), "set_freq")

        t0 = time.time()
        prev = (0, 0)
        prev_t = t0
        deadline = t0 + minutes * 60
        while not _stop.is_set() and time.time() < deadline:
            time.sleep(1.0)
            t = time.time()
            with c.lock:
                now = (c.recv, c.dropped)
            r = row(prev, now, t - t0, t - prev_t, fs)
            if onset_s is None and r["dropped"] > 0:
                onset_s = r["elapsed_s"]
                print(f"  >>> first drops at t+{onset_s:.0f}s", flush=True)
            r["onset"] = onset_s if onset_s is not None else ""
            wr.writerow(r)
            fh.flush()
            print(f"  t+{r['elapsed_s']:6.0f}s  recv {r['recv_rate_msps']:5.2f} MSPS"
                  f"  dropped {r['dropped']:>9,}  {r['drop_pct']:5.1f}%"
                  f"  cum_drop {r['cum_dropped']:>12,} ({r['cum_drop_pct']:.2f}%)",
                  flush=True)
            prev, prev_t = now, t
    except A.AirspyError as e:
        print(f"\n{e}")
    finally:
        time.sleep(0.3)
        try:
            A.lib.airspy_stop_rx(dev)
        except OSError:
            pass
        end = time.time() + 3
        while time.time() < end:
            try:
                if A.lib.airspy_is_streaming(dev) != 1:
                    break
            except OSError:
                break
            time.sleep(0.05)
        time.sleep(0.3)
        # airspy_close() deliberately skipped.
        fh.close()

    with c.lock:
        total = c.recv + c.dropped
    print(f"\nDone. received {c.recv:,}, dropped {c.dropped:,} "
          f"({100*c.dropped/total:.2f}% of {total:,}) in {c.blocks:,} blocks.")
    print(f"  onset: {'t+%.0fs' % onset_s if onset_s is not None else 'no drops'}")
    print(f"  csv: {csv_path}")
    return 0


def selftest():
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{'  ' + detail if detail else ''}")

    print("drop_probe self-test (no hardware)")
    fs = 6_000_000.0

    # a clean second: 6 M received, none dropped
    r = row((0, 0), (6_000_000, 0), 1.0, 1.0, fs)
    check("clean second: 0 % drop", r["drop_pct"] == 0.0, f"{r['drop_pct']}%")
    check("clean second: ~6 MSPS", abs(r["recv_rate_msps"] - 6.0) < 0.01,
          f"{r['recv_rate_msps']} MSPS")

    # a lossy second: 5.5 M received, 0.5 M dropped -> 8.33 %
    r = row((6_000_000, 0), (11_500_000, 500_000), 2.0, 1.0, fs)
    check("lossy second: drop % correct",
          abs(r["drop_pct"] - 100 * 0.5 / 6.0) < 0.01, f"{r['drop_pct']}%")
    check("lossy second: per-interval delta, not cumulative", r["dropped"] == 500_000,
          f"{r['dropped']}")
    check("cumulative drop % correct",   # over received + dropped = 12.0 M
          abs(r["cum_drop_pct"] - 100 * 500_000 / 12_000_000) < 0.01,
          f"{r['cum_drop_pct']}%")

    # a stall: almost nothing received
    r = row((11_500_000, 500_000), (11_600_000, 500_000), 3.0, 1.0, fs)
    check("stall shows low recv rate", r["recv_rate_msps"] < 0.2,
          f"{r['recv_rate_msps']} MSPS")

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--band", choices=sorted(BANDS), default="uplink")
    g.add_argument("--freq", type=float, help="explicit centre in MHz")
    ap.add_argument("--minutes", type=float, default=20.0)
    ap.add_argument("--rate", type=float, default=6.0)
    ap.add_argument("--lna", type=int, default=8)
    ap.add_argument("--mixer", type=int, default=8)
    ap.add_argument("--vga", type=int, default=8)
    ap.add_argument("--copy", action="store_true",
                    help="add the per-block memcpy watch.py does (isolates its cost)")
    ap.add_argument("--tag", default="dropprobe")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        raise SystemExit(selftest())

    centre = args.freq if args.freq else BANDS[args.band]
    raise SystemExit(probe(centre, args.rate * 1e6, args.minutes,
                           (args.lna, args.mixer, args.vga), args.copy, args.tag))


if __name__ == "__main__":
    main()
