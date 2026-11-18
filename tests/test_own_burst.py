"""A burst at a moment of its own is checked before the broadband moment that flagged its channel."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
import confirmation  # noqa: E402
import impulse       # noqa: E402
import pipeline      # noqa: E402
import watch         # noqa: E402

FS = 6_000_000
SHARED_S, OWN_S = 0.100, 0.200


def hit(channel, starts, shared=(SHARED_S,), flagged=True):
    out = {"tetra_channel": channel, "frequency_hz": 380e6 + channel * 25e3, "peak_snr_db": 20.0,
           "burst_intervals_s": [[s, s + 0.014] for s in starts], "first_s": starts[0], "last_s": starts[-1],
           "coincident_channels": flagged}
    if flagged:
        out["coincident_burst_starts_s"] = [s for s in starts if s in shared]
    return out


def storm(extra=()):
    """Eight channels lit by one broadband moment; channel 50 also holds a burst of its own."""
    return [hit(ch, [SHARED_S]) for ch in range(10, 90, 10) if ch != 50] + [hit(50, [SHARED_S, OWN_S])] + list(extra)


class Recorder:
    """Stands in for the recognizer: notes what was inspected, in order, and matches nothing."""

    def __init__(self, stop_after=None):
        self.calls = []
        self.stop_after = stop_after

    def confirm(self, iq, fs, centre_hz, h, selected_index=None):
        start = confirmation.burst_pairs(h)[selected_index][0]
        self.calls.append((h["tetra_channel"], start))
        h["confirmed"] = False
        h["confirm"] = {"seconds": 0.0, "phy": []}
        return np.zeros(4, np.complex64), {"segments": [{"offset": 0, "count": 4}]}

    def clock(self):
        return 1e9 if self.stop_after is not None and len(self.calls) >= self.stop_after else 0.0


def run(hits, stop_after=None):
    recorder = Recorder(stop_after)
    ctx = SimpleNamespace(stats=watch.Stats(), fs=FS, centre=382.5, confirm=True, last_confirmed_attempt={})
    with patch.object(confirmation, "confirm", recorder.confirm), patch.object(pipeline.time, "monotonic", recorder.clock):
        pipeline.evaluate_candidates(np.zeros(FS // 2, np.complex64), hits, ctx, deadline=1.0, window=1)
    return recorder.calls, ctx.stats


class OwnBurstOrder(unittest.TestCase):
    def test_the_own_burst_is_inspected_before_any_broadband_moment(self):
        calls, _ = run(storm())
        self.assertEqual(calls[0], (50, OWN_S))
        self.assertEqual(len(calls), 9, "then every region once, with time to spare")
        self.assertEqual(sorted(calls[1:]), sorted((ch, SHARED_S) for ch in range(10, 90, 10)))

    def test_an_unflagged_candidate_still_goes_first(self):
        calls, _ = run(storm(extra=[hit(120, [0.3], flagged=False)]))
        self.assertEqual(calls[:2], [(120, 0.3), (50, OWN_S)])

    def test_with_time_for_one_region_the_own_burst_gets_it(self):
        hits = storm()
        calls, stats = run(hits, stop_after=1)
        self.assertEqual(calls, [(50, OWN_S)])
        self.assertEqual(stats.unchecked, 7, "the broadband moment on the other seven channels")
        self.assertEqual(stats.unchecked_own_burst, 0)
        self.assertFalse(any(h["confirmation_coverage"].get("own_burst_unchecked") for h in hits))

    def test_an_own_burst_nobody_reached_is_counted_and_marked(self):
        hits = storm()
        calls, stats = run(hits, stop_after=0)
        self.assertEqual(calls, [])
        self.assertEqual(stats.unchecked, 8)
        self.assertEqual(stats.unchecked_own_burst, 1)
        marked = [h["tetra_channel"] for h in hits if h["confirmation_coverage"].get("own_burst_unchecked")]
        self.assertEqual(marked, [50])
        self.assertIsNone(hits[-1]["confirmed"], "unchecked stays unknown, never a rejection")

    def test_a_match_on_the_own_burst_needs_no_further_region(self):
        recorder = Recorder()

        def matching(iq, fs, centre_hz, h, selected_index=None):
            out = recorder.confirm(iq, fs, centre_hz, h, selected_index)
            h["confirmed"] = h["tetra_channel"] == 50
            return out
        hits = storm()
        ctx = SimpleNamespace(stats=watch.Stats(), fs=FS, centre=382.5, confirm=True, last_confirmed_attempt={})
        with patch.object(confirmation, "confirm", matching):
            pipeline.evaluate_candidates(np.zeros(FS // 2, np.complex64), hits, ctx, deadline=float("inf"), window=1)
        self.assertEqual([c for c in recorder.calls if c[0] == 50], [(50, OWN_S)])
        self.assertTrue(hits[-1]["confirmation_coverage"]["complete"])


class GeneratedSignal(unittest.TestCase):
    def test_a_real_burst_beside_a_broadband_spike_is_inspected_first_and_matched(self):
        """Generated IQ through the real detector and recognizer: a spike on every channel, one CUB."""
        import time

        import channelize
        import detect
        import test_detection
        iq, fs = test_detection.wideband(channels=(104,), snr=20, seconds=.3)
        rng = np.random.default_rng(1)
        first, count = int(0.25 * fs), int(0.006 * fs)
        iq = iq.copy()
        iq[first:first + count] += ((rng.normal(size=count) + 1j * rng.normal(size=count)) * np.abs(iq).std() * 8).astype(np.complex64)
        hits = detect.detect(channelize.channelise(iq, fs, 382.5e6), threshold_db=6, require_periodic=False)
        target = next(h for h in hits if h["tetra_channel"] == 104)
        self.assertGreater(sum(bool(h["coincident_channels"]) for h in hits), 100, "the spike lights the band")
        self.assertTrue(target["coincident_channels"])
        own = [a for a, _ in target["burst_intervals_s"] if a not in target["coincident_burst_starts_s"]]
        self.assertEqual(len(own), 1)
        inspected = []
        real = confirmation.confirm

        def spy(iq_, fs_, centre, h, selected_index=None):
            inspected.append((h["tetra_channel"], confirmation.burst_pairs(h)[selected_index][0]))
            return real(iq_, fs_, centre, h, selected_index=selected_index)
        ctx = watch.Ctx(stats=watch.Stats(), fs=fs, centre=382.5, dl=None, threshold_db=6,
                        history=detect.BurstHistory(), trend=watch.TrendTracker(), t_start=time.time(),
                        events=[], verbose=False, confirm=True, save_snippets=False)
        with patch.object(confirmation, "confirm", spy):
            pipeline.process_window(iq, None, len(iq), len(iq), ctx)
        self.assertEqual(inspected[0], (104, own[0]))
        self.assertEqual(ctx.stats.confirmed, 1)
        self.assertEqual(ctx.stats.weak_matches, 0, "a 20 dB burst is matched by the standard rule")
        self.assertEqual(ctx.stats.whole_band_windows, 1, "and the spike is counted as a whole-band window")
        self.assertEqual(ctx.stats.unchecked_own_burst, 0)


class SeveralThreads(unittest.TestCase):
    """Examining candidates on several threads must decide exactly as one thread does."""

    def storm_window(self):
        import test_detection
        iq, fs = test_detection.wideband(channels=(20, 104, 150), snr=16, seconds=.3)
        rng = np.random.default_rng(4)
        first, count = int(0.25 * fs), int(0.006 * fs)
        iq = iq.copy()
        iq[first:first + count] += ((rng.normal(size=count) + 1j * rng.normal(size=count)) * np.abs(iq).std() * 8).astype(np.complex64)
        return iq, fs

    def decide(self, iq, fs, workers, dropped=0):
        import channelize
        import detect
        hits = detect.detect(channelize.channelise(iq, fs, 382.5e6), threshold_db=6, require_periodic=False)
        stats = watch.Stats()
        stats.dropped = dropped
        ctx = SimpleNamespace(stats=stats, fs=fs, centre=382.5, confirm=True, last_confirmed_attempt={}, confirm_workers=workers)
        pipeline.evaluate_candidates(iq, hits, ctx, deadline=float("inf"), window=1)
        decisions = sorted((h["tetra_channel"], h["confirmed"], h["confirmation_coverage"]["regions_tested"],
                            (h.get("matched_burst") or {}).get("rule")) for h in hits)
        return decisions, stats

    def test_three_threads_decide_as_one_does(self):
        iq, fs = self.storm_window()
        one, stats_one = self.decide(iq, fs, 1)
        three, stats_three = self.decide(iq, fs, 3)
        self.assertEqual(one, three)
        self.assertGreater(len(one), 100, "a whole-band window, so the threads really share work")
        self.assertEqual(sorted(c for c, ok, _, _ in three if ok), [20, 104, 150])
        self.assertEqual((stats_one.confirm_attempts, stats_one.confirm_workers), (stats_three.confirm_attempts, 1))
        self.assertEqual(stats_three.confirm_workers, 3)

    def test_after_dropped_samples_the_work_goes_back_to_one_thread(self):
        iq, fs = self.storm_window()
        decisions, stats = self.decide(iq, fs, 3, dropped=65536)
        self.assertEqual(stats.confirm_workers, 1)
        self.assertEqual(sorted(c for c, ok, _, _ in decisions if ok), [20, 104, 150])

    def test_an_error_in_one_candidate_costs_only_that_candidate(self):
        iq, fs = self.storm_window()
        real = confirmation.confirm

        def faulty(iq_, fs_, centre, h, selected_index=None):
            if h["tetra_channel"] == 104:
                raise RuntimeError("boom")
            return real(iq_, fs_, centre, h, selected_index=selected_index)
        with patch.object(confirmation, "confirm", faulty):
            decisions, stats = self.decide(iq, fs, 3)
        self.assertEqual(sorted(c for c, ok, _, _ in decisions if ok), [20, 150])
        self.assertEqual(stats.confirm_errors, 1)


class ThreadCount(unittest.TestCase):
    """Any computer must be able to run this: one core gets one thread, more cores get more."""

    def chosen(self, core_count, option=0, env=None, dropped=0):
        stats = watch.Stats()
        stats.dropped = dropped
        ctx = SimpleNamespace(stats=stats, confirm_workers=option)
        variables = {} if env is None else {"SLOT17_CHECK_THREADS": env}
        with patch.object(pipeline, "cores", return_value=core_count), patch.dict(pipeline.os.environ, variables, clear=False):
            if env is None:
                pipeline.os.environ.pop("SLOT17_CHECK_THREADS", None)
            return pipeline.confirm_workers(ctx)

    def test_the_number_follows_the_cores_and_leaves_one_free(self):
        self.assertEqual([self.chosen(n) for n in (1, 2, 3, 4, 8, 64)], [1, 1, 2, 3, 4, 4])

    def test_the_owner_can_set_it_and_is_not_capped_by_the_automatic_limit(self):
        self.assertEqual(self.chosen(4, option=1), 1)
        self.assertEqual(self.chosen(16, option=8), 8)
        self.assertEqual(self.chosen(1, option=2), 2, "their choice, even on one core")
        self.assertEqual(self.chosen(4, env="2"), 2)
        self.assertEqual(self.chosen(4, option=1, env="6"), 1, "the command line wins over the environment")
        self.assertEqual(self.chosen(4, option=500), pipeline.MAX_WORKERS)

    def test_a_meaningless_setting_falls_back_to_automatic(self):
        for bad in ("", "0", "-3", "many", "2.5", "999"):
            self.assertEqual(self.chosen(4, env=bad), 3, bad)

    def test_dropped_samples_always_mean_one_thread(self):
        self.assertEqual(self.chosen(8, option=6, dropped=1), 1)

    def at(self, stats, when, option=6):
        ctx = SimpleNamespace(stats=stats, confirm_workers=option)
        with patch.object(pipeline.time, "monotonic", return_value=when):
            return pipeline.confirm_workers(ctx)

    def test_the_threads_come_back_after_a_spell_without_loss(self):
        stats = watch.Stats()
        self.assertEqual(self.at(stats, 1000.0), 6, "nothing lost yet")
        stats.dropped = 262144                                  # lost while another USB device was starting
        self.assertEqual(self.at(stats, 1054.0), 1)
        self.assertEqual(self.at(stats, 1054.0 + pipeline.DROP_HOLD_S - 1), 1)
        self.assertEqual(self.at(stats, 1054.0 + pipeline.DROP_HOLD_S + 1), 6)
        self.assertEqual(self.at(stats, 9000.0), 6, "and stay while nothing more is lost")

    def test_further_loss_during_the_wait_starts_it_again(self):
        stats = watch.Stats()
        stats.dropped = 655360
        self.assertEqual(self.at(stats, 12.0), 1)
        stats.dropped = 917504
        self.assertEqual(self.at(stats, 200.0), 1)
        self.assertEqual(self.at(stats, 12.0 + pipeline.DROP_HOLD_S + 1), 1, "counted from the last loss, not the first")
        self.assertEqual(self.at(stats, 200.0 + pipeline.DROP_HOLD_S + 1), 6)

    def test_loss_after_the_threads_came_back_ends_them_for_the_session(self):
        stats = watch.Stats()
        stats.dropped = 65536
        self.assertEqual(self.at(stats, 10.0), 1)
        self.assertEqual(self.at(stats, 10.0 + pipeline.DROP_HOLD_S + 1), 6)
        stats.dropped += 65536                                  # now the threads themselves are suspect
        self.assertEqual(self.at(stats, 2000.0), 1)
        self.assertEqual(self.at(stats, 2000.0 + 100 * pipeline.DROP_HOLD_S), 1)

    def test_one_thread_by_choice_is_never_raised_by_the_wait_ending(self):
        stats = watch.Stats()
        stats.dropped = 65536
        self.assertEqual(self.at(stats, 10.0, option=1), 1)
        self.assertEqual(self.at(stats, 10.0 + pipeline.DROP_HOLD_S + 1, option=1), 1)

    def test_this_computer_reports_at_least_one_core(self):
        self.assertGreaterEqual(pipeline.cores(), 1)
        self.assertGreaterEqual(pipeline.default_workers(), 1)


class BurstSelection(unittest.TestCase):
    def test_own_bursts_are_kept_when_there_are_too_many_to_inspect(self):
        starts = [round(0.05 * k, 3) for k in range(1, 11)]
        shared = starts[:8]
        h = hit(50, starts, shared=shared)
        chosen = [a for a, _ in confirmation.burst_pairs(h)]
        self.assertEqual(len(chosen), confirmation.MAX_BURSTS)
        self.assertTrue(set(starts[8:]) <= set(chosen), chosen)
        self.assertEqual(chosen, sorted(chosen))
        self.assertEqual(len(confirmation.intervals(h, FS, FS)), confirmation.MAX_BURSTS)

    def test_selection_is_unchanged_without_a_broadband_moment(self):
        starts = [round(0.05 * k, 3) for k in range(1, 11)]
        h = hit(50, starts, flagged=False)
        picks = np.unique(np.linspace(0, 9, confirmation.MAX_BURSTS, dtype=int))
        self.assertEqual([a for a, _ in confirmation.burst_pairs(h)], [starts[k] for k in picks])


class BurstClusters(unittest.TestCase):
    def test_only_the_shared_moment_is_reported_per_channel(self):
        bursts = {ch: [0.30] for ch in range(20, 73)}
        bursts[40] = [0.30, 0.90]
        clustered = impulse.coincident_bursts(bursts)
        self.assertEqual(clustered[40], [0.30])
        self.assertEqual(impulse.flag_coincident(bursts), set(range(20, 73)), "half of channel 40 is shared: still flagged")

    def test_separate_radios_have_no_shared_moment(self):
        bursts = {100: [0.0, 0.05667, 0.11333], 40: [0.021, 0.078, 0.134]}
        self.assertEqual(impulse.coincident_bursts(bursts), {100: [], 40: []})
        self.assertEqual(impulse.flag_coincident(bursts), set())


if __name__ == "__main__":
    unittest.main()
