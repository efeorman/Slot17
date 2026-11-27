#!/usr/bin/env python3
"""ctypes bindings for librtlsdr (osmocom rtl-sdr 2.0; Debian's librtlsdr0): only what Slot17 uses.

Written from rtl-sdr.h and checked against it by scripts/verify_rtlsdr.py (every bound prototype). The library is
loaded by load(), not at import, so this module can be imported and tested without it.

Two habits of librtlsdr matter here:
  * rtlsdr_set_freq_correction returns -2 when the correction is already that value (a new stick starts at 0),
    which is not a failure: see set_ppm().
  * rtlsdr_read_async blocks the calling thread until rtlsdr_cancel_async, and calls back on that same thread.
"""
import ctypes
import os
import sys
from ctypes import CFUNCTYPE, POINTER, c_char_p, c_int, c_uint32, c_void_p

# void (*rtlsdr_read_async_cb_t)(unsigned char *buf, uint32_t len, void *ctx)
ReadCallback = CFUNCTYPE(None, c_void_p, c_uint32, c_void_p)
USB_STRING = 256                    # the size librtlsdr writes into each of manufacturer, product, serial
TUNERS = {0: "unknown", 1: "E4000", 2: "FC0012", 3: "FC0013", 4: "FC2580", 5: "R820T", 6: "R828D"}
DEFAULT_BUF_LEN = 16 * 32 * 512      # librtlsdr's own default: 262144 bytes, 131072 samples

FUNCTIONS = {
    "rtlsdr_get_device_count": (c_uint32, []),
    "rtlsdr_get_device_usb_strings": (c_int, [c_uint32, c_char_p, c_char_p, c_char_p]),
    "rtlsdr_open": (c_int, [POINTER(c_void_p), c_uint32]),
    "rtlsdr_close": (c_int, [c_void_p]),
    "rtlsdr_set_sample_rate": (c_int, [c_void_p, c_uint32]),
    "rtlsdr_get_sample_rate": (c_uint32, [c_void_p]),
    "rtlsdr_set_center_freq": (c_int, [c_void_p, c_uint32]),
    "rtlsdr_get_center_freq": (c_uint32, [c_void_p]),
    "rtlsdr_set_freq_correction": (c_int, [c_void_p, c_int]),
    "rtlsdr_get_tuner_type": (c_int, [c_void_p]),
    "rtlsdr_get_tuner_gains": (c_int, [c_void_p, POINTER(c_int)]),
    "rtlsdr_set_tuner_gain_mode": (c_int, [c_void_p, c_int]),
    "rtlsdr_set_tuner_gain": (c_int, [c_void_p, c_int]),
    "rtlsdr_get_tuner_gain": (c_int, [c_void_p]),
    "rtlsdr_set_agc_mode": (c_int, [c_void_p, c_int]),
    "rtlsdr_reset_buffer": (c_int, [c_void_p]),
    "rtlsdr_read_async": (c_int, [c_void_p, ReadCallback, c_void_p, c_uint32, c_uint32]),
    "rtlsdr_cancel_async": (c_int, [c_void_p]),
}
OPTIONAL = {"rtlsdr_set_bias_tee": (c_int, [c_void_p, c_int])}    # not in every librtlsdr


class RtlError(RuntimeError):
    pass


# RTL-SDR Blog's own driver, built by deploy/install-rtlsdr.sh for a V4L, or a V4 on a librtlsdr older than 2.0.0
# (found in review, 26 November 2026). Loaded in place of the system's when it is there.
BLOG_LIBRARIES = ("/usr/local/lib/librtlsdr.so.0", "/usr/local/lib/aarch64-linux-gnu/librtlsdr.so.0",
                  "/usr/local/lib/x86_64-linux-gnu/librtlsdr.so.0")
BLOG_MARK = "/usr/local/share/slot17/rtl-sdr-blog.commit"


def library_path():
    override = os.environ.get("SLOT17_RTLSDR_LIB")
    if override:
        return override
    if sys.platform == "win32":
        return "rtlsdr.dll"
    if os.path.exists(BLOG_MARK):
        built = next((p for p in BLOG_LIBRARIES if os.path.exists(p)), None)
        if built:
            return built
    return "librtlsdr.so.0"


def load(path=None):
    lib = ctypes.CDLL(path or library_path())
    for name, (restype, argtypes) in FUNCTIONS.items():
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = restype, argtypes
    for name, (restype, argtypes) in OPTIONAL.items():
        fn = getattr(lib, name, None)
        if fn is not None:
            fn.restype, fn.argtypes = restype, argtypes
    return lib


def check(rc, what):
    if rc < 0:
        raise RtlError(f"{what} failed ({rc})")
    return rc


def usb_strings(lib, index):
    """(manufacturer, product, serial) of device `index`, without opening it."""
    bufs = [ctypes.create_string_buffer(USB_STRING) for _ in range(3)]
    check(lib.rtlsdr_get_device_usb_strings(index, *bufs), f"rtlsdr_get_device_usb_strings({index})")
    return tuple(b.value.decode("ascii", "replace") for b in bufs)


def set_ppm(lib, dev, ppm):
    """The frequency (and sample-rate) correction; -2 means it already was that, which is fine."""
    rc = lib.rtlsdr_set_freq_correction(dev, int(ppm))
    if rc < 0 and rc != -2:
        raise RtlError(f"rtlsdr_set_freq_correction({ppm}) failed ({rc})")


def tuner_gains(lib, dev):
    """The gains this tuner offers, in tenths of a dB."""
    n = lib.rtlsdr_get_tuner_gains(dev, None)
    if n <= 0:
        return []
    gains = (c_int * n)()
    n = lib.rtlsdr_get_tuner_gains(dev, gains)
    return list(gains[:max(n, 0)])
