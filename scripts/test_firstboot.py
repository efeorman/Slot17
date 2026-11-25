"""deploy/image/firstboot_config.py: reading slot17.txt as people edit it, and removing the secrets."""
import importlib.util
import shlex
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("firstboot_config", Path(__file__).resolve().parents[1] / "deploy" / "image" / "firstboot_config.py")
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)
TEMPLATE = (Path(__file__).resolve().parents[1] / "deploy" / "image" / "slot17.txt").read_text(encoding="utf-8")


class Reading(unittest.TestCase):
    def test_the_shipped_template_is_empty(self):
        # Nothing of anyone's in it; only Bluetooth's defaults (1.4.0: on, under the name Slot17).
        self.assertEqual(fc.parse(TEMPLATE), {**{k: "" for k in fc.KEYS}, "bluetooth": "yes", "bluetooth_name": "Slot17"})

    def test_notepad_quirks(self):
        text = "﻿# note\r\nserver = https://k7f2q.slot17.cloud \r\ncar_key=\"abc=def\"\r\nHOTSPOT_NAME=iPhone van Efe\r\nhotspot_password='p@ss word'\r\n"
        self.assertEqual(fc.parse(text), {"server": "https://k7f2q.slot17.cloud", "car_key": "abc=def",
                                          "hotspot_name": "iPhone van Efe", "hotspot_password": "p@ss word"})

    def test_unknown_keys_and_comments_are_ignored(self):
        self.assertEqual(fc.parse("# server=x\nfoo=bar\nserver=y\n"), {"server": "y"})

    def test_shell_output_is_safe(self):
        import contextlib
        import io
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write("hotspot_password=a'b; rm -rf / $(x)\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fc.main(["x", "read", f.name])
        line = [ln for ln in out.getvalue().splitlines() if ln.startswith("S17_HOTSPOT_PASSWORD=")][0]
        self.assertEqual(shlex.split(line.split("=", 1)[1]), ["a'b; rm -rf / $(x)"])
        Path(f.name).unlink()


class Forgetting(unittest.TestCase):
    def test_secrets_go_the_rest_stays(self):
        text = TEMPLATE.replace("server=\n", "server=https://k7f2q.slot17.cloud\n").replace("car_key=\n", "car_key=SECRETKEY\n") \
            .replace("hotspot_password=\n", "hotspot_password=hunter22\n")
        after = fc.forget(text)
        self.assertNotIn("SECRETKEY", after)
        self.assertNotIn("hunter22", after)
        self.assertIn("server=https://k7f2q.slot17.cloud", after)
        self.assertEqual(fc.parse(after)["car_key"], "")
        self.assertIn("was saved on the car computer", after)

    def test_empty_secrets_are_left_alone(self):
        self.assertEqual(fc.forget(TEMPLATE), TEMPLATE)

    def test_old_mac_line_endings_lose_their_secrets_too(self):
        """parse() read them; forget() didn't (found in review, 24 November 2026)."""
        text = "server=https://x.slot17.cloud\rcar_key=SECRETKEY\rhotspot_password=hunter22\r"
        self.assertEqual(fc.parse(text)["car_key"], "SECRETKEY")
        after = fc.forget(text)
        self.assertNotIn("SECRETKEY", after)
        self.assertNotIn("hunter22", after)

    def test_the_file_is_checked_after_it_is_rewritten(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "slot17.txt"
            path.write_text("car_key=SECRETKEY\n", encoding="utf-8")
            self.assertEqual(fc.main(["x", "forget", str(path)]), 0)
            self.assertNotIn("SECRETKEY", path.read_text(encoding="utf-8"))
            path.write_text("car_key=SECRETKEY\n", encoding="utf-8")
            with patch.object(fc.os, "replace", side_effect=OSError("read-only file system")):
                self.assertEqual(fc.main(["x", "forget", str(path)]), 1)
            with patch.object(fc, "forget", side_effect=lambda text: text):          # a rewrite that kept them
                self.assertEqual(fc.main(["x", "forget", str(path)]), 1)


def find_bash():
    import os
    import shutil
    if os.name == "nt":                      # not System32's bash.exe, which is WSL's
        for candidate in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
            if Path(candidate).is_file():
                return candidate
        return None
    return shutil.which("bash")


BASH = find_bash()
FIRSTBOOT = Path(__file__).resolve().parents[1] / "deploy" / "image" / "firstboot.sh"
STAND_INS = r'''
getent() { [ "$1" = passwd ] && [ -n "${T_USER:-}" ] && echo "$T_USER:x:1000:1000::$T_HOME:/bin/bash"; }
runuser() { echo "runuser $*" >> "$T_CALLS"; return "${T_INSTALL_RC:-0}"; }
lsusb() { return 1; }
python3() {
    if [ "$2" = forget ] && [ "${T_FORGET_FAILS:-0}" = 1 ]; then echo "couldn't rewrite" >&2; return 1; fi
    "$T_PYTHON" "$@"
}
'''


@unittest.skipUnless(BASH, "needs bash")
class FirstStart(unittest.TestCase):
    """deploy/image/firstboot.sh run in bash, its fixed paths moved into a scratch folder and the system's commands
    stood in for. Found in review on 24 November 2026: a power cut while the installer ran left its all-commands
    sudo grant on the card, and a failed removal of the secrets was reported as done."""

    def setUp(self):
        import shutil
        import sys
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        for d in ("boot", "opt", "var", "sudoers", "home"):
            (t / d).mkdir()
        shutil.copyfile(Path(spec.origin), t / "opt" / "firstboot_config.py")
        (t / "boot" / "slot17.txt").write_text("server=https://x.slot17.cloud\ncar_key=SECRETKEY\n", encoding="utf-8")
        self.t = t
        text = FIRSTBOOT.read_text(encoding="utf-8")
        for name, value in (("BOOT", "boot"), ("OPT", "opt"), ("DONE", "var/firstboot-done"),
                            ("INSTALLED", "var/firstboot-installed"), ("SUDOERS", "sudoers/90-slot17-firstboot")):
            head = f"\n{name}="
            if head not in text:            # an older firstboot.sh, run to show what it did
                continue
            start = text.index(head) + 1
            text = text[:start] + f'{name}="$T_ROOT/{value}"' + text[text.index("\n", start):]
        text = text.replace("set -uo pipefail\n", "set -uo pipefail\n" + STAND_INS, 1)
        self.script = t / "firstboot.sh"
        self.script.write_text(text, encoding="utf-8", newline="\n")
        self.env = {"T_ROOT": t.as_posix(), "T_USER": "person", "T_HOME": (t / "home").as_posix(),
                    "T_CALLS": (t / "calls.log").as_posix(), "T_PYTHON": Path(sys.executable).as_posix()}

    def tearDown(self):
        self.tmp.cleanup()

    def run_it(self, **env):
        import os
        import subprocess
        done = subprocess.run([BASH, self.script.as_posix()], env={**os.environ, **self.env, **env},
                              capture_output=True, text=True, timeout=60)
        calls = (self.t / "calls.log").read_text() if (self.t / "calls.log").exists() else ""
        return done.returncode, calls

    def test_a_grant_a_power_cut_left_is_gone_before_anything_else(self):
        (self.t / "sudoers" / "90-slot17-firstboot").write_text("person ALL=(ALL) NOPASSWD: ALL\n")
        rc, _ = self.run_it(T_USER="")                       # even when it only waits for the user
        self.assertEqual(rc, 75)
        self.assertFalse((self.t / "sudoers" / "90-slot17-firstboot").exists())

    def test_a_failed_removal_of_the_secrets_is_not_called_done_and_is_retried_alone(self):
        rc, calls = self.run_it(T_FORGET_FAILS="1")
        self.assertEqual(rc, 1)
        self.assertFalse((self.t / "var" / "firstboot-done").exists())
        self.assertIn("SECRETKEY", (self.t / "boot" / "slot17.txt").read_text())
        self.assertEqual(calls.count("install.sh"), 1)
        rc, calls = self.run_it()
        self.assertEqual(rc, 0)
        self.assertTrue((self.t / "var" / "firstboot-done").exists())
        self.assertNotIn("SECRETKEY", (self.t / "boot" / "slot17.txt").read_text())
        self.assertEqual(calls.count("install.sh"), 1, "the setup itself isn't run again")
        self.assertFalse((self.t / "sudoers" / "90-slot17-firstboot").exists())

    def test_a_setup_that_stops_leaves_no_grant_and_no_done(self):
        rc, _ = self.run_it(T_INSTALL_RC="3")
        self.assertEqual(rc, 1)
        self.assertFalse((self.t / "sudoers" / "90-slot17-firstboot").exists())
        self.assertFalse((self.t / "var" / "firstboot-done").exists())
        self.assertFalse((self.t / "var" / "firstboot-installed").exists())


if __name__ == "__main__":
    unittest.main()
