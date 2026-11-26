#!/usr/bin/env python3
"""The real detector, unchanged, on the Airspy stand-in: p0/watch.py run as its own process, as the service runs it
(no patching), with SLOT17_AIRSPY_LIB pointing at sim/native's library and SLOT17_ROOT and HOME at a scratch folder.

What it shows: one opening of the receiver per session, generated bursts recognised on the channels they are on and
no others, a stall ending the session as a stall, and a board that wedges at its second start refused without a
retry. What it doesn't: the supervisor, systemd, the publisher, or anything about real RF (see sim/README.md).

    python sim/native/test_watch_on_airspy_fake.py [LIBRARY]     (default: build it with sim/native/build.py)
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
LIBRARY = None
CHANNELS = (1, 99, 100, 198)                 # both ends of the band and both sides of 382.5 MHz


def scene(path, seconds=0.6):
    """Bursts on CHANNELS at 20 dB in noise, from the suite's own generator, as the stand-in's int16 file."""
    sys.path.insert(0, str(ROOT / "tests"))
    sys.path.insert(0, str(ROOT / "p0"))
    import test_detection
    band, fs = test_detection.wideband(channels=CHANNELS, snr=20, seconds=seconds)
    raw = np.empty(2 * len(band), "<i2")
    raw[0::2] = np.clip(np.round(band.real), -32768, 32767)
    raw[1::2] = np.clip(np.round(band.imag), -32768, 32767)
    raw.tofile(path)


class RealDetector(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global LIBRARY
        if LIBRARY is None:
            sys.path.insert(0, str(HERE))
            import build
            try:
                LIBRARY = str(build.build(Path(tempfile.mkdtemp()) / "build"))
            except SystemExit as exc:
                raise unittest.SkipTest(f"no stand-in library: {str(exc)[:200]}") from None
        cls.tmp = tempfile.TemporaryDirectory()
        cls.iq = Path(cls.tmp.name) / "scene.iq"
        scene(cls.iq)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def watch(self, minutes, control=None, state=None, extra=()):
        run = Path(tempfile.mkdtemp(dir=self.tmp.name))
        (run / "home").mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("SLOT17_", "NOTIFY_SOCKET"))}
        env.update(SLOT17_AIRSPY_LIB=LIBRARY, SLOT17_SIM_IQ=str(self.iq), SLOT17_SIM_LOOP="1",
                   SLOT17_SIM_LEDGER=str(run / "ledger.jsonl"), SLOT17_SIM_BOOT="boot-1", SLOT17_ROOT=str(run),
                   HOME=str(run / "home"), USERPROFILE=str(run / "home"))
        if control is not None:
            (run / "control.json").write_text(json.dumps(control))
            env["SLOT17_SIM_CONTROL"] = str(run / "control.json")
        if state is not None:
            env["SLOT17_SIM_STATE"] = str(state)
        done = subprocess.run([sys.executable, "-B", str(ROOT / "p0" / "watch.py"), "--minutes", str(minutes),
                               "--no-notify", "--heartbeat", str(run / "hb.json"), *extra],
                              env=env, capture_output=True, text=True, timeout=300, cwd=str(ROOT / "p0"))
        sessions = sorted((run / "captures").glob("*/events.jsonl"))
        events = [json.loads(x) for x in sessions[-1].read_text().splitlines() if x.strip()] if sessions else []
        summary = json.loads((sessions[-1].parent / "summary.json").read_text()) if sessions else {}
        ledger = [json.loads(x) for x in (run / "ledger.jsonl").read_text().splitlines()] \
            if (run / "ledger.jsonl").exists() else []
        return done, events, summary, ledger

    def test_the_bursts_are_recognised_on_their_channels_and_the_receiver_opened_once(self):
        done, events, summary, ledger = self.watch(0.1)
        out = done.stdout[-3000:] + done.stderr[-3000:]
        self.assertEqual(done.returncode, 0, out)
        confirmed = {e.get("tetra_channel") for e in events if e.get("confirmed") is True}
        self.assertTrue(set(CHANNELS) <= confirmed, (sorted(c for c in confirmed if c is not None), out[-1500:]))
        self.assertLessEqual(confirmed - {None}, set(CHANNELS), "a burst recognised on a channel it wasn't on")
        starts = [x for x in ledger if x["event"] == "start_rx"]
        self.assertEqual(len(starts), 1)
        self.assertEqual((starts[0]["packing"], starts[0]["lna_agc"], starts[0]["mixer_agc"], starts[0]["rf_bias"]),
                         (0, 0, 0, 0))
        self.assertGreater(summary.get("windows", 0), 0)
        self.assertEqual(summary.get("dropped_samples", 0), 0)

    def test_a_stall_ends_the_session_as_a_stall(self):
        done, events, summary, ledger = self.watch(1.0, control={"stall_after_samples": 6_000_000 * 3},
                                                   extra=("--stall-timeout", "3"))
        out = done.stdout[-3000:] + done.stderr[-3000:]
        self.assertEqual(done.returncode, 75, out)                     # failure.STREAM_STALL
        self.assertIn("stall", [x["event"] for x in ledger])
        self.assertEqual(len([x for x in ledger if x["event"] == "start_rx"]), 1, "no second opening")

    def test_a_board_that_wedges_at_its_second_start_is_refused_once(self):
        state = Path(tempfile.mkdtemp(dir=self.tmp.name)) / "board.json"
        state.write_text(json.dumps({"boot": "boot-1", "starts": 1}))       # started once already this power-up
        done, events, summary, ledger = self.watch(0.1, state=state)
        out = done.stdout[-3000:] + done.stderr[-3000:]
        self.assertNotEqual(done.returncode, 0, out)
        starts = [x for x in ledger if x["event"] == "start_rx"]
        self.assertEqual([x["rc"] for x in starts], [-1000], "refused, and not tried again")


if __name__ == "__main__":
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        LIBRARY = str(Path(sys.argv.pop(1)).resolve())
    unittest.main()
