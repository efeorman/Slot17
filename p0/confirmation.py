"""Bounded per-burst confirmation with replayable narrowband evidence.

Only small regions around candidate bursts are mixed from wideband IQ. Silence
between events costs neither full-rate mixing nor training search. Offsets and
sample rates are recorded so re-examination uses the identical data and policy.
"""
import time

import numpy as np

import examine

PAD_S = 0.006
MAX_BURSTS = 6
SLOT_S = 255 / 18000


def burst_pairs(hit):
    """The (start, stop) times that may be inspected, in the order `intervals` returns them."""
    pairs = hit.get("burst_intervals_s")
    if not pairs:
        first = float(hit.get("first_s", 0))
        duration = float(hit.get("median_burst_ms", 14)) / 1000
        pairs = [(first, first + duration)]
        last = float(hit.get("last_s", first))
        if last > first + duration:
            pairs.append((last, last + duration))
    expanded = []
    for a, b in pairs:
        if np.isfinite(a) and np.isfinite(b) and 0 < b - a <= 4 * SLOT_S + .004:
            expanded.append((a, b))
    if len(expanded) > MAX_BURSTS:
        # Bursts away from a broadband moment are kept before the ones inside it.
        shared = set(hit.get("coincident_burst_starts_s") or ())
        clean = [p for p in expanded if p[0] not in shared]
        if shared and 0 < len(clean) < len(expanded):
            rest = [p for p in expanded if p[0] in shared]
            expanded = sorted(_spread(clean, MAX_BURSTS) + _spread(rest, MAX_BURSTS - min(len(clean), MAX_BURSTS)))
        else:
            # Even coverage avoids always choosing the strongest/earliest repeated burst.
            expanded = _spread(expanded, MAX_BURSTS)
    return expanded


def _spread(items, count):
    if count <= 0:
        return []
    if len(items) <= count:
        return list(items)
    return [items[k] for k in np.unique(np.linspace(0, len(items) - 1, count, dtype=int))]


def intervals(hit, fs, size):
    return [(max(0, int((a - PAD_S) * fs)), min(size, int((b + PAD_S) * fs)))
            for a, b in burst_pairs(hit)]


def confirm(iq, fs, centre_hz, hit, selected_index=None):
    """Mutate a hit with the decision; return its small narrowband recording.

    The decision requires physical-layer evidence. The legacy spectral score is
    retained as diagnostic metadata, never sufficient to trigger an alert.
    """
    began = time.monotonic()
    hit.pop('matched_burst', None)
    chunks, segments, reports = [], [], []
    target = float(hit.get("tuning_hz") or hit["frequency_hz"])
    cursor = 0
    regions = intervals(hit, fs, len(iq))
    if selected_index is not None:
        regions = regions[selected_index:selected_index + 1]
    for lo, hi in regions:
        if hi <= lo:
            continue
        bb, rate = examine.tune(iq[lo:hi], fs, centre_hz, target)
        r = examine.analyse(bb, rate)
        for burst in r['phy']['bursts']:
            if burst['accepted'] and 'matched_burst' not in hit:
                # Select the first matched envelope, not an unrelated early energy burst.
                hit['matched_burst'] = {
                    'start_s': lo / fs + burst['start_sample'] / rate,
                    'stop_s': lo / fs + burst['stop_sample'] / rate,
                    'training': burst['training'],
                    'rule': burst.get('rule'),
                    'residual_offset_hz': burst.get('residual_offset_hz'),
                    # Whole-burst carrier offset and its noise (phy.carrier_frequency); a record, not evidence.
                    'carrier_hz': burst.get('carrier_hz'),
                    'carrier_noise_hz': burst.get('carrier_noise_hz'),
                    'timing_basis': 'matched narrowband envelope',
                }
                break
        reports.append(r)
        chunks.append(bb)
        segments.append({"offset": cursor, "count": len(bb),
                         "window_start_sample": lo, "window_stop_sample": hi})
        cursor += len(bb)
    accepted = any(r["accepted"] for r in reports)
    best = max(reports, key=lambda r: (r["accepted"],
               max((b["correlation"] * b["modulation_coherence"] for b in r["phy"]["bursts"]), default=0)), default=None)
    hit["confirmed"] = accepted
    hit["classification"] = "tetra_training_match" if accepted else "unconfirmed_rf_candidate"
    hit["confirm"] = {"method": "short-burst-phy-v2", "accepted": accepted,
                      "segments": len(segments), "seconds": round(time.monotonic() - began, 4),
                      "phy": [r["phy"] for r in reports]}
    if best:
        hit["confirm"].update({"signatures": best["signatures_present"],
                              "occ_bw_khz": best["occ_bw_khz"],
                              "spectral_peak_excess_db": best["peak_snr_db"]})
    samples = np.concatenate(chunks).astype(np.complex64) if chunks else np.empty(0, np.complex64)
    return samples, {"format": "complex64_le", "sample_rate_hz": examine.CHANNEL_FS,
                     "tuning_hz": target, "segments": segments,
                     "original_sample_rate_hz": fs, "centre_hz": centre_hz}


def replay(samples, metadata):
    """Re-evaluate each saved segment separately (no artificial join edges)."""
    decisions = []
    for segment in metadata["segments"]:
        lo, n = segment["offset"], segment["count"]
        decisions.append(examine.analyse(samples[lo:lo + n], metadata["sample_rate_hz"])["accepted"])
    return any(decisions)
