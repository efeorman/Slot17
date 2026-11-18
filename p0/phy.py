"""Receive-only, short-burst TETRA physical-layer evidence.

No identities, traffic bits or messages are decoded. Public training words:
ETSI EN 300 392-2 V3.2.1, sections 9.4.4.3.2/3, equations 9.4, 9.5, 9.9.
https://www.etsi.org/deliver/etsi_en/300300_300399/30039202/03.02.01_60/en_30039202v030201p.pdf

Matched RRC filtering, symbol timing search and differential training correlation
work on each burst separately. The old envelope score is not protocol evidence.
Thresholds require independent field validation; a match is not vehicle identity.
"""
import math
from functools import lru_cache

import numpy as np
from scipy.signal import resample_poly

SYMBOL_RATE = 18_000
# Samples per symbol, which is also the number of symbol-timing phases tried. 4 until 1.2.6: a
# grid of a quarter symbol left even a perfect generated burst at 40 dB with a vector error of
# 0.14 (0.07 to 0.09 at 16 per symbol) and cost a 12 dB burst 1 dB at the median, 4.6 dB in the
# worst tenth. At 8, generated bursts at 7 dB were recognised 46 times in 80 (control) and 52
# (normal), against 35 and 45 at 4, with no false match in 2,400 untrained generated bursts and
# 2,818 reversed or conjugated field bursts; the recogniser's own time per burst roughly doubles
# (0.9 to 1.7 ms on a PC), paid for by examine.tune's two stages. On 142,219 saved field snippets
# of three drives: 859 matches against 844 (none lost) and no false match in 284,438 reversed or
# conjugated control examinations, with 4 phases or 8 (Engineering Log, 17 November 2026).
SPS = 8
FS = SYMBOL_RATE * SPS
TRAINING_BITS = {
    "cub": "100111010000111010011101000011",
    "nub1": "1101000011101001110100",
    "nub2": "0111101001000011011110",
}
# Dibit phase mapping; the constant phase of a received carrier cancels in
# differential symbols. Residual carrier frequency is a common rotation.
PHASE = np.array([1, 3, -1, -3], float) * np.pi / 4
# ETSI 9.4.4.3.5, equation 9.12: tail dibits 11,00 at each end.
# Three observable transitions: the second leading tail and both trailing tails.
# The first leading transition has no reliable preceding symbol after ramp-up.
NUB_TAIL_OFFSETS = np.array([-109, 119, 120])
NUB_TAIL = np.exp(1j * PHASE[[0, 3, 0]])

# Acceptance limits. Two things are measured on every envelope: how well the whole burst looks
# like pi/4-DQPSK ("modulation coherence", which noise pulls down fast) and how well its middle
# holds the training word. The standard rule is the one every match up to 12 November 2026 was
# made with. Generated bursts showed that weak ones mostly failed the coherence limit while their
# training word was fine, so three further rules accept a lower coherence, two of them in exchange
# for a stricter word. They were chosen on 243,867 field snippets examined forwards, time-reversed
# and conjugated: 0 of 487,734 control examinations were accepted, against 125 more forwards (9 %).
# Records: the weak-burst review of 12 November 2026. A match says which rule made it.
STANDARD_COHERENCE = 0.35
WORD_LIMITS = {"cub": (0.82, 1), "nub1": (0.88, 0), "nub2": (0.88, 0)}  # correlation, symbol errors
WEAK_CUB_COHERENCE = 0.30               # with the standard word limits
WEAK_CUB_CLEAN = (0.20, 0.90, 35.0)     # coherence, correlation, worst training symbol (degrees); no errors
WEAK_NUB_COHERENCE = 0.15               # with the standard word limits; the tails must fit as always
MIN_COHERENCE = 0.15                    # below this no hypothesis is examined at all


