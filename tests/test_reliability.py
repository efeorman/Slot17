"""Regressions for the September drive audit. Offline, with fake transports."""
import hashlib
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

import numpy as np
from scipy.signal import resample_poly

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'p0'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify_isolation  # noqa: E402,F401 - see the module
import channelize
import confirmation
import examine
import failure
import notify
import outbox
import pipeline
import supervise
import tetra_gen
import watch
import test_notifications as notification_tests
from test_notifications import until


class DeliveryRegressions(unittest.TestCase):
    setUp = notification_tests.NotificationTests.setUp
    tearDown = notification_tests.NotificationTests.tearDown
    sender = notification_tests.NotificationTests.sender

    def test_retry_deadline_is_independent_of_display_and_wall_clock(self):
        calls = []
        def offline(f):
            calls.append(dict(f))
            raise notify.Transient('offline')
        n = self.sender(offline)
        # Margins wide enough for a slow test machine: the first attempt must come before the alert expires.
        with patch.object(notify, 'ALERT_MAX_AGE_S', .6), patch.object(notify, 'RETRY_MIN_S', 1.2):
            n.start()
            self.assertTrue(n.alert_hit({'confirmed': True, 'tetra_channel': 60,
                                       'peak_snr_db': 20, 'burst_mono': time.monotonic()}))
            self.assertTrue(until(lambda: n.expired == 1))
            time.sleep(.8)
            n.stop()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['ttl'], 900)
        self.assertNotIn('_freshness', calls[0])
        self.assertEqual(notify.spool_count(self.path), 0)

    def test_rf_restart_ntp_and_legacy_migration(self):
        with patch.object(outbox.time, 'monotonic', return_value=1000), \
             patch.object(outbox.time, 'time', return_value=10000):
            box = outbox.Outbox(self.path)
            fields = {'title': 'TETRA activity - ch 60', 'message': 'recent', 'ttl': 900,
                      'timestamp': 9000, '_freshness': outbox.fresh_until(1120)}
            box.add(fields)
            box.close()
            box = outbox.Outbox(self.path)
            self.assertEqual(box.expire(999999), 0)  # NTP forward
            self.assertEqual(box.expire(1), 0)       # NTP backward
            self.assertEqual(box.next()[1]['ttl'], 900)
            with patch.object(outbox.time, 'monotonic', return_value=1120):
                self.assertEqual(box.expire(1), 1)
            box.add(fields)
            with patch.object(outbox, 'BOOT_ID', 'different-boot'):
                self.assertEqual(box.expire(1), 1)
            fields.pop('_freshness')
            box.add(fields)                         # legacy RF discarded
            box.add({'message': 'status', 'timestamp': 10000, 'ttl': 3600})
            self.assertEqual(box.expire(10001), 1)
            self.assertEqual(box.next()[1]['message'], 'status')
            box.close()

    def test_escalation_only_counts_cooldown_bypass(self):
        n = self.sender(lambda f: None)
        h = {'confirmed': True, 'tetra_channel': 60, 'peak_snr_db': 10}
        with patch.object(notify.time, 'monotonic', return_value=1000) as clock:
            self.assertTrue(n.alert_hit(h))
            clock.return_value = 1019
            self.assertFalse(n.alert_hit(dict(h, peak_snr_db=30)))
            clock.return_value = 1020
            self.assertTrue(n.alert_hit(dict(h, peak_snr_db=16)))
            clock.return_value = 1080
            self.assertTrue(n.alert_hit(dict(h, peak_snr_db=30)))
        self.assertEqual(n.rf_alerts_escalated, 1)

    def test_unrefined_strength_cannot_escalate_or_set_strength_baseline(self):
        n = self.sender(lambda f:None)
        hit = {'confirmed':True,'tetra_channel':60,'peak_snr_db':10}
        with patch.object(notify.time,'monotonic',return_value=1000) as clock:
            self.assertTrue(n.alert_hit(hit))
            clock.return_value = 1020
            self.assertFalse(n.alert_hit(dict(hit,peak_snr_db=40,strength_reliable=False)))
            clock.return_value = 1060
            self.assertTrue(n.alert_hit(dict(hit,peak_snr_db=40,strength_reliable=False)))
            self.assertIsNone(n._last_alert_snr[60])
            clock.return_value = 1080
            self.assertFalse(n.alert_hit(dict(hit,peak_snr_db=60)))
        self.assertEqual(n.rf_alerts_escalated,0)


