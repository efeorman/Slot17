"""Run one local, hardware-free drive through the production supervisor."""
import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

from .score import EXIT, TRANSFER, score

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
P0 = ROOT / "p0"


def dump(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def digest(path):
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def render(scene, seed, path):
    # Import numerical libraries only after the idle guard has been obtained.
    import numpy as np
    from scipy.signal import resample_poly
    import tetra_gen

    fs = scene["sample_rate"]
    size = scene["stall_after_samples"]
    if type(size) is not int or fs != 6_000_000 or not 0 < size <= 60 * fs or size % TRANSFER:
        raise ValueError("smoke input needs 6 MS/s and up to 60 seconds of whole transfers")
    if not 0 < scene["tolerance_samples"] < 85_000 // 2:
        raise ValueError("onset tolerance must be less than half a slot")
    labels, bursts = [], []
    for row in scene["bursts"]:
        ch, slot, positive = row["channel"], row["slot"], row["training"]
        if type(slot) is not int or type(ch) is not int or not 0 <= ch < 200 or type(positive) is not bool:
            raise ValueError("invalid channel, slot or training flag")
        first = slot * 85_000
        # Fixed payloads make the reviewed controls repeatable across noise seeds.
        base = tetra_gen.burst("cub", 10, np.random.default_rng(row["payload_seed"]), training=positive)
        # Independently read the central 15 differential symbols after matched filtering.
        shaped = np.convolve(base, tetra_gen.rrc_taps(10), mode="same")
        symbols = shaped[::10]
        phase = np.angle(symbols[48:63] * np.conj(symbols[47:62]))
        quadrant = np.floor((phase % (2 * np.pi)) / (np.pi / 2)).astype(int)
        word = "".join(("00", "01", "11", "10")[q] for q in quadrant)
        if (word == "100111010000111010011101000011") != positive:
            raise ValueError(f"source word disagrees with reviewed CUB label {row['id']}")
        wave = resample_poly(base, 100, 3)
        end = first + len(wave)
        if first < 0 or end > size:
            raise ValueError("burst extends outside the scene")
        rf = (380_012_500 + ch * 25_000) * (1 + scene["receiver_ppm"] / 1e6)
        phase = 2 * np.pi * (rf - scene["centre_hz"]) * np.arange(len(wave)) / fs
        noise = scene["noise_rms_per_component"]
        rms = noise * math.sqrt(2 * tetra_gen.snr_bandwidth_hz() / fs * 10 ** (row["snr_db"] / 10))
        wave *= rms / np.sqrt(np.mean(abs(wave) ** 2))
        bursts.append((first, end, wave * np.exp(1j * phase)))
        labels.append(dict(id=row["id"], channel=ch, positive=positive, first_sample=first, stop_sample=end))
    if (len({lab["id"] for lab in labels}) != len(labels)
            or {lab["positive"] for lab in labels} != {True, False}):
        raise ValueError("scene needs unique IDs and both positive and negative controls")
    rng = np.random.default_rng(seed)
    with path.open("xb") as fh:
        for offset in range(0, size, TRANSFER):
            iq = rng.standard_normal((TRANSFER, 2), dtype=np.float32) * scene["noise_rms_per_component"]
            for first, end, wave in bursts:
                a, b = max(first, offset), min(end, offset + TRANSFER)
                if a < b:
                    block = wave[a-first:b-first]
                    iq[a-offset:b-offset, 0] += block.real
                    iq[a-offset:b-offset, 1] += block.imag
            np.clip(np.rint(iq), -32768, 32767).astype("<i2").tofile(fh)
    if path.stat().st_size != size * 4:
        raise OSError("incomplete IQ file")
    return labels


def child_environment(out, library, boot):
    home = out / "home"
    config = home / ".config"
    config.mkdir(parents=True)
    tmp = out / "tmp"
    tmp.mkdir()
    settings = config / "slot17-receiver.json"
    dump(settings, {"kind": "airspy"})
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(
        ("SLOT17_", "PUSHOVER_", "PYTHON", "NOTIFY_SOCKET", "WATCHDOG_", "XDG_"))}
    env.update(HOME=str(home), USERPROFILE=str(home), TMPDIR=str(tmp), TMP=str(tmp), TEMP=str(tmp),
               PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", PYTHONUNBUFFERED="1",
               SLOT17_ROOT=str(out), SLOT17_STATE_DIR=str(out / "state"), SLOT17_RECEIVER_SETTINGS=str(settings),
               SLOT17_AIRSPY_LIB=str(library), SLOT17_SIM_IQ=str(out / "scene.iq"),
               SLOT17_SIM_CONTROL=str(out / "control.json"), SLOT17_SIM_STATE=str(out / "board.json"),
               SLOT17_SIM_BOOT=boot, SLOT17_SIM_LEDGER=str(out / "ledger.jsonl"), SLOT17_SIM_LEDGER_EVERY="1")
    return env


