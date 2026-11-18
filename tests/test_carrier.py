"""The whole-burst carrier frequency (phy.carrier_frequency) and the start-up grace for dropped samples."""
import sys
import types
from pathlib import Path
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import examine
import phy
import pipeline
import watch
from test_phy import fixture


def matched(bb, fs):
    starts, stops = examine.find_bursts(bb, fs)
    for burst in phy.analyse(bb, fs, starts, stops)["bursts"]:
        if burst["accepted"]:
            return burst
    return None


class CarrierFrequencyTests(unittest.TestCase):
    def test_known_offsets_are_measured_within_a_few_hz(self):
        """Generated bursts at 25 dB with offsets from -300 to +300 Hz: every match within 3 Hz of the truth."""
        for kind in ("cub", "nub"):
            for offset in (-300.0, -85.0, 0.0, 40.0, 300.0):
                for seed in range(3):
                    bb, fs = fixture(kind, 25, seed, offset=offset)
                    b = matched(bb, fs)
                    self.assertIsNotNone(b, (kind, offset, seed))
                    self.assertAlmostEqual(b["carrier_hz"], offset, delta=3.0, msg=(kind, offset, seed, b))
                    self.assertLess(b["carrier_noise_hz"], 3.0, (kind, offset, seed, b))
                    self.assertGreaterEqual(b["carrier_symbols"], 40)

    def test_it_is_steadier_than_the_training_word_estimate(self):
        """At 15 dB the whole burst scatters several times less than the training word around the truth."""
        whole, word = [], []
        for seed in range(20):
            bb, fs = fixture("nub", 15, seed, offset=-60.0)
            b = matched(bb, fs)
            if b:
                whole.append(b["carrier_hz"] + 60.0)
                word.append(b["residual_offset_hz"] + 60.0)
        self.assertGreaterEqual(len(whole), 15)
        self.assertLess(np.std(whole) * 3, np.std(word), (np.std(whole), np.std(word)))

    def test_noise_estimate_tracks_the_real_error(self):
        """The stated noise is about the error's size: the median error stays under twice the median stated noise plus 1 Hz."""
        errors, stated = [], []
        for seed in range(20):
            bb, fs = fixture("cub", 12, seed, offset=25.0)
            b = matched(bb, fs)
            if b:
                errors.append(abs(b["carrier_hz"] - 25.0))
                stated.append(b["carrier_noise_hz"])
        self.assertGreaterEqual(len(errors), 10)
        self.assertLess(np.median(errors), 2 * np.median(stated) + 1.0, (errors, stated))

    def test_it_never_changes_a_decision(self):
        """Acceptance with and without the measurement is identical (it runs after the decision)."""
        original = phy.carrier_frequency
        try:
            for seed in range(6):
                bb, fs = fixture("cub", 9, seed)
                starts, stops = examine.find_bursts(bb, fs)
                with_it = phy.analyse(bb, fs, starts, stops)
                phy.carrier_frequency = lambda z: None
                without = phy.analyse(bb, fs, starts, stops)
                phy.carrier_frequency = original
                self.assertEqual(with_it["accepted"], without["accepted"])
                self.assertEqual([b["accepted"] for b in with_it["bursts"]], [b["accepted"] for b in without["bursts"]])
        finally:
            phy.carrier_frequency = original

    def test_short_or_empty_input_gives_none(self):
        self.assertIsNone(phy.carrier_frequency(np.zeros(10, complex)))
        self.assertIsNone(phy.carrier_frequency(np.zeros(200, complex)))
        self.assertIsNone(phy.carrier_frequency(np.full(200, np.nan, complex)))


class StartupGraceTests(unittest.TestCase):
    def stats(self, dropped, startup):
        st = watch.Stats()
        st.dropped, st.startup_dropped = dropped, startup
        return st

    def test_startup_loss_is_not_a_fault_and_keeps_the_threads(self):
        st = self.stats(720896, 720896)
        self.assertEqual([r["fault"] for r in watch.degraded_reasons(st, None)], [])
        ctx = types.SimpleNamespace(stats=st, confirm_workers=3)
        self.assertEqual(pipeline.confirm_workers(ctx), 3)

    def test_later_loss_is_still_a_fault_and_counts_only_itself(self):
        st = self.stats(720896 + 65536, 720896)
        reasons = watch.degraded_reasons(st, None)
        self.assertEqual([(r["fault"], r["total"]) for r in reasons], [("dropped samples", 65536)])
        ctx = types.SimpleNamespace(stats=st, confirm_workers=3)
        self.assertEqual(pipeline.confirm_workers(ctx), 1)

    def test_the_callback_sorts_losses_by_stream_position(self):
        """watch.count_drop, which the driver callback calls: losses before 60 s of stream are start-up ones."""
        st = watch.Stats()
        st.fs = 6_000_000
        ring = types.SimpleNamespace(write=0)
        for seconds, lost in ((10, 131072), (59, 65536), (61, 4096)):
            ring.write = int(seconds * st.fs)
            watch.count_drop(st, ring, lost)
        self.assertEqual(st.dropped, 131072 + 65536 + 4096)
        self.assertEqual(st.startup_dropped, 131072 + 65536)
        self.assertEqual(watch.faulty_drops(st), 4096)
        self.assertEqual(list(st.discontinuities)[-1], (int(61 * st.fs), st.dropped))

    def test_grace_is_settable_and_bounded(self):
        import os
        saved = os.environ.get("SLOT17_STARTUP_GRACE_S")
        try:
            for raw, want in (("0", 0.0), ("120", 120.0), ("-5", 0.0), ("100000", 600.0), ("nonsense", 60.0), ("nan", 60.0)):
                os.environ["SLOT17_STARTUP_GRACE_S"] = raw
                self.assertEqual(watch._startup_grace_s(), want, raw)
        finally:
            if saved is None:
                os.environ.pop("SLOT17_STARTUP_GRACE_S", None)
            else:
                os.environ["SLOT17_STARTUP_GRACE_S"] = saved


if __name__ == "__main__":
    unittest.main()
