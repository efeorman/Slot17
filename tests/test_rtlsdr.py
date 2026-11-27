"""The RTL-SDR pair: two sticks found, configured and joined into the detector's 6 MSPS band.

librtlsdr is replaced by FakeRtl below, called exactly as rtlsdr_ctypes binds the real library (whose prototypes
scripts/verify_rtlsdr.py checks against rtl-sdr.h). Its rtlsdr_read_async blocks its thread and calls back
through the real ctypes callback type, as librtlsdr does. The timing tests drive the receiver's own
callback and checks with a clock of their own, so losses, drift and late threads are exact. Nothing here opens
a receiver.
"""
import ctypes
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from fractions import Fraction
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import numpy as np
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "p0"))
sys.path.insert(0, str(ROOT / "tests"))
import receiver  # noqa: E402
import receiver_rtlsdr as R  # noqa: E402
import rtl_combine as C  # noqa: E402
import rtlsdr_ctypes as L  # noqa: E402
import watch  # noqa: E402

# The updater tests a release in a copy within 70 s on the car computer (p0/update.py, TEST_LIMIT_S). These take
# seconds each and test receivers a car with an Airspy doesn't use, so they run in the full checks (setup, preflight
# --tests, development) and not there. Measured on 24 November 2026: with them, the updater's copy took 138 s on
# the development PC, and every release of this branch would have run out of time.
FULL_ONLY = unittest.skipIf(os.environ.get("SLOT17_UPDATE_TEST"), "seconds each: in the full checks only")

R820T_GAINS = [0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229, 254, 280, 297, 328, 338, 364, 372,
               386, 402, 421, 434, 439, 445, 480, 496]
GAIN = receiver.KINDS["rtlsdr2"]["gain"]


class Sink:
    def __init__(self):
        self.calls, self.blocks, self.dropped, self.lost_count = [], [], 0, 0

    def first(self, n, delay_s=0.0):
        self.calls.append(("first", n))
        self.delay_s = delay_s

    def drop(self, lost):
        self.calls.append(("drop", lost))
        self.dropped += lost

    def push(self, iq):
        self.calls.append(("push", len(iq)))
        self.blocks.append(np.array(iq, copy=True))

    def lost(self, count):
        n = count() if callable(count) else count
        self.calls.append(("lost", n))
        self.lost_count += n


class FakeRtl:
    """librtlsdr, as rtlsdr_ctypes binds it: device handles are c_void_p, strings and gains are caller's buffers."""

    def __init__(self, sticks=(("00000002", 5), ("00000001", 5)), open_rc=0, pace=True):
        self.sticks = [{"serial": s, "tuner": t, "corr": 0, "freq": 0, "rate": 0, "gain": None, "manual": None,
                        "agc": None, "bias": None, "data": None, "cancel": threading.Event(), "fail_after": None,
                        "loss_at": None, "loss_bytes": 0, "start_delay": 0.0, "sent": 0}
                       for s, t in sticks]
        self.open_rc = open_rc
        self.pace = pace
        self.calls = []
        self.lock = threading.Lock()

    def _st(self, dev):
        handle = dev.value if isinstance(dev, ctypes.c_void_p) else dev
        return self.sticks[handle - 1000]

    def _log(self, *call):
        with self.lock:
            self.calls.append(call)

    def rtlsdr_get_device_count(self):
        return len(self.sticks)

    def rtlsdr_get_device_usb_strings(self, index, m, p, s):
        m.value, p.value, s.value = b"Realtek", b"RTL2838UHIDIR", self.sticks[index]["serial"].encode()
        return 0

    def rtlsdr_open(self, ref, index):
        self._log("open", index)
        if self.open_rc:
            return self.open_rc
        ref._obj.value = 1000 + index
        return 0

    def rtlsdr_close(self, dev):
        self._log("close", dev.value - 1000)
        return 0

    def rtlsdr_get_tuner_type(self, dev):
        return self._st(dev)["tuner"]

    def rtlsdr_set_sample_rate(self, dev, rate):
        self._log("rate", dev.value - 1000, rate)
        self._st(dev)["rate"] = rate
        return 0

    def rtlsdr_get_sample_rate(self, dev):
        return self._st(dev)["rate"]

    def rtlsdr_set_freq_correction(self, dev, ppm):
        st = self._st(dev)
        self._log("ppm", dev.value - 1000, ppm)
        if st["corr"] == ppm:
            return -2                                # librtlsdr: already that value
        st["corr"] = ppm
        return 0

    def rtlsdr_set_center_freq(self, dev, freq):
        self._log("freq", dev.value - 1000, freq)
        self._st(dev)["freq"] = freq
        return 0

    def rtlsdr_get_center_freq(self, dev):
        return self._st(dev)["freq"]

    def rtlsdr_set_tuner_gain_mode(self, dev, manual):
        self._log("gain_mode", dev.value - 1000, manual)
        self._st(dev)["manual"] = manual
        return 0

    def rtlsdr_get_tuner_gains(self, dev, gains):
        if gains is None:
            return len(R820T_GAINS)
        for i, g in enumerate(R820T_GAINS):
            gains[i] = g
        return len(R820T_GAINS)

    def rtlsdr_set_tuner_gain(self, dev, gain):
        self._log("gain", dev.value - 1000, gain)
        self._st(dev)["gain"] = gain
        return 0

    def rtlsdr_get_tuner_gain(self, dev):
        return self._st(dev)["gain"]

    def rtlsdr_set_agc_mode(self, dev, on):
        self._log("agc", dev.value - 1000, on)
        self._st(dev)["agc"] = on
        return 0

    def rtlsdr_set_bias_tee(self, dev, on):
        self._log("bias_tee", dev.value - 1000, on)
        self._st(dev)["bias"] = on
        return 0

    def rtlsdr_reset_buffer(self, dev):
        self._log("reset", dev.value - 1000)
        return 0

    def rtlsdr_cancel_async(self, dev):
        self._log("cancel", dev.value - 1000)
        self._st(dev)["cancel"].set()
        return 0

    def rtlsdr_read_async(self, dev, cb, ctx, buf_num, buf_len):
        """Blocks until cancelled, calling back with buf_len bytes at a time, at the stick's rate."""
        st = self._st(dev)
        self._log("read_async", dev.value - 1000, buf_num, buf_len)
        data = st["data"] if st["data"] is not None else np.full(1 << 20, 128, np.uint8)
        buf = np.empty(buf_len, np.uint8)
        addr = buf.ctypes.data
        pos, late = 0, 0.0
        if st["start_delay"]:
            st["cancel"].wait(st["start_delay"])
        t0 = time.monotonic()
        while not st["cancel"].is_set():
            if st["fail_after"] is not None and st["sent"] >= st["fail_after"]:
                return -5                            # LIBUSB_ERROR_NOT_FOUND: the stick went away
            if st["loss_at"] is not None and st["sent"] >= st["loss_at"]:
                pos = (pos + st["loss_bytes"]) % len(data)          # these samples never arrive ...
                late += st["loss_bytes"] / 2 / st["rate"]          # ... and the next ones come that much later
                st["loss_at"] = None
            take = np.arange(pos, pos + buf_len) % len(data)
            buf[:] = data[take]
            pos = (pos + buf_len) % len(data)
            if self.pace:
                wait = t0 + late + (st["sent"] + buf_len) / 2 / st["rate"] - time.monotonic()
                if wait > 0 and st["cancel"].wait(wait):
                    break
            cb(addr, buf_len, None)
            st["sent"] += buf_len
        return 0


