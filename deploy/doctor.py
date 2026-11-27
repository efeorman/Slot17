#!/usr/bin/env python3
"""`bash setup.sh doctor`: runs the setup check, which is p0/doctor.py.

The check itself lives in p0/ since 1.8.0, so signed updates keep it current: they replace p0/, tests/ and the
publisher, never deploy/. The apps' setup check runs it through the publisher, and a car computer set up before
1.3.1 kept a deploy/doctor.py without `--json`, so the apps' check never answered there.
"""
from pathlib import Path
import runpy
import sys

if __name__ == "__main__":
    check = Path(__file__).resolve().parents[1] / "p0" / "doctor.py"
    sys.argv[0] = str(check)
    runpy.run_path(str(check), run_name="__main__")
