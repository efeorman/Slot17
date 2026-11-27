"""Receivers: which one is set up, the Airspy unchanged behind the common interface, and the SDRplay RSP1B/RSP1A.

The SDRplay API is replaced by FakeSdrplay below, which keeps the parameter structures the way the real API
hands them out (sdrplay_ctypes.py, checked against SDRplay's headers by scripts/verify_sdrplay.py) and calls the
stream and event callbacks the way the API's thread does. Nothing here opens a receiver.
"""
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "p0"))
sys.path.insert(0, str(ROOT / "tests"))
import receiver  # noqa: E402
import receiver_sdrplay  # noqa: E402
import sdrplay_ctypes as S  # noqa: E402
import watch  # noqa: E402

# The updater tests a release in a copy within 70 s on the car computer (p0/update.py, TEST_LIMIT_S). These take
# seconds each and test receivers a car with an Airspy doesn't use, so they run in the full checks (setup, preflight
# --tests, development) and not there. Measured on 24 November 2026: with them, the updater's copy took 138 s on
# the development PC, and every release of this branch would have run out of time.
FULL_ONLY = unittest.skipIf(os.environ.get("SLOT17_UPDATE_TEST"), "seconds each: in the full checks only")


class Sink:
    """Records what a receiver hands to watch.Sink, in order."""

    def __init__(self):
        self.calls, self.blocks, self.dropped, self.lost_count = [], [], 0, 0

    def first(self, n, delay_s=0.0):
        self.calls.append(("first", n))

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


class FakeSdrplay:
    """The parts of the SDRplay API 3.15 that receiver_sdrplay.py uses, with the API's documented defaults."""

    def __init__(self, devices=((S.RSP1B_ID, b"2305012345"),), version=3.15, open_rc=0, init_rc=0):
        self.devices, self.version, self.open_rc, self.init_rc = devices, version, open_rc, init_rc
        self.calls, self.updates, self.fns, self.selected = [], [], None, None
        self.dev = S.DevParamsT()
        self.rx = S.RxChannelParamsT()
        d, t, c = self.dev, self.rx.tunerParams, self.rx.ctrlParams
        # Defaults as the specification lists them.
        d.fsFreq.fsHz, d.mode, d.ppm = 2_000_000.0, S.ISOCH, 0.0
        t.bwType, t.ifType, t.loMode = 200, S.IF_ZERO, S.LO_AUTO
        t.gain.gRdB, t.gain.LNAstate, t.gain.minGr = 50, 0, S.NORMAL_MIN_GR
        t.rfFreq.rfHz = 200_000_000.0
        t.dcOffsetTuner.dcCal, t.dcOffsetTuner.trackTime, t.dcOffsetTuner.refreshRateTime = 3, 1, 2048
        c.dcOffset.DCenable, c.dcOffset.IQenable = 1, 1
        c.decimation.enable, c.decimation.decimationFactor = 0, 1
        c.agc.enable, c.agc.setPoint_dBfs = 2, -60
        self.params = S.DeviceParamsT(ctypes.pointer(self.dev), ctypes.pointer(self.rx), None)
        self.stream_thread = None
        self.stop = threading.Event()

    def _call(self, name, rc=0):
        self.calls.append(name)
        return rc

    def sdrplay_api_Open(self):
        return self._call("Open", self.open_rc)

    def sdrplay_api_Close(self):
        return self._call("Close")

    def sdrplay_api_ApiVersion(self, ref):
        ref._obj.value = self.version
        return self._call("ApiVersion")

    def sdrplay_api_LockDeviceApi(self):
        return self._call("Lock")

    def sdrplay_api_UnlockDeviceApi(self):
        return self._call("Unlock")

    def sdrplay_api_GetDevices(self, devs, count, maxn):
        for i, (hw, serial) in enumerate(self.devices):
            devs[i].hwVer, devs[i].SerNo, devs[i].dev, devs[i].valid = hw, serial, 0x1000 + i, 1
        count._obj.value = len(self.devices)
        return self._call("GetDevices")

    def sdrplay_api_SelectDevice(self, ref):
        self.selected = (ref._obj.hwVer, ref._obj.dev)
        return self._call("SelectDevice")

    def sdrplay_api_ReleaseDevice(self, ref):
        return self._call("ReleaseDevice")

    def sdrplay_api_GetErrorString(self, code):
        return S.ERRORS.get(code, "?").encode()

    def sdrplay_api_GetDeviceParams(self, dev, ref):
        ref._obj.contents = self.params
        return self._call("GetDeviceParams")

    def sdrplay_api_Init(self, dev, fns, context):
        self.fns = fns._obj
        self.dev.samplesPerPkt = 1344
        return self._call("Init", self.init_rc)

    def sdrplay_api_Uninit(self, dev):
        self.stop.set()
        if self.stream_thread is not None:
            self.stream_thread.join(5)
        return self._call("Uninit")

    def sdrplay_api_Update(self, dev, tuner, reason, ext):
        self.updates.append((dev, tuner, reason, ext))
        return self._call("Update")

    # --- what the API's thread does ---
    def emit(self, i, q, first, reset=0, gr_changed=0):
        n = len(i)
        xi = (ctypes.c_short * n)(*[int(v) for v in i]) if n < 4096 else np.ascontiguousarray(i, np.int16).ctypes.data_as(ctypes.POINTER(ctypes.c_short))
        xq = (ctypes.c_short * n)(*[int(v) for v in q]) if n < 4096 else np.ascontiguousarray(q, np.int16).ctypes.data_as(ctypes.POINTER(ctypes.c_short))
        params = S.StreamCbParamsT(first, gr_changed, 0, 0, n)
        self.fns.StreamACbFn(ctypes.cast(xi, ctypes.POINTER(ctypes.c_short)), ctypes.cast(xq, ctypes.POINTER(ctypes.c_short)),
                             ctypes.pointer(params), n, reset, None)

    def event(self, event_id, overload=None):
        ev = S.EventParamsT()
        if overload is not None:
            ev.powerOverloadParams.powerOverloadChangeType = overload
        self.fns.EventCbFn(event_id, S.TUNER_A, ctypes.pointer(ev), None)

    def stream(self, iq_int16, block=60000, rate=6_000_000):
        """Stream interleaved int16 I/Q in a loop, at about real time, until Uninit."""
        i_all, q_all = iq_int16[0::2].copy(), iq_int16[1::2].copy()

        def run():
            first, pos, t0, sent = 0, 0, time.monotonic(), 0
            while not self.stop.is_set():
                i, q = i_all[pos:pos + block], q_all[pos:pos + block]
                if len(i) < block:
                    pos = 0
                    continue
                pos += block
                self.emit(i, q, first)
                first = (first + block) & 0xFFFFFFFF
                sent += block
                ahead = sent / rate - (time.monotonic() - t0)
                if ahead > 0:
                    time.sleep(ahead)
        self.stream_thread = threading.Thread(target=run, daemon=True)
        self.stream_thread.start()


