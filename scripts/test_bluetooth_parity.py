"""shared/parity/ble-frames.json is exactly what p0/bluetooth_frames.py makes, so the apps test the real format.

Not in tests/: the car computer runs tests/ before every start, and its signed updates bring p0/ and
tests/ but not shared/. Regenerate the file with: python -B p0/bluetooth_frames.py > shared/parity/ble-frames.json
"""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "p0"))
import bluetooth_frames as frames  # noqa: E402


class ParityFileTests(unittest.TestCase):
    def test_the_shared_file_is_current(self):
        stored = json.loads((ROOT / "shared/parity/ble-frames.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, json.loads(json.dumps(frames.parity_fixture())),
                         "regenerate: python -B p0/bluetooth_frames.py > shared/parity/ble-frames.json")

    def test_its_frames_decode_and_the_tampered_one_does_not(self):
        stored = json.loads((ROOT / "shared/parity/ble-frames.json").read_text(encoding="utf-8"))
        key = bytes.fromhex(stored["key"])
        for f in stored["frames"]:
            self.assertEqual(json.loads(json.dumps(frames.decode(bytes.fromhex(f["hex"]), key))), f["decoded"])
        with self.assertRaises(frames.FrameError):
            frames.decode(bytes.fromhex(stored["tampered"]), key)
        self.assertEqual({m["key"] for m in stored["match_keys"]},
                         {frames.match_key(m["id"]).hex() for m in stored["match_keys"]})


if __name__ == "__main__":
    unittest.main()
