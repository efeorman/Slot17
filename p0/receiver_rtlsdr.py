#!/usr/bin/env python3
"""Two RTL-SDRs as one receiver (kind "rtlsdr2"), through librtlsdr (rtlsdr_ctypes.py) and rtl_combine.py.

One RTL-SDR hears about 2.5 MHz without losing samples; the band is 5 MHz. So stick A listens to 381.25 MHz and
stick B to 383.75 MHz, each at 2.56 MS/s (or 2.88), and rtl_combine.Stitcher joins them into the 6 MSPS int16
I/Q the detector already takes from the Airspy, handing over at 382.5 MHz, between channels 99 and 100.

Which stick is A: the one whose USB serial sorts first. Many sticks leave the factory with the same serial
(00000001); then the order can change from boot to boot, which only matters when the two need different ppm
corrections. Then both get the mean of the two, as long as they are at most SAME_SERIAL_PPM apart; the session
refuses to start only beyond that, and says how to give them distinct serials.

Each stick, once per session as with the Airspy:
  sample rate, ppm correction (librtlsdr corrects the tuner and the sample rate from it), centre frequency,
  manual tuner gain (the nearest the tuner offers), the RTL2832's own AGC off, bias-T off (no power to the
  antenna), buffers reset; then rtlsdr_read_async on a thread of its own, until close().

Three threads besides the detector's: a reader per stick, which only copies each USB transfer into that
stick's waiting room (no arithmetic in the callback), and one joining thread, which takes the same number of
samples from both every 50 ms, joins them and hands the result to the ring.

The two sticks have two crystals. Each channel comes from one stick only, so they never need to agree to the
sample, but their streams must not drift apart without limit. Each USB transfer is timed when it arrives
(time.monotonic). The earliest arrivals over 5 s, less the samples counted, give each stick's start; their
difference is how far apart the two halves are, and when that passes 5 ms one stick's waiting samples are
skipped until they agree again (a realignment, counted in the status, not a loss). The same timing finds lost
samples, which librtlsdr does not report: a stick whose arrivals fall at least 20 ms further behind than the
other's has lost that many. That is reported as dropped samples, up to 5 s after the loss. Shorter losses are
not seen; how often a stick loses samples at all is a question for the real hardware (RECEIVERS.md).
"""
import ctypes
import threading
import time
from collections import deque

import numpy as np

import rtl_combine as C
import rtlsdr_ctypes as L
from receiver import ReceiverError, name

OUT_FS = C.OUT_FS
BUF_LEN = 65536                  # bytes per USB transfer: 32768 samples, 12.8 ms at 2.56 MS/s (a multiple of 16384)
BUF_NUM = 24                     # transfers queued: about 0.3 s in which the host may fall behind without loss
ROOM_S = 4.0                     # waiting room per stick
BATCH_BLOCKS = 25                # join 50 ms at a time (25 blocks of 2 ms) ...
MAX_BLOCKS = 100                 # ... and at most 200 ms in one go
CHECK_S = 1.0                    # how often the timing is checked
TIMING_WINDOW_S = 5.0            # earliest arrival over this long
ALIGN_TOL_S = 0.005              # the halves may be this far apart before one is realigned
DRIFT_STEP_S = 0.0025            # a gap that grew less than this since the previous check crept there: a crystal's drift
SAME_SERIAL_PPM = 3              # two sticks with one serial share a ppm when theirs are at most this far apart
LOSS_S = 0.020                   # a stick this much further behind than the other has lost samples
SILENT_S = 3.0                   # a stick without a transfer this long while the other streams has stopped
SETTLE_S = 1.0                   # a stick's first second isn't timed: its first transfers can come in a rush
OUT_GAIN = 12000.0               # one stick's full scale in the int16 the ring holds (room for both halves' peaks)
GOOD_TUNERS = {1: "E4000", 2: "FC0012", 3: "FC0013", 5: "R820T", 6: "R828D"}    # FC2580 can't tune 380 MHz


def nearest_gain(offered, wanted):
    """The tuner's gain closest to `wanted` (tenths of a dB); ties go to the lower one."""
    if not offered:
        return None
    return min(offered, key=lambda g: (abs(g - wanted), g))


