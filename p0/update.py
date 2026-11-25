#!/usr/bin/env python3
"""Signed software updates for the car computer, fetched from the display server.

The owner publishes a release from their own computer: a tar.gz of the detector, its tests and
the display publisher, signed with an Ed25519 key that never leaves that computer. This file
fetches it, checks the signature, and installs it the next time the detector starts, before the
receiver is opened. It never runs while a capture is going on, never restarts anything, and never
touches the receiver.

    python3 p0/update.py start      at every detector start: finish, fetch briefly, install
    python3 p0/update.py fetch      download and check a newer release; nothing is installed
    python3 p0/update.py status     what is installed and what is waiting

Safety, in the order things can go wrong:

  * No public key (~/.config/slot17-release-key.pub): updates are off and nothing is fetched.
  * A release is used only if its signature matches that key and its number is higher than the
    installed one, so neither the server nor anyone on the network can supply or replay code.
  * Only p0/, tests/ and display/display_publisher.py are replaced. This machine's own
    calibration and channel files, and anything named *.local.json, are never replaced.
  * A release older than the installed software (by p0/version.py) is refused, also on a car
    computer that has no record of a release yet (a fresh install from the tarball or the image).
  * The files are kept with their signed checksums, and checked against them again before they
    are tested and as each one is installed: a file damaged on the card is fetched again.
  * Before anything is replaced, the complete new tree is tested in a copy. A release that fails
    is rejected and remembered; the installed software stays as it was. Checks that ran out of
    time, or couldn't run, are tried again at the next start (three times), not held against it.
  * The power can be cut at any moment (the ignition does). The replacement is written down
    first and finished at the next start, so a half-replaced tree is never left in use; an
    install that can't be finished puts the old files back. Every rename is made durable
    (the folder too) before the next step relies on it.
  * `start` always exits 0 and has a time limit, so a broken update can never stop the detector.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
KEY_FILE = Path.home() / ".config/slot17-release-key.pub"
DISPLAY_CONFIG = Path.home() / ".config/slot17-display.json"
MAX_BUNDLE = 20 << 20
MAX_FILE = 8 << 20
MAX_FILES = 400
NEVER = {"p0/calibration.json", "p0/downlink_channels.json"}
TEST_LIMIT_S = 70.0
START_LIMIT_S = 80.0        # of the whole `start`, which runs before the detector does
FETCH_AT_START_S = 6.0      # the phone's hotspot is rarely up this early; the publisher fetches later
TRIES = 3                   # starts at which checks that ran out of time are tried again
# Problems that say nothing against the release itself: a slow card or a busy start. Until 24 November 2026
# they rejected the release for good, like a failing test (found in review).
NOT_ITS_FAULT = ("checks took longer than", "could not test:")
SOFTWARE_LINE = re.compile(rb'^SOFTWARE\s*=\s*"(\d+)\.(\d+)\.(\d+)"', re.M)


def software(path):
    """The version in a p0/version.py, as a tuple to compare, or None."""
    try:
        found = SOFTWARE_LINE.search(Path(path).read_bytes())
    except OSError:
        return None
    return tuple(int(x) for x in found.groups()) if found else None


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sync_dir(path):
    """Make the names in a folder durable (a rename is in the folder, not the file). Not on Windows."""
    if not hasattr(os, "O_DIRECTORY"):
        return
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def make_dirs(folder):
    """Create `folder` and any missing parents one at a time, each made durable in its own parent. A folder made
    with mkdir alone can vanish in a power cut while files renamed into place later survive (found in review,
    F34: a backup folder lost that way made the backup look like the new release)."""
    folder = Path(folder)
    missing = []
    while not folder.exists():
        missing.append(folder)
        folder = folder.parent
    for new in reversed(missing):
        new.mkdir(exist_ok=True)
        sync_dir(new.parent)


def write_durably(source, target, keep_times=False):
    """Copy a file to `target` through a temporary name: after a power cut there is the old file or the whole new
    one, never part of it. keep_times: also its mode and times, for a backup."""
    make_dirs(target.parent)
    tmp = target.with_name(target.name + ".updating")
    with open(source, "rb") as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())
    if keep_times:
        shutil.copystat(source, tmp)
    os.replace(tmp, target)
    sync_dir(target.parent)


def allowed(path):
    """Whether a release may replace this path."""
    parts = Path(path).parts
    if not parts or Path(path).is_absolute() or ".." in parts or "\\" in path or path != Path(path).as_posix():
        return False
    if path in NEVER or path.endswith(".local.json") or "__pycache__" in parts:
        return False
    return parts[0] in ("p0", "tests") and len(parts) > 1 or path == "display/display_publisher.py"


class PutBackFailed(Exception):
    """An install could not be undone: these paths hold neither their original nor a backup of it."""

    def __init__(self, paths):
        super().__init__(", ".join(paths))
        self.paths = paths


class Updater:
    def __init__(self, root=ROOT, key_file=KEY_FILE, config=DISPLAY_CONFIG, opener=None, boot_id=None):
        self.root = Path(root)
        self.work = self.root / "releases" / "update"
        self.key_file = Path(key_file)
        self.config = Path(config)
        self.opener = opener or self._open
        self._boot_id = boot_id

    # ---------------------------------------------------------------- small files
    def _read(self, name, default):
        try:
            return json.loads((self.work / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _write(self, name, data):
        self.work.mkdir(parents=True, exist_ok=True)
        tmp = self.work / (name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.work / name)
        sync_dir(self.work)

    def boot_id(self):
        if self._boot_id is None:
            try:
                self._boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            except OSError:
                self._boot_id = "unknown"
        return self._boot_id

    def installed(self):
        return self._read("installed.json", {"seq": 0, "commit": "", "files": []})

    def note(self, state, detail=""):
        """What the phone is told. Kept short: it is shown on a small tile."""
        inst = self.installed()
        self._write("status.json", {"state": state, "detail": str(detail)[:120], "seq": inst.get("seq", 0),
                                    "commit": str(inst.get("commit", ""))[:12], "boot": self.boot_id()})
        print(f"update: {state}{' - ' + str(detail) if detail else ''}", flush=True)

    def status(self):
        """For the publisher: state, installed commit, and whether it changed at this start."""
        if not self.key_file.exists():
            return {"state": "off", "commit": "", "seq": 0, "detail": ""}
        inst = self.installed()
        saved = self._read("status.json", {})
        state = saved.get("state", "current")
        if state == "installed" and inst.get("boot") != self.boot_id():
            state = "current"                  # installed at an earlier start: no longer news
        if state == "checked":
            state = "current"
        if state == "current" and self.staged():
            state = "staged"
        return {"state": state, "commit": str(inst.get("commit", ""))[:12], "seq": int(inst.get("seq", 0)),
                "detail": str(saved.get("detail", ""))[:120] if state in ("rejected", "error", "staged") else ""}

    # ---------------------------------------------------------------- fetching
    def _public_key(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        return Ed25519PublicKey.from_public_bytes(bytes.fromhex(self.key_file.read_text(encoding="ascii").strip()))

    def _open(self, path, timeout):
        cfg = json.loads(self.config.read_text(encoding="utf-8"))
        base = cfg["url"].split("/api/")[0]
        # The address is the owner's own, from a file only they can write; what comes back is
        # trusted only after its signature has been checked.
        req = urllib.request.Request(base + path, headers={"Authorization": "Bearer " + cfg["token"]})  # noqa: S310
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.read(MAX_BUNDLE + 1)

    def staged(self):
        """Number of the newest release waiting to be installed, or 0."""
        numbers = [int(p.stem) for p in self.work.glob("*.ready") if p.stem.isdigit()] if self.work.is_dir() else []
        newest = max(numbers, default=0)
        return newest if newest > int(self.installed().get("seq", 0)) else 0

    def verify(self, bundle, signature_hex):
        """The signed manifest and the files, or ValueError. Nothing is written."""
        if len(bundle) > MAX_BUNDLE:
            raise ValueError("release too large")
        try:
            self._public_key().verify(bytes.fromhex(signature_hex), bundle)
        except Exception as exc:        # noqa: BLE001 - any failure here means: not ours
            raise ValueError(f"signature does not match ({type(exc).__name__})") from None
        files = {}
        with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as tar:
            for member in tar:
                if len(files) > MAX_FILES:
                    raise ValueError("too many files")
                if not member.isfile() or member.size > MAX_FILE:
                    raise ValueError(f"not a plain file of ordinary size: {member.name}")
                files[member.name] = tar.extractfile(member).read()
        try:
            manifest = json.loads(files.pop("manifest.json"))
            seq, commit, listed = int(manifest["seq"]), str(manifest["commit"]), dict(manifest["files"])
        except (KeyError, ValueError, TypeError):
            raise ValueError("manifest missing or malformed") from None
        if set(listed) != set(files):
            raise ValueError("files do not match the manifest")
        for path, data in files.items():
            if not allowed(path):
                raise ValueError(f"path not allowed: {path}")
            if hashlib.sha256(data).hexdigest() != listed[path]:
                raise ValueError(f"checksum differs: {path}")
        # The signed checksums stay with the files: they are checked again before testing and installing.
        return {"seq": seq, "commit": commit, "files": sorted(files), "sha256": {p: listed[p] for p in files}}, files

    def fetch(self, timeout=20.0):
        """Download a newer release and keep it for the next start. Returns a short word for the log."""
        if not self.key_file.exists():
            return "off"
        try:
            meta = json.loads(self.opener("/api/release", timeout))
            seq, signature = int(meta["seq"]), str(meta["signature"])
        except Exception as exc:        # noqa: BLE001 - no network and no release are both ordinary
            return f"no answer ({type(exc).__name__})"
        have = int(self.installed().get("seq", 0))
        if seq <= have or seq == self.staged():
            return "nothing newer"
        if seq in self._read("rejected.json", {}).get("seqs", []):
            return "newest release was rejected before"
        try:
            bundle = self.opener("/api/release/bundle", max(timeout, 30.0))
            manifest, files = self.verify(bundle, signature)
            if manifest["seq"] != seq:
                raise ValueError("release number differs from the server's")
        except Exception as exc:        # noqa: BLE001
            self.note("error", f"release {seq} not accepted: {exc}"[:120])
            return "not accepted"
        target = self.work / "staged" / str(seq)
        shutil.rmtree(target, ignore_errors=True)
        make_dirs(target)
        folders = set()
        for path, data in files.items():
            out = target / path
            make_dirs(out.parent)
            with open(out, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            folders.add(out.parent)
        for folder in sorted(folders, key=lambda p: -len(p.parts)):     # the files' names, then the folders'
            sync_dir(folder)
        sync_dir(self.work / "staged")
        self._write(f"{seq}.ready", manifest)       # only now does it count as waiting
        self.note("staged", f"{manifest['commit'][:7]} installs at the next start")
        return "staged"

    # ---------------------------------------------------------------- installing
    def _staged_problem(self, seq, manifest):
        """'' when every file of a waiting release is on the card as it was signed, else what isn't."""
        sums = manifest.get("sha256")
        if not isinstance(sums, dict):
            return "kept without its checksums"       # staged by an updater from before 24 November 2026
        for path in manifest["files"]:
            staged = self.work / "staged" / str(seq) / path
            if not allowed(path) or not staged.is_file():
                return f"missing {path}"
            if sha256_of(staged) != sums.get(path):
                return f"changed on the card: {path}"
        return ""

    def _replace(self, journal):
        """Put the release's files in place. Idempotent: run again after a power cut, it ends in the same state.
        Raises if it can't finish; then _put_back undoes it."""
        source = self.work / "staged" / str(journal["seq"])
        backup = self.work / f"backup-{journal['seq']}"
        sums = journal.get("sha256")
        existed = journal.get("existed")
        for path in journal["files"]:
            target = self.root / path
            # A backup is written through a temporary name, so one that exists is whole (until 24 November 2026
            # it was copied straight to its name, and a cut during that copy left a short one that counted).
            # Only of a file that was there before this install: after a cut, a file the release added is already
            # in place, and backing it up made it look like the old release's (found in review, F32).
            if target.exists() and not (backup / path).exists() and (existed is None or path in existed) \
                    and self._holds_original(journal, path, target):
                write_durably(target, backup / path, keep_times=True)
            if isinstance(sums, dict) and sha256_of(source / path) != sums.get(path):
                raise ValueError(f"changed on the card: {path}")
            write_durably(source / path, target)
        for path in journal.get("remove", []):       # files an earlier release installed and this one dropped
            target = self.root / path
            if target.exists():
                if not (backup / path).exists():
                    write_durably(target, backup / path, keep_times=True)
                target.unlink()
                sync_dir(target.parent)

    def _put_back(self, journal):
        """Undo an install that can't be finished: every file it replaced or removed back from its backup, and the
        files it added (journal["existed"] says which those are) removed. Leaves the old release whole."""
        backup = self.work / f"backup-{journal['seq']}"
        existed = journal.get("existed")
        lost = []
        for path in list(journal["files"]) + list(journal.get("remove", [])):
            target = self.root / path
            if existed is not None and path in journal["files"] and path not in existed:
                if target.exists():                  # added by this release: it goes, backup or not
                    target.unlink()
                    sync_dir(target.parent)
            elif (backup / path).exists() and self._holds_original(journal, path, backup / path):
                write_durably(backup / path, target, keep_times=True)
            elif not (target.exists() and self._holds_original(journal, path, target)):
                lost.append(path)                    # neither the backup nor the file holds the original
        if lost:
            raise PutBackFailed(lost)
        (self.work / "applying.json").unlink(missing_ok=True)
        sync_dir(self.work)

    def _holds_original(self, journal, path, f):
        """Whether file `f` holds what `path` held before this install: its checksum when the journal kept it, else
        anything but the release's new file (a journal from before the originals were kept)."""
        originals = journal.get("originals") or {}
        if originals.get(path) is not None:
            return sha256_of(f) == originals[path]
        new = (journal.get("sha256") or {}).get(path)
        return new is None or sha256_of(f) != new

    def _finish(self, journal):
        self._write("installed.json", {"seq": journal["seq"], "commit": journal["commit"], "files": journal["files"],
                                       "boot": self.boot_id()})
        (self.work / "applying.json").unlink(missing_ok=True)
        sync_dir(self.work)
        shutil.rmtree(self.work / "staged" / str(journal["seq"]), ignore_errors=True)
        for marker in self.work.glob("*.ready"):
            if marker.stem.isdigit() and int(marker.stem) <= journal["seq"]:
                marker.unlink(missing_ok=True)
        (self.work / "tries.json").unlink(missing_ok=True)
        for old in sorted(self.work.glob("backup-*"), key=lambda p: p.stat().st_mtime)[:-3]:
            shutil.rmtree(old, ignore_errors=True)
        self.note("installed", journal["commit"][:7])

    def _install(self, journal, resumed=False):
        # The install record is written only once every file is in place, so a resume that finds it already naming
        # this release only has the cleanup left. Replacing again there, and failing, put the old files back under
        # the new release's record (found in review, 24 November 2026).
        if resumed and int(self.installed().get("seq", 0)) == int(journal["seq"]):
            self._finish(journal)
            return "finished an interrupted install"
        try:
            self._replace(journal)
        except Exception as exc:        # noqa: BLE001 - whatever it was, the old release goes back
            try:
                self._put_back(journal)
            except PutBackFailed as lost:
                # Not "put back": say so, and keep the journal, so the next start tries to finish the install.
                self.note("error", f"release {journal['seq']} could not be installed ({type(exc).__name__}), and "
                                   f"the old copy of {lost.paths[0]} could not be put back; tried again at the "
                                   "next start"[:120])
                return "not put back"
            self._drop(journal["seq"])
            self.note("error", f"release {journal['seq']} could not be installed ({type(exc).__name__}: {exc}); "
                               "the installed software was put back"[:120])
            return "put back"
        self._finish(journal)
        return "finished an interrupted install" if resumed else "installed"

    def _drop(self, seq):
        """Forget a waiting release, so that it is fetched again (not rejected)."""
        (self.work / f"{seq}.ready").unlink(missing_ok=True)
        shutil.rmtree(self.work / "staged" / str(seq), ignore_errors=True)

    def _reject(self, seq, manifest, problem):
        rejected = self._read("rejected.json", {"seqs": []})
        rejected["seqs"] = sorted(set(rejected["seqs"]) | {seq})[-50:]
        self._write("rejected.json", rejected)
        self._drop(seq)
        (self.work / "tries.json").unlink(missing_ok=True)
        self.note("rejected", f"{manifest['commit'][:7]}: {problem}")
        return "rejected"

    def _test(self, seq, manifest, limit):
        """Run the offline checks on a complete copy with the new files. Returns '' or the reason."""
        tree = self.work / f"test-{seq}"
        shutil.rmtree(tree, ignore_errors=True)
        try:
            for folder in ("p0", "tests"):
                if (self.root / folder).is_dir():
                    shutil.copytree(self.root / folder, tree / folder, ignore=shutil.ignore_patterns("__pycache__", "*.updating"))
            for path in manifest["files"]:
                (tree / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.work / "staged" / str(seq) / path, tree / path)
            for path in manifest.get("remove", []):
                (tree / path).unlink(missing_ok=True)
            for path in manifest["files"]:
                if path.endswith(".py"):
                    compile((tree / path).read_bytes(), path, "exec")
            done = subprocess.run([sys.executable, "-B", str(tree / "p0" / "preflight.py"), "--offline", "--tests"],
                                  capture_output=True, text=True, timeout=limit, cwd=str(tree / "p0"),
                                  env=dict(os.environ, SLOT17_UPDATE_TEST="1"))
            if done.returncode == 0 and '"offline_preflight_pass": true' in done.stdout:
                return ""
            lines = [x for x in (done.stdout + done.stderr).splitlines() if x.startswith(("FAIL", "ERROR", "FAILED"))]
            return ("checks failed: " + "; ".join(lines[:3]))[:120] or "checks failed"
        except SyntaxError as exc:
            return f"syntax error in {exc.filename}"[:120]
        except subprocess.TimeoutExpired:
            return f"checks took longer than {limit:.0f} s"
        except Exception as exc:        # noqa: BLE001
            return f"could not test: {type(exc).__name__}"[:120]
        finally:
            shutil.rmtree(tree, ignore_errors=True)

    def apply(self, limit=TEST_LIMIT_S):
        """Finish an interrupted install, or test and install the newest waiting release."""
        journal = self._read("applying.json", None)
        if journal:
            return self._install(journal, resumed=True)
        seq = self.staged()
        if not seq:
            return "nothing waiting"
        manifest = self._read(f"{seq}.ready", None)
        problem = self._staged_problem(seq, manifest) if manifest else "no record of it"
        if problem:
            self._drop(seq)
            self.note("error", f"release {seq} was incomplete on disk ({problem}); it will be fetched again"[:120])
            return "incomplete"
        # A car computer that has never installed a release (a fresh install from the tarball or the image) has
        # no record of its number, so the number alone let an older signed release in (found in review).
        mine, theirs = software(self.root / "p0/version.py"), software(self.work / "staged" / str(seq) / "p0/version.py")
        if mine and theirs and theirs < mine:
            return self._reject(seq, manifest, "older than the installed software "
                                               f"({'.'.join(map(str, theirs))} < {'.'.join(map(str, mine))})")
        previous = self.installed().get("files", [])
        manifest["remove"] = sorted(p for p in previous if p not in manifest["files"] and allowed(p))
        problem = self._test(seq, manifest, limit)
        if problem.startswith(NOT_ITS_FAULT):
            tries = self._read("tries.json", {})
            count = int(tries.get(str(seq), 0)) + 1 if isinstance(tries, dict) else 1
            if count < TRIES:
                self._write("tries.json", {str(seq): count})
                self.note("staged", f"{manifest['commit'][:7]}: {problem}; tried again at the next start "
                                    f"({count} of {TRIES})")
                return "postponed"
            problem += f" ({TRIES} starts running)"
        if problem:
            return self._reject(seq, manifest, problem)
        manifest["existed"] = sorted(p for p in manifest["files"] if (self.root / p).exists())
        # What each file held before, so that only the original is backed up and put back (F34).
        manifest["originals"] = {p: sha256_of(self.root / p) for p in manifest["existed"] + manifest["remove"]
                                 if (self.root / p).is_file()}
        self._write("applying.json", manifest)      # from here a power cut is finished at the next start
        return self._install(manifest)

    def start(self, has_route=None):
        began = time.monotonic()
        if not self.key_file.exists():
            print("update: off (no release key on this machine)", flush=True)
            return
        if self._read("applying.json", None):
            print("update:", self.apply(), flush=True)
            return
        if has_route is None:
            has_route = _default_route()
        if has_route and not self.staged():
            print("update: fetch:", self.fetch(timeout=FETCH_AT_START_S), flush=True)
        left = START_LIMIT_S - (time.monotonic() - began)
        result = self.apply(limit=min(TEST_LIMIT_S, max(5.0, left - 5.0)))
        if result == "nothing waiting" and self._read("status.json", {}).get("state") not in ("rejected", "error"):
            self.note("checked")
        print("update:", result, flush=True)


def _default_route():
    try:
        with open("/proc/net/route", encoding="ascii") as fh:
            return any(line.split()[1] == "00000000" for line in list(fh)[1:] if len(line.split()) > 2)
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("action", choices=("start", "fetch", "status"))
    args = ap.parse_args()
    updater = Updater()
    try:
        if args.action == "start":
            updater.start()
        elif args.action == "fetch":
            print("update: fetch:", updater.fetch(), flush=True)
        else:
            print(json.dumps(dict(updater.status(), waiting=updater.staged()), indent=1))
    except Exception as exc:            # noqa: BLE001 - an update problem must never stop the detector
        print(f"update: failed: {type(exc).__name__}: {exc}"[:300], flush=True)
        try:
            updater.note("error", f"{type(exc).__name__}: {exc}")
        except Exception:               # noqa: BLE001, S110 - already reported on the line above
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
