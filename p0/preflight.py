#!/usr/bin/env python3
"""Offline installation checks. Never opens the SDR, starts a service or sends messages."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import unittest


REQUIRED_TESTS = ("test_detection.py", "test_local_config.py", "test_health.py", "test_notifications.py", "test_phy.py",
                  "test_reliability.py", "test_runtime.py", "test_review_fixes.py")
MIN_TESTS = 70

# --tests-if-changed, for the start of every drive. Until 1.2.8 each start ran the whole suite (32 to 34 s
# on the car computer) before the receiver opened, ready 44 s after power and 76 s after an update -
# re-testing files that had already passed. Now a full pass records a fingerprint of everything the
# tests depend on; a start whose tree has that fingerprint runs SMOKE_TESTS (about 3 s) and the cheap
# configuration checks, and every FULL_EVERY-th such start runs the full suite anyway. Any changed byte,
# library version or machine means the full suite. Counted in starts, not days: the car computer has
# no clock it can trust at boot.
SMOKE_TESTS = (
    "test_detection.DetectionTests.test_saved_field_decisions_replay_exactly",
    "test_phy.RecognitionTests.test_a_strong_burst_is_matched_by_the_standard_rule",
    "test_reach.FullBandTests.test_two_hundred_channels",
    "test_reach.TwoStageTuneTests.test_same_samples_as_the_direct_path",
    "test_coherent.CoherentRuleTests.test_limits_as_chosen",
    "test_coherent.CoherentRuleTests.test_normal_sequence_two_is_the_conjugate_of_sequence_one",
    "test_carrier.StartupGraceTests.test_startup_loss_is_not_a_fault_and_keeps_the_threads",
    "test_health.HealthTests.test_atomic_summary_preserves_previous_on_failed_replace",
)
FULL_EVERY = 20


def state_file():
    """Where the record of the last full pass lives: outside the tree, so a tested copy and the installed
    tree share it (SLOT17_STATE_DIR, else ~/.local/state/slot17)."""
    base = os.environ.get("SLOT17_STATE_DIR") or str(Path.home() / ".local" / "state" / "slot17")
    return Path(base) / "preflight-tested.json"


def fingerprint(root):
    """SHA-256 over every file the tests read (p0/ and tests/, without caches) and the runtime they ran on."""
    import numpy
    import scipy
    h = hashlib.sha256()
    for folder in ("p0", "tests"):
        for path in sorted((root / folder).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix in (".pyc", ".updating"):
                continue
            h.update(path.relative_to(root).as_posix().encode() + b"\0")
            h.update(hashlib.sha256(path.read_bytes()).digest())
    h.update(f"python {platform.python_version()} numpy {numpy.__version__} scipy {scipy.__version__} "
             f"{platform.machine()}".encode())
    return h.hexdigest()


def read_record():
    try:
        record = json.loads(state_file().read_text(encoding="utf-8"))
        return record if isinstance(record, dict) else {}
    except (OSError, ValueError):
        return {}


def write_record(record):
    """Write atomically; a record that cannot be written only means the next start runs the full suite."""
    path = state_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def choose_run(record, tree):
    """'smoke' when this exact tree passed the full suite and fewer than FULL_EVERY - 1 smoke starts followed."""
    if record.get("fingerprint") == tree and int(record.get("smoke_starts", 0)) < FULL_EVERY - 1:
        return "smoke"
    return "full"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true", help="explicit no-hardware mode (also the default)")
    ap.add_argument("--tests", action="store_true", help="run the full offline regression suite")
    ap.add_argument("--tests-if-changed", action="store_true",
                    help="run the full suite only if this tree has not passed it (else a short smoke test); "
                         "used at every start of the detector")
    ap.add_argument('--require-idle', action='store_true', help='reject an active receiver before checks; automatic with --tests')
    args = ap.parse_args()
    testing = args.tests or args.tests_if_changed
    root = Path(__file__).resolve().parents[1]
    errors, notes = [], []
    owner = None
    if testing or args.require_idle:
        from instance import acquire_idle
        try:
            owner = acquire_idle(root)
        except RuntimeError as exc:
            print(json.dumps({'offline_preflight_pass': False, 'errors': [str(exc)],
                              'tests_started': False, 'hardware_tested': False}))
            return 1
    try:
        from version import provenance
        version = provenance()
        for path in (root / "p0").glob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        import math
        sys.path.insert(0, str(root / 'p0'))
        from analyze import config_file
        calibration = json.loads(Path(config_file('calibration.json')).read_text(encoding='utf-8'))
        ppm = float(calibration['median_ppm'])
        if not math.isfinite(ppm) or abs(ppm) > 100:
            errors.append("frequency calibration is non-finite or outside +/-100 ppm")
        downlink = json.loads(Path(config_file('downlink_channels.json')).read_text(encoding='utf-8'))['channels']
        if not isinstance(downlink, list) or any(not isinstance(ch, int) for ch in downlink):
            errors.append("downlink channel inventory is malformed")
        from phy import TRAINING_BITS
        if len(TRAINING_BITS["cub"]) != 30:
            errors.append("CUB training template is damaged")
        free = shutil.disk_usage(root).free
        if free < 5 * (1 << 30):
            notes.append("less than 5 GiB free: binary recordings will be skipped")
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        version = None
    run = None
    if testing:
        tree = None
        record = {}
        try:
            tree = fingerprint(root)
            record = read_record()
        except Exception as exc:          # noqa: BLE001 - an unreadable tree is tested in full
            notes.append(f"could not fingerprint the tree ({type(exc).__name__}): full suite")
        run = "full" if args.tests or tree is None else choose_run(record, tree)
        if run == "smoke":
            sys.path.insert(0, str(root / "tests"))
            suite = unittest.defaultTestLoader.loadTestsFromNames(SMOKE_TESTS)
            if not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful():
                errors.append("smoke tests failed")
            elif not errors:
                write_record(dict(record, smoke_starts=int(record.get("smoke_starts", 0)) + 1))
        else:
            suite = unittest.defaultTestLoader.discover(str(root / "tests"))
            have = {p.name for p in (root / "tests").glob("test_*.py")}
            missing = sorted(set(REQUIRED_TESTS) - have)
            if missing or suite.countTestCases() < MIN_TESTS:
                errors.append(f"regression suite is incomplete: {suite.countTestCases()} tests; missing {missing or 'none'}")
            elif not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful():
                errors.append("regression tests failed")
            elif not errors and tree is not None:
                write_record({"fingerprint": tree, "smoke_starts": 0, "tests": suite.countTestCases()})
    print(json.dumps({"offline_preflight_pass": not errors, "errors": errors, "notes": notes, "tests_run": run,
                      "software": version, "hardware_tested": False, "field_range_tested": False}, indent=2))
    if owner:
        owner.close()
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
