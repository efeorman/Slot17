"""Signed updates: only the owner's releases are installed, and a power cut never leaves half of one."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
import update  # noqa: E402

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
except ImportError:                     # the updater is simply off on such a machine
    Ed25519PrivateKey = None


def bundle(key, seq, files, commit="abcdef1234567", manifest_files=None):
    listed = {p: hashlib.sha256(d).hexdigest() for p, d in files.items()} if manifest_files is None else manifest_files
    manifest = json.dumps({"seq": seq, "commit": commit, "files": listed}).encode()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, data in [("manifest.json", manifest)] + sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    data = raw.getvalue()
    return data, key.sign(data).hex()


@unittest.skipUnless(Ed25519PrivateKey, "needs the cryptography package")
class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Slot17"
        (self.root / "p0").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "display").mkdir()
        (self.root / "p0/watch.py").write_text("OLD = 1\n")
        (self.root / "p0/calibration.json").write_text('{"median_ppm": 4.09}')
        (self.root / "display/display_publisher.py").write_text("OLD = 1\n")
        self.key = Ed25519PrivateKey.generate()
        self.key_file = Path(self.tmp.name) / "release-key.pub"
        self.key_file.write_text(self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex())
        self.served = {}
        self.requests = []
        self.verdict = ""               # what the stand-in for the offline checks answers
        self.updater = self.make()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, boot="boot-1"):
        updater = update.Updater(self.root, self.key_file, opener=self.open, boot_id=boot)
        updater._test = lambda seq, manifest, limit: self.verdict
        return updater

    def open(self, path, timeout):
        self.requests.append(path)
        if path not in self.served:
            raise OSError("no such thing")
        return self.served[path]

    def publish(self, seq, files, key=None, **kw):
        data, signature = bundle(key or self.key, seq, files, **kw)
        self.served["/api/release"] = json.dumps({"seq": seq, "signature": signature}).encode()
        self.served["/api/release/bundle"] = data
        return data, signature

    NEW = {"p0/watch.py": b"NEW = 2\n", "tests/test_new.py": b"X = 1\n", "display/display_publisher.py": b"NEW = 2\n"}


class Fetching(Base):
    def test_a_signed_newer_release_is_kept_for_the_next_start_and_nothing_is_replaced_yet(self):
        self.publish(3, self.NEW)
        self.assertEqual(self.updater.fetch(), "staged")
        self.assertEqual(self.updater.staged(), 3)
        self.assertEqual((self.root / "p0/watch.py").read_text(), "OLD = 1\n")
        self.assertEqual(self.updater.status()["state"], "staged")

    def test_a_release_signed_by_someone_else_is_refused(self):
        self.publish(3, self.NEW, key=Ed25519PrivateKey.generate())
        self.assertEqual(self.updater.fetch(), "not accepted")
        self.assertEqual(self.updater.staged(), 0)
        self.assertEqual(self.updater.status()["state"], "error")

    def test_a_release_changed_after_signing_is_refused(self):
        data, signature = self.publish(3, self.NEW)
        self.served["/api/release/bundle"] = data[:-1] + bytes([data[-1] ^ 1])
        self.assertEqual(self.updater.fetch(), "not accepted")
        self.assertEqual(self.updater.staged(), 0)

    def test_paths_outside_the_detector_are_refused_even_when_signed(self):
        for path in ("p0/calibration.json", "p0/downlink_channels.json", "p0/calibration.local.json", "../evil.py",
                     "/etc/passwd", "deploy/tetra-detector.service", "display/server.py", "p0/../../x.py"):
            self.publish(3, {path: b"x"})
            self.assertEqual(self.updater.fetch(), "not accepted", path)
            self.assertEqual(self.updater.staged(), 0, path)
        self.assertEqual(json.loads((self.root / "p0/calibration.json").read_text())["median_ppm"], 4.09)

    def test_a_manifest_that_does_not_match_the_files_is_refused(self):
        self.publish(3, self.NEW, manifest_files={"p0/watch.py": "0" * 64})
        self.assertEqual(self.updater.fetch(), "not accepted")

    def test_an_older_or_equal_release_is_ignored_so_nothing_can_be_replayed(self):
        self.publish(3, self.NEW)
        self.updater.fetch()
        self.assertEqual(self.updater.apply(), "installed")
        self.publish(3, {"p0/watch.py": b"REPLAY = 3\n"})
        self.assertEqual(self.updater.fetch(), "nothing newer")
        self.publish(2, {"p0/watch.py": b"OLDER = 0\n"})
        self.assertEqual(self.updater.fetch(), "nothing newer")
        self.assertEqual((self.root / "p0/watch.py").read_bytes(), b"NEW = 2\n")

    def test_no_key_means_off_and_no_request_is_made(self):
        self.key_file.unlink()
        self.publish(3, self.NEW)
        self.assertEqual(self.updater.fetch(), "off")
        self.updater.start(has_route=True)
        self.assertEqual(self.requests, [])
        self.assertEqual(self.updater.status()["state"], "off")

    def test_no_network_is_not_an_error(self):
        self.assertTrue(self.updater.fetch().startswith("no answer"))
        self.assertEqual(self.updater.status()["state"], "current")


class Installing(Base):
    def test_a_tested_release_replaces_the_files_and_keeps_the_old_ones(self):
        self.publish(3, self.NEW)
        self.updater.fetch()
        self.assertEqual(self.updater.apply(), "installed")
        self.assertEqual((self.root / "p0/watch.py").read_bytes(), b"NEW = 2\n")
        self.assertEqual((self.root / "tests/test_new.py").read_bytes(), b"X = 1\n")
        self.assertEqual((self.root / "display/display_publisher.py").read_bytes(), b"NEW = 2\n")
        self.assertEqual((self.root / "releases/update/backup-3/p0/watch.py").read_text(), "OLD = 1\n")
        status = self.updater.status()
        self.assertEqual((status["state"], status["commit"], status["seq"]), ("installed", "abcdef123456", 3))
        self.assertEqual(self.updater.staged(), 0)
        self.assertEqual(self.make(boot="boot-2").status()["state"], "current", "old news at the next start")

    def test_a_release_that_fails_its_checks_is_rejected_and_never_fetched_again(self):
        self.publish(3, self.NEW)
        self.updater.fetch()
        self.verdict = "checks failed: FAIL: test_x"
        self.assertEqual(self.updater.apply(), "rejected")
        self.assertEqual((self.root / "p0/watch.py").read_text(), "OLD = 1\n")
        status = self.updater.status()
        self.assertEqual(status["state"], "rejected")
        self.assertIn("checks failed", status["detail"])
        self.requests.clear()
        self.assertEqual(self.updater.fetch(), "newest release was rejected before")
        self.assertEqual(self.requests, ["/api/release"])
        self.verdict = ""
        self.publish(4, self.NEW)
        self.assertEqual(self.updater.fetch(), "staged", "a later release is still taken")

    def test_a_power_cut_during_the_install_is_finished_at_the_next_start(self):
        self.publish(3, self.NEW)
        self.updater.fetch()
        work = self.root / "releases/update"
        manifest = json.loads((work / "3.ready").read_text())
        manifest["remove"] = []
        (work / "applying.json").write_text(json.dumps(manifest))
        shutil.copyfile(work / "staged/3/p0/watch.py", self.root / "p0/watch.py")     # one file made it
        (self.root / "tests/test_new.py.updating").write_bytes(b"X")                  # one was half written
        later = self.make(boot="boot-2")
        later.start(has_route=False)
        self.assertEqual((self.root / "tests/test_new.py").read_bytes(), b"X = 1\n")
        self.assertEqual((self.root / "display/display_publisher.py").read_bytes(), b"NEW = 2\n")
        self.assertFalse((work / "applying.json").exists())
        self.assertEqual(later.status()["state"], "installed")
        self.assertEqual(later.installed()["seq"], 3)

    def test_a_file_a_later_release_drops_is_removed_but_strangers_are_left_alone(self):
        (self.root / "p0/mine.py").write_text("LOCAL = 1\n")
        self.publish(3, self.NEW)
        self.updater.fetch()
        self.updater.apply()
        self.publish(4, {"p0/watch.py": b"NEWER = 3\n"})
        self.updater.fetch()
        self.assertEqual(self.updater.apply(), "installed")
        self.assertFalse((self.root / "tests/test_new.py").exists())
        self.assertTrue((self.root / "releases/update/backup-4/tests/test_new.py").exists())
        self.assertTrue((self.root / "p0/mine.py").exists())
        self.assertTrue((self.root / "p0/calibration.json").exists())

    def test_start_fetches_and_installs_in_one_go_when_the_network_is_already_up(self):
        self.publish(3, self.NEW)
        self.updater.start(has_route=True)
        self.assertEqual((self.root / "p0/watch.py").read_bytes(), b"NEW = 2\n")
        self.assertEqual(self.updater.status()["state"], "installed")

    def test_start_without_network_only_installs_what_is_waiting(self):
        self.publish(3, self.NEW)
        self.updater.start(has_route=False)
        self.assertEqual(self.requests, [])
        self.assertEqual((self.root / "p0/watch.py").read_text(), "OLD = 1\n")
        self.assertEqual(self.updater.status()["state"], "current")

    def test_a_waiting_release_with_missing_files_is_dropped_not_installed(self):
        self.publish(3, self.NEW)
        self.updater.fetch()
        (self.root / "releases/update/staged/3/p0/watch.py").unlink()
        self.assertEqual(self.updater.apply(), "incomplete")
        self.assertEqual(self.updater.staged(), 0)
        self.assertEqual((self.root / "p0/watch.py").read_text(), "OLD = 1\n")


# Not inside the updater's own test run: that run includes this file, and would start itself again.
@unittest.skipUnless(Ed25519PrivateKey and not os.environ.get("SLOT17_UPDATE_TEST"), "needs the cryptography package")
class RealChecks(unittest.TestCase):
    def test_the_real_offline_checks_refuse_a_release_that_breaks_the_detector(self):
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Slot17"
            for folder in ("p0", "tests"):
                shutil.copytree(repo / folder, root / folder, ignore=shutil.ignore_patterns("__pycache__"))
            key = Ed25519PrivateKey.generate()
            key_file = Path(tmp) / "key.pub"
            key_file.write_text(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex())
            broken = (repo / "p0/phy.py").read_text(encoding="utf-8").replace('"cub": "1001', '"cub": "001')
            self.assertNotEqual(broken, (repo / "p0/phy.py").read_text(encoding="utf-8"))
            data, signature = bundle(key, 9, {"p0/phy.py": broken.encode()})
            served = {"/api/release": json.dumps({"seq": 9, "signature": signature}).encode(), "/api/release/bundle": data}
            updater = update.Updater(root, key_file, opener=lambda path, timeout: served[path], boot_id="b")
            self.assertEqual(updater.fetch(), "staged")
            self.assertEqual(updater.apply(limit=120), "rejected")
            self.assertEqual((root / "p0/phy.py").read_bytes(), (repo / "p0/phy.py").read_bytes())
            self.assertIn("checks failed", updater.status()["detail"])


if __name__ == "__main__":
    unittest.main()
