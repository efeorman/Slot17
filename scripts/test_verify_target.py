"""scripts/verify_sdrplay.py's check that a compiler builds for the target it is asked about (also used by
verify_rtlsdr.py). No compiler is run."""
import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("verify_sdrplay", Path(__file__).resolve().parent / "verify_sdrplay.py")
V = importlib.util.module_from_spec(spec)
spec.loader.exec_module(V)


class CompilerTarget(unittest.TestCase):
    def test_the_same_processor_and_system(self):
        for machine, target in (("aarch64-linux-gnu", "aarch64-linux-gnu"), ("x86_64-pc-linux-gnu", "x86_64-linux-gnu"),
                                ("x86_64-linux-gnu", "x86_64-linux-gnu")):
            self.assertFalse(V.target_mismatch(machine, target), machine)

    def test_another_system_with_the_same_processor_is_refused(self):
        """Found in the second review, 24 November 2026: only the processor was compared."""
        self.assertTrue(V.target_mismatch("x86_64-w64-mingw32", "x86_64-linux-gnu"))
        self.assertTrue(V.target_mismatch("x86_64-apple-darwin23.0.0", "x86_64-linux-gnu"))

    def test_another_processor_or_no_answer_is_refused(self):
        self.assertTrue(V.target_mismatch("x86_64-linux-gnu", "aarch64-linux-gnu"))
        self.assertTrue(V.target_mismatch("", "aarch64-linux-gnu"))


if __name__ == "__main__":
    unittest.main()
