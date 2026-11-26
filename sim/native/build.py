#!/usr/bin/env python3
"""Build sim/native's stand-in receivers with zig's C compiler (python -m ziglang cc), warnings as errors.

    python sim/native/build.py [OUTDIR]          for this computer (Windows: .dll, Linux: .so)
    python sim/native/build.py OUTDIR --target aarch64-linux-gnu

OUTDIR defaults to sim/native/build/ (ignored by git). Prints the path of each library it made.
"""
import argparse
from pathlib import Path
import platform
import subprocess
import sys

HERE = Path(__file__).resolve().parent


def native_target():
    machine = platform.machine().lower()
    cpu = {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}.get(machine, machine)
    return f"{cpu}-windows-gnu" if sys.platform == "win32" else f"{cpu}-linux-gnu"


def build(out_dir, target=None, cc=None):
    target = target or native_target()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    windows = "windows" in target
    out = out_dir / ("airspy_fake.dll" if windows else "libairspy_fake.so")
    cmd = (cc or [sys.executable, "-m", "ziglang", "cc"]) + [
        "-O2", "-shared", "-std=c11", "-Wall", "-Wextra", "-Werror", "-target", target,
        "-I", str(HERE), str(HERE / "airspy_fake.c"), "-o", str(out)]
    if not windows:
        cmd[cmd.index("-shared") + 1:cmd.index("-shared") + 1] = ["-fPIC", "-pthread", "-D_POSIX_C_SOURCE=200809L"]
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        raise SystemExit(f"build failed ({' '.join(cmd)}):\n{done.stdout}{done.stderr}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out", nargs="?", default=str(HERE / "build"))
    ap.add_argument("--target", help="a zig target, for example aarch64-linux-gnu (default: this computer)")
    args = ap.parse_args(argv)
    print(build(args.out, args.target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
