#!/usr/bin/env python3
"""The SDRplay RSP1B (and RSP1A) through the SDRplay API 3.15 (sdrplay_ctypes.py).

How it is driven, in SDRplay's own order (API specification 3.15, its example program):
Open, ApiVersion, LockDeviceApi, GetDevices, SelectDevice, UnlockDeviceApi, GetDeviceParams, set the
parameters, Init (which starts the stream); at the end Uninit, ReleaseDevice, Close. Once per session,
like the Airspy: a failure ends the session rather than reopening the receiver.

What it is set to, for the detector's 200 channels of 380 to 385 MHz:
  * 6 MSPS, zero IF, 5 MHz IF bandwidth: 6 MSPS is still the RSP1B's native 14-bit range (2 - 6.048 MSPS).
    The zero-IF DC spike falls on the centre, 382.5 MHz, which lies between two channels of the
    380.0125 + n x 0.025 MHz raster; the API's DC offset and IQ imbalance correction stay on (its defaults).
  * AGC off: matches, the noise floor and 'Rising' compare levels over minutes, so the gain must not move.
  * Fixed gain: LNA state and IF gain reduction (receiver.py has the defaults; --lna-state, --if-gain-reduction).
  * No bias-T (no power to the antenna), no notch filters (they are for AM/FM and DAB broadcast, far below).
  * ppm 0: the frequency error is corrected in software from calibration.json, as for the Airspy.

Samples arrive as two arrays (I and Q) per callback, thousands of times a second. Each callback only copies
them into two waiting arrays (two memmoves); every FLUSH_SAMPLES (about 2.7 ms) they are interleaved into the
int16 I/Q the ring holds, in one go. On the Pi this cut the cost of the callbacks to a fraction (measured with
scripts/sdrplay_fake_test.py); 2.7 ms is nothing against the detector's 0.25 s windows.
Lost samples are found from firstSampleNum, the running number of each callback's first sample (as in the
SoapySDRPlay3 maintainer's own recorder): a callback that starts later than the previous one ended has lost
the difference. Should nearly every early callback look like a gap, that number means something else on this
hardware and gap finding switches itself off, saying so in the status, rather than calling every window
degraded.
"""
import ctypes
import time

import numpy as np

import sdrplay_ctypes as S
from receiver import ReceiverError, name

WANTED_HW = {"rsp1b": S.RSP1B_ID, "rsp1a": S.RSP1A_ID}
SAMPLE_RATE = 6_000_000
CHECK_CALLBACKS = 200            # judge firstSampleNum over the first ~200 callbacks
SUSPECT_SHARE = 0.9              # ... and give up on it if this share of them looked like gaps
FLUSH_SAMPLES = 16384            # interleave and hand to the ring every this many samples (2.7 ms at 6 MSPS)
WAITING = 1 << 17                # room in the waiting arrays, samples per plane
SERVICE_WAIT_S = 45.0            # at power-up: how long SDRplay's service may take to answer and find the receiver
# Receivers whose Uninit failed: the API's thread may still call them, so their callbacks, buffers and sink stay
# alive until the process ends (found in review, 24 November 2026).
UNSTOPPED = []


