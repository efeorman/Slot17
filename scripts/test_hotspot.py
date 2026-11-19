"""deploy/hotspot.sh: what it writes for NetworkManager, and what it refuses. Runs without root or Wi-Fi."""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "hotspot.sh"
BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "needs bash")
class Hotspot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_it(self, name, password):
        env = {**os.environ, "SLOT17_NM_DIR": str(self.dir)}
        return subprocess.run([BASH, str(SCRIPT), name], input=password, text=True, capture_output=True, env=env)

    def saved(self):
        return (self.dir / "slot17-hotspot.nmconnection").read_text()

    def test_writes_a_private_connection(self):
        r = self.run_it("iPhone van Efe", "correct horse")
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.saved()
        self.assertIn("ssid=iPhone van Efe\n", text)
        self.assertIn("psk=correct horse\n", text)
        self.assertIn("autoconnect=true", text)
        if os.name == "posix":
            self.assertEqual(oct((self.dir / "slot17-hotspot.nmconnection").stat().st_mode & 0o777), "0o600")
        self.assertNotIn("correct horse", r.stdout + r.stderr, "the password is never printed")

    def test_backslashes_and_outer_spaces_survive(self):
        self.assertEqual(self.run_it("Phone", " a\\b cdef ").returncode, 0)
        self.assertIn("psk=\\sa\\\\b cdef\\s\n", self.saved())

    def test_saving_again_replaces_it(self):
        self.run_it("First", "password1")
        self.run_it("Second", "password2")
        self.assertIn("ssid=Second", self.saved())
        self.assertEqual(len(list(self.dir.glob("*.nmconnection"))), 1)

    def test_refuses_what_wifi_cannot_have(self):
        for name, pw in (("", "password1"), ("Phone", "short"), ("Phone", "x" * 64), ("a;b", "password1"),
                         ("x" * 33, "password1"), ("Phone", "pass\nword1")):
            with self.subTest(name=name, pw=pw):
                self.assertEqual(self.run_it(name, pw).returncode, 2)
        self.assertFalse(list(self.dir.glob("*.nmconnection")))


if __name__ == "__main__":
    unittest.main()