def pair_from_band(band, rate=2_560_000, rms_lsb=9.0):
    """Two RTL-SDRs' uint8 I/Q of a 6 MSPS band at 382.5 MHz: each half shifted to its stick, resampled, 8 bits."""
    n = np.arange(len(band))
    out = []
    fr = Fraction(rate, C.OUT_FS)
    for centre in C.CENTRES:
        shifted = band * np.exp(-2j * np.pi * (centre - C.OUT_CENTRE) * n / C.OUT_FS).astype(np.complex64)
        x = resample_poly(shifted, fr.numerator, fr.denominator)
        scale = rms_lsb / np.sqrt(np.mean(np.abs(x) ** 2) / 2)
        u = np.empty(2 * len(x), np.uint8)
        u[0::2] = np.clip(np.round(127.5 + scale * x.real), 0, 255)
        u[1::2] = np.clip(np.round(127.5 + scale * x.imag), 0, 255)
        out.append(u)
    return out


def rx_with(lib=None, gain=None, clock=None, **kw):
    lib = lib or FakeRtl(**kw)
    rx = R.Receiver("rtlsdr2", gain=dict(GAIN, **(gain or {})), lib=lib, clock=clock)
    return rx, lib


class Settings(unittest.TestCase):
    def test_the_kind_and_its_defaults(self):
        k = receiver.KINDS["rtlsdr2"]
        self.assertEqual(k["devices"], 2)
        self.assertEqual(k["usb"], ("0bda:2838", "0bda:2832"))
        self.assertEqual(receiver.name("rtlsdr2"), "RTL-SDR pair")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps({"kind": "rtlsdr2", "gain": {"ppm_a": -3, "ppm_b": 12}}))
            self.assertEqual(receiver.configured(path), "rtlsdr2")
            self.assertEqual(receiver.gain_for("rtlsdr2", path),
                             {"tuner_gain_db10": 297, "rate_khz": 2560, "ppm_a": -3, "ppm_b": 12})

    def test_settings_outside_the_sticks_are_refused(self):
        for bad in ({"rate_khz": 2400}, {"rate_khz": 3200}, {"tuner_gain_db10": -1}, {"tuner_gain_db10": 900},
                    {"ppm_a": 500}):
            with self.assertRaises(receiver.ReceiverError, msg=bad):
                rx_with(gain=bad)
        with self.assertRaises(receiver.ReceiverError):
            R.Receiver("rtlsdr2", fs=3_000_000, gain=GAIN, lib=FakeRtl())

    def test_no_librtlsdr_says_how_to_install_it(self):
        with patch.object(L, "load", side_effect=OSError("librtlsdr.so.0: cannot open shared object file")):
            with self.assertRaises(receiver.ReceiverError) as e:
                R.Receiver("rtlsdr2", gain=GAIN)
        self.assertIn("setup.sh car --receiver rtlsdr2", str(e.exception))

    def test_nearest_gain(self):
        self.assertEqual(R.nearest_gain(R820T_GAINS, 297), 297)
        self.assertEqual(R.nearest_gain(R820T_GAINS, 300), 297)
        self.assertEqual(R.nearest_gain(R820T_GAINS, 1000), 496)
        self.assertEqual(R.nearest_gain(R820T_GAINS, 0), 0)
        self.assertIsNone(R.nearest_gain([], 297))


