#!/usr/bin/env python3
"""Run the RTL-SDR pair's receiver code against scripts/rtlsdr_fake.c: real ctypes, C calling back into Python.

Build the stand-in first, against librtlsdr's header (see rtlsdr_fake.c), then:

    python scripts/rtlsdr_fake_test.py LIB [--seconds 20] [--detector]

Checks: every setting reaches each stick through the bindings (and A is the lower serial); a stream ends cleanly
(each stick cancelled, its thread ended, then closed); the joined output is bit for bit what rtl_combine makes
of the same bytes; a stick unplugged mid-stream ends the session; a 30 ms loss on one stick, with real threads
and real sleeping, is found and the halves realigned; what the whole receiver path costs (share of one core);
and with --detector, the whole detector (watch.py --receiver rtlsdr2) on synthetic TETRA bursts in both halves.
Prints one line per check and exits non-zero if any failed.
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
import receiver_rtlsdr as R  # noqa: E402
import rtl_combine as C  # noqa: E402
import rtlsdr_ctypes as L  # noqa: E402

results = []
GAIN = receiver.KINDS["rtlsdr2"]["gain"]
FIELDS = ("rate", "freq", "corr", "gain", "manual", "agc", "bias", "opened", "closed", "cancels", "reads",
          "buf_num", "buf_len", "sent_kib")


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""), flush=True)


class Sink:
    def __init__(self, keep=False):
        self.keep, self.blocks, self.samples, self.dropped, self.lost = keep, [], 0, 0, 0
        self.first_n = None

    def first(self, n, delay_s=0.0):
        self.first_n = n

    def drop(self, n):
        self.dropped += n

    def push(self, iq):
        self.samples += len(iq) // 2
        if self.keep:
            self.blocks.append(np.array(iq, copy=True))

    def lost(self, count):
        self.lost += count() if callable(count) else count


def setup(lib, serials=b"0000000200000001", tuner=5, data=None):
    lib.fake_configure(len(serials) // 8, serials, tuner, 0)
    for i, d in enumerate(data or ()):
        d = np.ascontiguousarray(d, np.uint8)
        lib.fake_load(i, d.ctypes.data_as(ctypes.c_void_p), ctypes.c_size_t(len(d)))


def report(lib, index):
    out = (ctypes.c_int64 * 14)()
    lib.fake_report(index, out)
    return dict(zip(FIELDS, out))


def session(lib, seconds, sink, gain=None, loss=None, fail=None):
    rx = R.Receiver("rtlsdr2", gain=dict(GAIN, **(gain or {})), lib=lib)
    rx.bind(sink)
    rx.open()
    if loss:
        lib.fake_loss(*loss)
    if fail:
        lib.fake_fail_after(*fail)
    cpu0, t0 = time.process_time(), time.perf_counter()
    rx.start()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and rx.fault() is None:
        time.sleep(0.05)
    cpu, wall = time.process_time() - cpu0, time.perf_counter() - t0
    status = rx.status()
    rx.close()
    return rx, status, cpu, wall


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("lib", help="the built stand-in (librtlsdr_fake.so or rtlsdr_fake.dll)")
    ap.add_argument("--seconds", type=float, default=20.0, help="length of the cost measurement")
    ap.add_argument("--detector", action="store_true", help="also run the whole detector on synthetic bursts")
    args = ap.parse_args(argv)
    lib = L.load(args.lib)
    for name in ("fake_configure", "fake_load", "fake_fail_after", "fake_loss", "fake_report"):
        getattr(lib, name).restype = None
    lib.fake_configure.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    lib.fake_load.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
    lib.fake_fail_after.argtypes = [ctypes.c_int, ctypes.c_size_t]
    lib.fake_loss.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_size_t]
    lib.fake_report.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int64)]

    # 1. Settings, through the bindings. Device 0 has serial ...2, device 1 ...1: A is device 1.
    setup(lib)
    rx = R.Receiver("rtlsdr2", gain=dict(GAIN, ppm_a=5, ppm_b=-9, tuner_gain_db10=290), lib=lib)
    rx.bind(Sink())
    info = rx.open()
    a, b = report(lib, 1), report(lib, 0)
    check("A is the lower serial", [s["serial"] for s in info["sticks"]] == ["00000001", "00000002"],
          [s["serial"] for s in info["sticks"]])
    check("each stick: 2.56 MS/s, its centre, its ppm, manual gain 29.7 dB, AGC and bias-T off",
          (a["rate"], a["freq"], a["corr"], b["freq"], b["corr"]) == (2_560_000, 381_250_000, 5, 383_750_000, -9)
          and all(s["gain"] == 297 and s["manual"] == 1 and s["agc"] == 0 and s["bias"] == 0 and s["opened"] == 1
                  for s in (a, b)), f"A {a} B {b}")
    rx.close()
    a, b = report(lib, 1), report(lib, 0)
    check("opened, never streamed: each closed once, nothing cancelled", (a["closed"], b["closed"], a["cancels"]) == (1, 1, 0))

    # 2. A stream, bit for bit: different random bytes on each stick, 1.6 s (before the first timing check
    #    can realign anything), against rtl_combine run directly on the same bytes.
    rng = np.random.default_rng(7)
    data = [rng.integers(0, 256, 2 * 2_560_000, dtype=np.uint8) for _ in (0, 1)]
    setup(lib, data=data)                            # device 0 = B, device 1 = A
    sink = Sink(keep=True)
    rx, status, _, _ = session(lib, 1.6, sink)
    got = np.concatenate(sink.blocks) if sink.blocks else np.zeros(0, np.int16)
    ref = C.Stitcher(2_560_000, gain=R.OUT_GAIN)
    m = len(got) // 2 // ref.n_out * ref.n_in
    # The stand-in plays each stick's 1 s of bytes round and round: so does the reference.
    y = ref.process(C.from_u8(np.resize(data[1], 2 * m)), C.from_u8(np.resize(data[0], 2 * m))).view(np.float32)
    want = np.rint(np.clip(y, -32768, 32767)).astype(np.int16)
    # Within one count (the FFT's last rounding depends on how many blocks a batch holds, on ARM: see
    # tests/test_rtlsdr.py), and such values rare.
    diff = np.abs(got.astype(np.int32) - want) if got.shape == want.shape and len(got) else np.array([99])
    share = np.count_nonzero(diff) / diff.size
    check("joined output is rtl_combine's, through C callbacks", int(diff.max()) <= 1 and share < 1e-3,
          f"{len(got) // 2} samples, {share * 100:.4f} % off by one, at most {int(diff.max())}")
    a, b = report(lib, 1), report(lib, 0)
    check("each stick cancelled once, its read ended, then closed once",
          all(s["cancels"] == 1 and s["reads"] == 1 and s["closed"] == 1 for s in (a, b)) and not rx.streaming)
    check("transfers of 24 x 65536 bytes", all((s["buf_num"], s["buf_len"]) == (24, 65536) for s in (a, b)))
    check("no fault, nothing lost", rx.fault() is None and sink.lost == 0 and sink.dropped == 0, rx.fault())

    # 3. A stick unplugged: its read returns -5 after 0.5 s.
    setup(lib)
    rx, status, _, _ = session(lib, 4.0, Sink(), fail=(0, 2 * 2_560_000 // 2))
    check("a stick unplugged mid-stream is the session's fault",
          "RTL-SDR B (00000002) stopped streaming (rtlsdr_read_async returned -5)" in (rx.fault() or ""), rx.fault())
    check("... and both are still closed", all(report(lib, i)["closed"] == 1 for i in (0, 1)))

    # 4. 30 ms lost on stick B 3 s in, real threads and real sleeping: found 5-6 s later, and realigned.
    setup(lib)
    sink = Sink()
    lost_bytes = int(0.030 * 2_560_000) * 2
    rx, status, _, _ = session(lib, 11.0, sink, loss=(0, 3 * 2 * 2_560_000, lost_bytes))
    found = status["lost_samples_b"]
    check("a 30 ms loss on one stick is found", abs(found - lost_bytes // 2) <= 2560 and status["lost_samples_a"] == 0,
          f"found {found} of {lost_bytes // 2} samples, A {status['lost_samples_a']}")
    check("... reported as dropped samples", abs(sink.dropped - found * 6_000_000 // 2_560_000) <= 3, sink.dropped)
    check("... and the halves realigned", abs(status["alignment_ms"] or 0) < 1.5 or status["realignments"] >= 1,
          f"alignment error {status['alignment_ms']} ms after, {status['realignments']} realignment(s)")

    # 5. What the whole receiver path costs: both readers' callbacks, the timing, and the joining.
    import test_detection
    band, _ = test_detection.wideband(channels=(1, 49, 99, 100, 150, 198), snr=20, seconds=0.6)
    import test_rtlsdr
    pair = test_rtlsdr.pair_from_band(band)
    setup(lib, data=[pair[1], pair[0]])
    sink = Sink()
    rx, status, cpu, wall = session(lib, args.seconds, sink)
    rate = sink.samples / wall
    check("the receiver path keeps up", rate > 0.97 * 6_000_000 and status["backlog_ms"] < 400,
          f"{rate / 1e6:.2f} MSPS out, backlog {status['backlog_ms']} ms")
    print(f"INFO  receiver path: {cpu:.1f} s CPU in {wall:.1f} s = {cpu / wall * 100:.0f} % of one core "
          f"(two readers, timing, joining; the stand-in's own copying included)", flush=True)
    check("no loss found where there was none", status["lost_samples_a"] + status["lost_samples_b"] == 0
          and sink.dropped == 0, f"{status['lost_samples_a']}, {status['lost_samples_b']}")

    # 6. The whole detector.
    if args.detector:
        import watch
        setup(lib, data=[pair[1], pair[0]])
        with tempfile.TemporaryDirectory() as tmp, patch.object(L, "load", return_value=lib), \
                patch.object(receiver, "SETTINGS", Path(tmp) / "none.json"), \
                patch.object(watch.paths, "ROOT", tmp), patch.object(watch.paths, "CAPTURES", str(Path(tmp) / "c")), \
                patch.object(watch.diagnostics, "host_health", return_value={}), \
                patch.object(watch.signal, "signal"), \
                patch.object(sys, "argv", ["watch.py", "--minutes", "0.25", "--no-notify", "--receiver", "rtlsdr2",
                                           "--heartbeat", str(Path(tmp) / "hb.json")]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            cpu0, t0 = time.process_time(), time.perf_counter()
            code = watch.main()
            cpu, wall = time.process_time() - cpu0, time.perf_counter() - t0
            folder = next((Path(tmp) / "c").glob("*"))
            events = [json.loads(x) for x in (folder / "events.jsonl").read_text().splitlines()]
            beat = json.loads((Path(tmp) / "hb.json").read_text())
        confirmed = {e.get("tetra_channel") for e in events if e.get("confirmed") is True}
        want = {1, 49, 99, 100, 150, 198}
        check("the whole detector recognises bursts in both halves, at the ends and at the handover",
              code == 0 and want <= confirmed and confirmed <= want, f"exit {code}, matched {sorted(confirmed)}")
        check("... with nothing dropped", beat["dropped"] == 0, beat.get("receiver"))
        print(f"INFO  the whole detector on the pair: {cpu:.1f} s CPU in {wall:.1f} s = {cpu / wall * 100:.0f} % of one "
              f"core (receiver path, analysis, recognition of six busy channels)", flush=True)
        if code != 0:
            print(out.getvalue()[-1500:])
    failed = results.count(False)
    print(f"{len(results) - failed} of {len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
