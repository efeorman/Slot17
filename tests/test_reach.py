"""1.2.6: two-stage channel extraction, a finer symbol-timing grid, and the full 200-channel band."""
import sys
from pathlib import Path
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import channelize
import examine
import phy
import tetra_gen
from channelize import channelise
from detect import detect
from test_phy import fixture

FS, CENTRE = 6_000_000.0, 382.5e6


def noise(seconds, seed=0):
    rng = np.random.default_rng(seed)
    n = int(seconds * FS)
    return ((rng.normal(size=n) + 1j * rng.normal(size=n)) * 300).astype(np.complex64)


class TwoStageTuneTests(unittest.TestCase):
    def test_same_samples_as_the_direct_path(self):
        x = noise(0.1, 1)
        t = np.arange(len(x)) / FS
        for target in (380.0125e6, 381.7375e6, 383.2125e6, 384.9875e6):
            y = x + (2000 * np.exp(2j * np.pi * (target - CENTRE) * t)).astype(np.complex64)
            a, fa = examine.tune(y, FS, CENTRE, target)
            b, fb = examine.tune_direct(y, FS, CENTRE, target)
            self.assertEqual((fa, len(a)), (fb, len(b)))
            core = slice(8, len(a) - 8)
            rel = np.linalg.norm(a[core] - b[core]) / np.linalg.norm(b[core])
            self.assertLess(rel, 0.01, (target, rel))

    def test_other_rates_use_the_direct_path(self):
        x = noise(0.05, 2)[::2]
        a, fa = examine.tune(x, 3e6, CENTRE, 383e6)
        b, fb = examine.tune_direct(x, 3e6, CENTRE, 383e6)
        self.assertEqual(fa, fb)
        np.testing.assert_array_equal(a, b)

    def test_empty_input(self):
        a, fa = examine.tune(np.zeros(0, np.complex64), FS, CENTRE, 383e6)
        self.assertEqual(len(a), 0)
        self.assertEqual(fa, 75_000.0)

    # Speed is measured in the audit, not here: these tests also run at every start on the car
    # computer, where a busy moment must never fail the start-up checks.


class TimingGridTests(unittest.TestCase):
    def test_eight_timing_phases(self):
        self.assertEqual(phy.SPS, 8)
        self.assertEqual(phy.FS, 144_000)

    def test_weak_bursts_recognised_more_often(self):
        """Seeds 0-39 at 7 dB: 19 control and 23 normal bursts with 4 phases, 25 and 26 with 8."""
        for kind, at_least in (("cub", 24), ("nub", 25)):
            found = 0
            for seed in range(40):
                bb, fs = fixture(kind, 7, seed)
                s, e = examine.find_bursts(bb, fs)
                found += phy.analyse(bb, fs, s, e)["accepted"]
            self.assertGreaterEqual(found, at_least, (kind, found))

    def test_untrained_bursts_still_refused(self):
        for kind in ("cub", "nub"):
            for snr in (10, 30):
                for seed in range(25):
                    bb, fs = fixture(kind, snr, 7000 + seed, training=False)
                    s, e = examine.find_bursts(bb, fs)
                    self.assertFalse(phy.analyse(bb, fs, s, e)["accepted"], (kind, snr, seed))


class FullBandTests(unittest.TestCase):
    def test_two_hundred_channels(self):
        for centre in (382.5e6, 392.5e6):
            ch = channelise(noise(0.05, 4), FS, centre, apply_cal=False)
            self.assertEqual(list(ch.tetra_ch), list(range(200)))

    def test_edge_channels_are_detected_and_flagged(self):
        for target, edge in ((1, True), (198, True), (100, False), (195, False)):
            x = noise(0.3136, 10 + target)
            off = 380.0125e6 + target * 25e3 - CENTRE
            y = tetra_gen.inject(x, FS, offset_hz=off, snr_db=20.0, seed=target, kind="cub",
                                 frames=[0], first_frame_offset_s=0.1)
            hits = [h for h in detect(channelise(y, FS, CENTRE, apply_cal=False), threshold_db=6.0,
                                      require_periodic=False) if h["tetra_channel"] == target]
            self.assertTrue(hits, target)
            self.assertIs(hits[0]["band_edge"], edge, target)
            self.assertIs(hits[0]["air_channel"], target >= 190, target)
        self.assertEqual((channelize.EDGE_LOW, channelize.EDGE_HIGH), (3, 195))

    def test_the_aircraft_channels_are_190_to_199(self):
        """ECC/DEC/(06)05: 384.750-385.000 MHz, the core band and its extension (1.8.0)."""
        import detect as d
        air = [ch for ch in range(200) if d.air_channel(380.0125e6 + ch * 25e3)]
        self.assertEqual(air, list(range(190, 200)))


if __name__ == "__main__":
    unittest.main()
