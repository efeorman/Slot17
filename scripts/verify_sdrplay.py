#!/usr/bin/env python3
"""Check p0/sdrplay_ctypes.py against the SDRplay API's own header files, without any SDRplay hardware.

1. Layout: a C file with a compile-time assertion for the size of every structure and the offset of every
   field, exactly as ctypes lays them out, is compiled against SDRplay's headers for each target (64-bit ARM
   Linux, the Raspberry Pi, and 64-bit x86 Linux). It compiles only if every number agrees.
2. Prototypes: the headers' function declarations, parsed with pycparser, against the argument and return
   types bound in sdrplay_ctypes.FUNCTIONS.
3. Optional (--fake-dll, on Windows): scripts/sdrplay_fake.c built against the same headers into a DLL that
   behaves like the API (a streaming thread, events), and the real receiver code run against it through ctypes;
   see that file.

The headers come with SDRplay's API (installed: /usr/local/include; or unpacked from SDRplay's installer). They
are SDRplay's and are not part of this repository.

    python scripts/verify_sdrplay.py --headers /usr/local/include
    python scripts/verify_sdrplay.py --headers DIR --cc "python -m ziglang cc" --targets aarch64-linux-gnu x86_64-linux-gnu
"""
import argparse
import ctypes
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "p0"))
import sdrplay_ctypes as S  # noqa: E402

# The checks are made for 64-bit targets: the offsets below come from this (64-bit) Python's ctypes.
TARGETS = ("aarch64-linux-gnu", "x86_64-linux-gnu")


def layout_source():
    lines = ['#include <stddef.h>', '#include "sdrplay_api.h"', ""]
    n = 0
    for cname, T in S.STRUCTS.items():
        lines.append(f'_Static_assert(sizeof({cname}) == {ctypes.sizeof(T)}, "size of {cname}");')
        n += 1
        for field, _ in T._fields_:
            offset = getattr(T, field).offset
            lines.append(f'_Static_assert(offsetof({cname}, {field}) == {offset}, "{cname}.{field} at {offset}");')
            n += 1
    lines.append("int slot17_layout_checked(void) { return 0; }")
    return "\n".join(lines) + "\n", n


def compiler(cc, target, tmp):
    """The command line and environment for one target; zig gets its own caches inside `tmp`."""
    cmd = shlex.split(cc, posix=(os.name != "nt"))
    env = dict(os.environ)
    if "zig" in cc:
        cmd += ["-target", target]
        env["ZIG_LOCAL_CACHE_DIR"] = str(Path(tmp) / "zig-local")
        env["ZIG_GLOBAL_CACHE_DIR"] = str(Path(tmp) / "zig-global")
    return cmd, env


def compile_for(cc, headers, target, source):
    """(verdict, output): "agree", "DISAGREE" (an assertion failed) or "TOOL ERROR" (nothing was checked)."""
    with tempfile.TemporaryDirectory() as tmp:
        c = Path(tmp) / "layout.c"
        c.write_text(source, encoding="utf-8")
        cmd, env = compiler(cc, target, tmp)
        r = subprocess.run(cmd + ["-std=c11", "-c", "-I", str(headers), str(c), "-o", str(Path(tmp) / "layout.o")],
                           capture_output=True, text=True, env=env)
        out = (r.stdout + r.stderr).strip()
        if r.returncode == 0:
            return "agree", out
        return ("DISAGREE" if "static" in out.lower() and "assert" in out.lower() else "TOOL ERROR"), out


def preprocess(cc, headers, target, include="sdrplay_api.h", extra=()):
    with tempfile.TemporaryDirectory() as tmp:
        c = Path(tmp) / "h.c"
        c.write_text(f'#include "{include}"\n', encoding="utf-8")
        cmd, env = compiler(cc, target, tmp)
        r = subprocess.run(cmd + ["-E", "-P", *extra, "-I", str(headers), str(c)], capture_output=True, text=True,
                           check=True, env=env)
        return r.stdout


def kind_of(ctype):
    """A ctypes type reduced to what decides how it is passed: pointer, int of a size, float, double."""
    if ctype is None:
        return "void"
    if ctype in (ctypes.c_char_p, ctypes.c_void_p) or hasattr(ctype, "_type_") and issubclass(ctype, ctypes._Pointer):
        return "pointer"
    if isinstance(ctype, type) and issubclass(ctype, ctypes._CFuncPtr):       # a callback: a function pointer
        return "pointer"
    if ctype is ctypes.c_float:
        return "float"
    if ctype is ctypes.c_double:
        return "double"
    return f"int{ctypes.sizeof(ctype) * 8}"


UNSIGNED_CTYPES = {ctypes.c_ubyte, ctypes.c_ushort, ctypes.c_uint, ctypes.c_ulong, ctypes.c_ulonglong}


def signed_of(ctype):
    """For a result: True for a signed int, False for an unsigned one, None for anything else. A result bound
    unsigned where C returns a negative error code would never read as negative (found in review, 24 November
    2026: until then signedness wasn't compared)."""
    if ctype is None or kind_of(ctype) in ("void", "pointer", "float", "double"):
        return None
    return ctype not in UNSIGNED_CTYPES


def c_signed(decl, typedefs):
    """The same for a pycparser declaration: True, False or None."""
    from pycparser import c_ast
    node = decl.type if isinstance(decl, c_ast.Decl) else decl
    if isinstance(node, c_ast.Typename):
        node = node.type
    if isinstance(node, c_ast.TypeDecl):
        t = node.type
        if isinstance(t, c_ast.Enum):
            return True
        if isinstance(t, c_ast.IdentifierType):
            names = " ".join(t.names)
            if names in typedefs:
                return c_signed(typedefs[names], typedefs)
            if names in ("void", "float", "double"):
                return None
            return "unsigned" not in names
    return None


