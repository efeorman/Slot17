#!/usr/bin/env python3
"""Where things live, resolved at run time rather than hard-coded.

The project has to run on the Windows bench machine and on the Raspberry Pi in
the car from the same source. Everything is derived from this file's own
location, and the Airspy library is looked up by platform, so no path needs
editing when the code moves.

Override any of it with environment variables: SLOT17_ROOT, SLOT17_IQ_DIR,
SLOT17_PLOT_DIR, SLOT17_AIRSPY_LIB.
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("SLOT17_ROOT", os.path.dirname(HERE))

CAPTURES = os.path.join(ROOT, "captures")
IQ_DIR = os.environ.get("SLOT17_IQ_DIR", os.path.join(CAPTURES, "iq"))
PLOT_DIR = os.environ.get("SLOT17_PLOT_DIR", os.path.join(CAPTURES, "plots"))
TOOLS_BIN = os.path.join(ROOT, "tools", "bin")

_EXE = ".exe" if sys.platform == "win32" else ""


def _tool(name):
    """A bundled tool on Windows; elsewhere the system's copy, for example from the airspy package."""
    bundled = os.path.join(TOOLS_BIN, name + _EXE)
    if sys.platform == "win32" or os.path.isfile(bundled):
        return bundled
    return shutil.which(name) or bundled


AIRSPY_RX = _tool("airspy_rx")
AIRSPY_INFO = _tool("airspy_info")
RTL_TEST = os.path.join(TOOLS_BIN, "rtl_test" + _EXE)


def airspy_library():
    """Path to libairspy for ctypes, per platform.

    On Linux the distribution package (libairspy0 / libairspy-dev) puts it on
    the normal loader path, so a bare soname is correct and portable across
    architectures; on Windows we ship the DLL ourselves.
    """
    override = os.environ.get("SLOT17_AIRSPY_LIB")
    if override:
        return override
    if sys.platform == "win32":
        return os.path.join(TOOLS_BIN, "airspy.dll")
    if sys.platform == "darwin":
        return "libairspy.dylib"
    return "libairspy.so.0"


def libusb_library():
    if sys.platform == "win32":
        return os.path.join(TOOLS_BIN, "libusb-1.0.dll")
    return "libusb-1.0.so.0"


def ensure_dirs():
    for d in (IQ_DIR, PLOT_DIR):
        os.makedirs(d, exist_ok=True)
