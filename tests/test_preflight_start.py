"""1.2.8: the start-of-drive check runs the full suite only for a tree that has not passed it."""
import os
import sys
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "p0"))
sys.path.insert(0, str(ROOT / "tests"))
import preflight


class FingerprintTests(unittest.TestCase):
    def tree(self, base):
        for folder in ("p0", "tests"):
            (base / folder / "__pycache__").mkdir(parents=True)
            (base / folder / "a.py").write_text("x = 1\n")
        return base

    def test_any_changed_byte_changes_it_and_caches_do_not(self):
        with tempfile.TemporaryDirectory() as d:
            root = self.tree(Path(d))
            first = preflight.fingerprint(root)
            self.assertEqual(first, preflight.fingerprint(root))
            (root / "p0" / "__pycache__" / "a.cpython.pyc").write_bytes(b"cache")
            self.assertEqual(first, preflight.fingerprint(root))
            (root / "tests" / "a.py").write_text("x = 2\n")
            second = preflight.fingerprint(root)
            self.assertNotEqual(first, second)
            (root / "p0" / "new.json").write_text("{}")
            self.assertNotEqual(second, preflight.fingerprint(root))


class ChoiceTests(unittest.TestCase):
    def test_full_unless_this_tree_passed_and_not_yet_due(self):
        self.assertEqual(preflight.choose_run({}, "abc"), "full")
        self.assertEqual(preflight.choose_run({"fingerprint": "xyz", "smoke_starts": 0}, "abc"), "full")
        self.assertEqual(preflight.choose_run({"fingerprint": "abc", "smoke_starts": 0}, "abc"), "smoke")
        self.assertEqual(preflight.choose_run({"fingerprint": "abc", "smoke_starts": preflight.FULL_EVERY - 2}, "abc"), "smoke")
        self.assertEqual(preflight.choose_run({"fingerprint": "abc", "smoke_starts": preflight.FULL_EVERY - 1}, "abc"), "full")

    def test_record_round_trip_in_its_own_folder(self):
        saved = os.environ.get("SLOT17_STATE_DIR")
        with tempfile.TemporaryDirectory() as d:
            os.environ["SLOT17_STATE_DIR"] = d
            try:
                self.assertEqual(preflight.read_record(), {})
                self.assertTrue(preflight.write_record({"fingerprint": "abc", "smoke_starts": 3}))
                self.assertEqual(preflight.read_record(), {"fingerprint": "abc", "smoke_starts": 3})
                Path(d, "preflight-tested.json").write_text("not json")
                self.assertEqual(preflight.read_record(), {})
            finally:
                if saved is None:
                    os.environ.pop("SLOT17_STATE_DIR", None)
                else:
                    os.environ["SLOT17_STATE_DIR"] = saved

    def test_every_smoke_test_exists(self):
        """A renamed test must fail here, not silently shrink the start-of-drive check."""
        suite = unittest.defaultTestLoader.loadTestsFromNames(preflight.SMOKE_TESTS)
        self.assertEqual(suite.countTestCases(), len(preflight.SMOKE_TESTS))
        for test in suite:
            self.assertNotIn("FailedTest", type(test).__name__)


if __name__ == "__main__":
    unittest.main()
