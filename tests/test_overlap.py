"""Overlapping windows: every burst is seen whole, by exactly one window, wherever it falls."""
from collections import deque
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np
from scipy.signal import resample_poly

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
import channelize  # noqa: E402
import pipeline    # noqa: E402
import tetra_gen   # noqa: E402
import watch       # noqa: E402

FS = 6_000_000
SECONDS = 1.6
HOP, LEAD = watch.WINDOW_S, watch.WINDOW_OVERLAP_S
# One control burst per channel, at chosen moments. Ownership boundaries fall at k x 0.25 + 0.0015 s;
# the old 0.5 s windows had their edges at k x 0.5 s.
BURSTS = {20: 0.0500, 40: 0.2480, 60: 0.2505, 80: 0.4975, 100: 0.7440, 120: 0.7540, 140: 0.9985,
          160: 1.2510, 180: 1.4970}
EDGE_CHANNEL = 80        # 0.4975 s: crosses the old 0.5 s window edge


def recording():
    n = int(FS * SECONDS)
    rng = np.random.default_rng(21)
    iq = ((rng.standard_normal(n, dtype=np.float32) + 1j * rng.standard_normal(n, dtype=np.float32)) * 30).astype(np.complex64)
    cal = channelize.load_calibration()
    for ch, start in BURSTS.items():
        tx = tetra_gen.make_transmission(SECONDS, 75000, kind="cub", frames=[0], seed=ch, first_frame_offset_s=start)
        tx = resample_poly(tx, 80, 1)[:n]
        offset = (380.0125e6 + ch * 25000) * (1 + cal * 1e-6) - 382.5e6
        tx *= np.exp(2j * np.pi * offset * np.arange(n) / FS).astype(np.complex64)
        iq += tx * np.sqrt(1800 * 10 ** (16 / 10) * tetra_gen.snr_bandwidth_hz() / FS)
    return iq


def context(overlap):
    return watch.Ctx(stats=watch.Stats(), fs=FS, centre=382.5, dl=None, threshold_db=6.0, history=None,
                     recorder=None, save_power=False, save_snippets=False, confirm=True, last_confirmed_attempt={},
                     trend=watch.TrendTracker(), notifier=None, events=deque(maxlen=10_000), verbose=False,
                     t_start=0.0, window_start_utc=0.0, hop_s=HOP if overlap else None,
                     overlap_s=LEAD if overlap else 0.0, confirm_workers=1)


def windows(overlap):
    """(start sample, length, own) exactly as watch._analysis_worker cuts them."""
    hop = int((HOP if overlap else 0.5) * FS)
    lead = int(LEAD * FS) if overlap else 0
    read, first, out = lead, True, []
    while read + hop <= int(FS * SECONDS):
        own = ((0.0 if first else watch.OWN_MARGIN_S), hop / FS + watch.OWN_MARGIN_S) if lead else None
        out.append((read - lead, hop + lead, own))
        first, read = False, read + hop
    return out


def recognised(iq, overlap):
    ctx = context(overlap)
    for start, length, own in windows(overlap):
        ctx.own_s, ctx.window_start_utc = own, start / FS
        pipeline.process_window(iq[start:start + length], None, start + length, start + length, ctx, t_abs_s=start / FS)
    matches = [e for e in ctx.events if e.get("event_type") == "candidate" and e.get("confirmed")]
    return {(e["tetra_channel"], round(e["window_start_utc"] + e["matched_burst"]["start_s"], 4)) for e in matches}, matches


class OverlappingWindows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.iq = recording()
        cls.new, cls.new_events = recognised(cls.iq, overlap=True)
        cls.old, _ = recognised(cls.iq, overlap=False)

    def test_every_burst_is_recognised_exactly_once_at_its_time(self):
        by_channel = {}
        for ch, start in self.new:
            by_channel.setdefault(ch, []).append(start)
        self.assertEqual(sorted(by_channel), sorted(BURSTS), "every burst, and nothing else")
        for ch, starts in by_channel.items():
            self.assertEqual(len(starts), 1, f"channel {ch} recognised {len(starts)} times")
            self.assertAlmostEqual(starts[0], BURSTS[ch], delta=0.004, msg=f"channel {ch}")
        self.assertEqual(len(self.new_events), len(BURSTS))

    def test_the_old_windows_lost_the_burst_on_their_edge(self):
        self.assertNotIn(EDGE_CHANNEL, {ch for ch, _ in self.old}, "a burst across the 0.5 s edge is cut in two")
        self.assertIn(EDGE_CHANNEL, {ch for ch, _ in self.new})

    def test_the_windows_tile_the_session_without_gaps(self):
        cuts = windows(overlap=True)
        owned = [(s / FS + own[0], s / FS + own[1]) for s, _, own in cuts]
        self.assertEqual(owned[0][0], 0.0)
        for (_, end), (begin, _) in zip(owned, owned[1:]):
            self.assertAlmostEqual(end, begin, places=9)
        for start, length, own in cuts:
            self.assertGreaterEqual(length / FS - own[1], 0.0587 - 1e-9, "the longest envelope still fits whole")


class WorkerCutsTheStream(unittest.TestCase):
    def test_the_worker_hands_out_overlapping_windows_and_their_ownership(self):
        fs, want, lead = 1000, 250, 64
        ring = watch.Ring(20000)
        ctx = watch.Ctx(stats=watch.Stats(), fs=fs, want_samples=want, overlap_s=lead / fs, t_start=time.time(),
                        history=None, verbose=False)
        seen, done = [], threading.Event()

        def process(iq, ring_, read_end, w_now, ctx_, t_abs_s=0.0):
            seen.append((int(round(iq[0].real)), len(iq), read_end, round(t_abs_s, 3), ctx_.own_s))
            if len(seen) == 3:
                done.set()
        watch._stop.clear()
        try:
            with patch.object(watch, "process_window", side_effect=process):
                worker = threading.Thread(target=watch.analysis_worker, args=(ring, ctx))
                worker.start()
                time.sleep(0.05)
                values = np.arange(1000, dtype=np.int16)
                ring.push(np.repeat(values, 2))          # I = Q = the sample's own index
                self.assertTrue(done.wait(3))
        finally:
            watch._stop.set()
            worker.join(3)
            watch._stop.clear()
        self.assertEqual(seen[0], (0, 314, 314, 0.0, (0.0, 0.25 + watch.OWN_MARGIN_S)))
        self.assertEqual(seen[1], (250, 314, 564, 0.25, (watch.OWN_MARGIN_S, 0.25 + watch.OWN_MARGIN_S)))
        self.assertEqual(seen[2][:4], (500, 314, 814, 0.5))


if __name__ == "__main__":
    unittest.main()