class Receiver:
    """One SDRplay session: find the device, configure it, stream, and end it once."""

    def __init__(self, kind="rsp1b", fs=SAMPLE_RATE, centre_hz=382.5e6, gain=None, sink=None, lib=None,
                 transfer="isoch", clock=None, sleep=None):
        if kind not in WANTED_HW:
            raise ReceiverError(f"{kind!r} is not an SDRplay receiver this module drives")
        if int(fs) != SAMPLE_RATE:
            raise ReceiverError(f"the {name(kind)} runs at {SAMPLE_RATE / 1e6:g} MSPS here, not {fs / 1e6:g}")
        g = gain or {}
        self.kind = kind
        self.fs = SAMPLE_RATE
        self.centre_hz = float(centre_hz)
        self.lna_state = int(g.get("lna_state", 2))
        self.gr_db = int(g.get("if_gain_reduction_db", 40))
        if not 0 <= self.lna_state < S.LNA_STATES_60_420MHZ:
            raise ReceiverError(f"LNA state {self.lna_state} is outside 0 to {S.LNA_STATES_60_420MHZ - 1}")
        if not S.GR_MIN <= self.gr_db <= S.GR_MAX:
            raise ReceiverError(f"IF gain reduction {self.gr_db} dB is outside {S.GR_MIN} to {S.GR_MAX}")
        if transfer not in ("isoch", "bulk"):
            raise ReceiverError(f"transfer mode {transfer!r} is neither isoch nor bulk")
        self.transfer = transfer
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep
        self.gain = (self.lna_state, self.gr_db)
        self.sink = sink
        # Loaded now, where the Airspy's library is loaded too: a missing API fails before the session starts.
        try:
            self.lib = lib if lib is not None else S.load()
        except OSError as exc:
            raise ReceiverError(f"the SDRplay API isn't installed ({exc}); run: bash setup.sh car --receiver {kind}") from exc
        self.api_open = self.selected = self.streaming = False
        self.opened = False                  # a device was selected: watch.py's "receiver opened"
        self.devs = (S.DeviceT * S.MAX_DEVICES)()   # kept for the whole session: the API is given pointers into it
        self.device = None
        self.params = None
        self.info = None
        self._fault = None
        self._expected = None
        self._early = [0, 0]                 # callbacks judged, of which looked like gaps
        self.flush_samples = FLUSH_SAMPLES   # tests set 1 to see every callback's samples at once
        self._i = np.empty(WAITING, np.int16)
        self._q = np.empty(WAITING, np.int16)
        self._out = np.empty(2 * WAITING, np.int16)
        self._i_addr, self._q_addr = self._i.ctypes.data, self._q.ctypes.data
        self._fill = 0
        self._started = False
        self._closing = False                # set by close(): a callback after it hands nothing on
        self.counters = {"callbacks": 0, "gaps": 0, "lost_samples": 0, "resets": 0, "parameter_changes": 0,
                         "overloads": 0, "overload_now": False, "gain_events": 0, "samples_per_packet": None,
                         "gap_detection": "on", "api_errors": [], "late_callbacks": 0, "service_waits": 0}
        # Kept on the instance for the whole session: the API calls them from its own thread.
        self._stream_cb = S.StreamCallback(self._stream)
        self._event_cb = S.EventCallback(self._event)
        self._fns = S.CallbackFnsT(self._stream_cb, self._stream_cb, self._event_cb)

    # --- the session ---------------------------------------------------------------------------------
    def bind(self, sink):
        """Where the samples go; before open()."""
        self.sink = sink

    def _noted(self, code, what):
        """A call whose failure doesn't end the session (unlock, an acknowledgement, the steps of closing): kept,
        the first few, in the status, rather than passed over as it was until 24 November 2026 (found in review).
        Returns whether it succeeded."""
        if code == S.SUCCESS:
            return True
        errors = self.counters["api_errors"]
        if len(errors) < 5:
            errors.append(f"{what}: {S.ERRORS.get(code, code)}")
        return False

    def _check(self, code, what):
        if code == S.SERVICE_NOT_RESPONDING:
            raise ReceiverError(f"{what}: the SDRplay API service isn't answering. Check it with: "
                                "systemctl status sdrplay")
        S.check(self.lib, code, what)

    def open(self):
        lib = self.lib
        # At a car's power-up the detector can be quicker than SDRplay's service, which may not answer yet, or not
        # have found the receiver yet. Asking again opens nothing, so both are waited for, up to SERVICE_WAIT_S.
        # Until 26 November 2026 the first answer counted, and the whole drive went without detection (found in
        # review).
        deadline = self.clock() + SERVICE_WAIT_S
        code = lib.sdrplay_api_Open()
        while code == S.SERVICE_NOT_RESPONDING and self.clock() < deadline:
            self.counters["service_waits"] += 1
            self.sleep(1.0)
            code = lib.sdrplay_api_Open()
        self._check(code, "sdrplay_api_Open")
        self.api_open = True
        version = ctypes.c_float(0.0)
        self._check(lib.sdrplay_api_ApiVersion(ctypes.byref(version)), "sdrplay_api_ApiVersion")
        if abs(version.value - S.API_VERSION) > 1e-3:
            raise ReceiverError(f"SDRplay API {version.value:.2f} is installed; Slot17 was checked against "
                                f"{S.API_VERSION:.2f} only. Install {S.API_VERSION:.2f}: bash setup.sh car --receiver {self.kind}")
        while True:
            self._check(lib.sdrplay_api_LockDeviceApi(), "sdrplay_api_LockDeviceApi")
            try:
                count = ctypes.c_uint(0)
                self._check(lib.sdrplay_api_GetDevices(self.devs, ctypes.byref(count), S.MAX_DEVICES),
                            "sdrplay_api_GetDevices")
                found = [self.devs[i] for i in range(min(count.value, S.MAX_DEVICES))]
                want = WANTED_HW[self.kind]
                # One the API marks free (valid); another program may hold one of the same model.
                mine = [d for d in found if d.hwVer == want]
                pick = next((d for d in mine if d.valid), None)
                if pick is None and self.clock() < deadline:
                    pass                     # not found, or not free, yet: ask again below, after unlocking
                elif pick is None and mine:
                    raise ReceiverError(f"the {name(self.kind)} is held by another program, or SDRplay's service is "
                                        "stuck: close other SDR programs, or run: sudo systemctl restart sdrplay")
                elif pick is None:
                    seen = ", ".join(f"{S.HW_NAMES.get(d.hwVer, d.hwVer)} {d.SerNo.decode(errors='replace')}"
                                     for d in found) or "no SDRplay receiver at all"
                    raise ReceiverError(f"no {name(self.kind)} found; the SDRplay API sees: {seen}")
                else:
                    self.device = pick       # a view into self.devs, as SDRplay's example uses &devs[i]
                    self._check(lib.sdrplay_api_SelectDevice(ctypes.byref(self.device)), "sdrplay_api_SelectDevice")
                    self.selected = self.opened = True
            finally:
                self._noted(lib.sdrplay_api_UnlockDeviceApi(), "sdrplay_api_UnlockDeviceApi")
            if self.selected:
                break
            self.counters["service_waits"] += 1
            self.sleep(1.0)
        params = ctypes.POINTER(S.DeviceParamsT)()
        self._check(lib.sdrplay_api_GetDeviceParams(self.device.dev, ctypes.byref(params)),
                    "sdrplay_api_GetDeviceParams")
        if not params:
            raise ReceiverError("the SDRplay API returned no device parameters")
        problems = S.check_domains(params.contents)
        if problems:
            raise ReceiverError("the SDRplay API's parameters don't match the layout Slot17 was checked against "
                                f"({'; '.join(problems[:4])}); not starting the receiver with them")
        self.params = params
        dev = params.contents.devParams.contents
        ch = params.contents.rxChannelA.contents
        dev.fsFreq.fsHz = float(self.fs)
        dev.ppm = 0.0
        dev.mode = S.BULK if self.transfer == "bulk" else S.ISOCH
        dev.rsp1aParams.rfNotchEnable = 0
        dev.rsp1aParams.rfDabNotchEnable = 0
        t = ch.tunerParams
        t.bwType = S.BW_5_000
        t.ifType = S.IF_ZERO
        t.loMode = S.LO_AUTO
        t.gain.gRdB = self.gr_db
        t.gain.LNAstate = self.lna_state
        t.gain.minGr = S.NORMAL_MIN_GR
        t.rfFreq.rfHz = self.centre_hz
        c = ch.ctrlParams
        c.agc.enable = S.AGC_DISABLE
        c.dcOffset.DCenable = 1
        c.dcOffset.IQenable = 1
        c.decimation.enable = 0
        c.decimation.decimationFactor = 1
        ch.rsp1aTunerParams.biasTEnable = 0
        self.info = {"model": S.HW_NAMES.get(self.device.hwVer, str(self.device.hwVer)),
                     "serial": self.device.SerNo.decode("ascii", "replace"),
                     "api_version": round(version.value, 2), "hw_ver": int(self.device.hwVer),
                     "sample_rates": [self.fs], "if_mode": "zero", "if_bandwidth_hz": 5_000_000,
                     "transfer": self.transfer}
        return self.info

    def start(self):
        """Init configures the device and starts the stream; callbacks begin on the API's own thread."""
        self._check(self.lib.sdrplay_api_Init(self.device.dev, ctypes.byref(self._fns), None), "sdrplay_api_Init")
        self.streaming = True
        try:
            self.counters["samples_per_packet"] = int(self.params.contents.devParams.contents.samplesPerPkt)
        except Exception:                    # noqa: BLE001, S110 - an output detail, never a failure
            pass

    def fault(self):
        return self._fault

    def status(self):
        return dict(self.counters)

    def gain_record(self):
        return {"lna_state": self.lna_state, "if_gain_reduction_db": self.gr_db, "agc": False}

    def session_fields(self):
        return {"agc": False, "if_mode": "zero", "if_bandwidth_hz": 5_000_000, "dc_correction": True,
                "iq_correction": True, "bias_t": False, "transfer": self.transfer}

    def close(self):
        """Uninit, ReleaseDevice, Close: each once, each attempted even when an earlier one failed. Uninit stops
        the API's thread, so the samples still waiting are handed on after it; when Uninit fails, that thread may
        still be calling, so they are left alone and counted as lost. A step that fails is kept in the status."""
        lib = self.lib
        self._closing = True
        stopped = not self.streaming
        for done, what, call in (
                (self.streaming, "sdrplay_api_Uninit", lambda: lib.sdrplay_api_Uninit(self.device.dev)),
                (self.selected, "sdrplay_api_ReleaseDevice", lambda: lib.sdrplay_api_ReleaseDevice(ctypes.byref(self.device))),
                (self.api_open, "sdrplay_api_Close", lambda: lib.sdrplay_api_Close())):
            if done:
                try:
                    ok = self._noted(call(), what)
                except Exception as exc:     # noqa: BLE001 - ending the session must reach Close
                    ok = False
                    if len(self.counters["api_errors"]) < 5:
                        self.counters["api_errors"].append(f"{what}: {type(exc).__name__}: {exc}")
                if what == "sdrplay_api_Uninit":
                    stopped = ok
        self.streaming = self.selected = self.api_open = False
        if not stopped and self not in UNSTOPPED:
            UNSTOPPED.append(self)
        try:
            if stopped:
                self._flush()
        except Exception as exc:             # noqa: BLE001 - the session is over either way; counted below
            if len(self.counters["api_errors"]) < 5:
                self.counters["api_errors"].append(f"last batch not handed on: {type(exc).__name__}: {exc}")
        # What is still waiting is lost: after a failed Uninit (the API's thread may still be calling), or when the
        # last push failed (until 24 November 2026 that was swallowed uncounted; found in review). Once: a second
        # close() finds nothing waiting.
        if self._fill:
            left, self._fill = self._fill, 0
            try:
                if self.sink is not None:
                    self.sink.lost(left)
            except Exception:                # noqa: BLE001, S110 - nothing more to do with them
                pass

    # --- the API's thread ----------------------------------------------------------------------------
    def _flush(self):
        """Interleave the waiting samples and hand them to the ring."""
        f = self._fill
        if f:
            out = self._out[:2 * f]
            out[0::2] = self._i[:f]
            out[1::2] = self._q[:f]
            self.sink.push(out)              # Ring.push copies, so _out is free again at once
            self._fill = 0                   # only now: a push that fails leaves them counted as waiting

    def _stream(self, xi, xq, params, num_samples, reset, context):
        """The API's stream callback, on its own thread: copy, check the running sample number, and now and then
        hand a batch on. xi, xq and params are plain addresses (sdrplay_ctypes.StreamCallback)."""
        sink = self.sink
        copied = False                       # whether this packet's samples are among the waiting ones
        if self._closing:
            self.counters["late_callbacks"] += 1
            return
        try:
            n = num_samples
            if not self._started:
                sink.first(n)
                self._started = True
            first, gr, rf, fs = S.STREAM_PARAMS.unpack(ctypes.string_at(params, 16))
            cnt = self.counters
            cnt["callbacks"] += 1
            if gr or rf or fs:
                cnt["parameter_changes"] += 1
            if self._expected is not None:
                if reset:
                    # A re-initialisation inside the API: continuity is unknown, so it is a gap.
                    cnt["resets"] += 1
                    self._flush()
                    sink.drop(1)
                elif cnt["gap_detection"] == "on":
                    behind = (first - self._expected) & 0xFFFFFFFF
                    if self._early[0] < CHECK_CALLBACKS:
                        self._early[0] += 1
                        self._early[1] += behind != 0
                        if self._early[0] == CHECK_CALLBACKS and self._early[1] >= SUSPECT_SHARE * CHECK_CALLBACKS:
                            cnt["gap_detection"] = (f"off: {self._early[1]} of the first {CHECK_CALLBACKS} "
                                                    "callbacks looked like gaps, so firstSampleNum counts "
                                                    "something else on this receiver")
                    if behind and cnt["gap_detection"] == "on":
                        # A forward step is the number of samples lost; a step backwards is a discontinuity
                        # of unknown size.
                        lost = behind if behind < 0x80000000 else 1
                        cnt["gaps"] += 1
                        cnt["lost_samples"] += lost
                        self._flush()
                        sink.drop(lost)
            self._expected = (first + n) & 0xFFFFFFFF
            if self._fill + n > WAITING:
                self._flush()
            if n > WAITING:                  # never seen: a packet larger than the waiting arrays
                out = np.empty(2 * n, np.int16)
                out[0::2] = np.ctypeslib.as_array(ctypes.cast(xi, ctypes.POINTER(ctypes.c_short)), shape=(n,))
                out[1::2] = np.ctypeslib.as_array(ctypes.cast(xq, ctypes.POINTER(ctypes.c_short)), shape=(n,))
                sink.push(out)
                return
            offset = 2 * self._fill
            ctypes.memmove(self._i_addr + offset, xi, 2 * n)
            ctypes.memmove(self._q_addr + offset, xq, 2 * n)
            self._fill += n
            copied = True
            if self._fill >= self.flush_samples:
                self._flush()
        except Exception:
            # Lost: everything waiting, and this packet once, whether or not it got in among them. Until 24
            # November 2026 only this packet was counted (found in review: 8 samples lost, 4 reported).
            lost = self._fill + (0 if copied else num_samples)
            self._fill = 0
            sink.lost(lambda: int(lost))

    def _event(self, event_id, tuner, params, context):
        if self._closing:                    # no acknowledgement to a device being released
            return
        try:
            cnt = self.counters
            if event_id == S.EVENT_POWER_OVERLOAD:
                change = params.contents.powerOverloadParams.powerOverloadChangeType
                if change == S.OVERLOAD_DETECTED:
                    cnt["overloads"] += 1
                    cnt["overload_now"] = True
                else:
                    cnt["overload_now"] = False
                # The API waits for this acknowledgement before it reports overloads again (SDRplay's example).
                self._noted(self.lib.sdrplay_api_Update(self.device.dev, tuner, S.UPDATE_CTRL_OVERLOAD_ACK,
                                                        S.UPDATE_EXT1_NONE), "overload acknowledgement")
            elif event_id == S.EVENT_GAIN_CHANGE:
                cnt["gain_events"] += 1
            elif event_id == S.EVENT_DEVICE_REMOVED:
                self._fault = f"the {name(self.kind)} was removed from USB"
            elif event_id == S.EVENT_DEVICE_FAILURE:
                self._fault = f"the SDRplay API reported a failure of the {name(self.kind)}"
        except Exception:                    # noqa: BLE001, S110 - nothing may escape into the API's thread
            pass
