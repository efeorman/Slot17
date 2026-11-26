#!/usr/bin/env python3
"""The Airspy Mini through libairspy (airspy_ctypes.py): the detector's receiver from the start.

Moved here from watch.py without changing what it does: the same calls in the same order, the same
settings, the same callback, and the same careful ending. This board races close/free_transfers after
streaming, so a session that streamed leaves the final device cleanup to process exit; a session that
opened but never streamed closes and exits. tests/test_runtime.py (written before the move) and
tests/test_receivers.py hold it to that.
"""
import ctypes
import time

import numpy as np

import diagnostics

BYTES_PER_SAMPLE = 4    # int16 I + int16 Q


def make_callback(sink):
    """libairspy's block callback, in the order watch.py always used: start time, lost samples, then data."""
    def _cb(tp):
        t = tp.contents
        try:
            n = t.sample_count
            sink.first(n)
            if t.dropped_samples:
                sink.drop(t.dropped_samples)
            buf = ctypes.string_at(t.samples, n * BYTES_PER_SAMPLE)
            sink.push(np.frombuffer(buf, dtype=np.int16))
        except Exception:
            # These samples are gone: mark the gap, like any other drop, so no window is treated as
            # continuous across it. Nothing may escape into the driver's thread.
            sink.lost(lambda: t.sample_count)
        return 0
    import airspy_ctypes as A
    return A.SAMPLE_BLOCK_CB(_cb)


class Receiver:
    """One Airspy session: open, configure, stream, and end it once."""

    def __init__(self, kind="airspy", fs=6_000_000, centre_hz=382.5e6, gain=None, sink=None):
        import airspy_ctypes as A            # loads libairspy now, where watch.py always loaded it
        g = gain or {}
        self.A = A
        self.kind = kind
        self.fs = int(fs)
        self.centre_hz = centre_hz
        self.gain = (int(g.get("lna", 12)), int(g.get("mixer", 12)), int(g.get("vga", 12)))
        self.cb = make_callback(sink) if sink is not None else None
        self.dev = ctypes.c_void_p()
        self.initialized = self.opened = self.streaming = False
        self.info = None

    def bind(self, sink):
        """Where the samples go; made before open(), where watch.py always made its callback."""
        self.cb = make_callback(sink)

    def open(self):
        A = self.A
        A.check(A.lib.airspy_init(), "airspy_init")
        self.initialized = True
        A.check(A.lib.airspy_open(ctypes.byref(self.dev)), "airspy_open")
        self.opened = True
        self.info = diagnostics.receiver_info(self.dev)
        if self.fs not in A.samplerates(self.dev):
            raise A.AirspyError(f"receiver does not advertise {self.fs} samples/s")
        lna, mixer, vga = self.gain
        for name, value in (("sample_type", A.SAMPLE_INT16_IQ), ("samplerate", self.fs),
                            ("lna_agc", 0), ("mixer_agc", 0), ("rf_bias", 0), ("packing", 0),
                            ("lna_gain", lna), ("mixer_gain", mixer), ("vga_gain", vga)):
            A.check(getattr(A.lib, "airspy_set_" + name)(self.dev, value), name)
        return self.info

    def start(self):
        A = self.A
        A.check(A.lib.airspy_start_rx(self.dev, self.cb, None), "start_rx")
        self.streaming = True
        A.check(A.lib.airspy_set_freq(self.dev, int(self.centre_hz)), "set_freq")

    def fault(self):
        return None                          # a stalled Airspy shows as a stalled ring (watch.py's watchdog)

    def status(self):
        return {}                            # nothing new in the heartbeat for an Airspy

    def gain_record(self):
        return dict(zip(("lna", "mixer", "vga"), self.gain))

    def session_fields(self):
        """What session.json has always said about the Airspy's settings."""
        return {"lna_agc": False, "mixer_agc": False, "rf_bias": False, "packing": False}

    def close(self):
        A = self.A
        if self.streaming:
            A.lib.airspy_stop_rx(self.dev)
            deadline = time.monotonic() + 3
            while A.lib.airspy_is_streaming(self.dev) == 1 and time.monotonic() < deadline:
                time.sleep(.05)
            # This board previously raced close/free_transfers after streaming.
            # Leave its final device cleanup to process exit.
        elif self.opened:
            A.lib.airspy_close(self.dev)
        if self.initialized and not self.streaming:
            A.lib.airspy_exit()
