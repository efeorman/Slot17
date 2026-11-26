"""Self-tests of the scorer against invented observations; no receiver or detector."""
import copy
import unittest

if __package__:
    from .score import TRANSFER, score
else:
    from score import TRANSFER, score

FS = 6_000_000


def observation():
    period = TRANSFER / FS
    rows = [{"event": "open", "rc": 0}, {"event": "start_rx", "rc": 0, "samplerate": FS}]
    for i in range(4):
        deadline = 10 + (i + 1) * period
        rows.append(dict(event="transfer", seq=i, first_sample=i*TRANSFER, samples=TRANSFER,
                         dropped=0, rc=0, deadline=deadline, queued=deadline + 0.0002,
                         delivered=deadline + 0.0005))
    rows.extend([{"event": "stall", "first_sample": 4*TRANSFER, "t_mono": 10 + 5*period},
                 {"event": "stop_rx", "t_mono": 30 + 5*period}])
    for r in rows:
        r["pid"] = 101
    labels = [dict(id="yes", channel=13, first_sample=TRANSFER + 6000, positive=True),
              dict(id="no", channel=71, first_sample=2*TRANSFER + 6000, positive=False)]
    events = [
        dict(event_type="candidate", confirmed=True, tetra_channel=13, event_id="A", window_start_sample=TRANSFER,
             sample_rate_hz=FS, matched_burst={"start_s": .001}),
        dict(event_type="candidate", confirmed=False, tetra_channel=71, event_id="B", window_start_sample=2*TRANSFER,
             sample_rate_hz=FS, first_s=.001, confirmation_coverage={"complete": True}),
        dict(event_type="runtime_failure", failure_category="stall", exit_code=75)]
    return dict(scene={"sample_rate": FS, "stall_after_samples": 4*TRANSFER, "tolerance_samples": 200},
                boot="boot", board={"boot": "boot", "starts": 1}, pace="realtime", labels=labels,
                ledger=rows, events=events, heartbeats=[{"ready": True, "windows": 1, "pid": 101}],
                supervisor_rc=75, supervisor_outcome="stall", supervisor_ready=True,
                summary={"final": True, "recorder_drained": True, "samples_received": 4*TRANSFER,
                         "failure_category": "stall"})