def matching_rule(kind, coherence, corr, errors, worst_deg):
    """'standard', 'weak' or None for one training hypothesis, before the NUB tail check."""
    limit, allowed = WORD_LIMITS[kind]
    word = corr >= limit and errors <= allowed
    if word and coherence >= STANDARD_COHERENCE:
        return "standard"
    if kind == "cub":
        if word and coherence >= WEAK_CUB_COHERENCE:
            return "weak"
        low, clean_corr, clean_worst = WEAK_CUB_CLEAN
        if coherence >= low and corr >= clean_corr and errors == 0 and worst_deg <= clean_worst:
            return "weak"
        return None
    return "weak" if word and coherence >= WEAK_NUB_COHERENCE else None


@lru_cache(maxsize=3)
def template(kind):
    bits = np.fromiter((int(b) for b in TRAINING_BITS[kind]), dtype=int).reshape(-1, 2)
    return np.exp(1j * PHASE[bits[:, 0] * 2 + bits[:, 1]])


@lru_cache(maxsize=1)
def matched_taps():
    from tetra_gen import rrc_taps
    return rrc_taps(SPS).astype(np.float32)


# A coherent test of the training word: the matched-filter symbols z against the word's own symbols
# (the cumulative product of its phase steps), over a grid of residual carrier frequencies. The
# differential test multiplies two noisy symbols per step and costs about 2 dB at low SNR; on
# generated bursts at one control pass in 10,000 this found 184 of 200 control bursts at 6 dB
# against 119, and 194 of 200 normal bursts at 7 dB against 158 (audit/signal-audit-20261117, 18
# November 2026). COHERENT_LIMITS holds the acceptance limits of the `coherent` rule; a limit above
# 1 switches the rule off and leaves only the diagnostic `coherent_correlation`.
# The limits were chosen on the field snippets of two drives (8 and 10 November): the highest valid
# control (time-reversed bursts, and conjugated control bursts) that would otherwise pass was 0.869
# for the CUB word and 0.753 for the NUB words (tails fitting); the limits are those plus 0.03, and
# never below the standard rule's 0.82 / 0.88. Normal training sequence 2 is the conjugate of
# sequence 1, so a conjugated normal burst is a real one and was not used as a word control. On the
# third drive, held out: 36 more matches, 0 false in its controls. Of the 151 matches added on all
# three, 89 % sat within 300 Hz of the drive's typical frequency (chance: 20 %), 95 % had the right
# length and 91 % had another match on the same channel within two minutes - as today's weak-rule
# matches do (96, 98, 91 %). Tested for both kinds at envelope lengths of 7 to 11 ms as well: no gain.
COHERENT_FREQS_HZ = np.arange(-1500.0, 1501.0, 150.0)
COHERENT_LIMITS = {"cub": 0.899, "nub1": 0.88, "nub2": 0.88}


@lru_cache(maxsize=3)
def _coherent_reference(kind):
    t = template(kind)
    r = np.concatenate([[1.0 + 0j], np.exp(1j * np.cumsum(np.angle(t)))])
    k = np.arange(len(r))
    rot = np.exp(-2j * np.pi * COHERENT_FREQS_HZ[:, None] * k[None, :] / SYMBOL_RATE)   # [freq, symbol]
    return (np.conj(r)[None, :] * rot).astype(np.complex64), len(r)


def coherent_word(z, kind, lower, upper):
    """Best coherent correlation of the training word in z over positions lower..upper-1.

    Positions are those of the differential search (index into d = z[1:]*conj(z[:-1]), so the word's
    first symbol is z[position]). Returns (correlation, position, frequency_hz), correlation in [0, 1].
    """
    ref, n = _coherent_reference(kind)
    if upper <= lower or upper - 1 + n > len(z):
        upper = min(upper, len(z) - n + 1)
    if upper <= lower:
        return 0.0, None, None
    zs = np.lib.stride_tricks.sliding_window_view(z, n)[lower:upper]                 # [pos, symbol]
    num = np.abs(zs.astype(np.complex64) @ ref.T)                                    # [pos, freq]
    den = np.sqrt((np.abs(zs) ** 2).sum(axis=1)) * math.sqrt(n)
    c = num / np.maximum(den, 1e-20)[:, None]
    p, f = np.unravel_index(int(np.argmax(c)), c.shape)
    return float(c[p, f]), lower + int(p), float(COHERENT_FREQS_HZ[f])


