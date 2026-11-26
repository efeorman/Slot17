#!/usr/bin/env python3
"""Two RTL-SDRs, each hearing half of 380 to 385 MHz, joined into the 6 MSPS stream the detector expects.

One RTL-SDR can't hear the whole band: it delivers 2.56 MS/s without losing samples (osmocom's highest tested
rate), about 2.5 MHz. Two do, tuned to 381.25 and 383.75 MHz. This module joins them:

  * In blocks of 2 ms, each stick's samples, with the 1 ms before them, go through an FFT (333 1/3 Hz bins),
    are moved to their place in the 6 MSPS band (1.25 MHz below and above 382.5 MHz: exactly 3750 bins, a whole
    number of turns per block, so the phase runs on) and weighted; an inverse FFT gives 6 MSPS, of which 0.5 ms
    at each end is discarded (overlap-save, centred), so block edges leave no trace. The weighting is zero-phase:
    what it does reaches both ways in time and wraps round both ends of a block, so both ends are discarded.
    Until 24 November 2026 the whole 1 ms was taken from the start, and the end of every block carried the
    wrap-round: an error of -10 dB of the signal in its last 16 samples, against -51 dB everywhere now
    (tests/test_rtlsdr.py, JoinAccuracy). The output is therefore 0.5 ms (DELAY_S) behind the sticks.
  * Stick A gives 380.0 to 382.5 MHz, stick B 382.5 to 385.0 MHz, handing over in 2 kHz at 382.5 MHz: on the
    boundary between channels 99 and 100 of the 380.0125 + n x 0.025 MHz raster. No channel is made of both
    sticks, so their clocks never need to agree to the sample; each channel comes from one of them.
  * Each stick's own DC spike (its centre, 381.25 and 383.75 MHz) also falls between two channels, 12.5 kHz from
    both; the one bin on it (333 Hz wide) is left out, which costs no channel anything.

Rates: 2.56 MS/s (the default, osmocom's highest without lost samples) or 2.88 MS/s (more room against
aliasing, but not every USB port keeps up). Not 2.4 MS/s: tested with two simulated sticks, six channels
fell outside, and bursts just beyond a stick's reach folded back into the band and were recognised on the
wrong channels (3 to 5 and 195 to 196). At 2.56 MS/s a stick reaches 30 kHz beyond its half, so channels 0,
99, 100 and 199 sit at the edge of a stick's own filters: how much the real sticks alias there has to be
measured with them.
"""
import numpy as np
from scipy import fft as sfft

OUT_FS = 6_000_000
OUT_CENTRE = 382.5e6                # the detector's centre; calibration uses 392.5 MHz
HALF_BAND = 2.5e6                   # the band joined: centre +- 2.5 MHz
STICK_OFFSET = 1.25e6               # stick A at centre - 1.25 MHz, stick B at centre + 1.25 MHz, handover at the centre
CENTRES = (OUT_CENTRE - STICK_OFFSET, OUT_CENTRE + STICK_OFFSET)
HANDOVER_HALF = 1_000.0             # raised-cosine half-width at the centre (382.5 MHz)
EDGE_HALF = 5_000.0                 # ... and just outside the band's two ends
HOP_S = 0.002                       # 2 ms of new samples per block: 12000 output samples kept
DISCARD_S = 0.001                   # 1 ms before them, transformed too; half of it discarded at each end
DELAY_S = DISCARD_S / 2             # how far the joined stream lags the sticks
RATES = (2_560_000, 2_880_000)
# An RTL-SDR's 8-bit unsigned I/Q, 0..255, to -1..+1: the value of each byte.
U8_TO_FLOAT = ((np.arange(256, dtype=np.float32) - 127.5) / 127.5)


def from_u8(raw):
    """An RTL-SDR's interleaved uint8 I/Q as complex64 in -1..+1, already interleaved as complex64 wants it.

    Arithmetic, not a look-up in U8_TO_FLOAT: the same float32 values, and on the Pi 5 about a fifth of the
    cost (10 against 56 ms of CPU per second of two sticks)."""
    f = np.asarray(raw, np.uint8).astype(np.float32)
    f -= np.float32(127.5)
    f /= np.float32(127.5)
    return f.view(np.complex64)


def _step(f, f0, half):
    """0 below f0 - half, 1 above f0 + half, a raised cosine between."""
    x = np.clip((f - (f0 - half)) / (2 * half), 0.0, 1.0)
    return 0.5 - 0.5 * np.cos(np.pi * x)