class FakeTime:
    """A clock that only moves when the code under test sleeps."""

    def __init__(self):
        self.t = 0.0
        self.slept = 0.0

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s
        self.slept += s


def rsp(lib=None, **kw):
    lib = lib or FakeSdrplay()
    ft = FakeTime()
    kw.setdefault("clock", ft.clock)
    kw.setdefault("sleep", ft.sleep)
    rx = receiver_sdrplay.Receiver("rsp1b", fs=6_000_000, centre_hz=382.5e6,
                                   gain={"lna_state": 2, "if_gain_reduction_db": 40}, lib=lib, **kw)
    sink = Sink()
    rx.bind(sink)
    rx.flush_samples = 1                  # every callback's samples at once; BatchedStream tests the batches
    return rx, lib, sink


class Settings(unittest.TestCase):
    def test_no_file_means_the_airspy_as_before(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(receiver.configured(Path(tmp) / "none.json"), "airspy")

    def test_saved_kind_and_tuned_gain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            receiver.save("rsp1b", path)
            self.assertEqual(receiver.configured(path), "rsp1b")
            self.assertEqual(receiver.gain_for("rsp1b", path), {"lna_state": 2, "if_gain_reduction_db": 40})
            path.write_text(json.dumps({"kind": "rsp1b", "gain": {"lna_state": 4}}))
            self.assertEqual(receiver.gain_for("rsp1b", path), {"lna_state": 4, "if_gain_reduction_db": 40})
            receiver.save("rsp1b", path)                  # setup run again keeps the tuned gain
            self.assertEqual(json.loads(path.read_text())["gain"], {"lna_state": 4})
            receiver.save("airspy", path)                 # ... but not across kinds
            self.assertNotIn("gain", json.loads(path.read_text()))

    def test_bad_files_are_errors_not_a_silent_airspy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            path.write_text("{not json")
            with self.assertRaises(receiver.ReceiverError):
                receiver.configured(path)
            path.write_text(json.dumps({"kind": "hackrf"}))
            with self.assertRaises(receiver.ReceiverError):
                receiver.configured(path)
            path.write_text(json.dumps({"kind": "rsp1b", "gain": {"lna": 3}}))
            with self.assertRaises(receiver.ReceiverError):
                receiver.gain_for("rsp1b", path)
            path.write_text(json.dumps({"kind": "rsp1b", "gain": {"lna_state": True}}))
            with self.assertRaises(receiver.ReceiverError):
                receiver.gain_for("rsp1b", path)

    def test_lsusb_listing(self):
        listing = ("Bus 001 Device 004: ID 1df7:3050 SDRplay RSP1B\n"
                   "Bus 003 Device 002: ID 1d50:60a1 OpenMoko, Inc. Airspy\n"
                   "Bus 002 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub\n")
        self.assertEqual(receiver.kinds_on_usb(listing), ["airspy", "rsp1b"])
        self.assertEqual(receiver.kinds_on_usb(""), [])

    def test_the_command_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.json"
            env = dict(os.environ, SLOT17_RECEIVER_SETTINGS=str(path))
            run = lambda *a: subprocess.run([sys.executable, "-B", str(ROOT / "p0/receiver.py"), *a],
                                            capture_output=True, text=True, env=env)
            self.assertEqual(json.loads(run("--show").stdout)["kind"], "airspy")
            self.assertEqual(run("--set", "rsp1b").returncode, 0)
            self.assertEqual(json.loads(run("--show").stdout)["kind"], "rsp1b")
            self.assertEqual(run("--usb", "ID 1df7:3050 x").stdout.split(), ["rsp1b"])
            self.assertNotEqual(run("--set", "nope").returncode, 0)


class OnlyTheChosenReceiverIsLoaded(unittest.TestCase):
    def loaded(self, kind):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import receiver; receiver.backend(sys.argv[2]); "
                "print(' '.join(m for m in ('airspy_ctypes', 'receiver_airspy', 'sdrplay_ctypes', 'receiver_sdrplay', "
                "'rtlsdr_ctypes', 'receiver_rtlsdr', 'rtl_combine') if m in sys.modules))")
        out = subprocess.run([sys.executable, "-B", "-c", code, str(ROOT / "p0"), kind], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return set(out.stdout.split())

    def test_an_airspy_car_never_loads_sdrplay_code(self):
        self.assertEqual(self.loaded("airspy"), {"receiver_airspy"})

    def test_an_rsp_car_never_loads_airspy_code(self):
        self.assertEqual(self.loaded("rsp1b"), {"sdrplay_ctypes", "receiver_sdrplay"})

    def test_an_rtl_pair_loads_only_its_own_code(self):
        self.assertEqual(self.loaded("rtlsdr2"), {"rtlsdr_ctypes", "receiver_rtlsdr", "rtl_combine"})


class AirspyUnchanged(unittest.TestCase):
    """The exact libairspy calls watch.py made before the move, in the same order."""

    def fake(self, fail=None):
        calls = []

        class Lib:
            def __getattr__(self, name):
                def call(*args):
                    calls.append((name,) + tuple(a for a in args[1:] if isinstance(a, int)))
                    if name == "airspy_is_streaming":
                        return 0
                    return -1 if name == fail else 0
                return call

        class Error(RuntimeError):
            pass

        def check(rc, what):
            if rc:
                raise Error(what)
        return calls, types.SimpleNamespace(lib=Lib(), AirspyError=Error, check=check, SAMPLE_INT16_IQ=2,
                                            SAMPLE_BLOCK_CB=lambda f: f, samplerates=lambda dev: [6000000])

    def session(self, fail=None):
        calls, fake = self.fake(fail)
        with patch.dict(sys.modules, {"airspy_ctypes": fake}), \
                patch("diagnostics.receiver_info", return_value={"firmware": "x"}):
            import receiver_airspy
            rx = receiver_airspy.Receiver("airspy", fs=6_000_000, centre_hz=382.5e6,
                                          gain={"lna": 12, "mixer": 12, "vga": 12})
            rx.bind(Sink())
            try:
                rx.open()
                rx.start()
            except fake.AirspyError:
                pass
            rx.close()
        return [c[0] for c in calls], calls, rx

    def test_a_good_session(self):
        names, calls, rx = self.session()
        self.assertEqual(names, ["airspy_init", "airspy_open", "airspy_set_sample_type", "airspy_set_samplerate",
                                 "airspy_set_lna_agc", "airspy_set_mixer_agc", "airspy_set_rf_bias",
                                 "airspy_set_packing", "airspy_set_lna_gain", "airspy_set_mixer_gain",
                                 "airspy_set_vga_gain", "airspy_start_rx", "airspy_set_freq", "airspy_stop_rx",
                                 "airspy_is_streaming"])
        settings = dict(c[:2] for c in calls if c[0].startswith("airspy_set_") and len(c) > 1)
        self.assertEqual(settings, {"airspy_set_sample_type": 2, "airspy_set_samplerate": 6000000,
                                    "airspy_set_lna_agc": 0, "airspy_set_mixer_agc": 0, "airspy_set_rf_bias": 0,
                                    "airspy_set_packing": 0, "airspy_set_lna_gain": 12, "airspy_set_mixer_gain": 12,
                                    "airspy_set_vga_gain": 12, "airspy_set_freq": 382500000})
        # A session that streamed leaves close and exit to process exit (this board races them).
        self.assertNotIn("airspy_close", names)
        self.assertNotIn("airspy_exit", names)
        self.assertEqual(rx.gain, (12, 12, 12))
        self.assertEqual(rx.gain_record(), {"lna": 12, "mixer": 12, "vga": 12})
        self.assertEqual(rx.session_fields(), {"lna_agc": False, "mixer_agc": False, "rf_bias": False, "packing": False})
        self.assertEqual(rx.status(), {})          # the heartbeat has nothing new for an Airspy

    def test_a_failed_start_closes_and_exits(self):
        names, _, _ = self.session(fail="airspy_start_rx")
        self.assertEqual(names[-2:], ["airspy_close", "airspy_exit"])
        self.assertNotIn("airspy_stop_rx", names)

    def test_a_failed_open_only_exits(self):
        names, _, _ = self.session(fail="airspy_open")
        self.assertEqual(names, ["airspy_init", "airspy_open", "airspy_exit"])

    def test_the_heartbeat_of_an_airspy_is_unchanged(self):
        ctx = watch.Ctx(stats=watch.Stats(), centre=382.5, fs=6_000_000, gain=(12, 12, 12), threshold_db=6,
                        t_start=time.time(), t_start_mono=time.monotonic())
        with tempfile.TemporaryDirectory() as tmp:
            beat = watch.write_heartbeat(os.path.join(tmp, "hb"), ctx, watch.Ring(16), time.time())
            self.assertNotIn("receiver", beat)
            ctx.receiver_status = lambda: {}
            self.assertNotIn("receiver", watch.write_heartbeat(os.path.join(tmp, "hb"), ctx, watch.Ring(16), time.time()))
            ctx.receiver_status = lambda: {"overloads": 2}
            self.assertEqual(watch.write_heartbeat(os.path.join(tmp, "hb"), ctx, watch.Ring(16), time.time())["receiver"],
                             {"overloads": 2})


class SdrplaySession(unittest.TestCase):
    def test_a_service_still_starting_is_waited_for(self):
        """At a car's power-up SDRplay's service may not answer yet, then not list the receiver yet. Until 26
        November 2026 the first answer counted, and the whole drive went without detection (found in review)."""
        lib = FakeSdrplay()
        answers = iter([S.SERVICE_NOT_RESPONDING, S.SERVICE_NOT_RESPONDING, 0])
        lib.sdrplay_api_Open = lambda: lib._call("Open", next(answers))
        listed = iter([(), (), lib.devices])
        real = FakeSdrplay.sdrplay_api_GetDevices

        def get_devices(devs, count, maxn):
            lib.devices = next(listed)
            return real(lib, devs, count, maxn)
        lib.sdrplay_api_GetDevices = get_devices
        ft = FakeTime()
        rx, _, _ = rsp(lib, clock=ft.clock, sleep=ft.sleep)
        self.assertEqual(rx.open()["model"], "RSP1B")
        self.assertEqual(rx.counters["service_waits"], 4)
        self.assertEqual(ft.slept, 4.0)
        self.assertEqual(lib.calls.count("Open"), 3)
        self.assertEqual(lib.calls.count("Lock"), lib.calls.count("Unlock"))
        self.assertEqual(lib.calls.count("SelectDevice"), 1)
        rx.close()

    def test_a_service_that_never_answers_still_fails_in_the_end(self):
        lib = FakeSdrplay(open_rc=S.SERVICE_NOT_RESPONDING)
        ft = FakeTime()
        rx, _, _ = rsp(lib, clock=ft.clock, sleep=ft.sleep)
        with self.assertRaises(receiver.ReceiverError) as e:
            rx.open()
        self.assertIn("systemctl status sdrplay", str(e.exception))
        self.assertGreaterEqual(ft.slept, receiver_sdrplay.SERVICE_WAIT_S)
        rx.close()

    def test_a_receiver_held_elsewhere_says_so(self):
        lib = FakeSdrplay()
        real = FakeSdrplay.sdrplay_api_GetDevices

        def held(devs, count, maxn):
            rc = real(lib, devs, count, maxn)
            devs[0].valid = 0
            return rc
        lib.sdrplay_api_GetDevices = held
        rx, _, _ = rsp(lib)
        with self.assertRaises(receiver.ReceiverError) as e:
            rx.open()
        self.assertIn("held by another program", str(e.exception))
        self.assertNotIn("SelectDevice", lib.calls)
        rx.close()

    def test_sdrplays_order_and_every_setting(self):
        rx, lib, _ = rsp()
        info = rx.open()
        rx.start()
        rx.close()
        self.assertEqual(lib.calls, ["Open", "ApiVersion", "Lock", "GetDevices", "SelectDevice", "Unlock",
                                     "GetDeviceParams", "Init", "Uninit", "ReleaseDevice", "Close"])
        d, t, c = lib.dev, lib.rx.tunerParams, lib.rx.ctrlParams
        self.assertEqual((d.fsFreq.fsHz, d.ppm, d.mode), (6_000_000.0, 0.0, S.ISOCH))
        self.assertEqual((d.rsp1aParams.rfNotchEnable, d.rsp1aParams.rfDabNotchEnable), (0, 0))
        self.assertEqual((t.bwType, t.ifType, t.loMode), (S.BW_5_000, S.IF_ZERO, S.LO_AUTO))
        self.assertEqual((t.gain.gRdB, t.gain.LNAstate, t.gain.minGr), (40, 2, S.NORMAL_MIN_GR))
        self.assertEqual(t.rfFreq.rfHz, 382.5e6)
        self.assertEqual(c.agc.enable, S.AGC_DISABLE)
        self.assertEqual((c.dcOffset.DCenable, c.dcOffset.IQenable), (1, 1))
        self.assertEqual((c.decimation.enable, c.decimation.decimationFactor), (0, 1))
        self.assertEqual(lib.rx.rsp1aTunerParams.biasTEnable, 0)
        self.assertEqual(lib.selected, (S.RSP1B_ID, 0x1000))
        self.assertEqual(info["model"], "RSP1B")
        self.assertEqual(info["serial"], "2305012345")
        self.assertEqual(rx.status()["samples_per_packet"], 1344)
        self.assertEqual(rx.gain, (2, 40))
        self.assertEqual(rx.gain_record(), {"lna_state": 2, "if_gain_reduction_db": 40, "agc": False})

    def test_bulk_transfer_when_asked(self):
        rx, lib, _ = rsp(transfer="bulk")
        rx.open()
        self.assertEqual(lib.dev.mode, S.BULK)

    def test_the_right_model_among_several(self):
        lib = FakeSdrplay(devices=((S.RSP1A_ID, b"A1"), (S.RSP1B_ID, b"B1")))
        rx, lib, _ = rsp(lib)
        rx.open()
        self.assertEqual(lib.selected, (S.RSP1B_ID, 0x1001))

    def test_no_rsp1b_says_what_is_there(self):
        rx, lib, _ = rsp(FakeSdrplay(devices=((S.RSP1A_ID, b"A1"),)))
        with self.assertRaisesRegex(receiver.ReceiverError, "no SDRplay RSP1B found.*RSP1A A1"):
            rx.open()
        rx.close()
        self.assertEqual(lib.calls[-2:], ["Unlock", "Close"])      # unlocked, closed, nothing released
        rx, lib, _ = rsp(FakeSdrplay(devices=()))
        with self.assertRaisesRegex(receiver.ReceiverError, "no SDRplay receiver at all"):
            rx.open()

    def test_another_api_version_is_refused(self):
        rx, lib, _ = rsp(FakeSdrplay(version=3.14))
        with self.assertRaisesRegex(receiver.ReceiverError, "3.14 is installed"):
            rx.open()
        rx.close()
        self.assertNotIn("Lock", lib.calls)
        self.assertEqual(lib.calls[-1], "Close")

    def test_a_service_that_does_not_answer(self):
        rx, lib, _ = rsp(FakeSdrplay(open_rc=S.SERVICE_NOT_RESPONDING))
        with self.assertRaisesRegex(receiver.ReceiverError, "service isn't answering"):
            rx.open()

    def test_parameters_that_look_misread_are_refused_before_anything_is_written(self):
        lib = FakeSdrplay()
        lib.rx.tunerParams.bwType = 4242               # what a wrong structure layout would read
        rx, lib, _ = rsp(lib)
        with self.assertRaisesRegex(receiver.ReceiverError, "don't match the layout"):
            rx.open()
        self.assertEqual(lib.dev.fsFreq.fsHz, 2_000_000.0)
        self.assertEqual(lib.rx.tunerParams.rfFreq.rfHz, 200_000_000.0)

    def test_a_failed_init_still_releases_and_closes(self):
        rx, lib, _ = rsp(FakeSdrplay(init_rc=7))      # HwError
        rx.open()
        with self.assertRaises(S.SdrplayError):
            rx.start()
        rx.close()
        self.assertEqual(lib.calls[-2:], ["ReleaseDevice", "Close"])
        self.assertNotIn("Uninit", lib.calls)

    def test_settings_outside_the_receiver_are_refused(self):
        for kw in ({"fs": 3_000_000}, {"gain": {"lna_state": 10, "if_gain_reduction_db": 40}},
                   {"gain": {"lna_state": 2, "if_gain_reduction_db": 19}},
                   {"gain": {"lna_state": 2, "if_gain_reduction_db": 60}}, {"transfer": "usb3"}):
            args = dict(fs=6_000_000, centre_hz=382.5e6, gain={"lna_state": 2, "if_gain_reduction_db": 40},
                        lib=FakeSdrplay())
            args.update(kw)
            with self.assertRaises(receiver.ReceiverError, msg=kw):
                receiver_sdrplay.Receiver("rsp1b", **args)

    def test_a_missing_api_says_how_to_install_it(self):
        with patch.object(S, "load", side_effect=OSError("libsdrplay_api.so.3: cannot open shared object file")):
            with self.assertRaisesRegex(receiver.ReceiverError, "setup.sh car --receiver rsp1b"):
                receiver_sdrplay.Receiver("rsp1b", fs=6_000_000, centre_hz=382.5e6, gain={})


class SdrplayStream(unittest.TestCase):
    def started(self):
        rx, lib, sink = rsp()
        rx.open()
        rx.start()
        return rx, lib, sink

    def test_i_and_q_are_interleaved_exactly(self):
        rx, lib, sink = self.started()
        i = np.array([1, -2, 3, 32767, -32768], np.int16)
        q = np.array([10, 20, -30, -1, 0], np.int16)
        lib.emit(i, q, first=1000)
        self.assertEqual(sink.calls, [("first", 5), ("push", 10)])
        np.testing.assert_array_equal(sink.blocks[0], np.array([1, 10, -2, 20, 3, -30, 32767, -1, -32768, 0], np.int16))

    def test_a_continuous_stream_has_no_gaps_even_across_the_counters_wrap(self):
        rx, lib, sink = self.started()
        first = 0xFFFFFFFF - 1500
        for _ in range(10):
            lib.emit(np.zeros(1000, np.int16), np.zeros(1000, np.int16), first)
            first = (first + 1000) & 0xFFFFFFFF
        self.assertEqual(sink.dropped, 0)
        self.assertEqual(rx.status()["gaps"], 0)

    def test_lost_samples_are_counted_exactly(self):
        rx, lib, sink = self.started()
        z = np.zeros(1000, np.int16)
        lib.emit(z, z, 0)
        lib.emit(z, z, 1000)
        lib.emit(z, z, 5000)                            # 3000 lost
        lib.emit(z, z, 6000)
        lib.emit(z, z, 0xFFFFFF00)                      # more than half the counter ahead: as good as a step
                                                        # backwards, so a gap of unknown size (1)
        self.assertIn(("drop", 3000), sink.calls)
        self.assertEqual(rx.status()["lost_samples"], 3000 + 1)
        self.assertEqual(rx.status()["gaps"], 2)
        # The drop sits between the samples before the gap and the ones after it; first() came once, at the start.
        at = sink.calls.index(("drop", 3000))
        self.assertEqual(sink.calls[at - 1], ("push", 2000))
        self.assertEqual(sink.calls[at + 1], ("push", 2000))
        self.assertEqual([c for c in sink.calls if c[0] == "first"], [("first", 1000)])

    def test_wrap_with_a_gap(self):
        rx, lib, sink = self.started()
        z = np.zeros(100, np.int16)
        lib.emit(z, z, 0xFFFFFF9C)                      # ends at 0x100000000 = 0
        lib.emit(z, z, 50)                              # 50 lost across the wrap
        self.assertEqual(sink.dropped, 50)

    def test_a_step_backwards_or_a_reset_is_a_gap_of_unknown_size(self):
        rx, lib, sink = self.started()
        z = np.zeros(100, np.int16)
        lib.emit(z, z, 5000, reset=1)                   # the first block may carry reset: that is the start
        lib.emit(z, z, 5100)
        lib.emit(z, z, 100)                             # backwards
        lib.emit(z, z, 0, reset=1)                      # the API re-initialised
        self.assertEqual(sink.dropped, 2)
        self.assertEqual(rx.status()["resets"], 1)
        self.assertEqual(rx.status()["gaps"], 1)

    def test_parameter_changes_are_counted(self):
        rx, lib, sink = self.started()
        z = np.zeros(10, np.int16)
        lib.emit(z, z, 0)
        lib.emit(z, z, 10, gr_changed=1)
        self.assertEqual(rx.status()["parameter_changes"], 1)

    def test_gap_finding_switches_itself_off_when_the_counter_means_something_else(self):
        rx, lib, sink = self.started()
        z = np.zeros(100, np.int16)
        for k in range(receiver_sdrplay.CHECK_CALLBACKS + 5):
            lib.emit(z, z, k)                           # counts callbacks, not samples
        status = rx.status()
        self.assertTrue(status["gap_detection"].startswith("off:"), status)
        before = sink.dropped
        lib.emit(z, z, 10_000_000)
        self.assertEqual(sink.dropped, before)

    def test_nothing_escapes_into_the_apis_thread(self):
        rx, lib, sink = self.started()

        def boom(iq):
            raise MemoryError("ring")
        sink.push = boom
        lib.emit(np.zeros(7, np.int16), np.zeros(7, np.int16), 0)
        self.assertIn(("lost", 7), sink.calls)

    def test_overloads_are_acknowledged_and_counted(self):
        rx, lib, sink = self.started()
        lib.event(S.EVENT_POWER_OVERLOAD, overload=S.OVERLOAD_DETECTED)
        self.assertEqual(rx.status()["overloads"], 1)
        self.assertTrue(rx.status()["overload_now"])
        lib.event(S.EVENT_POWER_OVERLOAD, overload=S.OVERLOAD_CORRECTED)
        self.assertFalse(rx.status()["overload_now"])
        self.assertEqual(lib.updates, [(0x1000, S.TUNER_A, S.UPDATE_CTRL_OVERLOAD_ACK, S.UPDATE_EXT1_NONE)] * 2)

    def test_removal_and_failure_end_the_session(self):
        rx, lib, sink = self.started()
        self.assertIsNone(rx.fault())
        lib.event(S.EVENT_GAIN_CHANGE)
        self.assertIsNone(rx.fault())
        lib.event(S.EVENT_DEVICE_REMOVED)
        self.assertIn("removed from USB", rx.fault())
        rx2, lib2, _ = self.started()
        lib2.event(S.EVENT_DEVICE_FAILURE)
        self.assertIn("failure", rx2.fault())


class BatchedStream(unittest.TestCase):
    """The real setting: samples wait and are handed on in batches, and a gap still lands exactly in place."""

    def test_a_gap_between_batches_is_where_the_samples_were_lost(self):
        rx, lib, sink = rsp()
        rx.flush_samples = receiver_sdrplay.FLUSH_SAMPLES
        rx.open()
        rx.start()
        first, ramp = 0, 0
        for k in range(40):
            i = (np.arange(ramp, ramp + 1000) % 30000).astype(np.int16)
            lib.emit(i, -i, first)
            first += 1000
            ramp += 1000
            if k == 24:
                first += 500                  # 500 samples lost after the 25th packet
                ramp += 500
        rx.close()                            # hands on what was still waiting
        pushed = [c for c in sink.calls if c[0] in ("push", "drop")]
        self.assertEqual(pushed[0], ("push", 2 * 17000))       # the first batch: 17 packets
        self.assertEqual(pushed[1], ("push", 2 * 8000))        # what waited when the gap was found
        self.assertEqual(pushed[2], ("drop", 500))
        iq = np.concatenate(sink.blocks)
        self.assertEqual(len(iq), 2 * 40000)
        i = iq[0::2].astype(np.int64)
        self.assertTrue(np.array_equal(iq[1::2], (-iq[0::2]).astype(np.int16)))
        steps = np.diff(i) % 30000
        self.assertEqual(sorted(set(steps.tolist())), [1, 501])
        self.assertEqual(int(np.argmax(steps != 1)) + 1, 25000)  # the jump comes right after the 25th packet

    def test_a_packet_larger_than_the_waiting_room(self):
        rx, lib, sink = rsp()
        rx.flush_samples = receiver_sdrplay.FLUSH_SAMPLES
        rx.open()
        rx.start()
        n = receiver_sdrplay.WAITING + 10
        i = (np.arange(n) % 1000).astype(np.int16)
        lib.emit(i, -i, 0)
        self.assertEqual(len(sink.blocks[0]), 2 * n)
        np.testing.assert_array_equal(sink.blocks[0][0::2], i)

    def test_a_batch_the_ring_refuses_is_counted_whole(self):
        """Every sample waiting is lost with a batch that can't be handed on, and the packet once. Until 24
        November 2026 only the last packet was counted: 8 lost, 4 reported (found in review)."""
        rx, lib, sink = rsp()
        rx.flush_samples = 8
        rx.open()
        rx.start()

        def refuse(data):
            raise OSError("no room")
        sink.push = refuse
        lib.emit([1] * 4, [2] * 4, 0)             # waits
        lib.emit([1] * 4, [2] * 4, 4)             # makes 8: the batch goes, and is refused
        self.assertEqual(sink.lost_count, 8)
        lib.emit([1] * 4, [2] * 4, 8)             # waits again: nothing was left counted twice
        self.assertEqual(sink.lost_count, 8)
        self.assertEqual(rx._fill, 4)

    def test_a_failed_uninit_is_kept_and_its_thread_left_alone(self):
        """Found in review: the results of Uninit and the other closing steps were passed over, and the waiting
        samples were handed on from the closing thread while the API's thread might still be calling."""
        class Stuck(FakeSdrplay):
            def sdrplay_api_Uninit(self, dev):
                FakeSdrplay.sdrplay_api_Uninit(self, dev)
                return 1                          # Fail
        rx, lib, sink = rsp(Stuck())
        rx.flush_samples = 100
        rx.open()
        rx.start()
        lib.emit([1] * 4, [2] * 4, 0)
        rx.close()
        self.assertEqual(sink.blocks, [])         # not handed on from this thread
        self.assertEqual(sink.lost_count, 4)
        self.assertEqual(rx.status()["api_errors"], ["sdrplay_api_Uninit: Fail"])
        self.assertEqual(lib.calls[-3:], ["Uninit", "ReleaseDevice", "Close"])   # every step still attempted
        receiver_sdrplay.UNSTOPPED.remove(rx)

    def test_a_callback_after_a_failed_uninit_hands_nothing_on_and_the_receiver_stays_alive(self):
        """Second review, 24 November 2026: after a failed Uninit the API's thread may still call; a late callback
        re-delivered samples already counted as lost into a sink whose session had ended."""
        class Stuck(FakeSdrplay):
            def sdrplay_api_Uninit(self, dev):
                FakeSdrplay.sdrplay_api_Uninit(self, dev)
                return 1
        rx, lib, sink = rsp(Stuck())
        rx.flush_samples = 8
        rx.open()
        rx.start()
        lib.emit([1] * 4, [2] * 4, 0)                 # waits
        rx.close()
        lib.emit([1] * 4, [2] * 4, 4)                 # the API's thread, still going
        lib.event(S.EVENT_POWER_OVERLOAD, S.OVERLOAD_DETECTED)
        self.assertEqual((sink.blocks, sink.lost_count), ([], 4))
        self.assertEqual(rx.status()["late_callbacks"], 1)
        self.assertNotIn(S.UPDATE_CTRL_OVERLOAD_ACK, [u[2] for u in lib.updates])
        self.assertIn(rx, receiver_sdrplay.UNSTOPPED)  # its callbacks and buffers outlive the session
        rx.close()                                    # again: nothing counted or handed on twice
        self.assertEqual((sink.blocks, sink.lost_count), ([], 4))
        receiver_sdrplay.UNSTOPPED.remove(rx)

    def test_a_last_batch_that_cannot_be_handed_on_is_counted(self):
        """Second review, 24 November 2026: Uninit worked, the final push failed, and the samples went uncounted."""
        rx, lib, sink = rsp()
        rx.flush_samples = 100
        rx.open()
        rx.start()
        lib.emit([1] * 4, [2] * 4, 0)

        def refuse(data):
            raise OSError("no room")
        sink.push = refuse
        rx.close()
        self.assertEqual(sink.lost_count, 4)
        self.assertIn("last batch not handed on: OSError: no room", rx.status()["api_errors"])
        self.assertNotIn(rx, receiver_sdrplay.UNSTOPPED)

    def test_a_free_receiver_is_chosen_over_one_in_use(self):
        class TwoOfThem(FakeSdrplay):
            def sdrplay_api_GetDevices(self, devs, count, maxn):
                rc = FakeSdrplay.sdrplay_api_GetDevices(self, devs, count, maxn)
                devs[0].valid = 0                 # held by another program
                return rc
        lib = TwoOfThem(devices=((S.RSP1B_ID, b"1111"), (S.RSP1B_ID, b"2222")))
        rx, lib, sink = rsp(lib)
        rx.open()
        self.assertEqual(rx.device.SerNo, b"2222")
        rx.close()


class CalibrationCapture(unittest.TestCase):
    """capture.py through the receiver's backend: the same .iq and .json calibrate.py reads."""

    def test_an_rsp1b_capture(self):
        import capture
        lib = FakeSdrplay()
        original_init = lib.sdrplay_api_Init

        def init(dev, fns, context):
            rc = original_init(dev, fns, context)
            lib.stream(synthetic_band(seconds=0.1, channels=(104,)), block=6000)
            return rc
        lib.sdrplay_api_Init = init
        with tempfile.TemporaryDirectory() as tmp, patch.object(S, "load", return_value=lib), \
                patch.object(capture, "IQ_DIR", tmp), patch.object(capture.paths, "ensure_dirs"), \
                patch.object(sys, "argv", ["capture.py", "--freq", "392.5", "--secs", "0.05", "--rate", "3",
                                           "--receiver", "rsp1b", "--tag", "downlink-calibration"]), \
                redirect_stdout(StringIO()) as out:
            self.assertEqual(capture.main(), 0, out.getvalue())
            raw = next(Path(tmp).glob("*.iq"))
            meta = json.loads(raw.with_suffix(".json").read_text())
            self.assertEqual(raw.stat().st_size, int(6e6 * 0.05) * 4)
        self.assertEqual(meta["sample_rate_hz"], 6_000_000)
        self.assertEqual(meta["centre_hz"], 392.5e6)
        self.assertEqual(meta["receiver"]["kind"], "rsp1b")
        self.assertEqual(meta["gain"]["lna_state"], 2)
        self.assertEqual(lib.rx.tunerParams.rfFreq.rfHz, 392.5e6)
        self.assertEqual(lib.calls[-3:], ["Uninit", "ReleaseDevice", "Close"])
        self.assertIn("records at 6 MSPS here (not 3)", out.getvalue())


# Setup's own steps are tested in scripts/test_setup_steps.py, by running them. They were read here until the second
# review (24 November 2026): tests/ is what the updater tests a release in, and its copy has no deploy/ folder, so
# this module couldn't load there and every release with it would have been rejected (F13).


class WaitForTheReceiver(unittest.TestCase):
    """watch.wait_for_receiver: at power-up, until every device the receiver needs is on USB (found in review, 26
    November 2026). It only reads the USB list."""

    def test_a_late_second_stick_is_waited_for(self):
        ft = FakeTime()
        seen = iter([1, 1, 1, 2])
        waited = watch.wait_for_receiver("rtlsdr2", limit=30, present=lambda k: next(seen),
                                         clock=ft.clock, sleep=ft.sleep)
        self.assertEqual(waited, 1.5)

    def test_it_gives_up_at_the_limit_and_when_usb_cannot_be_read(self):
        ft = FakeTime()
        self.assertGreaterEqual(watch.wait_for_receiver("rsp1b", limit=5, present=lambda k: 0,
                                                        clock=ft.clock, sleep=ft.sleep), 5)
        ft = FakeTime()
        self.assertEqual(watch.wait_for_receiver("airspy", limit=5, present=lambda k: None,
                                                 clock=ft.clock, sleep=ft.sleep), 0.0)

    def test_supervise_asks_for_the_wait_and_watch_alone_does_not(self):
        import supervise
        opts = types.SimpleNamespace(watch_args=["--band", "uplink"], heartbeat="hb", stall_timeout=15)
        cmd = supervise.build_cmd(opts)
        self.assertEqual(cmd[cmd.index("--receiver-wait") + 1], str(supervise.RECEIVER_WAIT_S))
        opts.watch_args = ["--receiver-wait", "5"]
        self.assertEqual(supervise.build_cmd(opts).count("--receiver-wait"), 1)


class ReceiverOverload(unittest.TestCase):
    """pipeline.receiver_overload: the receiver's own word on overload, where the int16 stream can't show it (found
    in review, 26 November 2026: an RTL stick clipping 25 % of its samples left the window "not saturating")."""

    def sig(self, status):
        import pipeline
        sig = {"saturating": False, "glitch_block": False}
        pipeline.receiver_overload(sig, (lambda: status) if status is not None else None)
        return sig

    def test_an_rtl_stick_clipping_or_an_rsp_overload_marks_the_window(self):
        self.assertTrue(self.sig({"saturation_a": 0.25, "saturation_b": 0.0})["saturating"])
        self.assertEqual(self.sig({"saturation_a": 0.0, "saturation_b": 0.002})["saturation_source"], "receiver")
        self.assertTrue(self.sig({"overload_now": True})["saturating"])

    def test_a_clean_receiver_or_none_changes_nothing(self):
        self.assertFalse(self.sig({"saturation_a": 0.0001, "saturation_b": 0.0, "overload_now": False})["saturating"])
        self.assertFalse(self.sig(None)["saturating"])
        self.assertFalse(self.sig({})["saturating"])


class PresenceAndMessages(unittest.TestCase):
    """Which receiver is on USB, and the words the supervisor and the boot message use for it."""

    def sysfs(self, tmp, ids):
        root = Path(tmp)
        for k, usb in enumerate(ids):
            d = root / f"1-{k}"
            d.mkdir()
            vid, pid = usb.split(":")
            (d / "idVendor").write_text(vid + "\n")
            (d / "idProduct").write_text(pid + "\n")
        (root / "1-0.interface").mkdir()         # like an interface folder: no IDs of its own
        return str(root)

    def test_devices_are_counted_from_sysfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.sysfs(tmp, ["1df7:3050", "1d6b:0003", "1d50:60a1"])
            self.assertEqual(receiver.present("rsp1b", path), 1)
            self.assertEqual(receiver.present("airspy", path), 1)
            self.assertEqual(receiver.present("rsp1a", path), 0)
        self.assertIsNone(receiver.present("rsp1b", "/no/such/sysfs"))

    def test_the_pair_needs_exactly_two_sticks(self):
        """Second review, 24 November 2026: three sticks passed setup's check, and the receiver refuses three."""
        for kind, count, want in (("rtlsdr2", 1, 1), ("rtlsdr2", 2, 0), ("rtlsdr2", 3, 1), ("airspy", 1, 0),
                                  ("airspy", 2, 0), ("rsp1b", 0, 1)):
            with self.subTest(kind=kind, count=count), patch.object(receiver, "present", return_value=count), \
                    redirect_stdout(StringIO()):
                self.assertEqual(receiver.main(["--present", kind]), want)

    def notifier(self, kind, present):
        import notify
        import supervise
        posted = []

        class Poster:
            def post(self, message, title, priority):
                posted.append((title, message))
        n = supervise.HardwareNotifier(Poster(), min_failures=1)
        with patch.object(notify, "receiver_kind", return_value=kind),                 patch.object(notify, "receiver_present", return_value=present):
            n.on_failure(2)
            n.on_streaming()
        return posted

    def test_the_airspys_words_are_unchanged(self):
        posted = self.notifier("airspy", True)
        self.assertEqual([t for t, _ in posted], ["Airspy wedged", "Airspy recovered"])
        self.assertTrue(posted[0][1].startswith("The Airspy has stopped after 2 consecutive failures."))

    def test_an_rsp1b_is_named(self):
        posted = self.notifier("rsp1b", False)
        self.assertEqual([t for t, _ in posted], ["SDRplay RSP1B wedged", "SDRplay RSP1B recovered"])
        self.assertIn("vanished from the USB bus", posted[0][1])

    def test_the_boot_message_names_the_receiver(self):
        import diagnostics
        import notify
        info = {"hostname": "slot17", "addresses": [], "ssid": None, "gateway": None, "online": False}
        with patch.object(diagnostics, "host_health", return_value={}),                 patch.object(notify, "receiver_kind", return_value="rsp1b"),                 patch.object(notify, "receiver_present", return_value=True):
            self.assertIn("rsp1b present", notify.boot_message(info))
        with patch.object(diagnostics, "host_health", return_value={}),                 patch.object(notify, "receiver_kind", return_value="airspy"),                 patch.object(notify, "airspy_present", return_value=False):
            self.assertIn("airspy NOT FOUND", notify.boot_message(info))


def synthetic_band(seconds=0.6, channels=(20, 104, 180), snr=20):
    """The detector's own synthetic TETRA bursts (tests/test_detection.py), as the int16 I/Q a receiver delivers."""
    import test_detection
    iq, fs = test_detection.wideband(channels=channels, snr=snr, seconds=seconds)
    out = np.empty(2 * len(iq), np.int16)
    out[0::2] = np.clip(np.round(iq.real), -32768, 32767)
    out[1::2] = np.clip(np.round(iq.imag), -32768, 32767)
    return out


@FULL_ONLY
class WholeDetectorOnAnRsp1b(unittest.TestCase):
    """watch.py with --receiver rsp1b, the fake API streaming synthetic TETRA bursts at about real time."""

    def run_watch(self, minutes, lib, extra=()):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(S, "load", return_value=lib), \
                patch.object(watch.paths, "ROOT", tmp), patch.object(watch.paths, "CAPTURES", str(Path(tmp) / "captures")), \
                patch.object(watch.diagnostics, "host_health", return_value={}), \
                patch.object(watch.signal, "signal"), \
                patch.object(sys, "argv", ["watch.py", "--minutes", str(minutes), "--no-notify", "--receiver", "rsp1b",
                                           "--heartbeat", str(Path(tmp) / "hb.json"), *extra]), \
                redirect_stdout(StringIO()) as out:
            code = watch.main()
            session = next((Path(tmp) / "captures").glob("*"))
            events = [json.loads(line) for line in (session / "events.jsonl").read_text().splitlines()]
            return (code, json.loads((session / "session.json").read_text()),
                    json.loads((session / "summary.json").read_text()), events,
                    json.loads((Path(tmp) / "hb.json").read_text()), out.getvalue())

    def test_bursts_through_the_rsp1b_path_are_recognised(self):
        lib = FakeSdrplay()
        signal = synthetic_band()
        original_init = lib.sdrplay_api_Init

        def init(dev, fns, context):
            rc = original_init(dev, fns, context)
            lib.stream(signal)
            return rc
        lib.sdrplay_api_Init = init
        code, session, summary, events, beat, out = self.run_watch(0.1, lib)
        self.assertEqual(code, 0, out[-2000:])
        self.assertEqual(session["receiver"], {"kind": "rsp1b", "name": "SDRplay RSP1B"})
        self.assertEqual(session["gain"], {"lna_state": 2, "if_gain_reduction_db": 40, "agc": False})
        self.assertFalse(session["agc"])
        self.assertEqual(summary["receiver"]["model"], "RSP1B")
        self.assertGreater(beat["receiver"]["callbacks"], 0)
        self.assertEqual(beat["receiver"]["gaps"], 0)
        self.assertEqual(beat["dropped"], 0)
        confirmed = {e.get("tetra_channel") for e in events if e.get("confirmed") is True}
        self.assertTrue({20, 104, 180} <= confirmed, (confirmed, out[-1500:]))
        self.assertEqual(lib.calls[-3:], ["Uninit", "ReleaseDevice", "Close"])

    def test_a_removed_rsp1b_ends_the_session_as_a_stall(self):
        lib = FakeSdrplay()
        original_init = lib.sdrplay_api_Init

        def init(dev, fns, context):
            rc = original_init(dev, fns, context)
            lib.stream(synthetic_band(seconds=0.3, channels=(104,)))
            threading.Timer(1.0, lambda: lib.event(S.EVENT_DEVICE_REMOVED)).start()
            return rc
        lib.sdrplay_api_Init = init
        code, session, summary, events, beat, out = self.run_watch(0.5, lib)
        self.assertEqual(code, watch.failure.STREAM_STALL, out[-1500:])
        self.assertIn("removed from USB", summary["failure_reason"])
        self.assertEqual(lib.calls[-3:], ["Uninit", "ReleaseDevice", "Close"])

    def test_no_api_installed_is_a_clean_receiver_failure(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(S, "load", side_effect=OSError("cannot open shared object file")), \
                patch.object(watch.paths, "ROOT", tmp), patch.object(watch.paths, "CAPTURES", str(Path(tmp) / "c")), \
                patch.object(sys, "argv", ["watch.py", "--minutes", ".01", "--no-notify", "--receiver", "rsp1b"]), \
                redirect_stdout(StringIO()) as out:
            self.assertEqual(watch.main(), watch.failure.RECEIVER_UNAVAILABLE)
        self.assertIn("setup.sh car --receiver rsp1b", out.getvalue())


class Layout(unittest.TestCase):
    """The structure sizes scripts/verify_sdrplay.py proved against SDRplay's 3.15 headers on 64-bit Linux.

    A change to sdrplay_ctypes.py that moves any of them must be checked against the headers again."""

    SIZES = {"sdrplay_api_DevParamsT": 64, "sdrplay_api_RxChannelParamsT": 144, "sdrplay_api_TunerParamsT": 72,
             "sdrplay_api_ControlParamsT": 32, "sdrplay_api_DeviceT": 96, "sdrplay_api_DeviceParamsT": 24,
             "sdrplay_api_StreamCbParamsT": 20, "sdrplay_api_CallbackFnsT": 24, "sdrplay_api_EventParamsT": 16,
             "sdrplay_api_ErrorInfoT": 1540, "sdrplay_api_GainT": 24}

    def test_sizes(self):
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            self.skipTest("the proven sizes are for 64-bit systems")
        got = {k: ctypes.sizeof(S.STRUCTS[k]) for k in self.SIZES}
        self.assertEqual(got, self.SIZES)


if __name__ == "__main__":
    unittest.main()