class ReportingRegressions(unittest.TestCase):
    def test_matched_later_burst_owns_strength_and_time(self):
        fs = 75000
        iq = np.zeros(fs, np.complex64)
        # An early strong tone must not lend its strength/time to a later CUB.
        iq[7500:7950] = 20
        tx = tetra_gen.make_transmission(.1, fs, kind='cub', frames=[0],
                                         first_frame_offset_s=.02, seed=5)
        iq[37500:45000] = tx
        h = {'tetra_channel': 60, 'frequency_hz': 381.5125e6,
             'first_s': .1, 'peak_snr_db': 30,
             'burst_intervals_s': [[.1,.106],[.52,.527]]}
        with patch.object(examine, 'tune', side_effect=lambda iq, *args: (iq, fs)):
            confirmation.confirm(iq, fs, h['frequency_hz'], h)
        self.assertTrue(h['confirmed'])
        self.assertAlmostEqual(h['matched_burst']['start_s'], .52, delta=.001)
        p = np.ones((1,1000), np.float32)
        p[0,100:106] = 1000
        p[0,519:528] = 10
        ch = channelize.Channelised(p, np.array([h['frequency_hz']]), np.array([60]), .001, 382.5e6, fs)
        stamp = pipeline.attribute_match(h, ch, ch.noise_floor())
        self.assertAlmostEqual(stamp, .52, delta=.001)
        self.assertAlmostEqual(h['peak_snr_db'], 10)
        self.assertEqual(h['candidate_peak_snr_db'], 30)
        st = watch.Stats()
        st.capture_start_mono = 1000
        ctx = watch.Ctx(stats=st, fs=fs, centre=382.5, dl=None, threshold_db=6,
                        trend=watch.TrendTracker(), t_start=1700000000,
                        events=[], verbose=False, confirm=True, save_snippets=False)
        with patch.object(pipeline, 'channelise', return_value=ch), \
             patch.object(pipeline, 'detect', return_value=[h]), \
             patch.object(pipeline, 'evaluate_candidates', return_value={}):
            pipeline.process_window(iq, None, len(iq), len(iq), ctx, t_abs_s=10)
        candidate = next(e for e in ctx.events if e.get('event_type') == 'candidate')
        self.assertAlmostEqual(candidate['elapsed_s'], 10.52, delta=.001)
        self.assertAlmostEqual(candidate['burst_mono'], 1010.52, delta=.001)
        import datetime
        self.assertAlmostEqual(datetime.datetime.fromisoformat(candidate['utc']).timestamp(),
                               1700000010.52, delta=.001)

    def test_coverage_cannot_cause_phone_reminders_or_hide_real_fault(self):
        st = watch.Stats()
        self.assertEqual(watch.degraded_reasons(st, None), [])
        n = notify.Notifier(cfg={'token': 'FAKE', 'user': 'FAKE'})
        with patch.object(notify.time, 'monotonic', return_value=0) as clock:
            for second in range(0,7200,30):
                clock.return_value = second
                st.unchecked += 3
                st.partial_confirmations += 7
                self.assertFalse(n.alert_health(watch.degraded_reasons(st, None)))
            self.assertEqual(n.q.qsize(), 0)
            st.dropped = 1000
            self.assertTrue(n.alert_health(watch.degraded_reasons(st, None)))
        fields = n.q.get_nowait()
        self.assertEqual(fields['priority'], notify.PRIORITY_ALERT)
        self.assertIn('dropped samples 1000', fields['message'])
        self.assertNotIn('unchecked', fields['message'])

    def test_attribution_failures_preserve_confirmed_hit_alert_and_recording(self):
        from types import SimpleNamespace
        import contextlib
        import io
        for cause in ('zero_floor','nan_floor','absent_channel','invalid_timing','floor_exception'):
            with self.subTest(cause=cause):
                fs = 75000
                power = np.ones((1,1000), np.float32)
                ch = channelize.Channelised(power,np.array([381.5125e6]),np.array([60]),.001,382.5e6,fs)
                hit = {'tetra_channel':60,'frequency_hz':381.5125e6,'peak_snr_db':35,
                       'first_s':.1,'confirmed':True,'confirm':{'phy':[]},
                       'confirmation_coverage':{'complete':True,'regions_tested':1,'regions_available':1},
                       'matched_burst':{'start_s':.5,'stop_s':.506}}
                if cause == 'absent_channel':
                    ch.tetra_ch[0] = 61
                if cause == 'invalid_timing':
                    hit['matched_burst']['start_s'] = float('nan')
                saved = []
                writer = SimpleNamespace(event=lambda e:bool(json.dumps(e,allow_nan=False)),
                                         samples=lambda *args:saved.append(args) or 'queued')
                n = notify.Notifier(cfg={'token':'FAKE','user':'FAKE'})
                st = watch.Stats()
                ctx = watch.Ctx(stats=st,fs=fs,centre=382.5,dl=None,threshold_db=6,
                                trend=watch.TrendTracker(),t_start=time.time(),events=[],
                                verbose=True,save_snippets=True,recorder=writer,notifier=n)
                floor = np.array([np.nan if cause == 'nan_floor' else 0 if cause == 'zero_floor' else 1.0])
                with patch.object(pipeline,'channelise',return_value=ch), \
                     patch.object(pipeline,'detect',return_value=[hit]), \
                     patch.object(pipeline,'evaluate_candidates',return_value={id(hit):(np.ones(20,np.complex64),{})}), \
                     patch.object(ch,'noise_floor',side_effect=RuntimeError('floor unavailable') if cause == 'floor_exception' else None,return_value=floor), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    pipeline.process_window(np.ones(fs,np.complex64),None,fs,fs,ctx)
                self.assertEqual(st.windows,1)
                self.assertEqual(st.confirmed,1)
                self.assertEqual(st.refinement_failures,1)
                self.assertEqual(len(saved),1)
                self.assertEqual(saved[0][0],'w00000001-ch60.c64')
                self.assertTrue(hit['confirmed'])
                self.assertEqual(hit['notification_state'],'queued')
                self.assertFalse(hit['strength_reliable'])
                self.assertEqual(hit['candidate_peak_snr_db'],35)
                self.assertIn('matched strength unavailable',output.getvalue())
                self.assertAlmostEqual(hit['elapsed_s'],.1 if cause == 'invalid_timing' else .5)
                self.assertIn('strength unavailable',n.q.get_nowait()['message'])

    def test_nub_rejection_counter_counts_inspected_envelopes_once(self):
        bb = np.fromfile(Path(__file__).parent/'fixtures/random-nub-7130.c64',dtype='<c8')
        h = {'tetra_channel':60,'frequency_hz':381.5125e6,'burst_intervals_s':[[.02,.034]]}
        ctx = watch.Ctx(stats=watch.Stats(),fs=75000,centre=382.5,confirm=True,last_confirmed_attempt={})
        with patch.object(examine,'tune',return_value=(bb,75000)):
            recordings = pipeline.evaluate_candidates(bb,[h],ctx,time.monotonic()+10,1)
        self.assertFalse(h['confirmed'])
        self.assertEqual(ctx.stats.nub_training_matches,1)
        self.assertEqual(ctx.stats.nub_tail_rejections,1)
        evidence = h['confirm']['phy'][0]['bursts'][0]
        self.assertTrue(evidence['nub_tail_rejected'])
        self.assertIsNotNone(evidence['nub_tail_rejection'])
        self.assertGreater(evidence['nub_tail_rejection']['tail_symbol_errors'],0)
        self.assertIn(id(h),recordings)

    def test_failure_categories_and_phone_remedies(self):
        self.assertEqual(supervise.classify(failure.exit_code(RuntimeError('NOT_FOUND'), False), False),
                         'receiver unavailable')
        self.assertEqual(supervise.classify(failure.exit_code(failure.StreamStall()), False), 'stall')
        self.assertEqual(supervise.classify(failure.exit_code(failure.AnalysisFailure()), False), 'analysis failure')
        self.assertEqual(supervise.classify(failure.exit_code(ValueError()), False), 'crash')
        hardware = supervise.HardwareNotifier.__new__(supervise.HardwareNotifier)
        with patch.object(hardware, '_push', return_value=True) as push:
            hardware.on_stopped('receiver unavailable')
            self.assertIn('could not be opened', push.call_args.args[0])