class Stitcher:
    """Joins equal-length blocks from stick A and stick B into 6 MSPS complex64, keeping its state between calls."""

    def __init__(self, fs_in=2_560_000, centre=OUT_CENTRE, gain=1.0):
        if fs_in not in RATES:
            raise ValueError(f"{fs_in} S/s: choose one of {RATES}")
        self.fs_in = fs_in
        self.centre = float(centre)
        self.centres = centres = (self.centre - STICK_OFFSET, self.centre + STICK_OFFSET)
        band = (self.centre - HALF_BAND, self.centre + HALF_BAND)
        self.n_in = int(round(fs_in * HOP_S))                 # new input samples per block
        self.l_in = self.n_in + int(round(fs_in * DISCARD_S))
        self.n_out = int(round(OUT_FS * HOP_S))                # 12000 output samples per block
        self.l_out = self.n_out + int(round(OUT_FS * DISCARD_S))
        self.df = fs_in / self.l_in                            # 333 1/3 Hz
        assert abs(self.df - OUT_FS / self.l_out) < 1e-9
        self.maps = []                                         # per stick: (input bins, output bins, weights)
        k = np.arange(self.l_in)
        rel = np.where(k < self.l_in // 2, k, k - self.l_in) * self.df        # Hz from the stick's centre
        for s, centre in enumerate(centres):
            shift = (centre - self.centre) / self.df
            if abs(shift - round(shift)) > 1e-9:
                raise ValueError("a stick's centre must be a whole number of bins from 382.5 MHz")
            absolute = centre + rel
            if s == 0:
                w = _step(absolute, band[0] - EDGE_HALF, EDGE_HALF) * (1 - _step(absolute, self.centre, HANDOVER_HALF))
            else:
                w = _step(absolute, self.centre, HANDOVER_HALF) * (1 - _step(absolute, band[1] + EDGE_HALF, EDGE_HALF))
            # Leave out the stick's own outermost bins (its filters are steepest there) and the bin on its DC spike.
            usable = (w > 0) & (np.abs(rel) < 0.49 * fs_in) & (np.abs(rel) > 0.5 * self.df)
            out_bins = (np.round(rel[usable] / self.df).astype(int) + int(round(shift))) % self.l_out
            self.maps.append((k[usable], out_bins, w[usable]))
        # The same mapping as runs of neighbouring bins: copied as slices, weighted only where the weight isn't 1
        # (the handover and the band's two ends). Fancy indexing cost a third of the time on the Pi.
        self.chunks = [_runs(*m) for m in self.maps]
        # The inverse FFT's length and the caller's gain, applied once at the end.
        self.scale = np.float32(self.l_out / self.l_in * gain)
        # A whole number of turns per block for each stick's shift, or the phase would jump at every block.
        for centre in centres:
            turns = (centre - self.centre) / self.df * self.n_out / self.l_out
            if abs(turns - round(turns)) > 1e-9:
                raise ValueError("a stick's shift must turn a whole number of times per block")
        self.prev = [np.zeros(self.l_in - self.n_in, np.complex64) for _ in centres]

    def coverage(self, channels=200, first_hz=None, step_hz=25_000.0, half_hz=11_000.0):
        """Per channel, the smallest weight the two sticks give across its central +-11 kHz (1.0: all of it)."""
        first_hz = self.centre - HALF_BAND + 12_500.0 if first_hz is None else first_hz
        got = np.zeros(self.l_out)
        for _, out_bins, w in self.maps:
            np.add.at(got, out_bins, np.abs(w))
        rel = np.where(np.arange(self.l_out) < self.l_out // 2, np.arange(self.l_out), np.arange(self.l_out) - self.l_out)
        freq = self.centre + rel * self.df
        result = []
        for n in range(channels):
            c = first_hz + n * step_hz
            sel = np.abs(freq - c) <= half_hz
            result.append(float(got[sel].min()) if sel.any() else 0.0)
        return np.array(result)

    def process(self, a, b):
        """a, b: complex64, the same whole number of blocks each (n_in samples per block). Returns 6 MSPS complex64."""
        a = np.asarray(a, np.complex64)
        b = np.asarray(b, np.complex64)
        if len(a) != len(b) or len(a) % self.n_in:
            raise ValueError(f"both sticks need the same whole number of {self.n_in}-sample blocks")
        hops = len(a) // self.n_in
        if hops == 0:
            return np.zeros(0, np.complex64)
        spectrum = np.zeros((hops, self.l_out), np.complex64)
        for s, x in enumerate((a, b)):
            joined = np.concatenate((self.prev[s], x))
            self.prev[s] = joined[-(self.l_in - self.n_in):].copy()
            segments = np.lib.stride_tricks.sliding_window_view(joined, self.l_in)[::self.n_in][:hops]
            bins = sfft.fft(segments, axis=1)
            for i0, i1, o0, o1, w in self.chunks[s]:
                src = bins[:, i0:i1] if w is None else bins[:, i0:i1] * w
                if s == 0:
                    spectrum[:, o0:o1] = src          # stick A's bins are all its own
                else:
                    spectrum[:, o0:o1] += src         # stick B meets A only in the handover
        keep = (self.l_out - self.n_out) // 2             # the middle: both ends carry the wrap-round
        out = np.ascontiguousarray(sfft.ifft(spectrum, axis=1)[:, keep:keep + self.n_out], np.complex64)
        out *= self.scale
        return out.reshape(-1)


def _runs(in_bins, out_bins, w):
    """(i0, i1, o0, o1, weights or None) for each run of neighbouring bins with the same kind of weight."""
    runs, i, n = [], 0, len(in_bins)
    while i < n:
        unit = w[i] == 1.0
        j = i + 1
        while (j < n and in_bins[j] == in_bins[j - 1] + 1 and out_bins[j] == out_bins[j - 1] + 1
               and (w[j] == 1.0) == unit):
            j += 1
        runs.append((int(in_bins[i]), int(in_bins[j - 1]) + 1, int(out_bins[i]), int(out_bins[j - 1]) + 1,
                     None if unit else w[i:j].astype(np.complex64)))
        i = j
    return runs
