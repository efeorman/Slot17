import contextlib
import ctypes
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "p0"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify_isolation  # noqa: E402,F401 - see the module
import recording
import watch
import pipeline


class RuntimeTests(unittest.TestCase):
    def test_ntp_step_reanchors_timestamps_and_is_recorded(self):
        """The car Pi cold-boots stale; timesyncd stepped it +9m18s on 9 September."""
        from types import SimpleNamespace
        events = []
        st = watch.Stats()
        st.capture_start_utc = 1000.0
        st.capture_start_mono = 500.0
        ctx = watch.Ctx(stats=st, recorder=SimpleNamespace(event=events.append),
                        events=[], t_start=1000.0, t_start_mono=500.0)
        with patch.object(watch.time, 'monotonic', return_value=560.0), \
             patch.object(watch.time, 'time', return_value=1060.0):
            self.assertEqual(watch.check_clock_step(ctx), 0.0)   # healthy: no step
        self.assertEqual(events, [])
        # NTP jumps the wall clock 558 s forward; monotonic is unaffected.
        with patch.object(watch.time, 'monotonic', return_value=561.0), \
             patch.object(watch.time, 'time', return_value=1619.0), \
             contextlib.redirect_stdout(io.StringIO()):
            step = watch.check_clock_step(ctx)
        self.assertAlmostEqual(step, 558.0, places=3)
        self.assertEqual(st.clock_steps, 1)
        self.assertAlmostEqual(st.clock_offset, 558.0, places=3)
        self.assertEqual(events[0]['event_type'], 'clock_step')
        self.assertAlmostEqual(events[0]['delta_s'], 558.0, places=3)
        # Re-anchored: a window at t_abs=61 s must now carry the corrected time.
        anchor = st.capture_start_utc + st.clock_offset + 61.0
        self.assertAlmostEqual(anchor, 1619.0, places=3)
        # And a settled clock must not keep re-stepping.
        with patch.object(watch.time, 'monotonic', return_value=570.0), \
             patch.object(watch.time, 'time', return_value=1628.0):
            self.assertEqual(watch.check_clock_step(ctx), 0.0)
        self.assertEqual(st.clock_steps, 1)

    def test_degraded_reasons_name_the_fault_and_ignore_a_shutting_down_recorder(self):
        """The 8 September drive's final heartbeat claimed degraded with no errors."""
        healthy = {'alive': True, 'dropped_events': 0, 'data_errors': 0,
                   'event_errors': 0, 'skipped_data': 0}
        st = watch.Stats()
        st.worker_alive = True
        self.assertEqual(watch.degraded_reasons(st, healthy), [])
        # Stopping is not a fault; that is what the end of a session looks like.
        st.worker_alive = False
        self.assertEqual(watch.degraded_reasons(st, dict(healthy, alive=False)), [])
        # A recorder that dies mid-run is a fault, and must say so.
        st.worker_alive = True
        self.assertEqual(watch.degraded_reasons(st, dict(healthy, alive=False)),
                         [{'fault': 'recorder stopped', 'total': None}])
        st.dropped = 4096
        st.overflow = 2
        self.assertEqual(watch.degraded_reasons(st, healthy),
                         [{'fault': 'dropped samples', 'total': 4096},
                          {'fault': 'overflow windows', 'total': 2}])
        # The fault identity must not carry its total, or a growing counter
        # would read as a new fault on every heartbeat.
        st.dropped = 9000
        self.assertEqual([r['fault'] for r in watch.degraded_reasons(st, healthy)],
                         ['dropped samples', 'overflow windows'])
        self.assertEqual(watch.describe_fault({'fault': 'dropped samples', 'total': 9000}),
                         'dropped samples 9000')
        self.assertEqual(watch.describe_fault({'fault': 'recorder stopped', 'total': None}),
                         'recorder stopped')

    def test_a_receiver_hearing_nothing_becomes_a_named_session_fault(self):
        """Seven of twelve sessions on the Pi ran deaf; all reported healthy."""
        healthy = {'alive': True, 'dropped_events': 0, 'data_errors': 0,
                   'event_errors': 0, 'skipped_data': 0}
        st = watch.Stats()
        st.worker_alive = True
        self.assertEqual(st.quiet_windows, 0)
        # A short run of quiet windows is normal: the working 12 September
        # sessions peaked at 5 consecutive. It must not raise a fault.
        st.quiet_windows = watch.QUIET_INPUT_WINDOWS - 1
        self.assertEqual(watch.degraded_reasons(st, healthy), [])
        st.quiet_windows = watch.QUIET_INPUT_WINDOWS
        reasons = watch.degraded_reasons(st, healthy)
        self.assertEqual([r['fault'] for r in reasons],
                         ['receiver input very low - antenna connection unverified'])
        # The run length is reported, but marked steady: it grows every window
        # and must not be mistaken for an accumulating fault by notify.py.
        self.assertEqual(reasons[0]['total'], watch.QUIET_INPUT_WINDOWS)
        self.assertIs(reasons[0]['steady'], True)
        # The wording must not claim a disconnected antenna. Low input cannot
        # prove one, and the operator runs deliberately antenna-less at home.
        self.assertIn('unverified', reasons[0]['fault'])
        st.quiet_windows = 0
        self.assertEqual(watch.degraded_reasons(st, healthy), [])

    def test_quiet_window_run_counts_only_a_definite_verdict(self):
        """`antenna_suspect` is True/False/None; None must neither raise nor clear."""
        from types import SimpleNamespace
        st = watch.Stats()
        ctx = SimpleNamespace(stats=st)

        def feed(verdict):
            # Exercise the counter exactly as pipeline.process_window does.
            sig = {'antenna_suspect': verdict}
            if sig.get('antenna_suspect') is True:
                st.quiet_windows = getattr(st, 'quiet_windows', 0) + 1
                st.quiet_windows_max = max(getattr(st, 'quiet_windows_max', 0), st.quiet_windows)
            elif sig.get('antenna_suspect') is False:
                st.quiet_windows = 0

        for _ in range(3):
            feed(True)
        self.assertEqual((st.quiet_windows, st.quiet_windows_max), (3, 3))
        feed(None)                       # unknown gain / corrupt block: no verdict
        self.assertEqual(st.quiet_windows, 3)
        feed(False)                      # the receiver heard something: run over
        self.assertEqual(st.quiet_windows, 0)
        # The peak survives, so a summary can still show the worst of a session.
        self.assertEqual(st.quiet_windows_max, 3)
        del ctx

    def test_session_records_its_corrected_start_time_when_the_clock_steps(self):
        """No RTC is possible: ignition cuts power, so every folder name is wrong."""
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp) / "20260914-174526-101083Z_watch_382.5MHz"
            outdir.mkdir()
            st = watch.Stats()
            st.capture_start_utc = 1789408000.0      # the stale boot clock
            st.capture_start_mono = 100.0
            st.clock_offset = 170275.327             # what timesyncd corrected
            st.clock_steps = 1
            ctx = SimpleNamespace(stats=st, outdir=str(outdir))
            with contextlib.redirect_stdout(io.StringIO()):
                watch.mark_session_time(ctx)
            marker = json.loads((outdir / "session-time.json").read_text(encoding="utf-8"))
            self.assertEqual(marker['clock_offset_s'], 170275.327)
            self.assertEqual(marker['directory_name_utc'], '20260914-174526-101083Z')
            self.assertAlmostEqual(marker['corrected_start_utc'],
                                   1789408000.0 + 170275.327, places=3)
            # The corrected time is ~47 h later, so it must not still read 09-14.
            self.assertTrue(marker['corrected_start_iso'].startswith('2026-09-16'),
                            marker['corrected_start_iso'])
            self.assertIn('no RTC', marker['note'])
            # Recorded evidence is never renamed or rewritten, only annotated.
            self.assertTrue(outdir.is_dir())

    def test_marking_the_session_time_never_breaks_a_capture(self):
        """A misnamed folder is a nuisance; a lost drive is not."""
        from types import SimpleNamespace
        st = watch.Stats()
        st.capture_start_utc, st.capture_start_mono = 1000.0, 10.0
        # An unwritable path must be swallowed, not raised into the window loop.
        ctx = SimpleNamespace(stats=st, outdir="/nonexistent-slot17/session")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(watch.mark_session_time(ctx))
        self.assertIn('WARN', out.getvalue())
        # No session directory at all: nothing to annotate, still no exception.
        self.assertIsNone(watch.mark_session_time(SimpleNamespace(stats=st, outdir=None)))

    def test_power_and_thermal_faults_reach_the_health_alert(self):
        """A glovebox install can cook the Pi; throttling must not be silent."""
        healthy_recorder = {'alive': True, 'dropped_events': 0, 'data_errors': 0,
                            'event_errors': 0, 'skipped_data': 0}
        st = watch.Stats()
        st.worker_alive = True
        ok = {'cpu_temp_c': 52.0, 'power': {'undervoltage_now': False,
              'frequency_capped_now': False, 'throttled_now': False,
              'soft_temperature_limit_now': False, 'undervoltage_since_boot': True}}
        # A historical flag alone must not mark a whole drive degraded.
        self.assertEqual(watch.degraded_reasons(st, healthy_recorder, ok), [])
        hot = {'cpu_temp_c': 81.4, 'power': dict(ok['power'], throttled_now=True,
                                                 soft_temperature_limit_now=True)}
        self.assertEqual(watch.degraded_reasons(st, healthy_recorder, hot),
                         [{'fault': 'thermal throttling', 'total': 81.4},
                          {'fault': 'soft temperature limit reached', 'total': 81.4}])
        brownout = {'cpu_temp_c': 48.0, 'power': dict(ok['power'], undervoltage_now=True,
                                                      frequency_capped_now=True)}
        self.assertEqual([r['fault'] for r in watch.degraded_reasons(st, healthy_recorder, brownout)],
                         ['supply undervoltage', 'CPU frequency capped'])
        # Off-Pi, or before the first host sample, there is nothing to report.
        self.assertEqual(watch.degraded_reasons(st, healthy_recorder, None), [])
        self.assertEqual(watch.degraded_reasons(st, healthy_recorder, {'power': None}), [])
        # Stream faults and host faults appear together, stream first.
        st.dropped = 512
        self.assertEqual([r['fault'] for r in watch.degraded_reasons(st, healthy_recorder, hot)],
                         ['dropped samples', 'thermal throttling',
                          'soft temperature limit reached'])

    def test_interrupted_session_still_leaves_countable_summary(self):
        """Vehicle power cut the 8 September drive and left a zero-byte summary."""
        with tempfile.TemporaryDirectory() as tmp:
            recorder = recording.Recorder(tmp).start()
            try:
                st = watch.Stats()
                st.worker_alive = True
                st.windows, st.hits, st.confirmed = 349, 7178, 23
                ctx = watch.Ctx(stats=st, t_start=time.time(), centre=382.5, fs=6e6, notifier=None)
                common = dict(stats=st, ring=watch.Ring(64), ctx=ctx, recorder=recorder,
                              started_mono=time.monotonic(), receiver=None, failed=False,
                              failure_reason=None, recorder_drained=None)
                mid = watch.build_summary(False, **common)
                path = Path(tmp)/'summary.json'
                recording.atomic_json(path, mid)
                # What a reader finds after the power goes: real counters, and no
                # claim that the session finished.
                saved = json.loads(path.read_text())
                self.assertFalse(saved['final'])
                self.assertEqual((saved['windows'], saved['hits'], saved['confirmed_hits']),
                                 (349, 7178, 23))
                self.assertIsNone(saved['host_at_end'])
                # Windows times their length is the signal time; the length changed twice in November.
                self.assertEqual(saved['window_s'], watch.WINDOW_S)
                self.assertAlmostEqual(saved['windows'] * saved['window_s'], 349 * watch.WINDOW_S)
                final = watch.build_summary(True, **dict(common, recorder_drained=True))
                self.assertTrue(final['final'])
                self.assertTrue(final['recorder_drained'])
            finally:
                recorder.stop(timeout=5)

    def test_the_stress_test_uses_the_detector_window_length(self):
        """It said 1.5 s for two days after the detector went to 0.5 s."""
        import stress_test
        self.assertEqual(stress_test.WINDOW_S, watch.WINDOW_S)
        self.assertEqual(watch.QUIET_INPUT_WINDOWS, round(watch.QUIET_INPUT_SECONDS / watch.WINDOW_S))

    def test_no_restart_stops_after_one_failed_child_without_usb_recovery(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import supervise
        for outcome in ('stall', 'crash'):
            hardware = Mock()
            with self.subTest(outcome=outcome), \
                 patch.object(supervise.subprocess, 'Popen') as popen, \
                 patch.object(supervise, 'monitor', return_value=outcome), \
                 patch.object(supervise.recover, 'perform') as recover, \
                 patch.object(supervise, 'service_notify'):
                rc = supervise.run(SimpleNamespace(no_restart=True), threading.Event(), lambda _: None,
                                   _build_cmd=lambda _: ['FAKE_NO_RECEIVER'], hardware=hardware)
            self.assertEqual(rc, supervise.STALL_EXIT_CODE)
            popen.assert_called_once()
            recover.assert_not_called()
            hardware.on_stopped.assert_called_once_with(outcome)

    def test_ntp_step_cannot_make_a_failed_child_look_healthy(self):
        """The supervisor timed children on the wall clock, which steps at boot.

        On 16 September a 6.6-minute child was logged as "clean after 170675s"
        because timesyncd corrected the clock mid-session. The same arithmetic
        fed `if ran >= healthy_after: consecutive = 0`, so a child that died
        seconds into a cold boot would have reset the recovery escalation
        ladder - in exactly the cold-boot-in-the-car case the ladder is for.
        """
        from types import SimpleNamespace
        from unittest.mock import Mock
        import supervise
        logs = []
        stop = threading.Event()
        opts = SimpleNamespace(no_restart=False, healthy_after=600.0, usb_reset=False,
                               power_cycle=False, hub_location=None, max_restarts=2,
                               backoff_base=0.0, backoff_cap=0.0)
        # Every wall-clock reading jumps 47.3 h forward, as the Pi's did; the
        # monotonic clock advances by the three seconds the child really lasted.
        wall = iter([1789408000.0] + [1789578275.0] * 20)
        mono = iter([100.0, 103.0] * 20)
        with patch.object(supervise.subprocess, 'Popen'), \
             patch.object(supervise, 'monitor', return_value='crash'), \
             patch.object(supervise.recover, 'perform', return_value=(True, 'relaunched')), \
             patch.object(supervise, 'service_notify'), \
             patch.object(supervise.time, 'time', lambda: next(wall)), \
             patch.object(supervise.time, 'monotonic', lambda: next(mono)):
            supervise.run(opts, stop, logs.append,
                          _build_cmd=lambda _: ['FAKE_NO_RECEIVER'], hardware=Mock())
        text = "\n".join(logs)
        self.assertIn('crash after 3s', text)
        self.assertNotIn('170675s', text)
        # The escalation must keep counting, not restart from one.
        self.assertIn('consecutive failure 2', text)
        self.assertNotIn('resetting escalation', text)

    def test_service_ready_requires_fresh_completed_work_from_current_child(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        import supervise
        good = {'pid': 9000, 'ready': True, 'windows': 1, 't': 1000,
                'last_success_utc': 1000, 'samples_in': 18000000}
        variants = [good, dict(good, pid=8999), dict(good, t=900),
                    dict(good, last_success_utc=900), dict(good, windows=0), dict(good, ready=False)]
        opts = SimpleNamespace(stall_timeout=20, grace=10, startup_grace=20, heartbeat='unused')
        stop = SimpleNamespace(is_set=lambda: False, wait=lambda _: None)
        for snap in variants:
            child = Mock(pid=9000)
            child.poll.side_effect = [None, 0]
            with self.subTest(snap=snap), \
                 patch.object(supervise, 'read_heartbeat', return_value=snap), \
                 patch.object(supervise.time, 'time', return_value=1000), \
                 patch.object(supervise, 'service_notify') as announce:
                self.assertEqual(supervise.monitor(child, opts, stop, lambda _: None), 'clean')
            self.assertEqual(announce.call_count, int(snap == good))

    def test_preflight_refuses_busy_receiver_before_discovering_tests(self):
        import contextlib
        import io
        import instance
        import preflight
        output = io.StringIO()
        with patch.object(sys, 'argv', ['preflight.py', '--offline', '--tests']), \
             patch.object(instance, 'acquire_idle', side_effect=RuntimeError('receiver busy')), \
             patch.object(preflight.unittest.defaultTestLoader, 'discover') as discover, \
             contextlib.redirect_stdout(output):
            self.assertEqual(preflight.main(), 1)
        discover.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())['tests_started'])

    def test_idle_guard_holds_detector_lock_and_checks_legacy_tools(self):
        import instance
        with tempfile.TemporaryDirectory() as tmp, patch.object(instance, 'receiver_processes', return_value=[]):
            owner = instance.acquire_idle(tmp)
            try:
                with self.assertRaises(RuntimeError):
                    instance.acquire(Path(tmp)/'runtime/watch.lock')
            finally:
                owner.close()
            with patch.object(instance, 'receiver_processes', return_value=[{'pid':123,'tool':'antenna.py'}]):
                with self.assertRaisesRegex(RuntimeError, 'antenna.py'):
                    instance.acquire_idle(tmp)
            # A refused preflight must release its reservation.
            instance.acquire(Path(tmp)/'runtime/watch.lock').close()

    def test_drop_accounting_survives_bounded_gap_history(self):
        """RF timestamps must never walk backwards while samples are being lost.

        `discontinuities` is a bounded deque, so the oldest gaps are evicted
        during exactly the sustained loss the operator is trying to diagnose.
        """
        stats = watch.Stats()
        ring = watch.Ring(1 << 16)
        # A stand-in for the driver's types, so this runs on a machine without libairspy.
        class Transfer(ctypes.Structure):
            _fields_ = [("device", ctypes.c_void_p), ("ctx", ctypes.c_void_p), ("samples", ctypes.c_void_p),
                        ("sample_count", ctypes.c_int), ("dropped_samples", ctypes.c_uint64),
                        ("sample_type", ctypes.c_int)]

        fake = types.SimpleNamespace(SAMPLE_BLOCK_CB=lambda f: f)
        with patch.dict(sys.modules, {"airspy_ctypes": fake}):
            cb = watch.make_callback(ring, stats)

        buf = (ctypes.c_int16 * 64)()
        maxlen = stats.discontinuities.maxlen
        for _ in range(maxlen + 50):                  # overflow the gap history
            blk = Transfer(
                samples=ctypes.cast(buf, ctypes.c_void_p), sample_count=32,
                dropped_samples=1000)
            cb(ctypes.pointer(blk))
        self.assertEqual(len(stats.discontinuities), maxlen)
        self.assertGreater(stats.dropped_retired, 0, "evicted gaps must keep counting")

        read = ring.write                             # ahead of every evicted gap
        floor = watch.dropped_before(list(stats.discontinuities), stats.dropped_retired, read)
        self.assertGreaterEqual(floor, stats.dropped_retired)

        # Replay the worker's accounting as `read` advances; it may only grow.
        seen, previous = 0, 0
        for position in range(0, read + 1, max(1, read // 40)):
            seen = watch.dropped_before(list(stats.discontinuities),
                                        stats.dropped_retired, position, seen)
            self.assertGreaterEqual(seen, previous, "drop total went backwards")
            previous = seen
        self.assertLessEqual(seen, stats.dropped)


    def test_human_mark_retains_independent_pre_and_post_iq(self):
        import marking
        with tempfile.TemporaryDirectory() as tmp:
            ring = watch.Ring(15000)
            ring.push(np.ones(10000, np.int16))  # five seconds
            r = recording.Recorder(tmp, min_free=0).start()
            ctx = watch.Ctx(outdir=tmp, fs=1000, centre=382.5, stats=watch.Stats(),
                            recorder=r, events=[])
            marks = marking.Marks(ctx, ring)
            (Path(tmp)/'marks/test.request.json').write_text(json.dumps(
                {'utc':time.time()-.2,'label':'ambulance','note':'human sighting'}))
            marks.poll()
            self.assertEqual(len(marks.pending), 1)
            ring.push(np.ones(6000, np.int16))  # postroll
            marks.last_poll = -1e30
            marks.poll()
            self.assertTrue(r.stop())
            outcomes = [json.loads(s) for s in (Path(tmp)/'recordings.jsonl').read_text().splitlines()]
            self.assertEqual(outcomes[0]['state'], 'written')
            self.assertEqual(outcomes[0]['bytes'], 24000)
            self.assertEqual(outcomes[0]['metadata']['mark']['source'], 'human; not inferred from RF')
            self.assertEqual(len(marks.pending), 0)

    def test_radio_setup_is_explicit_and_setup_failure_cleans_up(self):
        from types import SimpleNamespace
        import contextlib
        import io
        calls = []
        class Error(RuntimeError):
            pass
        class Lib:
            def __getattr__(self, name):
                def call(*args):
                    calls.append((name, args))
                    return -1 if name == 'airspy_start_rx' else 0
                return call
        def check(rc, name):
            if rc:
                raise Error(name)
        fake = SimpleNamespace(lib=Lib(), AirspyError=Error, check=check, SAMPLE_INT16_IQ=2,
                               SAMPLE_BLOCK_CB=lambda f: f, samplerates=lambda dev: [6000000])
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(sys.modules, {'airspy_ctypes': fake}), \
             patch.object(watch.paths, 'ROOT', tmp), patch.object(watch.paths, 'CAPTURES', str(Path(tmp)/'captures')), \
             patch.object(watch.diagnostics, 'receiver_info', return_value={}), \
             patch.object(watch.diagnostics, 'host_health', return_value={}), \
             patch.object(watch.signal, 'signal'), \
             patch.object(sys, 'argv', ['watch.py', '--minutes', '.01', '--no-notify']), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(watch.main(), 1)  # setup failure after open is not a stream stall
            summary = json.loads(next((Path(tmp)/'captures').glob('*/summary.json')).read_text())
            self.assertTrue(summary['failed'])
            self.assertTrue(summary['recorder_drained'])
            self.assertFalse(json.loads((Path(tmp)/'runtime/watch.heartbeat').read_text())['ready'])
        settings = {name: args[1] for name,args in calls if name.startswith('airspy_set_')}
        for key in ('lna_agc','mixer_agc','rf_bias','packing'):
            self.assertEqual(settings['airspy_set_'+key], 0)
        self.assertIn('airspy_close', [name for name,_ in calls])
        self.assertNotIn('airspy_stop_rx', [name for name,_ in calls])

    def test_threaded_worker_consumes_and_reports_death(self):
        ring = watch.Ring(15000)
        ctx = watch.Ctx(stats=watch.Stats(), fs=1000, want_samples=3000, t_start=time.time(),
                        history=None, verbose=False)
        completed = threading.Event()
        def process(*args, **kwargs):
            ctx.stats.windows += 1
            completed.set()
        watch._stop.clear()
        with patch.object(watch, "process_window", side_effect=process):
            worker = threading.Thread(target=watch.analysis_worker, args=(ring, ctx))
            worker.start()
            time.sleep(.03)
            ring.push(np.ones(6000, np.int16))
            self.assertTrue(completed.wait(2))
            watch._stop.set()
            worker.join(2)
        self.assertFalse(ctx.stats.worker_alive)
        self.assertEqual(ctx.stats.windows, 1)
        watch._stop.clear()

    def test_ring_wrap_oversized_and_unavailable(self):
        ring = watch.Ring(10)
        source = np.arange(70, dtype=np.int16)
        ring.push(source[:12])
        ring.push(source[12:34])
        self.assertEqual(ring.write, 17)
        np.testing.assert_array_equal(ring.extract(7, 10), source[14:34])
        with self.assertRaises(BufferError):
            ring.extract(6, 10)
        with self.assertRaises(BufferError):
            ring.extract(16, 2)
        ring.push(source[34:])
        np.testing.assert_array_equal(ring.extract(25, 10), source[50:])
        ring._reserved_write += 5  # producer has reserved overwrite, not published
        with self.assertRaises(BufferError):
            ring.extract(25, 3)

    def test_recording_metadata_and_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = recording.Recorder(tmp, min_free=0).start()
            value = np.arange(32, dtype="<i2")
            self.assertTrue(r.event({"event_id": "e", "recording_state": "requested"}))
            self.assertEqual(r.samples("e.iq", value, {"format": "int16_iq_le"}), "queued")
            self.assertTrue(r.stop())
            np.testing.assert_array_equal(np.fromfile(Path(tmp) / "e.iq", "<i2"), value)
            self.assertEqual(json.loads((Path(tmp) / "recordings.jsonl").read_text())["state"], "written")
            self.assertEqual(r.written_events, 1)
            self.assertFalse(r.event({"late": True}))

    def test_recording_limits_and_disk_full_keep_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = recording.Recorder(tmp, max_bytes=10, min_free=10**30, event_capacity=1)
            self.assertTrue(r.event({"important": True}))
            self.assertFalse(r.event({"overflow": True}))
            self.assertEqual(r.samples("too-big.iq", np.zeros(20, np.int16), {}), "queue_full")
            self.assertEqual(r.samples("small.iq", np.zeros(2, np.int16), {}), "queued")
            r.start()
            self.assertTrue(r.stop())
            self.assertEqual(r.pending_bytes, 0)
            self.assertTrue(json.loads((Path(tmp) / "events.jsonl").read_text())["important"])
            self.assertEqual(json.loads((Path(tmp) / "recordings.jsonl").read_text())["state"], "low_disk")
            self.assertFalse((Path(tmp) / "small.iq").exists())

    def test_heartbeat_requires_completed_work_and_stopped_is_not_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = watch.Ctx(stats=watch.Stats(), t_start=time.time(), centre=382.5)
            ring = watch.Ring(10)
            ring.push(np.ones(20, np.int16))
            path = str(Path(tmp) / "heartbeat")
            watch.write_heartbeat(path, ctx, ring, time.time())
            self.assertFalse(json.loads(Path(path).read_text())["ready"])
            ctx.stats.last_success_utc = time.time()
            watch.write_heartbeat(path, ctx, ring, time.time())
            self.assertTrue(json.loads(Path(path).read_text())["ready"])
            ctx.stats.worker_alive = False
            watch.write_heartbeat(path, ctx, ring, time.time())
            self.assertFalse(json.loads(Path(path).read_text())["ready"])

    def test_pipeline_failure_does_not_advance_completed_windows(self):
        ctx = watch.Ctx(stats=watch.Stats(), fs=1000, t_start=time.time(), centre=382.5)
        with patch.object(pipeline, "channelise", side_effect=RuntimeError("failure")), \
             patch.object(pipeline.diagnostics, "signal_health", return_value={"glitch_block": False}):
            with self.assertRaises(RuntimeError):
                pipeline.process_window(np.ones(3000, np.complex64), None, 3000, 3000, ctx)
        self.assertEqual(ctx.stats.windows, 0)
        self.assertEqual(ctx.stats.windows_started, 1)

    def test_signal_trend_does_not_claim_motion(self):
        t = watch.TrendTracker()
        self.assertEqual(t.update(1, 0, 10), "new")
        self.assertEqual(t.update(1, 1, 16), "rising")
        self.assertEqual(t.update(1, 2, 8), "falling")


if __name__ == "__main__":
    unittest.main()
