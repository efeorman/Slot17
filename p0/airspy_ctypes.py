#!/usr/bin/env python3
"""Minimal ctypes bindings for libairspy (airspy.dll).

Why this exists: `airspy_rx.exe` opens the device, streams, and closes, once per
capture. This board's firmware hangs after a stop/start cycle (— the USB engine stays healthy while the firmware's vendor-request handler
stalls, so only removing power recovers it). Keeping one device open and retuning
*while streaming* avoids the stop/start entirely.

Only the calls actually needed are bound. Everything returns AIRSPY_SUCCESS (0) or a
negative error code, and `check()` turns a failure into a readable exception.
"""
import ctypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

DLL_DIR = paths.TOOLS_BIN
DLL_PATH = paths.airspy_library()

# airspy.dll needs libusb-1.0.dll and pthreadVC2.dll from the same folder, and
# Python 3.8+ no longer searches the DLL's own directory automatically.
if hasattr(os, "add_dll_directory") and os.path.isdir(DLL_DIR):
    _dll_dir_handle = os.add_dll_directory(DLL_DIR)

lib = ctypes.CDLL(DLL_PATH)

SUCCESS = 0
ERRORS = {
    0: "SUCCESS", 1: "TRUE", -2: "INVALID_PARAM", -5: "NOT_FOUND",
    -6: "BUSY", -7: "NO_MEM", -1000: "LIBUSB", -1001: "THREAD",
    -1002: "STREAMING_THREAD_ERR", -1003: "STREAMING_STOPPED",
    -9999: "OTHER",
}

# enum airspy_sample_type
SAMPLE_FLOAT32_IQ = 0
SAMPLE_INT16_IQ = 2
BYTES_PER_SAMPLE = {SAMPLE_FLOAT32_IQ: 8, SAMPLE_INT16_IQ: 4}


class AirspyTransfer(ctypes.Structure):
    """airspy_transfer_t, as passed to the sample-block callback."""
    _fields_ = [
        ("device", ctypes.c_void_p),
        ("ctx", ctypes.c_void_p),
        ("samples", ctypes.c_void_p),
        ("sample_count", ctypes.c_int),
        ("dropped_samples", ctypes.c_uint64),
        ("sample_type", ctypes.c_int),
    ]


SAMPLE_BLOCK_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(AirspyTransfer))

lib.airspy_init.restype = ctypes.c_int
lib.airspy_exit.restype = ctypes.c_int
lib.airspy_open.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
lib.airspy_open.restype = ctypes.c_int
lib.airspy_close.argtypes = [ctypes.c_void_p]
lib.airspy_close.restype = ctypes.c_int
lib.airspy_set_samplerate.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
lib.airspy_set_samplerate.restype = ctypes.c_int
lib.airspy_set_sample_type.argtypes = [ctypes.c_void_p, ctypes.c_int]
lib.airspy_set_sample_type.restype = ctypes.c_int
lib.airspy_set_freq.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
lib.airspy_set_freq.restype = ctypes.c_int
lib.airspy_set_lna_gain.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
lib.airspy_set_lna_gain.restype = ctypes.c_int
lib.airspy_set_mixer_gain.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
lib.airspy_set_mixer_gain.restype = ctypes.c_int
lib.airspy_set_vga_gain.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
lib.airspy_set_vga_gain.restype = ctypes.c_int
for _name in ("airspy_set_lna_agc", "airspy_set_mixer_agc"):
    getattr(lib, _name).argtypes = [ctypes.c_void_p, ctypes.c_uint8]
    getattr(lib, _name).restype = ctypes.c_int
lib.airspy_set_packing.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
lib.airspy_set_packing.restype = ctypes.c_int
lib.airspy_set_rf_bias.argtypes = [ctypes.c_void_p, ctypes.c_uint8]
lib.airspy_set_rf_bias.restype = ctypes.c_int
lib.airspy_start_rx.argtypes = [ctypes.c_void_p, SAMPLE_BLOCK_CB, ctypes.c_void_p]
lib.airspy_start_rx.restype = ctypes.c_int
lib.airspy_stop_rx.argtypes = [ctypes.c_void_p]
lib.airspy_stop_rx.restype = ctypes.c_int
lib.airspy_is_streaming.argtypes = [ctypes.c_void_p]
lib.airspy_is_streaming.restype = ctypes.c_int
lib.airspy_board_id_read.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8)]
lib.airspy_board_id_read.restype = ctypes.c_int
lib.airspy_version_string_read.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                           ctypes.c_uint8]
lib.airspy_version_string_read.restype = ctypes.c_int
lib.airspy_get_samplerates.argtypes = [ctypes.c_void_p,
                                       ctypes.POINTER(ctypes.c_uint32),
                                       ctypes.c_uint32]
lib.airspy_get_samplerates.restype = ctypes.c_int


class PartIdSerialNo(ctypes.Structure):
    """airspy_read_partid_serialno_t - board part id and 128-bit serial."""
    _fields_ = [("part_id", ctypes.c_uint32 * 2),
                ("serial_no", ctypes.c_uint32 * 4)]


# Present in every current libairspy, but bound defensively so a stripped build
# cannot stop the diagnostics module importing.
try:
    lib.airspy_board_partid_serialno_read.argtypes = [ctypes.c_void_p,
                                                      ctypes.POINTER(PartIdSerialNo)]
    lib.airspy_board_partid_serialno_read.restype = ctypes.c_int
    _HAVE_SERIAL = True
except AttributeError:
    _HAVE_SERIAL = False


class AirspyError(RuntimeError):
    pass


def check(rc, what):
    if rc != SUCCESS:
        raise AirspyError(f"{what} failed: {ERRORS.get(rc, rc)} ({rc})")
    return rc


def version_string(dev):
    buf = ctypes.create_string_buffer(128)
    check(lib.airspy_version_string_read(dev, buf, 127), "airspy_version_string_read")
    return buf.value.decode("ascii", "replace")


def serial_number(dev):
    """128-bit board serial as a hex string, or None if unavailable."""
    if not _HAVE_SERIAL:
        return None
    ps = PartIdSerialNo()
    if lib.airspy_board_partid_serialno_read(dev, ctypes.byref(ps)) != SUCCESS:
        return None
    return "".join(f"{w:08X}" for w in ps.serial_no)


def samplerates(dev):
    n = ctypes.c_uint32()
    check(lib.airspy_get_samplerates(dev, ctypes.byref(n), 0),
          "airspy_get_samplerates(count)")
    arr = (ctypes.c_uint32 * n.value)()
    check(lib.airspy_get_samplerates(dev, arr, n.value), "airspy_get_samplerates")
    return list(arr)
