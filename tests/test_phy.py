"""Independent positive/negative cases for short-burst recognition; no radio."""
import sys
from pathlib import Path
import unittest

import numpy as np
from scipy.signal import firwin, lfilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
import examine
import phy
import tetra_gen


def fixture(kind, snr, seed, offset=0.0, training=True):
    fs = 75000
    tx = tetra_gen.make_transmission(0.10, fs, kind=kind, frames=[0], seed=seed,
                                    first_frame_offset_s=0.04, training=training)
    tx *= np.exp(2j * np.pi * offset * np.arange(len(tx)) / fs)
    rng = np.random.default_rng(1000 + seed)
    noise = (rng.normal(size=len(tx)) + 1j * rng.normal(size=len(tx))) / np.sqrt(2)
    iq = noise + tx * np.sqrt(10 ** (snr / 10) * tetra_gen.snr_bandwidth_hz() / fs)
    return iq.astype(np.complex64), fs


class RecognitionTests(unittest.TestCase):
    def test_contiguous_allocated_slots(self):
        fs = 75000
        rng = np.random.default_rng(22)
        tx = tetra_gen.burst("nub", 4, rng)
        from scipy.signal import resample_poly
        tx = resample_poly(tx, 25, 24)
        bb = np.zeros(9000, np.complex64)
        slot = 255 / 18000 * fs
        for k in range(4):
            start = 2000 + round(k * slot)
            bb[start:start + len(tx)] += tx
        noise = (rng.normal(size=len(bb)) + 1j*rng.normal(size=len(bb))) * .01
        bb += noise.astype(np.complex64)
        # The candidate envelope can merge adjacent allocations.
        r = phy.analyse(bb, fs, [2000], [2000 + round(4 * slot)])
        self.assertTrue(r["accepted"], r)

    def test_single_short_bursts(self):
        for kind in ("cub", "nub"):
            for snr in (10, 16, 25):
                found = 0
                for seed in range(12):
                    bb, fs = fixture(kind, snr, seed)
                    starts, stops = examine.find_bursts(bb, fs)
                    found += phy.analyse(bb, fs, starts, stops)["accepted"]
                self.assertGreaterEqual(found, 10, (kind, snr, found))

    def test_weak_bursts_are_recognised_more_often_and_say_which_rule_made_the_match(self):
        """Counts from 40 generated bursts each; the standard rule alone finds 13, 24, 1 and 22, the rules together 19, 33, 23 and 28."""
        for kind, snr, at_least in (("cub", 7, 14), ("cub", 8, 30), ("nub", 7, 14), ("nub", 8, 26)):
            found = 0
            rules = set()
            for seed in range(40):
                bb, fs = fixture(kind, snr, seed)
                starts, stops = examine.find_bursts(bb, fs)
                result = phy.analyse(bb, fs, starts, stops)
                found += result["accepted"]
                rules |= {b["rule"] for b in result["bursts"] if b["accepted"]}
            self.assertGreaterEqual(found, at_least, (kind, snr, found))
            # 1.2.7 added the `coherent` rule, which only accepts what both of these refused.
            self.assertLessEqual(rules, {"standard", "weak", "coherent"}, (kind, snr))
            self.assertIn("weak", rules, (kind, snr))

    def test_a_strong_burst_is_matched_by_the_standard_rule(self):
        bb, fs = fixture("cub", 25, 3)
        starts, stops = examine.find_bursts(bb, fs)
        accepted = [b for b in phy.analyse(bb, fs, starts, stops)["bursts"] if b["accepted"]]
        self.assertEqual([b["rule"] for b in accepted], ["standard"])

    def test_the_rules_only_ever_add_to_the_standard_one(self):
        # coherence, correlation, errors, worst training symbol in degrees
        cases = [("cub", 0.36, 0.83, 1, 80.0, "standard"), ("cub", 0.31, 0.83, 1, 80.0, "weak"),
                 ("cub", 0.29, 0.83, 1, 80.0, None), ("cub", 0.21, 0.91, 0, 34.0, "weak"),
                 ("cub", 0.21, 0.91, 0, 36.0, None), ("cub", 0.21, 0.89, 0, 20.0, None),
                 ("cub", 0.19, 0.99, 0, 5.0, None), ("cub", 0.90, 0.81, 0, 5.0, None),
                 ("cub", 0.90, 0.99, 2, 50.0, None),
                 ("nub1", 0.36, 0.89, 0, 40.0, "standard"), ("nub2", 0.16, 0.89, 0, 40.0, "weak"),
                 ("nub1", 0.14, 0.99, 0, 5.0, None), ("nub1", 0.90, 0.87, 0, 5.0, None),
                 ("nub2", 0.90, 0.99, 1, 50.0, None)]
        for kind, coherence, corr, errors, worst, wanted in cases:
            self.assertEqual(phy.matching_rule(kind, coherence, corr, errors, worst), wanted,
                             (kind, coherence, corr, errors, worst))

    def test_weak_noise_bursts_and_weak_random_modulation_are_still_refused(self):
        """Controls at the strengths where the looser rules act; none may be accepted in any direction."""
        fs = 75000
        accepted = 0
        for seed in range(60):
            rng = np.random.default_rng(seed + 9000)
            noise = (rng.normal(size=7500) + 1j * rng.normal(size=7500)) / np.sqrt(2)
            shaped = lfilter(firwin(129, 9500, fs=fs), [1.0], noise)
            envelope = np.zeros(len(noise))
            envelope[3000:3000 + (465 if seed % 2 else 1050)] = 1
            for bb in ((noise + shaped * (2, 3, 4)[seed % 3] * envelope).astype(np.complex64),
                       fixture("cub" if seed % 2 else "nub", (7, 8, 10)[seed % 3], seed + 7000, training=False)[0][::-1].copy()):
                for data in (bb, bb[::-1].copy(), np.conj(bb)):
                    a, b = examine.find_bursts(data, fs)
                    accepted += phy.analyse(data, fs, a, b)["accepted"]
        self.assertEqual(accepted, 0)

    def test_residual_frequency_offset(self):
        for offset in (-1200, 600, 1200):
            bb, fs = fixture("cub", 16, 35, offset=offset)
            a, b = examine.find_bursts(bb, fs)
            self.assertTrue(phy.analyse(bb, fs, a, b)["accepted"], offset)

    def test_noise_and_tone_confusers(self):
        fs = 75000
        for seed in range(40):
            rng = np.random.default_rng(seed + 3000)
            noise = (rng.normal(size=7500) + 1j * rng.normal(size=7500)) / np.sqrt(2)
            shaped = lfilter(firwin(129, 9500, fs=fs), [1.0], noise)
            envelope = np.zeros(len(noise))
            envelope[3000:3465] = 1
            for source in (shaped * 40, np.ones(len(noise)) * 20):
                bb = (noise + source * envelope).astype(np.complex64)
                a, b = examine.find_bursts(bb, fs)
                self.assertFalse(phy.analyse(bb, fs, a, b)["accepted"], seed)

    def test_random_dqpsk_is_not_training(self):
        found = 0
        for seed in range(50):
            bb, fs = fixture("cub", 25, seed + 100, training=False)
            a, b = examine.find_bursts(bb, fs)
            found += phy.analyse(bb, fs, a, b)["accepted"]
        self.assertEqual(found, 0)


if __name__ == "__main__":
    unittest.main()
