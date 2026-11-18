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
  * Before anything is replaced, the complete new tree is tested in a copy. A release that fails
    is rejected and remembered; the installed software stays as it was.
  * The power can be cut at any moment (the ignition does). The replacement is written down
    first and finished at the next start, so a half-replaced tree is never left in use.
  * `start` always exits 0 and has a time limit, so a broken update can never stop the detector.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
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


def allowed(path):
    """Whether a release may replace this path."""
    parts = Path(path).parts
    if not parts or Path(path).is_absolute() or ".." in parts or "\\" in path or path != Path(path).as_posix():
        return False
    if path in NEVER or path.endswith(".local.json") or "__pycache__" in parts:
        return False
    return parts[0] in ("p0", "tests") and len(parts) > 1 or path == "display/display_publisher.py"


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
        return {"seq": seq, "commit": commit, "files": sorted(files)}, files

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
        for path, data in files.items():
            out = target / path
            out.parent.mkdir(parents=True, exist_ok=True)
            with open(out, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        self._write(f"{seq}.ready", manifest)       # only now does it count as waiting
        self.note("staged", f"{manifest['commit'][:7]} installs at the next start")
        return "staged"

    # ---------------------------------------------------------------- installing
    def _copy_into_place(self, journal):
        """Idempotent: run again after a power cut, it ends in the same state."""
        source = self.work / "staged" / str(journal["seq"])
        backup = self.work / f"backup-{journal['seq']}"
        for path in journal["files"]:
            target = self.root / path
            if target.exists() and not (backup / path).exists():
                (backup / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup / path)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".updating")
            with open(source / path, "rb") as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
            os.replace(tmp, target)
        for path in journal.get("remove", []):       # files an earlier release installed and this one dropped
            target = self.root / path
            if target.exists():
                (backup / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(backup / path))
        self._write("installed.json", {"seq": journal["seq"], "commit": journal["commit"], "files": journal["files"],
                                       "boot": self.boot_id()})
        try:
            (self.work / "applying.json").unlink()
        except OSError:
            pass
        shutil.rmtree(source, ignore_errors=True)
        for marker in self.work.glob("*.ready"):
            if marker.stem.isdigit() and int(marker.stem) <= journal["seq"]:
                marker.unlink(missing_ok=True)
        for old in sorted(self.work.glob("backup-*"), key=lambda p: p.stat().st_mtime)[:-3]:
            shutil.rmtree(old, ignore_errors=True)
        self.note("installed", journal["commit"][:7])

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
            self._copy_into_place(journal)
            return "finished an interrupted install"
        seq = self.staged()
        if not seq:
            return "nothing waiting"
        manifest = self._read(f"{seq}.ready", None)
        if not manifest or not all((self.work / "staged" / str(seq) / p).is_file() and allowed(p) for p in manifest["files"]):
            (self.work / f"{seq}.ready").unlink(missing_ok=True)
            self.note("error", f"release {seq} was incomplete on disk; it will be fetched again")
            return "incomplete"
        previous = self.installed().get("files", [])
        manifest["remove"] = sorted(p for p in previous if p not in manifest["files"] and allowed(p))
        problem = self._test(seq, manifest, limit)
        if problem:
            rejected = self._read("rejected.json", {"seqs": []})
            rejected["seqs"] = sorted(set(rejected["seqs"]) | {seq})[-50:]
            self._write("rejected.json", rejected)
            (self.work / f"{seq}.ready").unlink(missing_ok=True)
            shutil.rmtree(self.work / "staged" / str(seq), ignore_errors=True)
            self.note("rejected", f"{manifest['commit'][:7]}: {problem}")
            return "rejected"
        self._write("applying.json", manifest)      # from here a power cut is finished at the next start
        self._copy_into_place(manifest)
        return "installed"

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