def identify_fake(out, env):
    """Prove the injected Init and ledger work before letting production call Open."""
    control = out / "probe-control.json"
    ledger = out / "probe-ledger.jsonl"
    dump(control, {"fail": {"call": "airspy_init", "rc": -1000}})
    # In libairspy 1.0.10 Init is a deprecated no-op. This probe never calls Open.
    probe = "import ctypes,os,sys; lib=ctypes.CDLL(os.environ['SLOT17_AIRSPY_LIB']); sys.exit(lib.airspy_init()!=-1000)"
    done = subprocess.run([sys.executable, "-B", "-c", probe], cwd=out,
                          env=dict(env, SLOT17_SIM_CONTROL=str(control), SLOT17_SIM_LEDGER=str(ledger)),
                          capture_output=True, timeout=10)
    errors = []
    rows = read_rows(ledger, errors)
    if (done.returncode or errors or [(r.get("event"), r.get("rc")) for r in rows] !=
            [("fail", -1000), ("init", -1000)] or rows[0].get("call") != "airspy_init"):
        raise ValueError("native stand-in identity check failed before receiver open")


@contextmanager
def process_tree(cmd, out, env, log):
    """Keep the supervisor and its child together even on interrupted runs."""
    if os.name == "nt":
        import ctypes as C
        from ctypes import wintypes as W
        import psutil

        class Limits(C.Structure):
            _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64), ("flags", W.DWORD),
                        ("min_ws", C.c_size_t), ("max_ws", C.c_size_t), ("count", W.DWORD),
                        ("affinity", C.c_size_t), ("priority", W.DWORD), ("scheduling", W.DWORD)]

        class Extended(C.Structure):
            _fields_ = [("basic", Limits), ("io", C.c_uint64 * 6), ("memory", C.c_size_t * 4)]

        kernel = C.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [W.LPVOID, W.LPCWSTR]
        kernel.CreateJobObjectW.restype = W.HANDLE
        kernel.SetInformationJobObject.argtypes = [W.HANDLE, C.c_int, W.LPVOID, W.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
        kernel.CloseHandle.argtypes = [W.HANDLE]
        job = kernel.CreateJobObjectW(None, None)
        if not job:
            raise C.WinError(C.get_last_error())
        try:
            info = Extended()
            info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(job, 9, C.byref(info), C.sizeof(info)):
                raise C.WinError(C.get_last_error())
            # CREATE_SUSPENDED closes the race between assignment and launching watch.py.
            with subprocess.Popen(cmd, cwd=out, env=env, stdout=log, stderr=subprocess.STDOUT,
                                  creationflags=0x4) as proc:
                try:
                    if not kernel.AssignProcessToJobObject(job, int(proc._handle)):
                        raise C.WinError(C.get_last_error())
                    psutil.Process(proc.pid).resume()
                    yield proc
                finally:
                    kernel.CloseHandle(job)
                    job = None
                    if proc.poll() is None:
                        proc.kill()
        finally:
            if job:
                kernel.CloseHandle(job)
    else:
        with subprocess.Popen(cmd, cwd=out, env=env, stdout=log, stderr=subprocess.STDOUT,
                              start_new_session=True) as proc:
            try:
                yield proc
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    until = time.monotonic() + 45
                    while time.monotonic() < until:
                        proc.poll()
                        os.killpg(proc.pid, 0)
                        time.sleep(0.1)
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass


def observe(out, cmd, env, report, timeout):
    hb = out / "heartbeat.json"
    with ExitStack() as stack:
        listener = None
        if sys.platform.startswith("linux"):
            listener = stack.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM))
            name = "slot17-sim-" + uuid.uuid4().hex
            listener.bind("\0" + name)
            listener.setblocking(False)
            env["NOTIFY_SOCKET"] = "@" + name
        log = stack.enter_context((out / "process.log").open("wb"))
        history = stack.enter_context((out / "heartbeats.jsonl").open("w", encoding="utf-8"))
        proc = stack.enter_context(process_tree(cmd, out, env, log))
        report["supervisor_pid"] = proc.pid
        end = time.monotonic() + timeout
        while True:
            rc = proc.poll()
            try:
                row = json.loads(hb.read_bytes())
                if not report["heartbeats"] or row != report["heartbeats"][-1]:
                    report["heartbeats"].append(row)
                    history.write(json.dumps(row) + "\n")
                    history.flush()
            except (OSError, ValueError):
                pass  # Atomic replacement can race this read; retry on the next poll.
            if listener is not None:
                while True:
                    try:
                        msg = listener.recv(4096).decode()
                    except BlockingIOError:
                        break
                    report["notify"].append({"t_mono": time.monotonic(), "message": msg})
            if rc is not None:
                report["supervisor_rc"] = rc
                break
            if time.monotonic() >= end:
                report["observation_errors"].append("runner_timeout")
                break
            time.sleep(0.05)
    report["supervisor_rc"] = proc.returncode


def read_rows(path, errors):
    rows = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("expected JSON object")
                rows.append(value)
    except (ValueError, OSError) as exc:
        errors.append(f"{path.name}: {exc}")
    return rows


