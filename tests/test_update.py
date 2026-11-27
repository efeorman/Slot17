"""Signed updates: only the owner's releases are installed, and a power cut never leaves half of one."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess
import tarfile
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
import update  # noqa: E402

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
except ImportError:                     # the updater is simply off on such a machine
    Ed25519PrivateKey = None

# Where the updater must be tested (a release being signed, CI), SLOT17_REQUIRE_CRYPTOGRAPHY=1 turns a missing
# package into an error instead of 27 quiet skips (found in review, 25 November 2026). A car computer never sets
# it: without the package its updates are off, and its detector must still start.
if Ed25519PrivateKey is None and os.environ.get("SLOT17_REQUIRE_CRYPTOGRAPHY") == "1":
    raise ImportError("SLOT17_REQUIRE_CRYPTOGRAPHY=1 but the cryptography package is missing, so the updater "
                      "can't be tested: pip install -r requirements-dev.txt")


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


class NoServer(unittest.TestCase):
    """Without a display server, the newest public release on GitHub (27 November 2026: Slot17 without any server)."""

    def test_the_public_releases_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            u = update.Updater(Path(tmp), Path(tmp) / "key.pub", config=Path(tmp) / "slot17-display.json")
            asked = []

            class Answer:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

                def read(self, n):
                    read.append(n)
                    return b"{}"

                def geturl(self):
                    return landed[0]

            read, landed = [], ["https://release-assets.githubusercontent.com/x"]

            def urlopen(req, timeout):
                asked.append((req.full_url, req.get_header("Authorization")))
                return Answer()
            with unittest.mock.patch.object(update.urllib.request, "urlopen", urlopen):
                u._open("/api/release", 5)
                u._open("/api/release/bundle", 5)
                landed[0] = "http://192.168.1.1/login"        # a Wi-Fi's login page instead of GitHub
                with self.assertRaises(OSError):
                    u._open("/api/release", 5)
        self.assertEqual(read, [update.RELEASE_JSON_MAX, update.MAX_BUNDLE + 1])
        self.assertEqual(asked[:2], [(update.GITHUB_RELEASE + "slot17-update.json", None),
                                 (update.GITHUB_RELEASE + "slot17-update.tar.gz", None)])
        self.assertTrue(update.GITHUB_RELEASE.startswith("https://github.com/efeorman/Slot17/releases/latest/download/"))


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


class AfterReview(Base):
    """Found in review on 24 November 2026; each test fails on the updater before that day."""

    def test_a_file_changed_on_the_card_after_fetching_is_fetched_again_not_installed(self):
        self.publish(3, self.NEW)
        self.updater.fetch()
        (self.root / "releases/update/staged/3/p0/watch.py").write_bytes(b"BAD = 2\n")      # same length
        self.assertEqual(self.updater.apply(), "incomplete")
        self.assertEqual((self.root / "p0/watch.py").read_text(), "OLD = 1\n")
        self.assertEqual(self.updater.staged(), 0)
        self.assertEqual(self.updater.fetch(), "staged", "fetched again, not held against the release")

    def test_a_file_changed_while_it_is_tested_puts_the_old_release_back(self):
        self.publish(3, self.NEW)
        self.updater.fetch()

        def test_and_damage(seq, manifest, limit):
            (self.root / "releases/update/staged/3/tests/test_new.py").write_bytes(b"X = 9\n")
            return ""
        self.updater._test = test_and_damage
        self.assertEqual(self.updater.apply(), "put back")
        self.assertEqual((self.root / "p0/watch.py").read_text(), "OLD = 1\n")
        self.assertEqual((self.root / "display/display_publisher.py").read_text(), "OLD = 1\n")
        self.assertFalse((self.root / "tests/test_new.py").exists(), "a file the release added goes too")
        self.assertFalse((self.root / "releases/update/applying.json").exists())
        self.assertEqual(self.updater.installed()["seq"], 0)
        self.assertEqual(self.updater.status()["state"], "error")

    def test_a_power_cut_while_a_backup_is_written_leaves_a_whole_backup(self):
        class Cut(BaseException):
            """The power going: no except clause sees it."""
        calls = []

        def cut_once(real):
            def copy(src, dst, *a, **kw):
                if calls:
                    return real(src, dst, *a, **kw)
                calls.append(1)
                if isinstance(src, (str, Path)):                 # shutil.copy2(src, dst)
                    Path(dst).write_bytes(Path(src).read_bytes()[:3])
                else:                                             # shutil.copyfileobj(fsrc, fdst)
                    dst.write(src.read()[:3])
                    dst.flush()
                raise Cut
            return copy
        self.publish(3, self.NEW)
        self.updater.fetch()
        with unittest.mock.patch.object(update.shutil, "copy2", cut_once(shutil.copy2)), \
                unittest.mock.patch.object(update.shutil, "copyfileobj", cut_once(shutil.copyfileobj)):
            with self.assertRaises(Cut):
                self.updater.apply()
        self.make(boot="boot-2").start(has_route=False)
        self.assertEqual((self.root / "display/display_publisher.py").read_bytes(), b"NEW = 2\n")
        # The files go in sorted order, so the cut was in the publisher's backup, the first one.
        self.assertEqual((self.root / "releases/update/backup-3/display/display_publisher.py").read_text(), "OLD = 1\n")

    def test_an_older_version_is_refused_where_no_release_was_installed_yet(self):
        (self.root / "p0/version.py").write_text('SOFTWARE = "1.6.0"\n')
        self.assertFalse((self.root / "releases/update/installed.json").exists())   # a fresh install
        self.publish(2, dict(self.NEW, **{"p0/version.py": b'SOFTWARE = "1.5.1"\n'}))
        self.assertEqual(self.updater.fetch(), "staged")
        self.assertEqual(self.updater.apply(), "rejected")
        self.assertIn("older than the installed software (1.5.1 < 1.6.0)", self.updater.status()["detail"])
        self.assertEqual((self.root / "p0/watch.py").read_text(), "OLD = 1\n")
        self.publish(3, dict(self.NEW, **{"p0/version.py": b'SOFTWARE = "1.6.0"\n'}))
        self.updater.fetch()
        self.assertEqual(self.updater.apply(), "installed", "the same version is no downgrade")

    def test_checks_that_ran_out_of_time_are_tried_again_not_held_against_the_release(self):
        self.publish(3, self.NEW)
        self.updater.fetch()
        self.verdict = "checks took longer than 70 s"
        self.assertEqual(self.updater.apply(), "postponed")
        self.assertEqual(self.updater.staged(), 3)
        self.assertEqual(self.updater.status()["state"], "staged")
        self.assertIn("tried again at the next start (1 of 3)", self.updater.status()["detail"])
        self.assertEqual((self.root / "p0/watch.py").read_text(), "OLD = 1\n")
        self.verdict = ""
        self.assertEqual(self.make(boot="boot-2").apply(), "installed")

    def staged_fails(self, path):
        """The card can't give back one staged file as signed any more (its digest is wrong from now on)."""
        real = update.sha256_of
        bad = self.root / "releases/update/staged/3" / path
        return unittest.mock.patch.object(update, "sha256_of", lambda p: "unreadable" if Path(p) == bad else real(p))

    def test_a_file_the_release_added_goes_when_a_resumed_install_is_put_back(self):
        """Third round, 24 November 2026 (F32): after a cut, the resumed install backed up the file the release had
        just added, as if it were the old release's, and putting back kept it."""
        class Cut(BaseException):
            pass
        self.publish(3, self.NEW)
        self.updater.fetch()
        with unittest.mock.patch.object(update.Updater, "_finish", side_effect=Cut):
            with self.assertRaises(Cut):
                self.updater.apply()                # every file in, then the power goes
        self.assertTrue((self.root / "tests/test_new.py").exists())
        with self.staged_fails("tests/test_new.py"):
            self.assertEqual(self.make(boot="boot-2").apply(), "put back")
        self.assertFalse((self.root / "tests/test_new.py").exists())
        self.assertEqual((self.root / "p0/watch.py").read_text(), "OLD = 1\n")
        self.assertEqual((self.root / "display/display_publisher.py").read_text(), "OLD = 1\n")

    def test_a_cut_after_the_install_record_only_finishes_the_cleanup(self):
        """Third round, 24 November 2026: a cut between the new install record and the journal's removal made the
        next start replace everything again; a failure there put the old files back under the new record."""
        class Cut(BaseException):
            pass
        self.publish(3, self.NEW)
        self.updater.fetch()
        real_write = update.Updater._write

        def write_then_cut(updater, name, data):
            real_write(updater, name, data)
            if name == "installed.json":
                raise Cut
        with unittest.mock.patch.object(update.Updater, "_write", write_then_cut):
            with self.assertRaises(Cut):
                self.updater.apply()
        later = self.make(boot="boot-2")
        with self.staged_fails("p0/watch.py"):
            self.assertEqual(later.apply(), "finished an interrupted install")
        self.assertEqual((self.root / "p0/watch.py").read_bytes(), b"NEW = 2\n")
        self.assertEqual(later.installed()["seq"], 3)
        self.assertFalse((self.root / "releases/update/applying.json").exists())

    def test_a_cut_while_putting_back_still_ends_with_the_old_release_whole(self):
        class Cut(BaseException):
            pass
        self.publish(3, self.NEW)
        self.updater.fetch()
        real = update.write_durably
        restores = []

        def cut_at_the_first_restore(source, target, keep_times=False):
            if "backup-3" in str(source) and not restores:
                restores.append(target)
                raise Cut
            return real(source, target, keep_times)
        def test_and_damage(seq, manifest, limit):  # after the checks before testing, so the install itself fails
            (self.root / "releases/update/staged/3/tests/test_new.py").write_bytes(b"X = 9\n")
            return ""
        self.updater._test = test_and_damage
        with unittest.mock.patch.object(update, "write_durably", cut_at_the_first_restore):
            with self.assertRaises(Cut):
                self.updater.apply()                # the install fails, and the power goes while putting back
        self.assertEqual(len(restores), 1)
        self.assertEqual(self.make(boot="boot-2").apply(), "put back")
        self.assertEqual((self.root / "p0/watch.py").read_text(), "OLD = 1\n")
        self.assertEqual((self.root / "display/display_publisher.py").read_text(), "OLD = 1\n")
        self.assertFalse((self.root / "tests/test_new.py").exists())
        self.assertEqual(self.updater.installed()["seq"], 0)

    def test_a_lost_backup_folder_is_never_put_back_as_the_old_release(self):
        """Fourth round, 24 November 2026 (F34): a power cut that loses the backup folder made the resumed install back
        up the already-new file, and "put back" left new contents under the old release's number. Now the install
        says it could not put the old release back, keeps its journal, and finishes at a later start."""
        class Cut(BaseException):
            pass
        self.publish(3, self.NEW)
        self.updater.fetch()
        with unittest.mock.patch.object(update.Updater, "_finish", side_effect=Cut):
            with self.assertRaises(Cut):
                self.updater.apply()                # every file in, then the power goes
        shutil.rmtree(self.root / "releases/update/backup-3")             # and the folder's name with it
        with self.staged_fails("tests/test_new.py"):
            later = self.make(boot="boot-2")
            self.assertEqual(later.apply(), "not put back")
        self.assertIn("could not be put back", later.status()["detail"])
        self.assertTrue((self.root / "releases/update/applying.json").exists(), "kept for the next start")
        self.assertEqual(later.installed()["seq"], 0)
        self.assertEqual(self.make(boot="boot-3").apply(), "finished an interrupted install")
        self.assertEqual((self.root / "p0/watch.py").read_bytes(), b"NEW = 2\n")
        self.assertEqual(self.updater.installed()["seq"], 3)

    def test_checks_that_never_finish_in_time_reject_it_at_the_third_start(self):
        self.publish(3, self.NEW)
        self.updater.fetch()
        self.verdict = "could not test: OSError"
        self.assertEqual([self.updater.apply() for _ in range(3)], ["postponed", "postponed", "rejected"])
        self.assertIn("3 starts running", self.updater.status()["detail"])


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
            # Rejected for the broken file, not because a test module couldn't even load in the copy (a test that
            # read deploy/ at import was rejected like this, on 24 November 2026, and so was every release with it).
            self.assertNotIn("_FailedTest", updater.status()["detail"])

    def test_every_test_module_loads_in_the_copy_the_updater_tests(self):
        """The updater tests a release in a tree of p0/, tests/ and the publisher only (Updater._test): a test that
        needs deploy/, scripts/ or anything else there can't load, and every release would be rejected."""
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            for folder in ("p0", "tests"):
                shutil.copytree(repo / folder, Path(tmp) / folder, ignore=shutil.ignore_patterns("__pycache__"))
            (Path(tmp) / "display").mkdir()
            shutil.copyfile(repo / "display/display_publisher.py", Path(tmp) / "display/display_publisher.py")
            done = subprocess.run(
                [sys.executable, "-B", "-c", "import unittest\n"
                 "def walk(s):\n"
                 "    for t in s:\n"
                 "        yield from walk(t) if isinstance(t, unittest.TestSuite) else [t]\n"
                 "bad = [t.id() for t in walk(unittest.defaultTestLoader.discover('tests')) if 'FailedTest' in t.id()]\n"
                 "print(bad or 'all loaded')"],
                cwd=tmp, capture_output=True, text=True, timeout=120, env=dict(os.environ, SLOT17_UPDATE_TEST="1"))
            self.assertEqual(done.stdout.strip(), "all loaded", done.stdout + done.stderr[-2000:])


class RequiredCryptographyTests(unittest.TestCase):
    """Never skipped: these say whether a missing package is allowed to pass quietly."""

    def load_without_the_package(self, require):
        code = ("import sys\n"
                "sys.modules['cryptography'] = None     # as if it were not installed\n"
                f"sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n"
                "import test_update\n"
                "print('loaded', test_update.Ed25519PrivateKey)\n")
        env = {k: v for k, v in os.environ.items() if k != "SLOT17_REQUIRE_CRYPTOGRAPHY"}
        if require:
            env["SLOT17_REQUIRE_CRYPTOGRAPHY"] = "1"
        return subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=60, env=env)

    def test_without_the_package_a_car_computer_only_skips_them(self):
        done = self.load_without_the_package(require=False)
        self.assertEqual(done.returncode, 0, done.stderr[-1500:])
        self.assertIn("loaded None", done.stdout)

    def test_where_it_is_required_a_missing_package_fails(self):
        done = self.load_without_the_package(require=True)
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("SLOT17_REQUIRE_CRYPTOGRAPHY=1", done.stderr)


if __name__ == "__main__":
    unittest.main()
