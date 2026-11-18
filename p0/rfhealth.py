"""Bounded receive-path telemetry from the detector's existing channel powers.

No receiver access, extra FFT, tuning, threshold changes or distance inference.
Power is in the channeliser's uncalibrated units. Temporal floor includes any
persistent transmissions/interference; it is not a measured receiver noise figure.
"""
import time

import numpy as np

MAX_FRAMES = 512


class Monitor:
    def __init__(self):
        self.reference = None
        self.reference_key = None
        self.reference_window = None
        self.last_match = None

    def update(self, ch, sig, hits, *, window_id, utc_start, gain, threshold_db,
               processing_seconds, channel_seconds, sample_stride=32):
        started = time.monotonic()
        power = ch.power
        if power.ndim != 2 or not all(power.shape) or not np.isfinite(power).all() or (power < 0).any():
            raise ValueError('invalid channel power matrix')
        # Full-window peaks retain brief energy; floors/activity are bounded
        # samples, explicitly labelled. These diagnostics never select candidates.
        step = max(1, int(np.ceil(power.shape[1] / MAX_FRAMES)))
        sampled = power[:, ::step]
        floor = np.percentile(sampled, 25, axis=1)
        peak = power.max(axis=1)
        db = lambda a: 10 * np.log10(np.maximum(a, 1e-30))
        floor_db, peak_db = db(floor), db(peak)
        median_floor = float(np.median(floor_db))
        gain = list(gain) if gain is not None else None
        key = (ch.fs, ch.centre_hz, ch.frame_s, ch.cal_ppm, tuple(gain or ()), tuple(ch.channel_hz))
        if key != self.reference_key:
            self.reference_key = key
            self.reference = floor_db.copy()
            self.reference_window = window_id
        changes = floor_db - self.reference
        activity = np.mean(sampled > floor[:, None] * 10 ** (threshold_db / 10), axis=1)
        channels = []
        for i in range(len(floor)):
            channels.append({'channel': int(ch.tetra_ch[i]), 'frequency_hz': float(ch.channel_hz[i]),
                             'floor_db': round(float(floor_db[i]), 2),
                             'peak_db': round(float(peak_db[i]), 2),
                             'peak_above_floor_db': round(float(peak_db[i]-floor_db[i]), 2),
                             'floor_change_db': round(float(changes[i]), 2),
                             'activity_fraction_sampled': round(float(activity[i]), 4)})
        by_channel = {r['channel']: r for r in channels}
        confirmed = [h for h in hits if h.get('confirmed') is True]
        for hit in sorted(confirmed, key=lambda h: h.get('elapsed_s', 0)):
            self.last_match = {'event_id': hit.get('event_id'), 'utc': hit.get('utc'),
                               'channel': hit['tetra_channel'], 'frequency_hz': hit['frequency_hz'],
                               'peak_snr_db': hit['peak_snr_db'] if hit.get('strength_reliable', True) else None,
                               'strength_reliable': hit.get('strength_reliable', True),
                               'signal_label': hit.get('proximity_label'),
                               'trend': hit.get('trend'), 'processed_utc': hit.get('processed_utc'),
                               'channel_power': by_channel.get(hit['tetra_channel']),
                               'vehicle_identity': None, 'distance_m': None}
        unchecked = sum(h.get('confirmed') is None for h in hits)
        partial = sum(h.get('confirmed') is not None and not h.get('confirmation_coverage', {}).get('complete', False) for h in hits)
        costs = sorted(({'channel': h['tetra_channel'],
                         'seconds': float(h.get('confirm', {}).get('seconds', 0)),
                         'confirmed': h.get('confirmed'),
                         'coverage': h.get('confirmation_coverage')} for h in hits),
                       key=lambda r: r['seconds'], reverse=True)
        warnings = []
        if sig.get('saturating'):
            warnings.append('digital_iq_near_rail')
        if sig.get('antenna_suspect') is True:
            warnings.append('low_sampled_input_against_legacy_reference')
        elevated = int(np.sum(floor_db > median_floor + 6))
        if elevated:
            warnings.append('uneven_channel_floor')
        if abs(float(np.median(changes))) >= 6:
            warnings.append('floor_changed_from_first_window')
        if unchecked or partial:
            warnings.append('confirmation_incomplete')
        if processing_seconds >= ch.duration_s * .8:
            warnings.append('processing_budget_tight')
        result = {'schema_version': 1, 'event_type': 'rf_health', 'window_id': window_id,
                  'utc_start': utc_start, 'measured_utc': time.time(), 'window_duration_s': ch.duration_s,
                  'sample_rate_hz': ch.fs, 'centre_hz': ch.centre_hz, 'gain_indices': gain,
                  'threshold_db': threshold_db, 'calibration_ppm': ch.cal_ppm,
                  'input': dict(sig, sample_stride=sample_stride, antenna_connected=None,
                                adc_clipping_measured=False, power_calibrated_dbm=False),
                  'power_units': 'dB re 1 channeliser power unit; not dBm or dBFS',
                  'floor_method': 'temporal 25th percentile of sampled channel power; may include persistent RF',
                  'frames_total': power.shape[1], 'frames_sampled': sampled.shape[1],
                  'floor_reference_window': self.reference_window,
                  'median_floor_db': round(median_floor, 2),
                  'median_floor_change_db': round(float(np.median(changes)), 2),
                  'elevated_floor_channels': elevated, 'channels': channels,
                  'strongest_channels': sorted(channels, key=lambda r: r['peak_above_floor_db'], reverse=True)[:6],
                  'highest_floor_channels': sorted(channels, key=lambda r: r['floor_db'], reverse=True)[:6],
                  'candidates': len(hits), 'confirmed': len(confirmed), 'unchecked': unchecked,
                  'partial': partial, 'confirmation_seconds': round(sum(r['seconds'] for r in costs), 4),
                  'costliest_candidates': costs[:6], 'last_match': self.last_match,
                  'processing_before_telemetry_s': round(processing_seconds, 4),
                  'channel_seconds': round(channel_seconds, 4), 'warnings': warnings}
        result['telemetry_compute_seconds'] = round(time.monotonic()-started, 5)
        return result


def compact(reading):
    """Keep all-channel history on disk, but keep the heartbeat small."""
    return {k: v for k, v in reading.items() if k != 'channels'}