class TailRegressions(unittest.TestCase):
    def test_saved_random_nub_and_repeated_copies_are_rejected(self):
        path = Path(__file__).parent/'fixtures/random-nub-7130.c64'
        meta = json.loads(path.with_suffix('.json').read_text())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), meta['sha256'])
        bb = np.fromfile(path, dtype='<c8')
        for offset in (0, -2124, 4500):
            shifted = bb * np.exp(2j*np.pi*offset*np.arange(len(bb))/75000)
            self.assertFalse(examine.analyse(shifted, 75000)['accepted'])
        repeated = np.zeros(len(bb)+4250, np.complex64)
        repeated[:len(bb)] += bb
        repeated[4250:] += bb
        self.assertFalse(examine.analyse(repeated, 75000)['accepted'])

    def test_independent_nub_words_tail_corruption_and_offset(self):
        # Literal standard words/dibit mapping; do not import detector templates.
        rng = np.random.default_rng(101)
        phases = np.array([1,3,-1,-3]) * np.pi/4
        for word in ('1101000011101001110100', '0111101001000011011110'):
            for bad_tail in (False, True):
                bits = rng.integers(0,2,(232,2))  # one preceding phase reference
                bits[111:122] = np.array(list(map(int, word))).reshape(-1,2)
                bits[1:3] = bits[230:232] = [[1,1],[0,0]]
                if bad_tail:
                    bits[230:232] = [[0,0],[1,1]]
                syms = np.exp(1j*np.cumsum(phases[bits[:,0]*2+bits[:,1]]))
                up = np.zeros(len(syms)*4, complex)
                up[::4] = syms
                shaped = np.convolve(up, tetra_gen.rrc_taps(4), mode='same')
                bb = np.pad(resample_poly(shaped,25,24), (1500,1500)).astype(np.complex64)
                for offset in (-2124, 0, 1200):
                    rotated = bb*np.exp(2j*np.pi*offset*np.arange(len(bb))/75000)
                    r = examine.analyse(rotated,75000)
                    self.assertEqual(r['accepted'], not bad_tail, (word, bad_tail, offset, r['phy']))
                    if not bad_tail:
                        b = next(b for b in r['phy']['bursts'] if b['accepted'])
                        self.assertEqual(b['tail_symbols_checked'], 3)
                        self.assertEqual(b['tail_symbol_errors'], 0)

    def test_slotted_voice_remains_inspectable(self):
        tx = tetra_gen.make_transmission(1,75000,kind='nub', seed=10, first_frame_offset_s=.02)
        r = examine.analyse(tx.astype(np.complex64),75000)['phy']
        self.assertEqual(r['tested_bursts'], 12)
        self.assertEqual(r['matched_bursts'], 12)
