"""A channel recognised in the last minute is checked first; interference candidates get no extra time."""
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
import channelize    # noqa: E402
import confirmation  # noqa: E402
import pipeline      # noqa: E402
import watch         # noqa: E402

FS = 6_000_000


def hit(channel, flagged, start=0.1):
    out = {"tetra_channel": channel, "frequency_hz": 380e6 + channel * 25e3, "peak_snr_db": 20.0,
           "burst_intervals_s": [[start, start + 0.014]], "first_s": start, "last_s": start,
           "coincident_channels": flagged}
    if flagged:
        out["coincident_burst_starts_s"] = [start]
    return out


class Clock:
    """Each check takes one second and recognises nothing."""

    def __init__(self):
        self.calls = []

    def confirm(self, iq, fs, centre_hz, h, selected_index=None):
        self.calls.append(h["tetra_channel"])
        h["confirmed"] = False
        h["confirm"] = {"seconds": 1.0, "phy": []}
        return np.zeros(4, np.complex64), {"segments": [{"offset": 0, "count": 4}]}

    def now(self):
        return float(len(self.calls))


class Budget(unittest.TestCase):
    def test_flagged_candidates_get_only_what_the_window_budget_leaves(self):
        hits = [hit(100, False), hit(110, False), hit(120, False)] + [hit(ch, True) for ch in range(10, 60, 10)]
        clock = Clock()
        ctx = SimpleNamespace(stats=watch.Stats(), fs=FS, centre=382.5, confirm=True, last_confirmed_attempt={},
                              confirm_workers=1)
        with patch.object(confirmation, "confirm", clock.confirm), patch.object(pipeline.time, "monotonic", clock.now):
            pipeline.evaluate_candidates(np.zeros(FS // 2, np.complex64), hits, ctx, deadline=2.0, window=1)
        self.assertEqual(clock.calls, [100, 110], "unflagged first; nothing is checked after the deadline")
        flagged = [h for h in hits if h["coincident_channels"]]
        self.assertTrue(all(h["confirmed"] is None for h in flagged))
        self.assertTrue(all(h["confirm"]["skipped"].startswith("broadband impulse: deprioritised") for h in flagged))

    def test_the_catch_up_setting_is_gone(self):
        self.assertFalse(hasattr(pipeline, "impulse_catchup"))
        self.assertFalse(hasattr(watch.Stats(), "catchup_checks"))


class Order(unittest.TestCase):
    """The order process_window gives evaluate_candidates."""

    def order(self, hits, t_abs_s, last_match):
        seen = {}

        def capture(iq, hits, ctx, deadline, window, on_final=None):
            seen["order"] = [h["tetra_channel"] for h in hits]
            return {}
        ctx = watch.Ctx(stats=watch.Stats(), fs=1000, centre=382.5, dl=None, threshold_db=6,
                        trend=watch.TrendTracker(), t_start=time.time(), events=[], verbose=False, confirm=True,
                        save_snippets=False, hop_s=0.25, confirm_workers=1)
        ctx.last_match_s = dict(last_match)
        p = np.ones((len(hits), 100), np.float32)
        ch = channelize.Channelised(p, np.arange(len(hits)) * 25e3 + 380e6, np.arange(len(hits)), .001, 382.5e6, 1000)
        with patch.object(pipeline, "channelise", return_value=ch), \
             patch.object(pipeline, "detect", return_value=[dict(h) for h in hits]), \
             patch.object(pipeline, "evaluate_candidates", capture):
            pipeline.process_window(np.ones(250, np.complex64), None, 250, 250, ctx, t_abs_s=t_abs_s)
        return seen["order"]

    def test_a_channel_recognised_in_the_last_minute_is_checked_first(self):
        hits = [hit(10, False), hit(20, False), hit(119, True)]
        hits[0]["peak_snr_db"] = 40.0
        self.assertEqual(self.order(hits, 100.0, {119: 99.0})[0], 119)
        self.assertEqual(self.order(hits, 100.0, {119: 30.0})[-1], 119, "a match over a minute ago gives no priority")


if __name__ == "__main__":
    unittest.main()