class Session(unittest.TestCase):
    def test_both_sticks_configured_in_order_and_a_is_the_lower_serial(self):
        rx, lib = rx_with(gain={"ppm_a": 4, "ppm_b": -7, "tuner_gain_db10": 300})
        info = rx.open()
        # Device 0 has serial ...2 and device 1 serial ...1, so A is device 1.
        self.assertEqual([st.index for st in rx.sticks], [1, 0])
        self.assertEqual([s["serial"] for s in info["sticks"]], ["00000001", "00000002"])
        for st, centre, ppm in ((1, 381_250_000, 4), (0, 383_750_000, -7)):
            mine = [c[0] for c in lib.calls if len(c) > 1 and c[1] == st]
            self.assertEqual(mine, ["open", "rate", "ppm", "freq", "gain_mode", "gain", "agc", "bias_tee", "reset"])
            self.assertEqual(lib.sticks[st]["rate"], 2_560_000)
            self.assertEqual(lib.sticks[st]["freq"], centre)
            self.assertEqual(lib.sticks[st]["corr"], ppm)
            self.assertEqual(lib.sticks[st]["manual"], 1)
            self.assertEqual(lib.sticks[st]["gain"], 297)        # the nearest the R820T offers to 30.0 dB
            self.assertEqual(lib.sticks[st]["agc"], 0)
            self.assertEqual(lib.sticks[st]["bias"], 0)          # no power up the antenna cable
        self.assertEqual(rx.gain_record(), {"tuner_gain_db10": 300, "agc": False})
        self.assertEqual([s["gain_db10"] for s in info["sticks"]], [297, 297])
        self.assertEqual(rx.session_fields()["stick_centres_hz"], [381.25e6, 383.75e6])
        self.assertFalse(info["same_serial"])
        rx.close()
        self.assertEqual(sorted(c for c in lib.calls if c[0] == "close"), [("close", 0), ("close", 1)])

    def test_a_ppm_of_zero_is_not_a_failure(self):
        rx, lib = rx_with()                          # librtlsdr answers -2: "already 0"
        rx.open()
        self.assertEqual([st.ppm for st in rx.sticks], [0, 0])
        rx.close()

    def test_the_wrong_number_of_sticks(self):
        for sticks, words in (((("00000001", 5),), "1 RTL-SDR found; the RTL-SDR pair needs two"),
                              ((), "0 RTL-SDRs found"),
                              ((("1", 5), ("2", 5), ("3", 5)), "3 RTL-SDRs found; Slot17 uses exactly two")):
            rx, lib = rx_with(sticks=sticks)
            with self.assertRaises(receiver.ReceiverError) as e:
                rx.open()
            self.assertIn(words, str(e.exception))
            self.assertFalse(any(c[0] == "open" for c in lib.calls))
            rx.close()

    def test_the_same_serial_twice(self):
        rx, lib = rx_with(sticks=(("00000001", 5), ("00000001", 5)), gain={"ppm_a": 1, "ppm_b": 30})
        with self.assertRaises(receiver.ReceiverError) as e:
            rx.open()
        self.assertIn("rtl_eeprom -s 00000002", str(e.exception))
        self.assertFalse(any(c[0] == "open" for c in lib.calls))
        rx, lib = rx_with(sticks=(("00000001", 5), ("00000001", 5)))    # same ppm: which is A doesn't matter
        self.assertTrue(rx.open()["same_serial"])
        rx.close()

    def test_the_same_serial_with_nearby_ppm_shares_the_mean(self):
        """Two good sticks out of the box, calibrated to 0 and 1 ppm: until 26 November 2026 every start refused
        (found in review). Now both get the mean, which is at most 1.5 ppm off for either."""
        for ppm, shared in (((0, 1), 0), ((-2, 1), 0), ((4, 6), 5)):
            rx, lib = rx_with(sticks=(("00000001", 5), ("00000001", 5)), gain={"ppm_a": ppm[0], "ppm_b": ppm[1]})
            info = rx.open()
            self.assertEqual(info["shared_ppm"], shared)
            self.assertEqual([st["ppm"] for st in info["sticks"]], [shared, shared])
            self.assertEqual([c[2] for c in lib.calls if c[0] == "ppm"], [shared, shared])
            rx.close()

    def test_a_tuner_that_cannot_reach_380_mhz(self):
        rx, lib = rx_with(sticks=(("00000001", 4), ("00000002", 5)))   # 4 = FC2580
        with self.assertRaises(receiver.ReceiverError) as e:
            rx.open()
        self.assertIn("FC2580", str(e.exception))
        rx.close()
        self.assertIn(("close", 0), lib.calls)       # what was opened is closed

    def test_open_failures_say_what_to_do(self):
        for rc, words in ((-6, "DVB-T television driver"), (-3, "plugdev"), (-1, "rtlsdr_open failed (-1)")):
            rx, lib = rx_with(open_rc=rc)
            with self.assertRaises((receiver.ReceiverError, L.RtlError)) as e:
                rx.open()
            self.assertIn(words, str(e.exception))
            self.assertFalse(rx.opened)


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        return self.t


