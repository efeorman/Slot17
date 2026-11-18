import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'p0'))
import diagnostics
import pipeline
import recording
import rfhealth
import status
import watch
from channelize import Channelised


class HealthTests(unittest.TestCase):
    def channels(self, scale=1):
        p = np.ones((8, 1000), np.float32)*scale
        p[3] *= 100
        p[6, 50:70] *= 1000
        return Channelised(p, 380.0125e6+np.arange(8)*25000, np.arange(8), .003, 382.5e6, 6e6)

    def update(self, monitor, ch, window='w1', gain=(12, 12, 12), hits=None):
        return monitor.update(ch, diagnostics.signal_health(np.ones(20, np.complex64), 1000), hits or [],
                              window_id=window, utc_start=1000, gain=gain, threshold_db=6,
                              processing_seconds=.3, channel_seconds=.1)

    def test_digital_component_headroom_is_not_complex_magnitude_or_antenna_proof(self):
        normal = diagnostics.signal_health(np.array([23000+23000j], np.complex64), 1000)
        self.assertFalse(normal['saturating'])
        self.assertGreater(normal['headroom_db'], 3)
        self.assertIsNone(normal['antenna_suspect'])
        hot = diagnostics.signal_health(np.array([-32768+0j], np.complex64), 1000)
        self.assertFalse(hot['glitch_block'])
        self.assertTrue(hot['saturating'])
        for value in (40000+0j, complex(float('nan'), 0), complex(float('inf'), 0)):
            bad = diagnostics.signal_health(np.array([value], np.complex64), 1000)
            self.assertTrue(bad['glitch_block'])
            self.assertFalse(bad['saturating'])
            json.dumps(bad, allow_nan=False)

    def test_floor_changes_bursts_and_reference_reset_without_mutating_power(self):
        m = rfhealth.Monitor()
        ch = self.channels()
        before = ch.power.copy()
        first = self.update(m, ch)
        self.assertEqual(first['highest_floor_channels'][0]['channel'], 3)
        self.assertEqual(first['strongest_channels'][0]['channel'], 6)
        self.assertLessEqual(first['frames_sampled'], 512)
        self.assertEqual(first['median_floor_change_db'], 0)
        self.assertIsNone(first['input']['antenna_connected'])
        second = self.update(m, self.channels(10), 'w2')
        self.assertAlmostEqual(second['median_floor_change_db'], 10)
        self.assertIn('floor_changed_from_first_window', second['warnings'])
        self.assertEqual(second['channels'][6]['peak_above_floor_db'], 30)
        third = self.update(m, self.channels(10), 'w3', gain=(8, 8, 8))
        self.assertEqual(third['median_floor_change_db'], 0)
        self.assertEqual(third['floor_reference_window'], 'w3')
        np.testing.assert_array_equal(ch.power, before)
        json.dumps(third, allow_nan=False)

    def test_telemetry_failure_does_not_discard_completed_detection(self):
        ctx = watch.Ctx(stats=watch.Stats(), fs=1000, centre=382.5, threshold_db=6, dl=None,
                        t_start=time.time(), events=[], verbose=False, save_snippets=False,
                        trend=watch.TrendTracker())
        with patch.object(pipeline, 'channelise', return_value=self.channels()), \
             patch.object(pipeline, 'detect', return_value=[]), \
             patch.object(rfhealth.Monitor, 'update', side_effect=ValueError('probe failed')):
            pipeline.process_window(np.ones(3000, np.complex64), None, 3000, 3000, ctx)
        self.assertEqual(ctx.stats.windows, 1)
        self.assertEqual(ctx.stats.telemetry_errors, 1)
        self.assertEqual(ctx.events[-1]['event_type'], 'telemetry_error')

    def test_status_does_not_scan_logs_and_labels_rf_stale_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'heartbeat'
            path.write_text(json.dumps({'t': time.time(), 'pid': 42, 'ready': True,
                'rf_health': {'measured_utc': time.time()-90, 'input': {}}, 'session': tmp}))
            with patch.object(status, 'detector_processes', return_value=['42 watch.py']), \
                 patch.object(status, 'airspy_on_usb', return_value=True), \
                 patch.object(status, 'newest_session', side_effect=AssertionError('must not scan')):
                result = status.collect(str(path))
            self.assertTrue(result['ready'])
            self.assertGreater(result['rf_age_s'], 85)
            self.assertIsNone(result['events'])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status.report(result)
            self.assertIn('STALE/HISTORICAL', output.getvalue())
            self.assertIn('antenna connection/efficiency: UNKNOWN', output.getvalue())

    def test_atomic_summary_preserves_previous_on_failed_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'summary.json'
            recording.atomic_json(path, {'windows': 17})
            with patch.object(recording.os, 'replace', side_effect=OSError('disk error')):
                with self.assertRaises(OSError):
                    recording.atomic_json(path, {'windows': 18})
            self.assertEqual(json.loads(path.read_text()), {'windows': 17})
            recording.atomic_json(path, {'windows': 19})
            self.assertEqual(json.loads(path.read_text()), {'windows': 19})

    def test_heartbeat_carries_probes_and_reports_failed_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = watch.Ctx(stats=watch.Stats(), t_start=time.time(), centre=382.5, fs=1000,
                            rf_health={'window_id': 'w1'})
            ring = watch.Ring(100)
            path = str(Path(tmp)/'heartbeat')
            with patch.object(recording, 'atomic_json', side_effect=OSError('test failure')), contextlib.redirect_stdout(io.StringIO()):
                watch.write_heartbeat(path, ctx, ring, time.time())
            self.assertEqual(ctx.stats.heartbeat_errors, 1)
            watch.write_heartbeat(path, ctx, ring, time.time())
            result = json.loads(Path(path).read_text())
            self.assertEqual(result['rf_health']['window_id'], 'w1')
            self.assertEqual(result['heartbeat_errors'], 1)
            self.assertTrue(result['degraded'])

    def test_host_power_bits_keep_current_and_historical_separate(self):
        from types import SimpleNamespace
        with patch.object(diagnostics.subprocess, 'run', return_value=SimpleNamespace(stdout='throttled=0x90002')):
            flags = diagnostics._read_pi_throttled()
        self.assertTrue(flags['frequency_capped_now'])
        self.assertFalse(flags['undervoltage_now'])
        self.assertTrue(flags['undervoltage_since_boot'])
        self.assertTrue(flags['soft_temperature_limit_since_boot'])
        self.assertFalse(flags['throttled_since_boot'])


if __name__ == '__main__':
    unittest.main()
