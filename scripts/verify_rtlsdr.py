#!/usr/bin/env python3
"""Check p0/rtlsdr_ctypes.py against librtlsdr's own header, rtl-sdr.h: every bound function's arguments and result.

The header is osmocom's (GPL, from https://github.com/osmocom/rtl-sdr include/, or /usr/include with librtlsdr-dev).
<stdint.h> is replaced by a few typedefs, which is all a prototype check needs.

    python scripts/verify_rtlsdr.py --headers /usr/include --cc "python -m ziglang cc"
"""
import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "p0"))
sys.path.insert(0, str(ROOT / "scripts"))
import rtlsdr_ctypes as L  # noqa: E402
import verify_sdrplay as V  # noqa: E402

STDINT = """typedef unsigned char uint8_t; typedef unsigned short uint16_t; typedef unsigned int uint32_t;
typedef signed char int8_t; typedef short int16_t; typedef int int32_t;
"""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--headers", required=True, help="the folder with rtl-sdr.h and rtl-sdr_export.h")
    ap.add_argument("--cc", default="cc")
    ap.add_argument("--target", default="aarch64-linux-gnu")
    args = ap.parse_args(argv)
    if not (Path(args.headers) / "rtl-sdr.h").is_file():
        print(f"no rtl-sdr.h in {args.headers}")
        return 2
    problem = V.compiler_target_problem(args.cc, [args.target])
    if problem:
        print(problem)
        return 2
    try:
        import pycparser  # noqa: F401
    except ImportError:
        print("prototypes: NOT CHECKED, so this check is incomplete (pip install pycparser)")
        return 3
    with tempfile.TemporaryDirectory() as fake:
        (Path(fake) / "stdint.h").write_text(STDINT, encoding="utf-8")
        source = V.preprocess(args.cc, args.headers, args.target, include="rtl-sdr.h",
                              extra=("-nostdinc", "-I", fake, "-Drtlsdr_STATIC"))
    bound = {**L.FUNCTIONS, **L.OPTIONAL}
    problems, n = V.check_prototypes(source, bound)
    print(f"prototypes: {n} functions " + ("agree" if not problems else "DISAGREE"))
    for p in problems:
        print("   ", p)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
