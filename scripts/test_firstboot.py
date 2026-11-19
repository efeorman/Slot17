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
        self.assertEqual(fc.parse(TEMPLATE), {k: "" for k in fc.KEYS})

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


if __name__ == "__main__":
    unittest.main()
