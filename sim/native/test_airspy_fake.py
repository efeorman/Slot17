#!/usr/bin/env python3
"""The Airspy stand-in (sim/native/airspy_fake.c) against its contract, through the detector's own receiver class
(p0/receiver_airspy.py and the ctypes bindings in p0/airspy_ctypes.py): what a real libairspy would hand it, faults
included. Each case runs in a process of its own, as the detector does, because the bindings load the library when
they are imported.

    python sim/native/test_airspy_fake.py [LIBRARY]     (default: build it with sim/native/build.py)

Not part of tests/: the updater tests a release in a copy with p0/ and tests/ only (see F13 in the review record).
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TRANSFER = 65536
LIBRARY = None


def probe(seconds, slow_s=0.0, stop_after=None):
    """In this process: open the stand-in with receiver_airspy, stream, and report what the sink saw (JSON)."""
    import time
    sys.path.insert(0, str(ROOT / "p0"))
    import receiver_airspy

    class Sink:
        def __init__(self):
            self.first_n, self.blocks, self.dropped, self.lost = None, [], [], 0

        def first(self, n, delay_s=0.0):
            if self.first_n is None:
                self.first_n = n

        def drop(self, n):
            self.dropped.append((len(self.blocks), int(n)))

        def push(self, iq):
            self.blocks.append(np.array(iq, copy=True))
            if slow_s:
                time.sleep(slow_s)

        def lost(self, count):
            self.lost += count() if callable(count) else count

    sink = Sink()
    rx = receiver_airspy.Receiver("airspy", fs=6_000_000, centre_hz=382.5e6, sink=sink)
    out = {"open": None, "error": None}
    try:
        out["open"] = str(rx.open())
        rx.start()
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds and (stop_after is None or len(sink.blocks) < stop_after):
            time.sleep(0.01)
        out["streaming"] = rx.A.lib.airspy_is_streaming(rx.dev)
    except Exception as exc:                     # noqa: BLE001 - reported to the test
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        rx.close()
    flat = np.concatenate(sink.blocks) if sink.blocks else np.zeros(0, np.int16)
    out.update(blocks=len(sink.blocks), sizes=sorted({len(b) for b in sink.blocks}), dropped=sink.dropped,
               lost=sink.lost, first_n=sink.first_n, head=flat[:8].tolist(), samples=flat.tolist()[:2 * TRANSFER * 3])
    print(json.dumps(out))


class Contract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global LIBRARY
        if LIBRARY is None:
            sys.path.insert(0, str(HERE))
            import build
            LIBRARY = str(build.build(Path(tempfile.mkdtemp()) / "build"))
        cls.tmp = tempfile.TemporaryDirectory()
        t = Path(cls.tmp.name)
        cls.iq = t / "scene.iq"
        n = 3 * TRANSFER + 1000                     # three transfers and a bit: then zeros
        ramp = np.arange(2 * n, dtype=np.int64)
        (ramp % 30011 - 15005).astype("<i2").tofile(cls.iq)
        cls.n = n

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_probe(self, seconds=0.6, control=None, slow_s=0.0, stop_after=None, iq=True, state=None, boot="b1",
                  loop=False):
        t = Path(self.tmp.name)
        ledger = t / f"ledger-{self.id().split('.')[-1]}-{os.urandom(3).hex()}.jsonl"
        env = {k: v for k, v in os.environ.items() if not k.startswith("SLOT17_SIM_")}
        env.update(SLOT17_AIRSPY_LIB=LIBRARY, SLOT17_SIM_LEDGER=str(ledger), SLOT17_SIM_BOOT=boot)
        if iq:
            env["SLOT17_SIM_IQ"] = str(self.iq if iq is True else iq)
        if loop:
            env["SLOT17_SIM_LOOP"] = "1"
        if control is not None:
            path = t / f"control-{os.urandom(3).hex()}.json"
            path.write_text(json.dumps(control))
            env["SLOT17_SIM_CONTROL"] = str(path)
        if state is not None:
            env["SLOT17_SIM_STATE"] = str(state)
        code = f"import sys; sys.path.insert(0, {str(HERE)!r}); import test_airspy_fake as T; " \
               f"T.probe({seconds!r}, {slow_s!r}, {stop_after!r})"
        done = subprocess.run([sys.executable, "-B", "-c", code], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        result = json.loads(done.stdout.strip().splitlines()[-1])
        lines = [json.loads(x) for x in ledger.read_text().splitlines()] if ledger.exists() else []
        return result, lines

    def test_the_scene_arrives_as_libairspy_would_hand_it_over(self):
        r, ledger = self.run_probe(seconds=0.6)
        self.assertIsNone(r["error"])
        self.assertIn("Slot17 stand-in", r["open"])
        self.assertEqual(r["first_n"], TRANSFER)
        self.assertEqual(r["sizes"], [2 * TRANSFER])
        self.assertGreaterEqual(r["blocks"], 4)
        want = (np.arange(2 * 3 * TRANSFER, dtype=np.int64) % 30011 - 15005)
        self.assertEqual(r["samples"], want.tolist(), "the file's samples, in order, interleaved I/Q")
        events = [x["event"] for x in ledger]
        # The receiver's own order: set everything, start, then tune (as released watch.py did).
        self.assertLess(events.index("set_packing"), events.index("start_rx"))
        self.assertLess(events.index("start_rx"), events.index("set_freq"))
        self.assertIn("scene_end", events)
        starts = [x for x in ledger if x["event"] == "start_rx"]
        self.assertEqual((starts[0]["samplerate"], starts[0]["packing"], starts[0]["lna_agc"], starts[0]["rf_bias"]),
                         (6_000_000, 0, 0, 0))
        transfers = [x for x in ledger if x["event"] == "transfer"]
        self.assertEqual([x["first_sample"] for x in transfers[:3]], [0, TRANSFER, 2 * TRANSFER])
        self.assertTrue(all(x["delivered"] >= x["deadline"] - 0.002 for x in transfers), "never ahead of real time")

    def test_without_a_scene_there_is_no_receiver(self):
        r, ledger = self.run_probe(iq=False)
        self.assertIn("airspy_open", r["error"] or "")
        self.assertEqual(r["blocks"], 0)

    def test_a_scene_that_cant_be_read_or_isnt_whole_is_refused_at_open(self):
        """Fourth review round (F35): a missing file became zeros, which a negative control passes as quiet."""
        t = Path(self.tmp.name)
        (t / "empty.iq").write_bytes(b"")
        (t / "odd.iq").write_bytes(b"\x01\x00\x02")
        for name, why in (("absent.iq", "can't be read"), ("empty.iq", "is empty"),
                          ("odd.iq", "not whole int16 I/Q samples")):
            with self.subTest(name=name):
                r, ledger = self.run_probe(iq=t / name)
                self.assertIn("airspy_open", r["error"] or "")
                self.assertEqual(r["blocks"], 0)
                self.assertTrue(any(why in x.get("why", "") for x in ledger if x["event"] == "open"), ledger)

    def test_a_short_looped_scene_fills_every_transfer(self):
        """Fourth review round (F35): it rewound once a transfer, and padded the rest with zeros."""
        one = Path(self.tmp.name) / "one-sample.iq"
        np.array([1, 2], "<i2").tofile(one)
        r, ledger = self.run_probe(seconds=0.3, iq=one, loop=True)
        self.assertIsNone(r["error"])
        self.assertGreaterEqual(r["blocks"], 1)
        self.assertEqual(r["samples"][:2 * TRANSFER], [1, 2] * TRANSFER)
        self.assertNotIn("scene_end", [x["event"] for x in ledger])

    def test_a_scheduled_drop_is_reported_on_the_next_transfer(self):
        r, ledger = self.run_probe(seconds=0.6, control={"drop": [[TRANSFER, 2]]})
        self.assertEqual(r["dropped"][0], [1, 2 * TRANSFER], "after the first block, two transfers' worth")
        transfers = [x for x in ledger if x["event"] == "transfer"]
        self.assertEqual([x["first_sample"] for x in transfers[:2]], [0, 3 * TRANSFER])
        self.assertEqual(transfers[1]["dropped"], 2 * TRANSFER)

    def test_a_slow_callback_loses_whole_transfers_like_libairspy(self):
        """Eight in the queue; when it is full the newest is dropped and counted on the next one that gets in."""
        r, ledger = self.run_probe(seconds=1.5, slow_s=0.05)        # 50 ms a transfer against 10.9 ms of samples
        reported = sum(n for _, n in r["dropped"])
        dropped = [x for x in ledger if x["event"] == "drop" and x["why"] == "queue_full"]
        self.assertGreater(len(dropped), 5)
        self.assertEqual(reported % TRANSFER, 0)
        # Every dropped transfer is reported, except those after the last one delivered.
        self.assertLessEqual(reported, len(dropped) * TRANSFER)
        self.assertGreaterEqual(reported, (len(dropped) - 40) * TRANSFER)

    def test_a_stall_stops_the_samples_but_not_the_streaming_flag(self):
        r, ledger = self.run_probe(seconds=0.8, control={"stall_after_samples": 2 * TRANSFER})
        self.assertEqual(r["blocks"], 2)
        self.assertEqual(r["streaming"], 1, "as a wedged board: streaming, nothing comes")
        self.assertIn("stall", [x["event"] for x in ledger])

    def test_a_second_start_in_one_boot_fails_as_the_board_does(self):
        state = Path(self.tmp.name) / "board-state.json"
        r1, _ = self.run_probe(seconds=0.2, state=state, boot="boot-A")
        r2, ledger = self.run_probe(seconds=0.2, state=state, boot="boot-A")
        r3, _ = self.run_probe(seconds=0.2, state=state, boot="boot-B")
        self.assertIsNone(r1["error"])
        self.assertIn("start_rx", r2["error"] or "")
        self.assertIn("wedge", [x["event"] for x in ledger])
        self.assertIsNone(r3["error"], "a new power-up")

    def test_an_injected_failure(self):
        r, ledger = self.run_probe(control={"fail": {"call": "airspy_set_vga_gain", "rc": -1000}})
        self.assertIn("vga_gain", r["error"] or "")
        self.assertIn({"call": "airspy_set_vga_gain", "rc": -1000},
                      [{"call": x["call"], "rc": x["rc"]} for x in ledger if x["event"] == "fail"])

    def test_fast_pace_runs_ahead_of_real_time(self):
        r, ledger = self.run_probe(seconds=0.3, control={"pace": "fast"}, stop_after=60)
        self.assertGreaterEqual(r["blocks"], 60)                   # 60 x 10.9 ms of samples in under 0.3 s

    def test_every_ledger_line_is_one_json_object_with_a_time_and_a_pid(self):
        r, ledger = self.run_probe(seconds=0.3)
        self.assertTrue(ledger)
        self.assertTrue(all(isinstance(x["t_mono"], float) and x["pid"] > 0 and x["event"] for x in ledger))


if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        LIBRARY = str(Path(sys.argv.pop(1)).resolve())
    unittest.main()
