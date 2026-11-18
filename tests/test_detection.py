import hashlib
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

import numpy as np
from scipy.signal import resample_poly

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
import channelize
import confirmation
import detect
import pipeline
import tetra_gen
import watch


def wideband(channels=(104,), snr=16, seed=9, seconds=.3):
    fs = 6_000_000
    n = int(fs * seconds)
    rng = np.random.default_rng(seed)
    iq = ((rng.standard_normal(n, dtype=np.float32) + 1j * rng.standard_normal(n, dtype=np.float32)) * 30).astype(np.complex64)
    cal = channelize.load_calibration()
    for ch in channels:
        tx = tetra_gen.make_transmission(seconds, 75000, kind="cub", frames=[0], seed=seed+ch,
                                         first_frame_offset_s=.1)
        tx = resample_poly(tx, 80, 1)[:n]
        target = 380.0125e6 + ch * 25000
        offset = target * (1 + cal * 1e-6) - 382.5e6
        tx *= np.exp(2j * np.pi * offset * np.arange(n) / fs).astype(np.complex64)
        iq += tx * np.sqrt(1800 * 10 ** (snr / 10) * tetra_gen.snr_bandwidth_hz() / fs)
    return iq, fs


class DetectionTests(unittest.TestCase):
    def test_confirmation_round_robin_and_replay_coverage(self):
        hits = [{"tetra_channel": ch, "frequency_hz": 381e6,
                 "burst_intervals_s": [[.1,.106],[.2,.206]]} for ch in (1, 2, 3)]
        ctx = watch.Ctx(stats=watch.Stats(), fs=1000, centre=382.5, confirm=True,
                        last_confirmed_attempt={})
        order = []
        def confirm(iq, fs, centre, h, selected_index=None):
            order.append((h['tetra_channel'], selected_index))
            h.update(confirmed=False, confirm={"seconds": .001, "phy": []})
            return np.zeros(3, np.complex64), {"sample_rate_hz": 75000,
                                             "segments": [{"offset": 0,"count": 3}]}
        with patch.object(confirmation, 'confirm', side_effect=confirm):
            result = pipeline.evaluate_candidates(np.zeros(1000), hits, ctx, time.monotonic()+10, 1)
        # Every candidate gets its first region before any gets a second. Within one round the
        # candidates are examined side by side, so only the order of the rounds is fixed.
        self.assertEqual(sorted(order[:3]), [(1,0),(2,0),(3,0)])
        self.assertEqual(sorted(order[3:]), [(1,1),(2,1),(3,1)])
        self.assertEqual(result[id(hits[0])][1]['segments'][1]['offset'], 3)
        self.assertTrue(hits[0]['confirmation_coverage']['complete'])

    def test_uninspectable_envelope_is_not_reported_as_complete(self):
        """An envelope longer than four slots yields no region to inspect.

        `null` with `complete: true` would read as a finished decision, which is
        exactly the misreading the evidence rule in CONTRIBUTING.md exists to prevent.
        """
        long_hit = {"tetra_channel": 7, "frequency_hz": 381e6,
                    "burst_intervals_s": [[.1, .3]]}          # 200 ms, continuous
        short_hit = {"tetra_channel": 8, "frequency_hz": 381e6,
                     "burst_intervals_s": [[.1, .114]]}       # one normal slot
        ctx = watch.Ctx(stats=watch.Stats(), fs=1000, centre=382.5, confirm=True,
                        last_confirmed_attempt={})
        self.assertEqual(confirmation.intervals(long_hit, ctx.fs, 1000), [])

        def confirm(iq, fs, centre, h, selected_index=None):
            h.update(confirmed=False, confirm={"seconds": .001, "phy": []})
            return np.zeros(3, np.complex64), {"sample_rate_hz": 75000,
                                               "segments": [{"offset": 0, "count": 3}]}

        with patch.object(confirmation, 'confirm', side_effect=confirm):
            pipeline.evaluate_candidates(np.zeros(1000), [long_hit, short_hit], ctx,
                                         time.monotonic() + 10, 1)

        coverage = long_hit['confirmation_coverage']
        self.assertIsNone(long_hit['confirmed'])
        self.assertEqual(coverage['regions_available'], 0)
        self.assertFalse(coverage['complete'], "zero of zero regions is not a decision")
        self.assertEqual(long_hit['confirm']['skipped'], "no burst region within four slots")
        self.assertEqual(ctx.stats.unconfirmable, 1)
        # The inspectable candidate beside it still completes normally.
        self.assertTrue(short_hit['confirmation_coverage']['complete'])
        self.assertIs(short_hit['confirmed'], False)
        self.assertEqual(ctx.stats.unchecked, 1)

    def test_saved_field_decisions_replay_exactly(self):
        root = Path(__file__).parent / "fixtures/field"
        if not (root / "manifest.json").exists():
            # Real recordings of a live network are kept out of the public repository; the
            # generated bursts in the other tests cover recognition there.
            self.skipTest("the field recordings are not part of this copy")
        rows = json.loads((root / "manifest.json").read_text())["fixtures"]
        self.assertEqual(len(rows), 21)
        for row in rows:
            path = root / (row["fixture_name"] + ".c64")
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), row["sha256"])
            metadata = json.loads(path.with_suffix(".json").read_text())
            self.assertEqual(confirmation.replay(np.fromfile(path, dtype="<c8"), metadata), row["accepted"], path.name)

    def test_single_cub_survives_first_stage_and_confirmation(self):
        for snr in (10, 16):
            iq, fs = wideband(snr=snr)
            channels = channelize.channelise(iq, fs, 382.5e6)
            hits = detect.detect(channels, threshold_db=6, require_periodic=False)
            target = next((h for h in hits if h["tetra_channel"] == 104), None)
            self.assertIsNotNone(target, (snr, hits))
            samples, meta = confirmation.confirm(iq, fs, 382.5e6, target)
            self.assertTrue(target["confirmed"], (snr, target["confirm"]))
            self.assertTrue(confirmation.replay(samples, meta))
            self.assertIsNone(target["approx_range_m"])

    def test_frequency_correction_moves_power_to_physical_channel(self):
        fs = 6_000_000
        n = 1920 * 30
        # A 12.5 kHz calibration shift makes the otherwise marginal effect clear.
        offset = 112500 + 12500
        iq = np.exp(2j * np.pi * offset * np.arange(n) / fs).astype(np.complex64)
        original = iq.copy()
        with patch.object(channelize, "load_calibration", return_value=12500 / 382.5):
            corrected = channelize.channelise(iq, fs, 382.5e6)
            raw = channelize.channelise(iq, fs, 382.5e6, apply_cal=False)
        index = corrected.row_for(104)
        self.assertGreater(corrected.power[index].mean(), raw.power[index].mean() * 1.4)
        np.testing.assert_array_equal(iq, original)

    def test_coincident_channels_are_not_hard_rejected(self):
        iq, fs = wideband(channels=(20, 40, 60, 80), snr=25)
        ch = channelize.channelise(iq, fs, 382.5e6)
        hits = detect.detect(ch, threshold_db=6, require_periodic=False)
        accepted = []
        for h in hits:
            if h['tetra_channel'] in (20, 40, 60, 80):
                confirmation.confirm(iq, fs, 382.5e6, h)
                if h['confirmed']:
                    accepted.append(h['tetra_channel'])
        self.assertEqual(sorted(accepted), [20, 40, 60, 80])

    def test_pipeline_completes_and_logs_unchecked_honestly(self):
        iq, fs = wideband()
        ctx = watch.Ctx(stats=watch.Stats(), fs=fs, centre=382.5, dl=None, threshold_db=6,
                        history=detect.BurstHistory(), trend=watch.TrendTracker(), t_start=time.time(),
                        events=[], verbose=False, confirm=True, save_snippets=False)
        with patch.object(pipeline, "MAX_CONFIRM_SECONDS", 0):
            pipeline.process_window(iq, None, len(iq), len(iq), ctx)
        self.assertEqual(ctx.stats.windows, 1)
        self.assertGreater(ctx.stats.unchecked, 0)
        self.assertIsNone(ctx.events[0]['confirmed'])
        self.assertIsNotNone(ctx.stats.last_success_utc)


if __name__ == "__main__":
    unittest.main()