def c_kind(decl, typedefs):
    """The same reduction for a pycparser declaration node."""
    from pycparser import c_ast
    node = decl.type if isinstance(decl, c_ast.Decl) else decl
    if isinstance(node, c_ast.Typename):
        node = node.type
    if isinstance(node, (c_ast.PtrDecl, c_ast.ArrayDecl)):
        return "pointer"
    if isinstance(node, c_ast.TypeDecl):
        t = node.type
        if isinstance(t, c_ast.IdentifierType):
            names = " ".join(t.names)
            if names in typedefs:
                return c_kind(typedefs[names], typedefs)
            if names == "void":
                return "void"
            if names == "float":
                return "float"
            if names == "double":
                return "double"
            if "long long" in names:
                return "int64"
            if names in ("int", "unsigned int", "signed int", "unsigned"):
                return "int32"
            if "char" in names:
                return "int8"
            if "short" in names:
                return "int16"
            return f"?{names}"
        if isinstance(t, c_ast.Enum):
            return "int32"
        if isinstance(t, (c_ast.Struct, c_ast.Union)):
            return "struct"
    return f"?{type(node).__name__}"


def check_prototypes(source, functions=None):
    functions_bound = S.FUNCTIONS if functions is None else functions
    from pycparser import c_ast, c_parser
    ast = c_parser.CParser().parse(source)
    typedefs, functions = {}, {}
    for ext in ast.ext:
        if isinstance(ext, c_ast.Typedef):
            typedefs[ext.name] = ext.type
        elif isinstance(ext, c_ast.Decl) and isinstance(ext.type, c_ast.FuncDecl):
            functions[ext.name] = ext.type
    problems = []
    for name, (restype, argtypes) in functions_bound.items():
        f = functions.get(name)
        if f is None:
            problems.append(f"{name}: not declared in the headers")
            continue
        params = [] if f.args is None else [p for p in f.args.params]
        if len(params) == 1 and c_kind(params[0], typedefs) == "void":
            params = []
        want = [kind_of(a) for a in argtypes]
        have = [c_kind(p, typedefs) for p in params]
        if want != have:
            problems.append(f"{name}: arguments {have} in the headers, {want} bound")
        ret_have, ret_want = c_kind(f.type, typedefs), kind_of(restype)
        if ret_have != ret_want:
            problems.append(f"{name}: returns {ret_have} in the headers, {ret_want} bound")
        elif c_signed(f.type, typedefs) != signed_of(restype):
            signs = {True: "signed", False: "unsigned", None: "neither"}
            problems.append(f"{name}: returns {signs[c_signed(f.type, typedefs)]} {ret_have} in the headers, "
                            f"{signs[signed_of(restype)]} bound")
    return problems, len(functions_bound)


def compiler_target_problem(cc, targets):
    """Why these targets can't be checked with this compiler, or None. Only zig is given a target (-target); any
    other compiler builds for its own, so it can check one target only, the one it builds for (found in review:
    a native compiler used to be run once per target, and each run was reported as that target's)."""
    if "zig" in cc:
        return None
    if len(targets) != 1:
        return (f"{cc} builds for its own target only: give the one it builds for (--targets ...), or use "
                '"python -m ziglang cc" to check several')
    try:
        machine = subprocess.run(shlex.split(cc, posix=(os.name != "nt")) + ["-dumpmachine"], capture_output=True,
                                 text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{cc} can't say what it builds for ({exc})"
    if target_mismatch(machine, targets[0]):
        return f"{cc} builds for {machine or 'an unknown target'}, not {targets[0]}"
    return None


def target_mismatch(machine, target):
    """Whether a compiler's -dumpmachine names another processor or another system than `target` (cpu-os-abi).
    The processor alone let a Windows x86_64 compiler pass for x86_64 Linux (the second review, 24 November
    2026): the layouts it checks then were Windows's."""
    have, want = machine.lower().split("-"), target.lower().split("-")
    if not machine or have[0] != want[0]:
        return True
    return len(want) > 1 and want[1] not in have[1:]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--headers", required=True, help="the folder with sdrplay_api.h")
    ap.add_argument("--cc", default="cc", help='a C compiler, e.g. "python -m ziglang cc" or "gcc"')
    ap.add_argument("--targets", nargs="*", default=list(TARGETS))
    args = ap.parse_args(argv)
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        print("run this with a 64-bit Python: the offsets it checks are 64-bit ones")
        return 2
    headers = Path(args.headers)
    if not (headers / "sdrplay_api.h").is_file():
        print(f"no sdrplay_api.h in {headers}")
        return 2
    problem = compiler_target_problem(args.cc, args.targets)
    if problem:
        print(problem)
        return 2
    failed = False
    source, count = layout_source()
    for target in args.targets:
        verdict, out = compile_for(args.cc, headers, target, source)
        print(f"layout, {target}: {count} sizes and offsets {verdict}")
        if verdict != "agree":
            print(out[-3000:])
            failed = True
    try:
        problems, n = check_prototypes(preprocess(args.cc, headers, args.targets[0]))
    except ImportError:
        print("prototypes: NOT CHECKED, so this check is incomplete (pip install pycparser)")
        return 3
    print(f"prototypes: {n} functions " + ("agree" if not problems else "DISAGREE"))
    for p in problems:
        print("   ", p)
    failed |= bool(problems)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