class Timing(unittest.TestCase):
    """The receiver's callback and checks on a clock of the test's own: every arrival time is exact."""

    T = 32768 / 2_560_000                            # one transfer of R.BUF_LEN bytes, 12.8 ms

    def setUp(self):
        self.clock = Clock()
        self.rx, self.lib = rx_with(clock=self.clock)
        self.rx.open()
        self.sink = Sink()
        self.rx.bind(self.sink)
        self.rx._t_start = self.clock.t
        self.joined = []
        self.rx._join = lambda raw: self.joined.append(len(raw[0]) // 2)
        self.buf = np.full(R.BUF_LEN, 128, np.uint8)

    def run_for(self, seconds, start=(0.0, 0.0), late=None, rate_ppm=(0.0, 0.0), loss=None, silent=None):
        """Both sticks' transfers in time order; a check every second, joins in between.
        late(t, stick) -> extra delay; loss = (stick, at_s, seconds); silent = (stick, from_s)."""
        t_begin = self.clock.t
        k = [0, 0]
        shift = [0.0, 0.0]
        next_check = t_begin + R.CHECK_S
        while True:
            due = []
            for s in (0, 1):
                period = self.T / (1 + rate_ppm[s] * 1e-6)
                if loss and loss[0] == s and shift[s] == 0 and (k[s] + 1) * period >= loss[1]:
                    shift[s] = loss[2]
                due.append(t_begin + start[s] + (k[s] + 1) * period + shift[s])
            s = 0 if due[0] <= due[1] else 1
            t = due[s]
            if t - t_begin > seconds:
                break
            while next_check <= t:
                self.clock.t = next_check
                self.rx._check_timing(next_check)
                next_check += R.CHECK_S
            k[s] += 1
            if silent and silent[0] == s and t - t_begin >= silent[1]:
                continue
            self.clock.t = t + (late(t - t_begin, s) if late else 0.0)
            self.rx._on_transfer(self.rx.sticks[s], self.buf.ctypes.data, R.BUF_LEN)
            self.rx.join_once()

    def lost_by_the_join(self, st):
        """Input samples the joined stream has lost, from the test's own counts: what stick `st` delivered (it
        loses nothing on USB here), less what was joined and what still waits to be."""
        return st.received - sum(self.joined) - (st.written - st.read) // 2

    def test_the_halves_are_aligned_at_the_start(self):
        self.run_for(4.0, start=(0.0, 0.007))       # B began 7 ms after A
        a, b = self.rx.sticks
        skipped = (a.read // 2 + a.skip) - (b.read // 2 + b.skip)
        self.assertAlmostEqual(skipped, 0.007 * 2_560_000, delta=2)
        self.assertEqual(self.rx.counters["realignments"], 0)
        self.assertEqual(self.sink.dropped, 0)
        self.assertEqual(a.lost + b.lost, 0)

    def test_a_lost_stretch_is_found_reported_and_realigned(self):
        self.run_for(14.0, loss=(1, 4.0, 0.030))    # B loses 30 ms 4 s in
        a, b = self.rx.sticks
        self.assertAlmostEqual(b.lost, 0.030 * 2_560_000, delta=2)
        self.assertEqual(a.lost, 0)
        self.assertAlmostEqual(self.sink.dropped, 0.030 * 6_000_000, delta=10)
        skipped = (a.read // 2 + a.skip) - (b.read // 2 + b.skip)
        self.assertAlmostEqual(skipped, 0.030 * 2_560_000, delta=2)   # A passed over the same stretch
        self.assertEqual(self.rx.status()["lost_samples_b"], b.lost)

    def test_a_loss_under_the_threshold_is_not_seen(self):
        self.run_for(14.0, loss=(0, 4.0, 0.010))
        self.assertEqual(self.sink.dropped, 0)       # 10 ms: under LOSS_S, a known limit (RECEIVERS.md)

    def test_late_threads_are_not_a_loss(self):
        # Both readers kept 25 ms late for 7 s (a busy computer): the same for both, so nothing was lost.
        self.run_for(16.0, late=lambda t, s: 0.025 if 3.0 <= t < 10.0 else 0.0)
        self.assertNotIn("drop", [c for c, _ in self.sink.calls])
        self.assertEqual(self.rx.counters["realignments"], 0)

    def test_one_late_thread_costs_a_realignment_there_and_back_and_it_is_reported(self):
        """A's reader 15 ms late for 7 s, B on time: under the loss threshold, but over the alignment tolerance, so
        B is realigned to A's apparent start and A back when its arrivals return. Both halves have then passed over
        15 ms: until 24 November 2026 that jump went unreported, and every later timestamp was 15 ms early."""
        self.run_for(16.0, late=lambda t, s: 0.015 if s == 0 and 3.0 <= t < 10.0 else 0.0)
        a, b = self.rx.sticks
        self.assertEqual(self.rx.counters["realignments"], 2)
        self.assertEqual(a.lost + b.lost, 0, "not counted as samples lost on USB")
        self.assertAlmostEqual(self.sink.dropped * 2_560_000 / 6_000_000, self.lost_by_the_join(a), delta=4)
        self.assertAlmostEqual(self.sink.dropped / 6_000_000, 0.015, delta=0.0005)

    def test_a_start_skew_and_a_drift_on_the_other_stick_lose_nothing(self):
        """B starts 12 ms after A, so A skips its first 12 ms; then B's crystal runs fast, so B skips too. Both
        skips only take away samples a stick had in excess. Until 26 November 2026 the smaller of the two was
        reported as dropped samples, which degraded the session and cut checking to one thread (found in review)."""
        self.run_for(200.0, start=(0.0, 0.012), rate_ppm=(0.0, 60.0))
        a, b = self.rx.sticks
        self.assertGreaterEqual(self.rx.counters["realignments"], 2)
        self.assertGreater(a.skipped, 0)
        self.assertGreater(b.skipped, 0)
        self.assertEqual(self.sink.dropped, 0)
        self.assertEqual(self.rx.counters["joined_lost_samples"], 0)

    def test_a_drift_that_changes_direction_loses_nothing(self):
        """B fast while cold, then A fast once warm: each skips in its turn, and nothing is lost."""
        self.run_for(100.0, rate_ppm=(0.0, 60.0))
        self.run_for(200.0, rate_ppm=(60.0, 0.0))
        a, b = self.rx.sticks
        self.assertGreater(a.skipped, 0)
        self.assertGreater(b.skipped, 0)
        self.assertEqual(self.sink.dropped, 0)

    def test_drifting_clocks_are_realigned_without_any_loss(self):
        # B's crystal 60 ppm fast: 154 samples a second, past the 5 ms tolerance after about 83 s.
        self.run_for(100.0, rate_ppm=(0.0, 60.0))
        a, b = self.rx.sticks
        self.assertGreaterEqual(self.rx.counters["realignments"], 1)
        self.assertEqual(self.sink.dropped, 0)
        self.assertEqual(a.lost + b.lost, 0)
        self.assertLess(abs(self.rx.counters["alignment_ms"]), R.ALIGN_TOL_S * 1000 + 0.5)
        self.assertGreater(b.read // 2 + b.skip, a.read // 2 + a.skip)    # the fast one passed over samples

    def test_a_stick_that_falls_silent_ends_the_session(self):
        self.run_for(8.0, silent=(0, 3.0))
        self.assertIn("RTL-SDR A (00000001) has delivered nothing", self.rx.fault())

    def test_a_stick_that_never_delivers(self):
        self.run_for(5.0, silent=(1, 0.0))
        self.assertIn("since the start", self.rx.fault())

    def test_overwritten_samples_are_dropped_not_hidden(self):
        """Both waiting rooms overflow over the same stretch of time, so the joined stream loses it once. Until
        24 November 2026 the two sticks' losses were added up: twice the time, and every later timestamp off."""
        self.rx.join_once = lambda wait=0.0: False   # the joining thread fell far behind
        self.run_for(5.0)
        del self.rx.join_once
        a, b = self.rx.sticks
        self.assertGreater(a.overflow_lost, 0)
        self.assertTrue(self.rx.join_once())
        # The two totals differ by at most the one transfer A was ahead when the joining resumed.
        self.assertLessEqual(abs(a.overflow_lost - b.overflow_lost), R.BUF_LEN // 2)
        self.assertEqual(self.sink.dropped, min(a.overflow_lost, b.overflow_lost) * 6_000_000 // 2_560_000)
        self.assertLess(self.sink.dropped, (a.overflow_lost + b.overflow_lost) * 6_000_000 // 2_560_000)
        before = self.sink.dropped
        self.rx.join_once()
        self.assertEqual(self.sink.dropped, before)  # reported once, not again at the next join

    def test_overflows_of_each_stick_at_different_times_are_both_lost(self):
        """Found in review, 24 November 2026: B's reader stalls and A's waiting room overflows; later A's stalls
        and B's overflows. The joined stream loses both stretches (each realignment passes the other stick over
        the same stretch), but the larger of the two totals, reported until then, counted only one."""
        a, b = self.rx.sticks
        for st in (a, b):                            # a waiting room of 0.2 s, so that a 0.5 s stall overflows it
            st.cap = int(0.2 * 2_560_000) * 2
        self.run_for(3.0)
        for stalled in (b, a):
            other = a if stalled is b else b
            backlog = 0
            for _ in range(int(0.5 / self.T)):       # 0.5 s: only the other stick's reader delivers
                self.clock.t += self.T
                self.rx._on_transfer(other, self.buf.ctypes.data, R.BUF_LEN)
                self.rx.join_once()
                backlog += 1
            for _ in range(backlog):                 # then the stalled one's queue, all at once
                self.rx._on_transfer(stalled, self.buf.ctypes.data, R.BUF_LEN)
                self.rx.join_once()
            self.run_for(6.0)                        # timing checks find the misalignment and realign
        self.assertGreater(a.overflow_lost, 0)
        self.assertGreater(b.overflow_lost, 0)
        self.assertLess(abs(self.rx.counters["alignment_ms"]), R.ALIGN_TOL_S * 1000)     # the halves agree again
        truth = self.lost_by_the_join(a)
        self.assertGreater(truth, 0.55 * 2_560_000)                 # two stalls, each past the 0.2 s room
        self.assertAlmostEqual(self.sink.dropped * 2_560_000 / 6_000_000, truth, delta=R.ALIGN_TOL_S * 2_560_000)
        self.assertEqual(self.rx.status()["joined_lost_samples"], self.sink.dropped)

    def test_the_larger_backlog_shows_while_one_stick_delivers_nothing(self):
        """Until 24 November 2026 the gauge moved only when something was joined (found in review)."""
        self.run_for(2.0)
        a, b = self.rx.sticks
        for _ in range(40):                          # B's reader stalls for 0.5 s
            self.clock.t += self.T
            self.rx._on_transfer(a, self.buf.ctypes.data, R.BUF_LEN)
            self.rx.join_once()
        self.assertGreater(self.rx.counters["backlog_max_ms"], 400)

    def test_the_first_sample_is_dated_back_by_the_joins_delay(self):
        rx, lib = rx_with(clock=self.clock)
        rx.open()
        sink = Sink()
        rx.bind(sink)
        n_in = rx.stitcher.n_in
        raw = [np.full(2 * R.BATCH_BLOCKS * n_in, 128, np.uint8)] * 2
        rx._join(raw)
        self.assertEqual(sink.delay_s, C.DELAY_S)
        rx.close()


def tones_at(freqs, phases, amp, t):
    """sum of amp * exp(j(2 pi f t + phase)), evaluated exactly (float64) at times t."""
    out = np.zeros(len(t), np.complex128)
    for f, p in zip(freqs, phases):
        out += amp * np.exp(1j * (2 * np.pi * f * t + p))
    return out


class JoinAccuracy(unittest.TestCase):
    """The join against an independent reference: many tones at random frequencies (not on the FFT's bins, as
    real signals never are) in both halves, evaluated exactly at every stick sample and every output sample.
    Until 24 November 2026 the join discarded its overlap at the start of each block only; the zero-phase
    weighting wraps round both ends, and the last 16 samples of every 2 ms block were 10 dB under the signal
    (found in review). Comparing the stitcher with itself could never have shown that."""

    def error_by_position(self, rate, blocks=10, n_tones=60, seed=1):
        rng = np.random.default_rng(seed)
        lo, hi = -C.HALF_BAND + 2 * C.EDGE_HALF, C.HALF_BAND - 2 * C.EDGE_HALF
        f = rng.uniform(lo, hi, 4 * n_tones)
        keep = np.abs(f) > 3 * C.HANDOVER_HALF                  # not in the 2 kHz handover
        for c in (-C.STICK_OFFSET, C.STICK_OFFSET):
            keep &= np.abs(f - c) > 1_000.0                    # nor on a stick's left-out DC bin
        f = f[keep][:n_tones]
        p = rng.uniform(0, 2 * np.pi, len(f))
        amp = 1 / np.sqrt(len(f))
        st = C.Stitcher(rate)
        t_in = np.arange(blocks * st.n_in) / rate
        halves = [tones_at(f[f < 0] + C.STICK_OFFSET, p[f < 0], amp, t_in).astype(np.complex64),
                  tones_at(f[f >= 0] - C.STICK_OFFSET, p[f >= 0], amp, t_in).astype(np.complex64)]
        y = st.process(*halves)
        ref = tones_at(f, p, amp, np.arange(len(y)) / C.OUT_FS - C.DELAY_S)
        err = np.abs(y - ref)[2 * st.n_out:].reshape(-1, st.n_out)      # the first blocks start from silence
        rms = np.sqrt(np.mean(np.abs(ref) ** 2))
        return 20 * np.log10(np.sqrt(np.mean(err ** 2, axis=0)) / rms)

    def test_no_block_edge_carries_an_error(self):
        """The error is the same everywhere in a block (about -47 dB here: what leaks into the bins left out on
        purpose, the DC bins and a stick's outermost bins), and no more at the ends. The old join: +1.6 dB in
        the last 16 samples."""
        for rate in C.RATES:
            db = self.error_by_position(rate)
            middle = float(np.median(db))
            self.assertLess(middle, -40.0, rate)
            self.assertLess(db[-16:].max() - middle, 1.5, f"{rate}: the end of each block")
            self.assertLess(db[:16].max() - middle, 1.5, f"{rate}: the start of each block")
            self.assertLess(db.max() - middle, 1.5, rate)


class Joining(unittest.TestCase):
    def test_the_joined_stream_is_the_stitchers(self):
        """Within one count: the FFT of a batch of blocks may group them differently for the processor's vector
        unit, so how the stream is cut into batches moves the last float rounding (seen on the Pi's ARM cores:
        at most 0.014 in values up to 42640). Bit for bit on x86; never more than 1 anywhere."""
        rx, lib = rx_with(clock=Clock())
        rx.open()
        sink = Sink()
        rx.bind(sink)
        rng = np.random.default_rng(3)
        n = R.BUF_LEN * 12
        data = [rng.integers(0, 256, n, dtype=np.uint8) for _ in (0, 1)]
        data[0][:40] = 255                           # some clipping on A
        for k in range(0, n, R.BUF_LEN):
            for st, d in zip(rx.sticks, data):
                chunk = np.ascontiguousarray(d[k:k + R.BUF_LEN])
                rx._on_transfer(st, chunk.ctypes.data, len(chunk))
            rx.join_once()
        got = np.concatenate(sink.blocks)
        ref = C.Stitcher(2_560_000, gain=R.OUT_GAIN)
        m = len(got) // 2 // ref.n_out * ref.n_in
        y = ref.process(C.from_u8(data[0][:2 * m]), C.from_u8(data[1][:2 * m])).view(np.float32)
        want = np.rint(np.clip(y, -32768, 32767)).astype(np.int16)
        self.assertEqual(sink.calls[0], ("first", sink.calls[1][1] // 2))
        self.assertEqual(got.shape, want.shape)
        diff = np.abs(got.astype(np.int32) - want)
        self.assertLessEqual(int(diff.max()), 1)
        self.assertLess(np.count_nonzero(diff) / diff.size, 1e-3)
        self.assertEqual(len(got) % (2 * ref.n_out), 0)
        self.assertGreater(rx.sticks[0].saturated, 0)
        self.assertEqual(rx.sticks[1].saturated,
                         int(np.count_nonzero(data[1][:2 * m] == 0) + np.count_nonzero(data[1][:2 * m] == 255)))


class OddTransfers(unittest.TestCase):
    def test_an_odd_length_transfer_does_not_swap_i_and_q(self):
        rx, lib = rx_with(clock=Clock())
        rx.open()
        sink = Sink()
        rx.bind(sink)
        rng = np.random.default_rng(11)
        n = R.BUF_LEN * 6
        data = [rng.integers(0, 256, n, dtype=np.uint8) for _ in (0, 1)]
        cuts = [0, 1001, 65536, 70001, 131072, 200003, n]        # A's transfers: odd lengths among them
        for lo, hi in zip(cuts, cuts[1:]):
            chunk = np.ascontiguousarray(data[0][lo:hi])
            rx._on_transfer(rx.sticks[0], chunk.ctypes.data, len(chunk))
        for k in range(0, n, R.BUF_LEN):
            chunk = np.ascontiguousarray(data[1][k:k + R.BUF_LEN])
            rx._on_transfer(rx.sticks[1], chunk.ctypes.data, len(chunk))
        while rx.join_once():
            pass
        got = np.concatenate(sink.blocks)
        ref = C.Stitcher(2_560_000, gain=R.OUT_GAIN)
        m = len(got) // 2 // ref.n_out * ref.n_in
        y = ref.process(C.from_u8(data[0][:2 * m]), C.from_u8(data[1][:2 * m])).view(np.float32)
        want = np.rint(np.clip(y, -32768, 32767)).astype(np.int16)
        self.assertGreater(m, 0)
        self.assertLessEqual(int(np.abs(got.astype(np.int32) - want).max()), 1)


class Threads(unittest.TestCase):
    """The real threads: two readers in rtlsdr_read_async, calling back through ctypes, and the joining one."""

    def test_a_stream_and_a_clean_end(self):
        rx, lib = rx_with()
        sink = Sink()
        rx.bind(sink)
        rx.open()
        rx.start()
        time.sleep(1.5)
        self.assertIsNone(rx.fault())
        rx.close()
        pushed = sum(n for c, n in sink.calls if c == "push") // 2
        self.assertGreater(pushed, 0.8 * 6_000_000)
        self.assertEqual(sorted(c for c in lib.calls if c[0] == "cancel"), [("cancel", 0), ("cancel", 1)])
        self.assertEqual(sorted(c for c in lib.calls if c[0] == "close"), [("close", 0), ("close", 1)])
        self.assertEqual([c[2:] for c in lib.calls if c[0] == "read_async"], [(R.BUF_NUM, R.BUF_LEN)] * 2)
        self.assertFalse(rx.streaming)
        self.assertEqual(sink.lost_count, 0)

    def test_a_stick_unplugged_mid_stream(self):
        rx, lib = rx_with()
        lib.sticks[0]["fail_after"] = R.BUF_LEN * 40
        rx.bind(Sink())
        rx.open()
        rx.start()
        deadline = time.monotonic() + 5
        while rx.fault() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn("RTL-SDR B (00000002) stopped streaming", rx.fault() or "")
        rx.close()


@FULL_ONLY
class WholeDetectorOnAnRtlPair(unittest.TestCase):
    """watch.py --receiver rtlsdr2, two paced stand-in sticks carrying synthetic TETRA bursts."""

    CHANNELS = (1, 49, 99, 100, 150, 198)            # the band's ends, beside each DC spike, the handover

    def run_watch(self, minutes, lib, extra=()):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(L, "load", return_value=lib), \
                patch.object(receiver, "SETTINGS", Path(tmp) / "none.json"), \
                patch.object(watch.paths, "ROOT", tmp), patch.object(watch.paths, "CAPTURES", str(Path(tmp) / "captures")), \
                patch.object(watch.diagnostics, "host_health", return_value={}), \
                patch.object(watch.signal, "signal"), \
                patch.object(sys, "argv", ["watch.py", "--minutes", str(minutes), "--no-notify", "--receiver", "rtlsdr2",
                                           "--heartbeat", str(Path(tmp) / "hb.json"), *extra]), \
                redirect_stdout(StringIO()) as out:
            code = watch.main()
            session = next((Path(tmp) / "captures").glob("*"))
            events = [json.loads(line) for line in (session / "events.jsonl").read_text().splitlines()]
            return (code, json.loads((session / "session.json").read_text()),
                    json.loads((session / "summary.json").read_text()), events,
                    json.loads((Path(tmp) / "hb.json").read_text()), out.getvalue())

    def test_bursts_across_both_halves_are_recognised(self):
        import test_detection
        band, _ = test_detection.wideband(channels=self.CHANNELS, snr=20, seconds=0.6)
        lib = FakeRtl()
        a, b = pair_from_band(band)
        # Device 1 is A (the lower serial), device 0 is B.
        lib.sticks[1]["data"], lib.sticks[0]["data"] = a, b
        code, session, summary, events, beat, out = self.run_watch(0.12, lib, ("--rtl-gain", "380"))
        self.assertEqual(code, 0, out[-2000:])
        self.assertEqual(session["receiver"], {"kind": "rtlsdr2", "name": "RTL-SDR pair"})
        self.assertEqual(session["gain"], {"tuner_gain_db10": 380, "agc": False})
        self.assertEqual([s["gain_db10"] for s in summary["receiver"]["sticks"]], [386, 386])   # nearest to 38.0
        self.assertEqual(session["stick_centres_hz"], [381.25e6, 383.75e6])
        self.assertFalse(session["bias_t"])
        self.assertEqual([s["serial"] for s in summary["receiver"]["sticks"]], ["00000001", "00000002"])
        self.assertGreater(beat["receiver"]["callbacks_a"], 0)
        self.assertEqual(beat["receiver"]["lost_samples_a"] + beat["receiver"]["lost_samples_b"], 0)
        self.assertEqual(beat["dropped"], 0)
        confirmed = {e.get("tetra_channel") for e in events if e.get("confirmed") is True}
        self.assertTrue(set(self.CHANNELS) <= confirmed, (sorted(confirmed), out[-1500:]))
        self.assertLessEqual(confirmed, set(self.CHANNELS), "a burst recognised on a channel it wasn't on")
        self.assertEqual(sorted(c for c in lib.calls if c[0] == "close"), [("close", 0), ("close", 1)])

    def test_an_unplugged_stick_ends_the_session_as_a_stall(self):
        lib = FakeRtl()
        lib.sticks[1]["fail_after"] = R.BUF_LEN * 60
        code, session, summary, events, beat, out = self.run_watch(0.3, lib)
        self.assertEqual(code, watch.failure.STREAM_STALL, out[-1500:])
        self.assertIn("RTL-SDR A (00000001) stopped streaming", summary["failure_reason"])
        self.assertEqual(sorted(c for c in lib.calls if c[0] == "close"), [("close", 0), ("close", 1)])

    def test_one_stick_is_a_clean_receiver_failure(self):
        lib = FakeRtl(sticks=(("00000001", 5),))
        with tempfile.TemporaryDirectory() as tmp, patch.object(L, "load", return_value=lib), \
                patch.object(receiver, "SETTINGS", Path(tmp) / "none.json"), \
                patch.object(watch.paths, "ROOT", tmp), patch.object(watch.paths, "CAPTURES", str(Path(tmp) / "c")), \
                patch.object(watch.diagnostics, "host_health", return_value={}), \
                patch.object(watch.signal, "signal"), \
                patch.object(sys, "argv", ["watch.py", "--minutes", ".01", "--no-notify", "--receiver", "rtlsdr2",
                                           "--heartbeat", str(Path(tmp) / "hb.json")]), \
                redirect_stdout(StringIO()) as out:
            code = watch.main()
        self.assertNotEqual(code, 0)
        self.assertIn("1 RTL-SDR found", out.getvalue())


class CalibrationCapture(unittest.TestCase):
    def test_an_rtl_pair_capture(self):
        import capture
        lib = FakeRtl()
        with tempfile.TemporaryDirectory() as tmp, patch.object(L, "load", return_value=lib), \
                patch.object(receiver, "SETTINGS", Path(tmp) / "none.json"), \
                patch.object(capture, "IQ_DIR", tmp), patch.object(capture.paths, "ensure_dirs"), \
                patch.object(sys, "argv", ["capture.py", "--freq", "392.5", "--secs", "0.3", "--rate", "3",
                                           "--receiver", "rtlsdr2", "--tag", "downlink-calibration"]), \
                redirect_stdout(StringIO()) as out:
            self.assertEqual(capture.main(), 0, out.getvalue())
            raw = next(Path(tmp).glob("*.iq"))
            meta = json.loads(raw.with_suffix(".json").read_text())
            self.assertEqual(raw.stat().st_size, int(6e6 * 0.3) * 4)
        self.assertEqual(meta["sample_rate_hz"], 6_000_000)
        self.assertEqual(meta["receiver"]["kind"], "rtlsdr2")
        self.assertEqual([s["centre_hz"] for s in meta["receiver"]["info"]["sticks"]], [391_250_000, 393_750_000])


@FULL_ONLY
class PairCalibration(unittest.TestCase):
    """calibrate.py on a pair's downlink capture: each stick's ppm from the carriers in its own half."""

    def capture(self, folder, name, applied=(2, -3), error=(12, -20), kind="rtlsdr2", seconds=0.4,
                serials=("00000001", "00000002"), handover=None):
        import tetra_gen
        fs, fc = 6_000_000, 392.5e6
        n = int(seconds * fs)
        rng = np.random.default_rng(5)
        iq = ((rng.standard_normal(n) + 1j * rng.standard_normal(n)) * 30).astype(np.complex64)
        t = np.arange(n) / fs
        # Two continuous downlink carriers in each half, each read low by its stick's (error - setting). Each has a
        # weak tone at its centre, as real ones have (analyze.find_carriers): random-data pi/4-DQPSK alone has a flat
        # top 12 kHz wide, on which refine()'s peak could land anywhere. The tone is about 2 dB above that top in
        # the finder's 732 Hz bins (so the carrier still measures TETRA-wide) and 11 dB in refine()'s 91 Hz bins.
        for ch, e, p in ((30, error[0], applied[0]), (70, error[0], applied[0]),
                         (130, error[1], applied[1]), (170, error[1], applied[1])):
            f = 390.0125e6 + ch * 25_000
            seen = f * (1 - (e - p) * 1e-6) if kind == "rtlsdr2" else f
            x = resample_poly(tetra_gen.modulate(int(seconds * 18_000) + 20, 10, rng), 100, 3)[:n]
            iq += ((300 * x + 20) * np.exp(2j * np.pi * (seen - fc) * t)).astype(np.complex64)
        raw = np.empty(2 * n, np.int16)
        raw[0::2], raw[1::2] = np.round(iq.real), np.round(iq.imag)
        path = Path(folder) / f"{name}_downlink-calibration_392.5000MHz_6Msps.iq"
        raw.tofile(path)
        rx = {"kind": kind}
        if kind == "rtlsdr2":
            rx["info"] = {"sticks": [{"label": "A", "ppm": applied[0], "serial": serials[0]},
                                     {"label": "B", "ppm": applied[1], "serial": serials[1]}],
                          "handover_hz": handover or fc}
        path.with_suffix(".json").write_text(json.dumps({"centre_hz": fc, "sample_rate_hz": fs, "receiver": rx}))
        return str(path)

    def calibrate(self, files, settings_kind="rtlsdr2", iq_dir=None):
        """calibrate.py on `files` (none: the newest in iq_dir); (calibration written or None, settings, output)."""
        import calibrate
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "receiver.json"
            settings.write_text(json.dumps({"kind": settings_kind}))
            cal_path = Path(tmp) / "calibration.local.json"
            with patch.object(receiver, "SETTINGS", settings), patch.object(calibrate, "CAL_PATH", str(cal_path)), \
                    patch.object(calibrate, "CHANNELS_PATH", str(Path(tmp) / "dl.json")), \
                    patch.object(calibrate, "IQ_DIR", iq_dir or tmp), \
                    patch.object(sys, "argv", ["calibrate.py", *files]), redirect_stdout(StringIO()) as out:
                calibrate.main()
            cal = json.loads(cal_path.read_text()) if cal_path.exists() else None
            self.assertEqual([p.name for p in Path(tmp).iterdir() if p.name.endswith(".tmp")], [])
            return cal, json.loads(settings.read_text()), out.getvalue()

    def test_each_stick_gets_its_own_ppm(self):
        with tempfile.TemporaryDirectory() as tmp:
            cal, settings, out = self.calibrate([self.capture(tmp, "20261124-010000")])
        pair = cal["rtl_pair"]
        self.assertEqual(pair["carriers"], [2, 2])
        self.assertEqual(pair["setting_ppm"], [12, -20], out)
        self.assertEqual(settings, {"kind": "rtlsdr2", "gain": {"ppm_a": 12, "ppm_b": -20}})
        self.assertTrue(pair["settings_written"])
        for e, want in zip(pair["error_ppm"], (12, -20)):
            self.assertAlmostEqual(e, want, delta=0.3)
        self.assertLess(abs(cal["median_ppm"]), 0.3)   # whole-ppm settings leave next to nothing to share

    def test_another_receivers_computer_only_gets_a_suggestion(self):
        """And writes nothing: until 24 November 2026 the pair's measurement replaced this receiver's own
        calibration (found in review)."""
        with tempfile.TemporaryDirectory() as tmp:
            cal, settings, out = self.calibrate([self.capture(tmp, "20261124-010000")], settings_kind="airspy")
        self.assertEqual(settings, {"kind": "airspy"})
        self.assertIsNone(cal)
        self.assertIn("would want ppm_a = 12, ppm_b = -20", out)
        self.assertIn("Nothing written", out)

    def test_two_sticks_with_one_serial_get_one_setting(self):
        """Two sticks out of the box (both 00000001), 0.4 and 0.6 ppm off: saved as one setting, so the next start
        works. Until 26 November 2026 they were saved as 0 and 1, and every start refused (found in review)."""
        with tempfile.TemporaryDirectory() as tmp:
            cap = self.capture(tmp, "20261126-100000", applied=(0, 0), error=(0.4, 0.6),
                               serials=("00000001", "00000001"))
            cal, settings, out = self.calibrate([cap])
        self.assertEqual(settings["gain"]["ppm_a"], settings["gain"]["ppm_b"], out)
        self.assertTrue(cal["rtl_pair"]["same_serial"])
        self.assertIn("both get ppm", out)
        lib = FakeRtl(sticks=(("00000001", 5), ("00000001", 5)))
        rx = R.Receiver("rtlsdr2", gain=dict(receiver.KINDS["rtlsdr2"]["gain"], **settings["gain"]), lib=lib)
        self.assertTrue(rx.open()["same_serial"])
        rx.close()

    def test_an_older_pair_capture_never_overrides_the_airspy(self):
        """Found in review: an older pair capture among the files made calibration measure the pair, not the
        Airspy just captured. Given as files, and found by the newest in the folder, in either order of age."""
        import os
        with tempfile.TemporaryDirectory() as tmp:
            old = self.capture(tmp, "20261101-010000")
            new = self.capture(tmp, "20261124-010000", kind="airspy")
            cal, settings, out = self.calibrate([old, new], settings_kind="airspy")
            self.assertNotIn("rtl_pair", cal)
            self.assertTrue(all(r["file"].startswith("20261124") for r in cal["measurements"]))
            self.assertIn("left out 1 capture(s) not made with the Airspy Mini", out)
            for pair_newer in (False, True):            # a stale boot clock can make the older file look newer
                os.utime(old, (2e9 if pair_newer else 1e9,) * 2)
                os.utime(new, (1e9 if pair_newer else 2e9,) * 2)
                cal, _, _ = self.calibrate([], settings_kind="airspy", iq_dir=tmp)
                self.assertNotIn("rtl_pair", cal)
                self.assertTrue(all(r["file"].startswith("20261124") for r in cal["measurements"]))
                cal, _, _ = self.calibrate([], settings_kind="rtlsdr2", iq_dir=tmp)
                self.assertEqual(cal["rtl_pair"]["setting_ppm"], [12, -20])

    def test_replaced_sticks_are_not_measured_with_the_old_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = self.capture(tmp, "20261101-010000", error=(40, 40), serials=("00000007", "00000008"))
            new = self.capture(tmp, "20261124-010000")
            cal, settings, out = self.calibrate([old, new])
        self.assertEqual(cal["rtl_pair"]["setting_ppm"], [12, -20])
        self.assertTrue(all(r["file"].startswith("20261124") for r in cal["measurements"]))

    def test_captures_split_at_another_handover_are_not_measured_together(self):
        """Found in review, 24 November 2026: the same sticks, tuned 1 MHz apart, were measured as one group, and
        the older file's carriers between the two handovers were put on the wrong stick."""
        import calibrate
        with tempfile.TemporaryDirectory() as tmp:
            old = self.capture(tmp, "20261101-010000", handover=391.5e6)
            new = self.capture(tmp, "20261124-010000")
            chosen, pair, others = calibrate.choose_captures([old, new], "rtlsdr2")
            self.assertEqual((chosen, others), ([new], [old]))
            self.assertEqual(pair["handover_hz"], 392.5e6)
            cal, settings, out = self.calibrate([old, new])
        self.assertTrue(all(r["file"].startswith("20261124") for r in cal["measurements"]))

    def test_an_older_airspy_capture_is_left_out(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = self.capture(tmp, "20261101-010000", kind="airspy")
            new = self.capture(tmp, "20261124-010000")
            cal, settings, out = self.calibrate([old, new])
        self.assertIn("left out 1 capture(s) not made with the RTL-SDR pair", out)
        self.assertEqual(cal["rtl_pair"]["setting_ppm"], [12, -20])
        self.assertTrue(all(r["file"].startswith("20261124") for r in cal["measurements"]))

    def test_an_airspy_calibration_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            cal, settings, out = self.calibrate([self.capture(tmp, "20261124-010000", kind="airspy")], "airspy")
        self.assertNotIn("rtl_pair", cal)
        self.assertIsInstance(cal["median_offset_hz"], float)
        self.assertLess(abs(cal["median_ppm"]), 0.3)
        self.assertEqual(settings, {"kind": "airspy"})

    def test_update_gain_refuses_what_it_should(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text(json.dumps({"kind": "rtlsdr2", "gain": {"tuner_gain_db10": 380}}))
            self.assertEqual(receiver.update_gain("rtlsdr2", {"ppm_a": 4}, path),
                             {"tuner_gain_db10": 380, "ppm_a": 4})
            for bad in ({"lna": 3}, {"ppm_a": 1.5}, {"ppm_b": True}):
                with self.assertRaises(receiver.ReceiverError):
                    receiver.update_gain("rtlsdr2", bad, path)
            with self.assertRaises(receiver.ReceiverError):
                receiver.update_gain("rsp1b", {"lna_state": 3}, path)
            self.assertEqual(json.loads(path.read_text()),
                             {"kind": "rtlsdr2", "gain": {"tuner_gain_db10": 380, "ppm_a": 4}})


class Presence(unittest.TestCase):
    def test_both_sticks_must_be_on_usb(self):
        import notify
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for k, usb in enumerate(["0bda:2838", "1d6b:0003"]):
                d = root / f"1-{k}"
                d.mkdir()
                (d / "idVendor").write_text(usb.split(":")[0] + "\n")
                (d / "idProduct").write_text(usb.split(":")[1] + "\n")
            self.assertEqual(receiver.present("rtlsdr2", str(root)), 1)
            with patch.object(receiver, "present", side_effect=lambda kind: receiver.KINDS and 1):
                self.assertFalse(notify.receiver_present("rtlsdr2"))
            with patch.object(receiver, "present", side_effect=lambda kind: 2):
                self.assertTrue(notify.receiver_present("rtlsdr2"))
        self.assertEqual(receiver.kinds_on_usb("Bus 001 Device 004: ID 0bda:2838 Realtek RTL2838UHIDIR"),
                         ["rtlsdr2"])
        with redirect_stdout(StringIO()):
            with patch.object(receiver, "present", return_value=1):
                self.assertEqual(receiver.main(["--present", "rtlsdr2"]), 1)
            with patch.object(receiver, "present", return_value=2):
                self.assertEqual(receiver.main(["--present", "rtlsdr2"]), 0)


if __name__ == "__main__":
    unittest.main()
