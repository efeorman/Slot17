"""Grade one Airspy smoke run from its saved observations. No detector imports."""
import argparse
import json
import math
from pathlib import Path

TRANSFER = 65_536
EXIT = {"pass": 0, "fail": 1, "inconclusive": 2}


def _ledger(run, errors, failures):
    rows = run["ledger"]
    fs = run["scene"]["sample_rate"]
    limit = run["scene"]["stall_after_samples"]
    end = (limit + TRANSFER - 1) // TRANSFER * TRANSFER
    spans, producer, host = [], [], []
    opens = [r for r in rows if r["event"] == "open"]
    refused_opens = sum(r["event"] == "fail" and r.get("call") == "airspy_open" for r in rows)
    starts = [r for r in rows if r["event"] == "start_rx"]
    stops = [r for r in rows if r["event"] == "stop_rx"]
    stalls = [r for r in rows if r["event"] == "stall"]
    if max(len(opens) + refused_opens, len(starts), len(stops), run.get("board", {}).get("starts", 0),
           run.get("supervisor_launches", 0)) > 1:
        failures.append("second_open_or_start_or_stop")
    if not (len(opens) == len(starts) == len(stops) == 1):
        errors.append("missing_lifecycle_evidence")
    elif opens[0]["rc"] or starts[0]["rc"] or starts[0]["samplerate"] != fs:
        failures.append("receiver_start")
    if run.get("board") != {"boot": run.get("boot"), "starts": 1}:
        errors.append("boot_latch")
    if run.get("supervisor_launches") == 0:
        errors.append("missing_supervisor_launch_record")
    if [r["first_sample"] for r in stalls] != [end]:
        errors.append("missing_or_wrong_stall_record")
    if len({r["pid"] for r in rows}) != 1:
        errors.append("ledger_pid")
    transfers = [r for r in rows if r["event"] == "transfer"]
    drops = [r for r in rows if r["event"] == "drop"]
    if drops:
        failures.append("unexpected_input_loss")
    positions = [r["first_sample"] for r in transfers + drops]
    if sorted(positions) != list(range(0, end, TRANSFER)):
        errors.append("missing_or_duplicate_source_transfer")
    previous = 0
    for i, r in enumerate(transfers):
        first = r["first_sample"]
        if (r["seq"] != i or r["samples"] != TRANSFER or first < previous
                or r["dropped"] != first - previous or r["rc"] != 0):
            errors.append("invalid_transfer_sequence")
            break
        deadline, queued, delivered = (r[k] for k in ("deadline", "queued", "delivered"))
        if not all(map(math.isfinite, (deadline, queued, delivered))) or delivered < queued:
            errors.append("invalid_transfer_clock")
            break
        producer.append(queued - deadline)
        host.append(delivered - queued)
        spans.append(first)
        previous = first + TRANSFER
    if not spans:
        errors.append("no_callback_evidence")
    quarter = TRANSFER / fs / 4
    # The merged plan: a stand-in whose p99 lateness is over a quarter of a transfer "invalidates the performance
    # comparison": its timings don't count as performance figures (timing_qualified false), while what the
    # detector recognised and how the session ended still stand. A transfer more than a whole period late is
    # different: the detector may then have seen a gap, so the run is inconclusive. Until 24 November 2026 any
    # single late transfer made the whole run inconclusive; on the Pi, isolated wake-ups of 3 to 4 ms while the
    # detector's threads were busy did that to runs that were right in every other way.
    ordered = sorted(producer)
    p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))] if ordered else None
    timing = {"quarter_period_s": quarter, "max_producer_late_s": max(producer, default=None),
              "p99_producer_late_s": p99,
              "qualified": p99 is not None and p99 <= quarter and ordered[-1] <= TRANSFER / fs,
              "max_callback_wait_s": max(host, default=None),
              "late_transfers": sum(v > quarter for v in producer), "delivered_transfers": len(spans)}
    if len(stalls) == len(stops) == 1:
        elapsed = stops[0]["t_mono"] - stalls[0]["t_mono"]
        timing["stall_to_stop_s"] = elapsed
        # Unit's 20 s stream timeout, with one poll before detection and up to
        # 8 s waiting for the analysis worker before stop_rx.
        if not 19 <= elapsed <= 30:
            failures.append("stall_stop_outside_deadline")
    if run["pace"] != "realtime":
        errors.append("fast_pace")
    elif ordered and ordered[-1] > TRANSFER / fs:
        errors.append("harness_underrun")
    return spans, timing


def _source_sample(event, offset, spans, fs):
    if event["sample_rate_hz"] != fs or not math.isfinite(offset) or offset < 0:
        raise ValueError("invalid event offset or rate")
    ring_sample = event["window_start_sample"] + round(offset * fs)
    index, remainder = divmod(ring_sample, TRANSFER)
    if not 0 <= index < len(spans):
        raise ValueError("event outside delivered IQ")
    return spans[index] + remainder