def _line_slope(k, theta, w):
    total = w.sum()
    km, tm = (w * k).sum() / total, (w * theta).sum() / total
    return (w * (k - km) * (theta - tm)).sum() / (w * (k - km) ** 2).sum()


def carrier_frequency(z):
    """The burst's carrier offset from its whole length, for the record only; no decision uses it.

    `residual_offset_hz` comes from the training word alone (11 to 15 symbols) and scatters by tens
    of Hz. Here every symbol is decided, its pi/4-DQPSK phase step removed, and a power-weighted
    straight line fitted to the carrier phase that is left. `carrier_noise_hz` is the first half's
    estimate minus the second half's, divided by four.
    Generated bursts with a known offset, standard deviation of the error: 0.3 Hz (normal bursts)
    and 1.2 to 1.7 Hz (control bursts) from 15 dB up, against 16 to 49 Hz for the training word;
    at 10 to 12 dB 7 to 41 Hz, because an occasional burst is tens of Hz off and its noise figure
    does not always show it. Trust it from about 15 dB. On 1,409 field bursts of 8 to 17 November
    2026 the noise figure was 1 to 2 Hz at 15 dB or more (Engineering Log, 17 November 2026).
    The receiver's own clock drifts by 37 to 124 Hz over a drive, so this is a relative figure.
    """
    z = np.asarray(z, np.complex128)
    amp = np.abs(z)
    if len(z) < 40 or not np.isfinite(amp).all():
        return None
    core = np.flatnonzero(amp >= 0.5 * np.median(amp[amp > 0])) if (amp > 0).any() else []
    if len(core) < 40:
        return None
    z = z[core[0]:core[-1] + 1]
    d = z[1:] * np.conj(z[:-1])
    u = d / np.maximum(np.abs(d), 1e-20)
    w0 = (np.angle(np.mean(u ** 4)) - np.pi) / 4
    w0 = (w0 + np.pi / 4) % (np.pi / 2) - np.pi / 4
    k = np.arange(len(z), dtype=float)
    w = np.abs(z) ** 2
    for _ in range(2):
        ang = np.angle(u * np.exp(-1j * w0))
        dec = PHASE[np.argmin(np.abs((ang[:, None] - PHASE[None, :] + np.pi) % (2 * np.pi) - np.pi), axis=1)]
        theta = np.unwrap(np.angle(z * np.exp(-1j * np.concatenate([[0.0], np.cumsum(dec)]))))
        w0 = _line_slope(k, theta, w)
    h = len(z) // 2
    half = _line_slope(k[:h], theta[:h], w[:h]) - _line_slope(k[h:], theta[h:], w[h:])
    hz = SYMBOL_RATE / (2 * np.pi)
    if not np.isfinite(w0) or not np.isfinite(half):
        return None
    return {"carrier_hz": round(float(w0 * hz), 2), "carrier_noise_hz": round(abs(float(half * hz)) / 4, 2),
            "carrier_symbols": int(len(z))}