def drive(scene, seed, pace, out):
    library = HERE / "native/build" / ("airspy_fake.dll" if os.name == "nt" else "libairspy_fake.so")
    if not library.is_file():
        raise ValueError("build the native stand-in using python sim/native/build.py first")
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError("choose an empty or new run directory")
    # Exclusive creation also keeps two simultaneous launches from sharing this run.
    source = out / "p0"
    source.mkdir()
    for f in [*P0.glob("*.py"), P0 / "calibration.json", P0 / "downlink_channels.json"]:
        shutil.copyfile(f, source / f.name)
    boot = uuid.uuid4().hex
    # Use one private copy for the identity check and the service, even if another
    # process rebuilds sim/native/build between the two.
    private_lib = out / library.name
    shutil.copyfile(library, private_lib)
    env = child_environment(out, private_lib, boot)
    identify_fake(out, env)
    labels = render(scene, seed, out / "scene.iq")
    dump(out / "labels.json", labels)
    dump(out / "control.json", {"stall_after_samples": scene["stall_after_samples"], "pace": pace})
    cmd = [sys.executable, "-B", str(source / "supervise.py"), "--no-restart", "--no-notify",
           "--heartbeat", str(out / "heartbeat.json"), "--log", str(out / "supervise.log"),
           "--", "--band", "uplink", "--sensitivity", "highway", "--save-power", "--no-notify"]
    report = dict(schema=1, scene=scene, seed=seed, labels=labels, pace=pace, boot=boot, command=cmd,
                  iq_sha256=digest(out / "scene.iq"), library_sha256=digest(private_lib), platform=sys.platform,
                  events=[], ledger=[], heartbeats=[], notify=[], observation_errors=[], summary={},
                  supervisor_rc=None, supervisor_outcome=None, supervisor_ready=False)
    dump(out / "run.json", report)
    try:
        observe(out, cmd, env, report, 100 + scene["stall_after_samples"] / scene["sample_rate"])
    except (OSError, KeyboardInterrupt) as exc:
        report["observation_errors"].append(f"launcher: {type(exc).__name__}: {exc}")
    errors = report["observation_errors"]
    report["ledger"] = read_rows(out / "ledger.jsonl", errors)
    sessions = list((out / "captures").glob("*/session.json"))
    # Linux watch creates a corrected-time symlink to the same session.
    sessions = [p for p in sessions if not p.parent.is_symlink()]
    try:
        if len(sessions) != 1:
            raise ValueError(f"expected one session, found {len(sessions)}")
        session = sessions[0].parent
        report["events"] = read_rows(session / "events.jsonl", errors)
        report["summary"] = json.loads((session / "summary.json").read_bytes())
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
    try:
        log = (out / "supervise.log").read_text(encoding="utf-8")
        report["supervisor_launches"] = len(re.findall(r"  launch #\d+: ", log))
        outcome = re.search(r"watch\.py ([a-z ]+); --no-restart:", log)
        if outcome:
            report["supervisor_outcome"] = outcome.group(1)
        elif "watch.py exited cleanly" in log:
            report["supervisor_outcome"] = "clean"
        report["supervisor_ready"] = (any("READY=1" in x["message"].splitlines() for x in report["notify"])
                                      if sys.platform.startswith("linux") else "READY: checked current child" in log)
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
    try:
        report["board"] = json.loads((out / "board.json").read_bytes())
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
    report["score"] = score(report)
    dump(out / "run.json", report)
    return report["score"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scene", help="scene JSON path or built-in name")
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--pace", choices=["realtime", "fast"], default="realtime")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--car-root", type=Path, help="reserve the real installation's idle lock for an idle Pi run")
    args = parser.parse_args()
    path = Path(args.scene)
    if not path.is_file():
        path = HERE / "scenes" / (args.scene + ".json")
    sys.path.insert(0, str(P0))
    with ExitStack() as stack:
        try:
            if os.name == "posix":
                def stop(_signum, _frame):
                    raise KeyboardInterrupt
                previous = signal.signal(signal.SIGTERM, stop)
                stack.callback(signal.signal, signal.SIGTERM, previous)
            model = Path("/proc/device-tree/model")
            pi = sys.platform.startswith("linux") and model.is_file() and b"Raspberry Pi" in model.read_bytes()
            if pi and args.car_root is None:
                parser.error("on a Pi provide --car-root pointing to the actual idle Slot17 installation")
            if args.car_root:
                from instance import acquire_idle
                if not (args.car_root / "p0/watch.py").is_file():
                    parser.error("--car-root must contain the installed p0/watch.py")
                stack.enter_context(acquire_idle(args.car_root.resolve()))
            scene = json.loads(path.read_bytes())
            if not isinstance(scene, dict) or not isinstance(scene.get("bursts"), list):
                raise ValueError("expected a scene object with a list of bursts")
            result = drive(scene, args.seed, args.pace, args.out.resolve())
        except KeyError as exc:
            parser.exit(2, f"missing scene field: {exc}\n")
        except (ValueError, OSError, RuntimeError) as exc:
            parser.exit(2, f"{exc}\n")
    print(json.dumps(result, indent=2))
    return EXIT[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())

