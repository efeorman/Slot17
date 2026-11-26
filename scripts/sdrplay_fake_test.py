#!/usr/bin/env python3
"""Run the RSP1B receiver code against scripts/sdrplay_fake.c: real ctypes, real foreign-thread callbacks.

Build the stand-in first, against SDRplay's headers (see sdrplay_fake.c), then:

    python scripts/sdrplay_fake_test.py LIB [--seconds 3] [--detector]

Scenarios: a stream with a wrap of the sample counter, an injected gap and an overload that must be acknowledged,
checked sample by sample; a reset; a removal; the wrong model; the cost of the callback path at three block sizes;
and with --detector, the whole detector (watch.py --receiver rsp1b) on synthetic TETRA bursts streamed by the
stand-in's own thread. Prints one line per check and exits non-zero if any failed.
"""
import argparse
import contextlib
import ctypes
import io
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "p0"))
sys.path.insert(0, str(ROOT / "tests"))
import receiver  # noqa: E402
import receiver_sdrplay  # noqa: E402
import sdrplay_ctypes as S  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


class Sink:
    def __init__(self):
        self.blocks, self.drops, self.lost, self.firsts = [], [], 0, 0

    def first(self, n, delay_s=0.0):
        self.firsts += 1

    def drop(self, n):
        self.drops.append((sum(len(b) for b in self.blocks) // 2, n))

    def push(self, iq):
        self.blocks.append(np.array(iq, copy=True))

    def lost(self, count):
        self.lost += count() if callable(count) else count


def load(path):
    lib = S.load(path)
    fake = ctypes.CDLL(path)
    fake.fake_configure.argtypes = [ctypes.c_int] * 9
    fake.fake_report.restype = ctypes.c_char_p
    fake.fake_load_iq.argtypes = [ctypes.c_char_p]
    return lib, fake


def session(path, seconds, block=1344, realtime=1, gap_after=-1, gap_samples=0, reset_at=-1, overload_at=-1,
            remove_at=-1, max_blocks=-1, hw=S.RSP1B_ID, kind="rsp1b"):
    lib, fake = load(path)
    fake.fake_configure(block, realtime, gap_after, gap_samples, reset_at, overload_at, remove_at, max_blocks, hw)
    rx = receiver_sdrplay.Receiver(kind, fs=6_000_000, centre_hz=382.5e6,
                                   gain={"lna_state": 2, "if_gain_reduction_db": 40}, lib=lib)
    sink = Sink()
    rx.bind(sink)
    error = None
    cpu0, wall0 = time.process_time(), time.monotonic()
    try:
        rx.open()
        rx.start()
        time.sleep(seconds)
    except receiver.ReceiverError as exc:
        error = str(exc)
    cpu, wall = time.process_time() - cpu0, time.monotonic() - wall0
    fault = rx.fault()
    rx.close()
    report = json.loads(fake.fake_report().decode())
    return rx, sink, report, error, fault, cpu / wall


def ramp_breaks(blocks):
    """Where the ramp I = n, Q = -n (mod 2^15) does not continue: (sample index, step)."""
    iq = np.concatenate(blocks)
    i, q = iq[0::2].astype(np.int64), iq[1::2].astype(np.int64)
    bad_q = int(np.count_nonzero(q != -i))
    step = (np.diff(i) % 0x8000)
    where = np.nonzero(step != 1)[0]
    return [(int(k + 1), int(step[k])) for k in where], bad_q, len(i)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("lib")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.add_argument("--detector", action="store_true")
    args = ap.parse_args(argv)
    path = str(Path(args.lib).resolve())

    # 1. A real-time stream: counter wrap, a gap of 777 samples after block 500, an overload at block 200.
    rx, sink, rep, err, fault, load_ = session(path, args.seconds, gap_after=500, gap_samples=777, overload_at=200)
    check("parameters Slot17 set, read back through the C structures", rep["params_ok"] == 1,
          f"fs {rep['fs']:.0f}, rf {rep['rf']:.0f}, bw {rep['bw']}, if {rep['if']}, gRdB {rep['grdb']}, "
          f"LNA {rep['lna']}, AGC {rep['agc']}, DC/IQ {rep['dc']}/{rep['iq']}, bias {rep['bias']}, ppm {rep['ppm']}")
    check("SDRplay's call order, each once", (rep["open"], rep["lock"], rep["unlock"], rep["select"], rep["init"],
                                               rep["uninit"], rep["release"], rep["close"]) == (1,) * 8, str(rep))
    breaks, bad_q, n = ramp_breaks(sink.blocks)
    check("I and Q interleaved exactly, through the counter's wrap", bad_q == 0 and n == rep["samples"],
          f"{n} samples, {bad_q} wrong Q")
    check("the one injected gap is where it was made, and only there", breaks == [(501 * 1344, 778)], str(breaks[:5]))
    st = rx.status()
    check("the gap is counted as 777 lost samples", st["gaps"] == 1 and st["lost_samples"] == 777
          and [d[1] for d in sink.drops] == [777], f"{st['gaps']} gap(s), {st['lost_samples']} lost, drops {sink.drops}")
    check("the overload is counted and acknowledged", st["overloads"] == 1 and rep["overload_acks"] == 1,
          f"overloads {st['overloads']}, acknowledgements {rep['overload_acks']}")
    rate = rep["samples"] / args.seconds
    check("about real time", 0.9 * 6e6 < rate < 1.1 * 6e6, f"{rate / 1e6:.2f} MSPS, {st['callbacks']} callbacks")
    check("nothing lost in Python", sink.lost == 0 and err is None and fault is None)

    # 2. A reset inside the API: a gap of unknown size.
    rx, sink, rep, err, fault, _ = session(path, 0.5, reset_at=50)
    check("a reset after the start is a gap", rx.status()["resets"] == 1 and len(sink.drops) == 1, str(sink.drops))

    # 3. The device is removed: the session must end.
    rx, sink, rep, err, fault, _ = session(path, 0.6, remove_at=100)
    check("a removed device is a fault", fault is not None and "removed" in fault, str(fault))

    # 4. The wrong model is never started.
    rx, sink, rep, err, fault, _ = session(path, 0.2, hw=S.RSP1A_ID)
    check("an RSP1A where an RSP1B was set up is refused", err is not None and "no SDRplay RSP1B" in err
          and rep["init"] == 0 and rep["close"] == 1, str(err))

    # 5. What the callback path costs, as a share of one core, at three block sizes.
    for block in (1008, 1344, 4032):
        rx, sink, rep, err, fault, load_ = session(path, 2.0, block=block)
        print(f"INFO  callback path at {block} samples per callback: {load_ * 100:.1f} % of one core "
              f"({rx.status()['callbacks'] / 2.0:.0f} callbacks/s)", flush=True)

    # 6. The whole detector on synthetic TETRA bursts, streamed by the stand-in's own thread.
    if args.detector:
        import test_receivers
        import watch
        iq = test_receivers.synthetic_band(seconds=0.6, channels=(20, 104, 180), snr=20)
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "band.iq"
            iq.tofile(raw)
            lib, fake = load(path)
            fake.fake_configure(1344, 1, -1, 0, -1, -1, -1, -1, S.RSP1B_ID)
            if fake.fake_load_iq(str(raw).encode()) != 0:
                check("the stand-in reads the synthetic band", False)
                return 1
            with patch.object(S, "load", return_value=lib), \
                    patch.object(watch.paths, "ROOT", tmp), patch.object(watch.paths, "CAPTURES", str(Path(tmp) / "c")), \
                    patch.object(watch.diagnostics, "host_health", return_value={}), \
                    patch.object(sys, "argv", ["watch.py", "--minutes", "0.15", "--no-notify", "--receiver", "rsp1b",
                                               "--heartbeat", str(Path(tmp) / "hb.json")]), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                code = watch.main()
            session_dir = next((Path(tmp) / "c").glob("*"))
            events = [json.loads(x) for x in (session_dir / "events.jsonl").read_text().splitlines()]
            beat = json.loads((Path(tmp) / "hb.json").read_text())
            confirmed = sorted({e.get("tetra_channel") for e in events if e.get("confirmed") is True})
            check("the whole detector ends cleanly", code == 0, out.getvalue()[-800:] if code else "")
            check("bursts on channels 20, 104 and 180 recognised through the RSP1B path",
                  {20, 104, 180} <= set(confirmed), f"confirmed on {confirmed}")
            check("no gaps and no drops in the detector", beat["dropped"] == 0 and beat["receiver"]["gaps"] == 0,
                  f"dropped {beat['dropped']}, receiver {beat['receiver']}")
            print(f"INFO  detector: {beat['windows']} windows, window time {beat['window_seconds']} s, "
                  f"{beat['receiver']['callbacks']} callbacks", flush=True)
    print(f"{sum(results)} of {len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