def inspect_burst(bb, fs, start=0, stop=None):
    """Return bounded evidence for one envelope, indices in the supplied IQ.

    Search the middle of the envelope for the embedded training word. TETRA
    uplink CUB/NUB training is centred in the useful burst. Padding preserves
    filter context without adding silence to the modulation statistic.
    """
    stop = len(bb) if stop is None else int(stop)
    start = int(start)
    if not np.isfinite(fs) or fs <= 0 or not 0 <= start < stop <= len(bb):
        raise ValueError("invalid sample rate or burst interval")
    duration = (stop - start) / float(fs)
    empty = {"accepted": False, "training": None, "correlation": 0.0,
             "modulation_coherence": 0.0, "symbol_errors": None,
             "start_sample": start, "stop_sample": stop,
             "duration_ms": round(duration * 1000, 3)}
    if not 0.003 <= duration <= 0.022 or not np.isfinite(fs) or fs <= 0:
        return empty
    pad = int(math.ceil(fs * 0.001))
    lo, hi = max(0, start - pad), min(len(bb), stop + pad)
    g = math.gcd(int(round(fs)), FS)
    x = resample_poly(np.asarray(bb[lo:hi], np.complex64), FS // g, int(round(fs)) // g)
    if not len(x) or not np.isfinite(x).all():
        return empty
    y = np.convolve(x, matched_taps(), mode="same")
    a, b = (start - lo) * FS / fs, (stop - lo) * FS / fs
    kinds = ("cub",) if duration < 0.009 else ("nub1", "nub2")
    best = empty
    best_z = None
    best_coh = None
    nub_training_match = False
    best_tail_rejection = None
    for phase in range(SPS):
        times = np.arange(phase, len(y), SPS)
        valid = (times >= a + 2 * SPS) & (times < b - 2 * SPS)
        z = y[times[valid]]
        if len(z) < 35:
            continue
        d = z[1:] * np.conj(z[:-1])
        d /= np.maximum(np.abs(d), 1e-20)
        base = int(np.flatnonzero(valid)[0])
        coherence = float(abs(np.mean(d ** 4)))
        if coherence < MIN_COHERENCE:
            continue
        for kind in kinds:
            t = template(kind)
            length = len(t)
            # Symbol envelopes have imperfect edges. Allow +/- 12 symbols;
            # avoid searching an entire multi-second recording for a short word.
            middle = (len(d) - length) / 2
            lower, upper = max(0, int(middle - 12)), min(len(d) - length + 1, int(middle + 13))
            if upper <= lower:
                continue
            c_corr, c_pos, c_hz = coherent_word(z, kind, lower, upper)
            if c_pos is not None and (best_coh is None or c_corr > best_coh["correlation"]):
                c_tail = None
                if kind != "cub":
                    indices = base + c_pos + NUB_TAIL_OFFSETS
                    if indices.min() >= 0 and indices.max() + 1 < len(times):
                        tail = y[times[indices + 1]] * np.conj(y[times[indices]])
                        if np.all(np.abs(tail) > 1e-20):
                            ang = np.angle(tail * np.conj(NUB_TAIL) * np.exp(-2j * np.pi * c_hz / SYMBOL_RATE))
                            c_tail = int(np.count_nonzero(np.abs(ang) > np.pi / 4))
                best_coh = {"correlation": c_corr, "training": kind, "hz": c_hz, "tail_errors": c_tail,
                            "coherence": coherence, "z": z}
            segments = np.lib.stride_tricks.sliding_window_view(d, length)[lower:upper]
            residual = segments * np.conj(t)
            means = np.mean(residual, axis=1)
            for ix in np.argsort(np.abs(means))[-2:]:
                corr = float(abs(means[ix]))
                angles = np.abs(np.angle(residual[ix] * np.exp(-1j * np.angle(means[ix]))))
                errors = int(np.count_nonzero(angles > np.pi / 4))
                rule = matching_rule(kind, coherence, corr, errors, float(np.degrees(angles.max())))
                accepted = rule is not None
                tail_errors = None
                if kind != 'cub' and accepted:
                    nub_training_match = True
                    indices = base + lower + int(ix) + NUB_TAIL_OFFSETS
                    if indices.min() >= 0 and indices.max() + 1 < len(times):
                        tail = y[times[indices + 1]] * np.conj(y[times[indices]])
                        angles = np.angle(tail * np.conj(NUB_TAIL) * np.exp(-1j * np.angle(means[ix])))
                        tail_errors = int(np.count_nonzero(np.abs(angles) > np.pi / 4))
                        # No extra frequency/phase fit on the tails. Require the
                        # training word's rotation to also explain the tail bits.
                        accepted = tail_errors == 0 and bool(np.all(np.abs(tail) > 1e-20))
                    else:
                        accepted = False
                    if not accepted:
                        rejection = {'training': kind, 'correlation': round(corr, 4),
                                     'symbol_errors': errors, 'tail_symbol_errors': tail_errors,
                                     'tail_symbols_checked': 3 if tail_errors is not None else 0,
                                     'reason': 'tail mismatch or unusable tail amplitude'
                                               if tail_errors is not None else 'tail samples unavailable'}
                        if best_tail_rejection is None or corr > best_tail_rejection['correlation']:
                            best_tail_rejection = rejection
                rank = (accepted, corr * coherence)
                prior = (best["accepted"], best["correlation"] * best["modulation_coherence"])
                if rank > prior:
                    best_z = z
                    best = {"accepted": bool(accepted), "training": kind, "rule": rule if accepted else None,
                            "start_sample": start, "stop_sample": stop,
                            "residual_offset_hz": round(float(np.angle(means[ix])) * SYMBOL_RATE / (2 * np.pi), 1),
                            "tail_symbols_checked": 3 if tail_errors is not None else 0,
                            "tail_symbol_errors": tail_errors,
                            "correlation": round(corr, 4),
                            "modulation_coherence": round(coherence, 4),
                            "symbol_errors": errors, "duration_ms": round(duration * 1000, 3)}
    # The coherent rule only ever adds a match the standard and weak rules refused; it never changes
    # or removes theirs. Normal bursts must still fit their tail symbols, here with the frequency of
    # the coherent fit. Its score is kept on every inspected burst for later study.
    if best_coh is not None:
        if (not best["accepted"] and best_coh["correlation"] >= COHERENT_LIMITS[best_coh["training"]]
                and (best_coh["training"] == "cub" or best_coh["tail_errors"] == 0)):
            best_z = best_coh["z"]
            best = {"accepted": True, "training": best_coh["training"], "rule": "coherent",
                    "start_sample": start, "stop_sample": stop,
                    "residual_offset_hz": round(best_coh["hz"], 1),
                    "tail_symbols_checked": 0 if best_coh["tail_errors"] is None else 3,
                    "tail_symbol_errors": best_coh["tail_errors"],
                    "correlation": round(best_coh["correlation"], 4),
                    "modulation_coherence": round(best_coh["coherence"], 4),
                    "symbol_errors": None, "duration_ms": round(duration * 1000, 3)}
        best["coherent_correlation"] = round(best_coh["correlation"], 4)
        best["coherent_training"] = best_coh["training"]
        best["coherent_tail_errors"] = best_coh["tail_errors"]
    if best["accepted"] and best_z is not None:
        try:
            carrier = carrier_frequency(best_z)
        except (ValueError, FloatingPointError, ZeroDivisionError):
            carrier = None
        if carrier:
            best.update(carrier)
    best['nub_training_match'] = nub_training_match
    # Count one rejected envelope, not each timing/phase hypothesis. Retain
    # rejection evidence even if a different, unaccepted hypothesis ranked higher.
    best['nub_tail_rejected'] = bool(nub_training_match and not best['accepted'])
    best['nub_tail_rejection'] = best_tail_rejection if best['nub_tail_rejected'] else None
    return best


def analyse(bb, fs, starts, stops, max_bursts=12):
    """Inspect a bounded, time-distributed selection of individual bursts."""
    pairs = []
    for a, b in zip(starts, stops):
        if .022 < (b - a) / fs <= .062:
            slot = 255 / SYMBOL_RATE * fs
            for k in range(4):
                lo, hi = int(a + k * slot), min(int(a + (k + 1) * slot), int(b))
                if hi - lo >= .003 * fs:
                    pairs.append((lo, hi))
        else:
            pairs.append((a, b))
    if len(pairs) > max_bursts:
        indices = np.unique(np.linspace(0, len(pairs) - 1, max_bursts, dtype=int))
        pairs = [pairs[i] for i in indices]
    evidence = [inspect_burst(bb, fs, a, b) for a, b in pairs]
    matches = [e for e in evidence if e["accepted"]]
    return {"accepted": bool(matches), "matched_bursts": len(matches),
            "nub_training_matches": sum(bool(e.get('nub_training_match')) for e in evidence),
            "nub_tail_rejections": sum(bool(e.get('nub_tail_rejected')) for e in evidence),
            "tested_bursts": len(evidence), "bursts": evidence,
            "method": "rrc-differential-training-v2-nub-tail"}