class Stick:
    """One RTL-SDR: its device, its waiting room and its counters."""

    def __init__(self, label, index, strings, room_bytes):
        self.label, self.index = label, index
        self.manufacturer, self.product, self.serial = strings
        self.dev = ctypes.c_void_p()
        self.opened = self.streaming = False
        self.room = np.zeros(room_bytes, np.uint8)
        self.addr, self.cap = self.room.ctypes.data, room_bytes
        self.written = self.read = 0                 # bytes ever written and read
        self.skip = 0                                # samples still to skip (realignment)
        self.skipped = 0                             # samples skipped so far (realignment)
        self.excused = 0                             # of those, samples it only had in excess: no loss (below)
        self.arrivals = deque()                      # (monotonic time, samples received by then), once settled
        self.first_arrival = self.last_arrival = None
        self.received = 0                            # samples received from USB
        self.callbacks = 0
        self.overflow_lost = 0                       # samples overwritten unread (joining fell 4 s behind)
        self.lost = 0                                # samples found lost from the timing
        self.saturated = 0                           # bytes at 0 or 255: the 8-bit converter clipping
        self.last_share = 0.0
        self.baseline = None                         # the start found at the previous check
        self.thread = self.callback = None
        self.rc = None
        self.tuner = self.gain_db10 = self.rate = self.centre = self.ppm = None

    def describe(self):
        return {"label": self.label, "serial": self.serial, "manufacturer": self.manufacturer,
                "product": self.product, "tuner": self.tuner, "gain_db10": self.gain_db10, "ppm": self.ppm,
                "centre_hz": self.centre, "sample_rate_hz": self.rate}