def _matches(run, spans):
    labels = run["labels"]
    fs, tolerance = (run["scene"][k] for k in ("sample_rate", "tolerance_samples"))
    if len({lab["id"] for lab in labels}) != len(labels) or {lab["positive"] for lab in labels} != {True, False}:
        raise ValueError("incomplete label set")
    hits, negatives, unexpected = {}, set(), []
    for event in run["events"]:
        if event.get("event_type") != "candidate":
            continue
        confirmed = event.get("confirmed")
        if confirmed is True:
            offset = event["matched_burst"]["start_s"]
        elif confirmed is False and event.get("confirmation_coverage", {}).get("complete"):
            offset = event["first_s"]
        else:
            continue
        at = _source_sample(event, offset, spans, fs)
        channel = event["tetra_channel"]
        close = [lab for lab in labels if abs(lab["first_sample"] - at) <= tolerance]
        label = next((lab for lab in close if lab["channel"] == channel), None)
        if confirmed is False:
            if label and not label["positive"]:
                negatives.add(label["id"])
            continue
        if label and label["positive"] and label["id"] not in hits:
            hits[label["id"]] = {"sample": at, "error_samples": at - label["first_sample"],
                                 "event_id": event["event_id"], "channel": channel}
            continue
        if label:
            why = "duplicate" if label["positive"] else "negative_confirmed"
        elif close:
            why = "wrong_channel"
        elif any(lab["channel"] == channel for lab in labels):
            why = "wrong_onset"
        else:
            why = "unlabelled_confirmation"
        unexpected.append({"kind": why, "channel": channel, "sample": at, "event_id": event["event_id"]})
    missed = [lab["id"] for lab in labels if lab["positive"] and lab["id"] not in hits]
    unchecked = [lab["id"] for lab in labels if not lab["positive"] and lab["id"] not in negatives]
    return {"hits": hits, "misses": missed, "negative_controls_checked": sorted(negatives),
            "negative_controls_unchecked": unchecked, "unexpected": unexpected}


def score(run):
    errors = list(run.get("observation_errors", []))
    failures = []
    result = {"errors": errors, "failures": failures}
    unexpected_exit = run.get("supervisor_outcome") in ("analysis failure", "crash", "receiver unavailable", "clean")
    if unexpected_exit:
        failures.append("supervisor_reported_" + run["supervisor_outcome"].replace(" ", "_"))
    try:
        spans, timing = _ledger(run, errors, failures)
        result["timing"] = timing
        result["timing_qualified"] = bool(timing["qualified"]) and run["pace"] == "realtime"
        matches = _matches(run, spans)
        result["recognition"] = matches
        failures.extend(e["kind"] for e in matches["unexpected"])
        if matches["misses"]:
            failures.append("missing_positive")
        if matches["negative_controls_unchecked"]:
            failures.append("negative_not_examined")
        summary = run["summary"]
        if not summary.get("final") or not summary.get("recorder_drained"):
            errors.append("missing_final_summary_or_events")
        if summary.get("samples_received") != len(spans) * TRANSFER:
            errors.append("ledger_and_ring_disagree")
        if any(summary.get(k) for k in ("dropped_samples", "window_errors", "overflow_windows",
                                        "callback_errors", "confirm_errors")):
            failures.append("detector_errors_or_loss")
        terminal = [r for r in run["events"] if r.get("event_type") == "runtime_failure"]
        result["detector_exit"] = [r.get("failure_category") for r in terminal]
        if (result["detector_exit"] != ["stall"] or terminal[0].get("exit_code") != 75
                or summary.get("failure_category") != "stall" or run["supervisor_outcome"] != "stall"
                or run["supervisor_rc"] != 75):
            failures.append("wrong_exit_reason")
        if not run["heartbeats"]:
            errors.append("missing_heartbeat_evidence")
        elif not any(h.get("ready") and h.get("windows", 0) > 0 and
                   h.get("pid") == run["ledger"][0]["pid"] for h in run["heartbeats"]):
            failures.append("no_ready_analysis")
        if not run.get("supervisor_ready"):
            failures.append("supervisor_never_ready")
    except (ValueError, KeyError, TypeError, IndexError, OverflowError, AttributeError) as exc:
        errors.append(f"incomplete_evidence: {exc}")
    # Preserve independently observed wrong behaviour even on an unqualified host.
    # An incomplete observation cannot certify a regression in recognition.
    status = "fail" if failures else "pass"
    if errors and not (unexpected_exit or "second_open_or_start_or_stop" in failures):
        status = "inconclusive"
    result["status"] = status
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="run directory or run.json")
    args = parser.parse_args()
    path = args.run / "run.json" if args.run.is_dir() else args.run
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("run.json must be an object")
        result = score(data)
    except (OSError, ValueError) as exc:
        result = {"status": "inconclusive", "errors": [str(exc)], "failures": []}
    print(json.dumps(result, indent=2))
    return EXIT[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())

