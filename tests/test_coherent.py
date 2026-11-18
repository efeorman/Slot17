"""1.2.7: the coherent training-word rule, and what it must never do."""
import sys
from pathlib import Path
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import examine
import phy
from test_phy import fixture

OFF = {"cub": 2.0, "nub1": 2.0, "nub2": 2.0}


def run(kind, snr, seed, limits=None, transform=None):
    saved = dict(phy.COHERENT_LIMITS)
    try:
        if limits is not None:
            phy.COHERENT_LIMITS = limits
        bb, fs = fixture(kind, snr, seed)
        if transform is not None:
            bb = transform(bb)
        s, e = examine.find_bursts(bb, fs)
        return phy.analyse(bb, fs, s, e)
    finally:
        phy.COHERENT_LIMITS = saved


class CoherentRuleTests(unittest.TestCase):
    def test_limits_as_chosen(self):
        self.assertEqual(phy.COHERENT_LIMITS, {"cub": 0.899, "nub1": 0.88, "nub2": 0.88})

    def test_normal_sequence_two_is_the_conjugate_of_sequence_one(self):
        """Why a conjugated normal burst is not a control for the word (it is a real one)."""
        np.testing.assert_allclose(phy.template("nub2"), np.conj(phy.template("nub1")))
        self.assertLess(abs(np.mean(phy.template("cub") ** 2)), 0.1)

    def test_more_weak_control_bursts_recognised(self):
        """Seeds 0-39: at 6 dB 14 without the rule, 27 with it; at 7 dB 25 and 38."""
        for snr, at_least in ((6, 25), (7, 36)):
            found = sum(run("cub", snr, seed)["accepted"] for seed in range(40))
            self.assertGreaterEqual(found, at_least, (snr, found))

    def test_it_only_adds(self):
        """Every burst the standard and weak rules accept stays accepted, by the same rule."""
        for kind in ("cub", "nub"):
            for snr in (7, 9, 15):
                for seed in range(12):
                    off = run(kind, snr, seed, OFF)
                    on = run(kind, snr, seed)
                    for a, b in zip(off["bursts"], on["bursts"]):
                        if a["accepted"]:
                            self.assertTrue(b["accepted"])
                            self.assertEqual(a["rule"], b["rule"])
                        if b["accepted"] and not a["accepted"]:
                            self.assertEqual(b["rule"], "coherent")

    def test_reversed_and_conjugated_bursts_refused(self):
        for kind in ("cub", "nub"):
            for seed in range(15):
                for transform in (lambda x: x[::-1].copy(), np.conj):
                    self.assertFalse(run(kind, 25, seed, transform=transform)["accepted"], (kind, seed))

    def test_untrained_bursts_refused(self):
        for kind in ("cub", "nub"):
            for snr in (8, 15, 30):
                for seed in range(20):
                    bb, fs = fixture(kind, snr, 9000 + seed, training=False)
                    s, e = examine.find_bursts(bb, fs)
                    self.assertFalse(phy.analyse(bb, fs, s, e)["accepted"], (kind, snr, seed))

    def test_score_recorded_on_every_inspected_burst(self):
        r = run("cub", 20, 1)
        b = r["bursts"][0]
        self.assertIn("coherent_correlation", b)
        self.assertGreater(b["coherent_correlation"], 0.9)
        self.assertEqual(b["coherent_training"], "cub")


if __name__ == "__main__":
    unittest.main()