class Oracle(unittest.TestCase):
    def setUp(self):
        self.run = observation()

    def verdict(self, expected, reason=None):
        result = score(self.run)
        self.assertEqual(result["status"], expected, result)
        if reason:
            self.assertIn(reason, result["failures"] + result["errors"])
        return result

    def test_clean(self):
        r = self.verdict("pass")
        self.assertEqual(r["recognition"]["hits"]["yes"]["error_samples"], 0)
        self.assertEqual(r["recognition"]["negative_controls_checked"], ["no"])

    def test_duplicate(self):
        self.run["events"].append(copy.deepcopy(self.run["events"][0]))
        self.verdict("fail", "duplicate")

    def test_missing_positive(self):
        del self.run["events"][0]
        self.verdict("fail", "missing_positive")

    def test_wrong_channel(self):
        self.run["events"][0]["tetra_channel"] += 1
        self.verdict("fail", "wrong_channel")

    def test_wrong_onset(self):
        self.run["events"][0]["window_start_sample"] += 201
        self.verdict("fail", "wrong_onset")

    def test_missing_fault_record(self):
        del self.run["ledger"][-2]
        self.verdict("inconclusive", "missing_or_wrong_stall_record")

    def test_second_open(self):
        self.run["ledger"].insert(1, self.run["ledger"][0].copy())
        self.verdict("fail", "second_open_or_start_or_stop")

    def test_dead_worker_is_not_a_stall_even_when_supervisor_returns_75(self):
        self.run["events"][-1].update(failure_category="analysis failure", exit_code=77)
        self.run["summary"]["failure_category"] = self.run["supervisor_outcome"] = "analysis failure"
        self.verdict("fail", "wrong_exit_reason")

    def test_supervisor_observes_worker_death_before_the_requested_stall(self):
        del self.run["ledger"][-2]
        self.run.update(supervisor_outcome="analysis failure", heartbeats=[], summary={}, events=[])
        self.verdict("fail", "supervisor_reported_analysis_failure")

    def test_second_open_attempt_refused_before_logging_open(self):
        self.run["ledger"].append({"pid": 101, "event": "fail", "call": "airspy_open", "rc": -1000})
        self.verdict("fail", "second_open_or_start_or_stop")

    def test_late_producer(self):
        """Over a quarter of a transfer late, at p99: the timings don't count; what was recognised still does."""
        row = self.run["ledger"][2]
        row["queued"] = row["delivered"] = row["deadline"] + TRANSFER / FS / 4 + .000001
        r = self.verdict("pass")
        self.assertFalse(r["timing_qualified"])

    def many_transfers(self, n=200):
        """The observation with n transfers instead of 4, the labels where they were."""
        rows = [r for r in self.run["ledger"] if r["event"] not in ("transfer", "stall", "stop_rx")]
        period = TRANSFER / FS
        for i in range(n):
            deadline = 10 + (i + 1) * period
            rows.append(dict(event="transfer", seq=i, first_sample=i * TRANSFER, samples=TRANSFER, dropped=0, rc=0,
                             deadline=deadline, queued=deadline + 0.0002, delivered=deadline + 0.0005, pid=101))
        rows += [{"event": "stall", "first_sample": n * TRANSFER, "t_mono": 10 + (n + 1) * period, "pid": 101},
                 {"event": "stop_rx", "t_mono": 30 + (n + 1) * period, "pid": 101}]
        self.run["ledger"] = rows
        self.run["scene"]["stall_after_samples"] = n * TRANSFER
        self.run["summary"]["samples_received"] = n * TRANSFER
        return [r for r in rows if r["event"] == "transfer"]

    def test_an_isolated_late_wake_does_not_disqualify_the_run(self):
        """As on the Pi: two of 280 transfers 3.1 and 3.4 ms late, p99 0.58 ms."""
        rows = self.many_transfers()
        rows[101]["queued"] = rows[101]["delivered"] = rows[101]["deadline"] + 0.0034
        r = self.verdict("pass")
        self.assertEqual(r["timing"]["late_transfers"], 1)
        self.assertTrue(r["timing_qualified"])

    def test_a_host_that_keeps_falling_behind_has_timings_that_dont_count(self):
        rows = self.many_transfers()
        for row in rows[::50]:                                   # 4 of 200: over p99
            row["queued"] = row["delivered"] = row["deadline"] + 0.003
        r = self.verdict("pass")
        self.assertFalse(r["timing_qualified"])

    def test_one_transfer_a_whole_period_late_is_inconclusive(self):
        """Then the detector may have seen a gap the scene doesn't have: nothing it did can be graded."""
        rows = self.many_transfers()
        rows[101]["queued"] = rows[101]["delivered"] = rows[101]["deadline"] + TRANSFER / FS + 0.001
        r = self.verdict("inconclusive", "harness_underrun")
        self.assertFalse(r["timing_qualified"])

    def test_late_consumer_does_not_disqualify_the_producer(self):
        self.run["ledger"][2]["delivered"] += .005
        r = self.verdict("pass")
        self.assertGreater(r["timing"]["max_callback_wait_s"], .005)

    def test_no_credit_for_an_unexamined_negative(self):
        self.run["events"][1]["confirmation_coverage"]["complete"] = False
        self.verdict("fail", "negative_not_examined")

    def test_confirmation_on_negative(self):
        self.run["events"][1].update(confirmed=True, matched_burst={"start_s": .001})
        self.verdict("fail", "negative_confirmed")

    def test_a_missing_transfer_invalidates_the_observer(self):
        del self.run["ledger"][3]
        self.verdict("inconclusive", "missing_or_duplicate_source_transfer")

    def test_ring_indices_translate_through_a_known_gap(self):
        self.run["ledger"][2].update(event="drop", why="queue_full")
        for i, row in enumerate(self.run["ledger"][3:6]):
            row.update(seq=i, dropped=TRANSFER if i == 0 else 0)
        for event in self.run["events"][:2]:
            event["window_start_sample"] -= TRANSFER
        self.run["summary"]["samples_received"] -= TRANSFER
        r = self.verdict("fail", "unexpected_input_loss")
        self.assertEqual(r["recognition"]["hits"]["yes"]["error_samples"], 0)

    def test_no_ready_before_analysis(self):
        self.run["heartbeats"][0]["windows"] = 0
        self.verdict("fail", "no_ready_analysis")

    def test_early_watchdog_exit(self):
        self.run["ledger"][-1]["t_mono"] -= 15
        self.verdict("fail", "stall_stop_outside_deadline")


if __name__ == "__main__":
    unittest.main()