class Receiver:
    """Two RTL-SDRs, found, configured, streamed and joined, once per session."""

    def __init__(self, kind="rtlsdr2", fs=OUT_FS, centre_hz=382.5e6, gain=None, sink=None, lib=None, clock=None):
        if kind != "rtlsdr2":
            raise ReceiverError(f"{kind!r} is not a receiver this module drives")
        if int(fs) != OUT_FS:
            raise ReceiverError(f"the {name(kind)} delivers {OUT_FS / 1e6:g} MSPS here, not {fs / 1e6:g}")
        g = dict(gain or {})
        self.kind = kind
        self.fs = OUT_FS
        self.centre_hz = float(centre_hz)
        self.gain_db10 = int(g.get("tuner_gain_db10", 297))
        self.rate = int(g.get("rate_khz", 2560)) * 1000
        self.ppm = (int(g.get("ppm_a", 0)), int(g.get("ppm_b", 0)))
        if self.rate not in C.RATES:
            raise ReceiverError(f"rate_khz {self.rate // 1000}: choose {' or '.join(str(r // 1000) for r in C.RATES)}")
        if not 0 <= self.gain_db10 <= 500:
            raise ReceiverError(f"tuner_gain_db10 {self.gain_db10} is outside 0 to 500 (tenths of a dB)")
        if not all(-200 <= p <= 200 for p in self.ppm):
            raise ReceiverError(f"ppm_a/ppm_b {self.ppm} are outside -200 to 200")
        self.gain = ("rtlsdr2", self.gain_db10)      # never one of the Airspy's (lna, mixer, vga) references
        self.sink = sink
        self.clock = clock or time.monotonic
        try:
            self.lib = lib if lib is not None else L.load()
        except OSError as exc:
            raise ReceiverError(f"librtlsdr isn't installed ({exc}); run: bash setup.sh car --receiver rtlsdr2") from exc
        self.stitcher = C.Stitcher(self.rate, centre=self.centre_hz, gain=OUT_GAIN)
        self.sticks = []
        self.opened = self.streaming = False
        self.info = None
        self._fault = None
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._joiner = None
        self._started = False
        self._aligned = False
        self._prev_err = None                        # the alignment gap at the previous check (samples)
        # Output samples already reported as lost (see _newly_lost).
        self._loss_reported = 0
        self._t_start = None
        self.counters = {"realignments": 0, "alignment_ms": None, "joined_s": 0.0, "backlog_ms": 0.0,
                         "backlog_max_ms": 0.0, "joined_lost_samples": 0}

    # --- the session ---------------------------------------------------------------------------------
    def bind(self, sink):
        self.sink = sink

    def open(self):
        lib = self.lib
        count = int(lib.rtlsdr_get_device_count())
        if count < 2:
            raise ReceiverError(f"{count} RTL-SDR{'' if count == 1 else 's'} found; the {name(self.kind)} needs two "
                                "(both plugged in, on a powered hub or the computer's own ports)")
        if count > 2:
            raise ReceiverError(f"{count} RTL-SDRs found; Slot17 uses exactly two. Unplug the others.")
        found = sorted(((L.usb_strings(lib, i), i) for i in range(count)), key=lambda x: (x[0][2], x[1]))
        serials = [s[2] for s, _ in found]
        shared = None
        if serials[0] == serials[1] and self.ppm[0] != self.ppm[1]:
            # Which stick is A can change from one start to the next, and its ppm with it. Most sticks leave the
            # factory as 00000001, and two good ones measure, say, 0 and 1 ppm: until 26 November 2026 that refused
            # every start (found in review). Both get the mean, each then at most 1.5 ppm (600 Hz) off, which the
            # recognition takes in its stride. Further apart, the mean would cost reception: then say what to do.
            if abs(self.ppm[0] - self.ppm[1]) > SAME_SERIAL_PPM:
                raise ReceiverError(
                    f"both RTL-SDRs have serial {serials[0]!r}, so which is A can change from one start to the next, "
                    f"and their ppm corrections ({self.ppm[0]}, {self.ppm[1]}) are too far apart to share. Give one "
                    "another serial, with only that one plugged in: rtl_eeprom -s 00000002, then unplug it and plug "
                    "it back in.")
            shared = int(round((self.ppm[0] + self.ppm[1]) / 2))
            self.ppm = (shared, shared)
        room = int(ROOM_S * self.rate) * 2
        self.sticks = [Stick(label, index, strings, room) for label, (strings, index) in zip("AB", found)]
        for st, centre, ppm in zip(self.sticks, self.stitcher.centres, self.ppm):
            self._configure(st, centre, ppm)
        self.info = {"model": name(self.kind), "sticks": [st.describe() for st in self.sticks],
                     "sample_rates": [self.fs], "rate_per_stick": self.rate, "handover_hz": self.centre_hz,
                     "same_serial": serials[0] == serials[1]}
        if shared is not None:
            self.info["shared_ppm"] = shared
        return self.info

    def _configure(self, st, centre, ppm):
        lib, what = self.lib, f"RTL-SDR {st.label} ({st.serial})"
        rc = lib.rtlsdr_open(ctypes.byref(st.dev), st.index)
        if rc == -6:                                 # LIBUSB_ERROR_BUSY: usb_claim_interface failed
            raise ReceiverError(f"{what} is in use: by the DVB-T television driver, or another program. "
                                "bash setup.sh car --receiver rtlsdr2 blacklists that driver (then unplug and "
                                "replug both sticks); otherwise stop the program using it (rtl_tcp, rtl_test, ...)")
        if rc == -3:                                 # LIBUSB_ERROR_ACCESS
            raise ReceiverError(f"{what}: no permission to open it. The rtl-sdr package's udev rules give it to "
                                "the plugdev group: bash setup.sh car --receiver rtlsdr2 checks both")
        L.check(rc, f"{what}: rtlsdr_open")
        st.opened = self.opened = True
        t = int(lib.rtlsdr_get_tuner_type(st.dev))
        st.tuner = L.TUNERS.get(t, str(t))
        if t not in GOOD_TUNERS:
            raise ReceiverError(f"{what} has a {st.tuner} tuner, which can't receive 380 to 385 MHz "
                                "(R820T, R828D and E4000 sticks can)")
        L.check(lib.rtlsdr_set_sample_rate(st.dev, self.rate), f"{what}: rtlsdr_set_sample_rate({self.rate})")
        L.set_ppm(lib, st.dev, ppm)
        L.check(lib.rtlsdr_set_center_freq(st.dev, int(round(centre))), f"{what}: rtlsdr_set_center_freq")
        L.check(lib.rtlsdr_set_tuner_gain_mode(st.dev, 1), f"{what}: rtlsdr_set_tuner_gain_mode(manual)")
        offered = L.tuner_gains(lib, st.dev)
        gain = nearest_gain(offered, self.gain_db10)
        if gain is None:
            raise ReceiverError(f"{what}: the tuner offers no manual gains")
        L.check(lib.rtlsdr_set_tuner_gain(st.dev, gain), f"{what}: rtlsdr_set_tuner_gain({gain})")
        L.check(lib.rtlsdr_set_agc_mode(st.dev, 0), f"{what}: rtlsdr_set_agc_mode(off)")
        if hasattr(lib, "rtlsdr_set_bias_tee"):
            L.check(lib.rtlsdr_set_bias_tee(st.dev, 0), f"{what}: rtlsdr_set_bias_tee(off)")
        L.check(lib.rtlsdr_reset_buffer(st.dev), f"{what}: rtlsdr_reset_buffer")
        st.rate = int(lib.rtlsdr_get_sample_rate(st.dev))
        st.centre = int(lib.rtlsdr_get_center_freq(st.dev))
        st.gain_db10, st.ppm = gain, ppm
        if abs(st.rate - self.rate) > 1:
            raise ReceiverError(f"{what} runs at {st.rate} S/s instead of {self.rate}")

    def start(self):
        """The joining thread first, then a reader per stick: both start streaming within a few milliseconds."""
        self._t_start = self.clock()
        self._joiner = threading.Thread(target=self._join_loop, name="rtl-join", daemon=True)
        self._joiner.start()
        for st in self.sticks:
            st.callback = L.ReadCallback(lambda buf, n, ctx, st=st: self._on_transfer(st, buf, n))
            st.thread = threading.Thread(target=self._read, args=(st,), name=f"rtl-read-{st.label}", daemon=True)
            st.streaming = True
            st.thread.start()
        self.streaming = True

    def fault(self):
        return self._fault

    def status(self):
        out = dict(self.counters)
        for st in self.sticks:
            k = st.label.lower()
            out[f"callbacks_{k}"] = st.callbacks
            out[f"lost_samples_{k}"] = st.lost + st.overflow_lost
            out[f"saturation_{k}"] = round(st.last_share, 5)
        return out

    def gain_record(self):
        """What was asked for; written before the sticks are opened, so what each applied is in info["sticks"]."""
        return {"tuner_gain_db10": self.gain_db10, "agc": False}

    def session_fields(self):
        return {"agc": False, "bias_t": False, "rate_per_stick": self.rate, "ppm": list(self.ppm),
                "stick_centres_hz": list(self.stitcher.centres), "handover_hz": self.centre_hz}

    def close(self):
        """Cancel both streams, wait for their threads and the joining one, then close each stick once. A stick
        whose stream will not end is not closed under it (that would crash); the process's end releases it."""
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        for st in self.sticks:
            if st.streaming:
                try:
                    self.lib.rtlsdr_cancel_async(st.dev)
                except Exception:            # noqa: BLE001, S110 - closing must reach every stick
                    pass
        for st in self.sticks:
            if st.thread is not None:
                st.thread.join(timeout=3)
        if self._joiner is not None:
            self._joiner.join(timeout=3)
        for st in self.sticks:
            stuck = st.thread is not None and st.thread.is_alive()
            if st.opened and not stuck:
                try:
                    self.lib.rtlsdr_close(st.dev)
                except Exception:            # noqa: BLE001, S110
                    pass
                st.opened = False
            st.streaming = stuck
        self.streaming = any(st.streaming for st in self.sticks)

    # --- the readers ---------------------------------------------------------------------------------
    def _read(self, st):
        rc = self.lib.rtlsdr_read_async(st.dev, st.callback, None, BUF_NUM, BUF_LEN)
        st.rc = rc
        if not self._stop.is_set() and self._fault is None:
            self._fault = f"RTL-SDR {st.label} ({st.serial}) stopped streaming (rtlsdr_read_async returned {rc})"
        with self._cond:
            self._cond.notify_all()

    def _on_transfer(self, st, buf, n):
        """librtlsdr's callback, on that stick's reader thread: copy the transfer into the waiting room."""
        try:
            now = self.clock()
            # Every byte is kept, even an odd number of them: the waiting room is the stick's byte stream, read
            # in whole I/Q pairs, so a short transfer can't shift I into Q for the rest of the session.
            n = int(n)
            with self._cond:
                free = st.cap - (st.written - st.read)
                if n > free:                         # 4 s unread: the oldest samples go
                    over = (n - free + 1) & ~1      # whole pairs, so reading stays on I
                    st.read += over
                    st.overflow_lost += over // 2
                w = st.written % st.cap
                first = min(n, st.cap - w)
                ctypes.memmove(st.addr + w, buf, first)
                if n > first:
                    ctypes.memmove(st.addr, buf + first, n - first)
                st.written += n
                st.received = st.written // 2
                st.callbacks += 1
                if st.first_arrival is None:
                    st.first_arrival = now
                st.last_arrival = now
                if now - st.first_arrival >= SETTLE_S:
                    st.arrivals.append((now, st.received))
                self._cond.notify_all()
        except Exception:                            # noqa: BLE001 - nothing may escape into librtlsdr
            if self.sink is not None:
                self.sink.lost(lambda: int(n) // 2 * OUT_FS // self.rate)

    # --- joining -------------------------------------------------------------------------------------
    def _ready(self):
        """Samples both sticks can give now, after any skipping still owed (under the lock)."""
        for st in self.sticks:
            have = (st.written - st.read) // 2
            take = min(st.skip, have)
            st.read += 2 * take
            st.skip -= take
            st.skipped += take
        return min((st.written - st.read) // 2 for st in self.sticks)

    def _take(self, st, k):
        """k samples (2k bytes) out of a stick's waiting room, as one array (under the lock)."""
        r = st.read % st.cap
        first = min(2 * k, st.cap - r)
        out = np.empty(2 * k, np.uint8)
        out[:first] = st.room[r:r + first]
        if first < 2 * k:
            out[first:] = st.room[:2 * k - first]
        st.read += 2 * k
        return out

    def _join_loop(self):
        next_check = self.clock() + CHECK_S
        try:
            while not self._stop.is_set():
                self.join_once(wait=0.25)
                now = self.clock()
                if now >= next_check:
                    next_check = now + CHECK_S
                    self._check_timing(now)
        except Exception as exc:                     # noqa: BLE001 - becomes the session's fault, never silent
            if self._fault is None:
                self._fault = f"joining the two RTL-SDRs failed: {type(exc).__name__}: {exc}"

    def join_once(self, wait=0.0):
        """Join what both sticks have waiting, when that is at least one batch (waiting up to `wait` seconds for
        it); report samples overwritten unread as dropped. True when something was joined."""
        n_in = self.stitcher.n_in
        with self._cond:
            ready = self._ready()
            if ready < BATCH_BLOCKS * n_in and wait:
                self._cond.wait(wait)
                ready = self._ready()
            raw = None
            if ready >= BATCH_BLOCKS * n_in and not self._stop.is_set():
                k = min(ready, MAX_BLOCKS * n_in) // n_in * n_in
                raw = [self._take(st, k) for st in self.sticks]
            # At every attempt, not only after a join: a stick that delivers nothing while the other waits is
            # the backlog worth seeing (found in review, 24 November 2026).
            backlogs = [(st.written - st.read) // 2 for st in self.sticks]
            self.counters["backlog_ms"] = round(min(backlogs) / self.rate * 1000, 1)
            # The larger one too: one stick's reader running behind shows here, not in the smaller one.
            self.counters["backlog_max_ms"] = round(max(backlogs) / self.rate * 1000, 1)
            lost = self._newly_lost()
        if lost > 0:
            self.sink.drop(lost)
        if raw is not None:
            self._join(raw)
        return raw is not None

    def _join(self, raw):
        for st, r in zip(self.sticks, raw):
            sat = int(np.count_nonzero(r == 0)) + int(np.count_nonzero(r == 255))
            st.saturated += sat
            st.last_share = sat / len(r)
        y = self.stitcher.process(C.from_u8(raw[0]), C.from_u8(raw[1]))
        f = y.view(np.float32)                       # already interleaved I, Q
        np.clip(f, -32768.0, 32767.0, out=f)
        np.rint(f, out=f)
        out = f.astype(np.int16)
        if not self._started:
            self._started = True
            # The join's output is DELAY_S behind the sticks: the session's start moves back by as much (F15).
            self.sink.first(len(y), C.DELAY_S)
        self.sink.push(out)
        self.counters["joined_s"] = round(self.counters["joined_s"] + len(raw[0]) / 2 / self.rate, 3)

    def _check_timing(self, now):
        """Each stick's start from its earliest arrivals; realign the halves, and find lost samples."""
        starts = []
        with self._cond:
            for st in self.sticks:
                while st.arrivals and st.arrivals[0][0] < now - TIMING_WINDOW_S:
                    st.arrivals.popleft()
                if len(st.arrivals) < 8:
                    starts.append(None)
                    continue
                starts.append(min(t - got / self.rate for t, got in st.arrivals))
            quiet = [st for st in self.sticks if now - (st.last_arrival or self._t_start) > SILENT_S]
        if len(quiet) == 1 and self._fault is None:
            st = quiet[0]
            self._fault = (f"RTL-SDR {st.label} ({st.serial}) has delivered nothing for {SILENT_S:g} s"
                           + ("" if st.last_arrival else " since the start") + " while the other streams")
            return
        if None in starts:
            return
        a, b = self.sticks
        # Lost samples: one stick's start moving later than the other's by LOSS_S or more.
        if a.baseline is not None:
            rise = (starts[0] - a.baseline, starts[1] - b.baseline)
            for st, mine, other in ((a, rise[0], rise[1]), (b, rise[1], rise[0])):
                if mine - other >= LOSS_S and mine >= LOSS_S:
                    lost = int(round((mine - max(other, 0.0)) * self.rate))
                    if lost > 0:
                        st.lost += lost             # reported below, with the realignment it causes
        a.baseline, b.baseline = starts
        # Alignment: A's sample i and B's sample i should be from the same moment.
        with self._cond:
            want = int(round((starts[1] - starts[0]) * self.rate))      # samples A must have skipped more than B
            # Both sticks always give the same number to the join, so what one has passed over more than the
            # other (skipped, or overwritten unread) is the difference of their read positions, plus skips owed.
            have = (a.read // 2 + a.skip) - (b.read // 2 + b.skip)
            err = want - have
            self.counters["alignment_ms"] = round(err / self.rate * 1000, 2)
            if abs(err) > (ALIGN_TOL_S * self.rate if self._aligned else 64):
                st = a if err > 0 else b
                st.skip += abs(err)
                # Skipping loses nothing when it only takes away samples the stick has in excess: its extra first
                # milliseconds at the start, or what a faster crystal gives, which builds up so slowly that the
                # gap creeps over the tolerance. A gap that appears at once (a stretch lost on USB, a reader that
                # fell behind) costs the joined stream that stretch. Until 26 November 2026 both counted alike, so
                # the start's skip on one stick and a drift's on the other made a loss that never happened, and
                # the session degraded (found in review).
                if not self._aligned or (self._prev_err is not None
                                         and abs(err - self._prev_err) < DRIFT_STEP_S * self.rate):
                    st.excused += abs(err)
                if self._aligned:
                    self.counters["realignments"] += 1
                self._prev_err = 0
            else:
                self._prev_err = err
            self._aligned = True
            lost = self._newly_lost()
        if lost > 0:
            self.sink.drop(lost)

    def _newly_lost(self):
        """Output samples the joined stream has lost since the last report (under the lock, on the joining thread).

        Each stick passes over samples it never gives the join: overwritten unread, skipped to realign, a skip
        still owed, and samples it lost before they reached the computer (found from its timing). Once the halves
        are aligned again, both have passed over the same stretch of time, so either total is the joined stream's
        loss; while one of them still owes its skip, the smaller is what both have passed over. The smaller also
        leaves out the skips that only keep a faster crystal in step, which lose nothing.

        Until 24 November 2026 overflow was reported apart from the rest, first as the two sticks' sum (twice the
        time), then as the larger of their totals, which lost a later overflow of the other stick (both found in
        review). One rule now covers overflow, losses found from the timing and the gaps realignment makes."""
        total = min(st.overflow_lost + st.skipped + st.skip + st.lost - st.excused for st in self.sticks) \
            * OUT_FS // self.rate
        new = total - self._loss_reported
        if new <= 0:
            return 0
        self._loss_reported = total
        self.counters["joined_lost_samples"] = total
        return new
