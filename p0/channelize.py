#!/usr/bin/env python3
"""Split a wideband capture into the TETRA 25 kHz channel grid.

The detector works per channel, not on the whole 4.8 MHz at once, because a
burst only occupies one 25 kHz channel and looking channel-by-channel is what
makes a weak burst stand out against its own local noise floor.

Two details make this exact rather than approximate:

  * **The FFT grid divides 25 kHz exactly.** At 6 MSPS there are 240 channels
    across the band; the FFT runs 8x finer (1920 bins) and sums 8 bins per
    channel. One bin per channel would be simpler but makes a poor filter - a
    Hann main lobe spans 4 bins, so at 25 kHz per bin the channel response is
    ~100 kHz wide and a burst shows up almost as brightly in its neighbours.
    Oversampling cut measured adjacent-channel leakage from 3.9 dB to 14 dB
    below the wanted channel.
  * **The signal is shifted so the TETRA raster lands on bin centres.** TETRA
    channels sit at base + n x 25 kHz with base = 380.0125 / 390.0125 MHz, which
    is offset half a bin from any multiple of 25 kHz. Without the shift every
    carrier would straddle two bins and lose ~3 dB.

Output is a power matrix [channel, time] with ~0.64 ms time resolution (the
0.5 ms request rounds up to a whole number of FFT frames: 2 x 1920 / 6 MSPS) -
fine
enough to resolve a 14.17 ms burst, coarse enough that 40 s of capture stays a
few hundred MB rather than tens of GB.

    python channelize.py --selftest
    python channelize.py <capture.iq>
"""
import argparse
import os
import sys

import numpy as np
from scipy import fft as sfft

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze import load, load_calibration

CHANNEL_HZ = 25_000.0
BASE_DL_HZ = 390.0125e6
BASE_UL_HZ = 380.0125e6
DEFAULT_FRAME_MS = 0.5
OVERSAMPLE = 8          # FFT bins per 25 kHz channel, summed back together

# The 200 channels of a TETRA band (0-199). Until 1.2.6 only +-2.4 MHz of the 6 MSPS band was used,
# leaving channels 0-2 and 196-199 out. Measured on this receiver (17 November 2026): its own noise
# floor stays within about 2 dB out to +-2.45 MHz and falls 1 to 2.7 dB by +-2.5 MHz, and in the car
# those channels sat 0.2 to 3.4 dB below the band's middle, like the edge channels already used.
# (A review gives the Airspy Mini 5 MHz alias-free at 6 MSPS; the maker's own page states no figure.)
# Channels outside EDGE_LOW..EDGE_HIGH are examined but flagged `band_edge` in their events.
CHANNELS = 200
USABLE_HZ = 5.0e6
EDGE_LOW, EDGE_HIGH = 3, 195


def raster_base(centre_hz):
    return BASE_DL_HZ if centre_hz > 387e6 else BASE_UL_HZ


def alignment_shift_hz(centre_hz):
    """Frequency shift that puts TETRA channel centres on FFT bin centres."""
    off = raster_base(centre_hz) - centre_hz
    s = (-off) % CHANNEL_HZ
    return s - CHANNEL_HZ if s > CHANNEL_HZ / 2 else s


class Channelised:
    """Power per TETRA channel over time, plus the frequency bookkeeping."""

    def __init__(self, power, channel_hz, tetra_ch, frame_s, centre_hz, fs,
                 occupancy_hz=None, cal_ppm=0.0):
        self.power = power              # float32 [n_channels, n_frames]
        self.channel_hz = channel_hz    # true centre frequency of each row
        self.tetra_ch = tetra_ch        # TETRA channel number of each row
        self.frame_s = frame_s          # seconds per column
        self.centre_hz = centre_hz
        self.fs = fs
        # Occupied bandwidth per channel, measured across each channel's own
        # busiest frames from the oversampled fine bins. A real TETRA carrier
        # fills most of the 25 kHz; a pure tone or a narrow spur fills one bin.
        self.occupancy_hz = occupancy_hz
        # Measured receiver frequency error, in ppm. `channel_hz` is the TRUE RF
        # (exactly on the TETRA raster); add `cal_offset_hz(f)` to get where that
        # carrier actually sits on this receiver's own uncorrected axis, which is
        # what anything mixing against the raw IQ has to tune to.
        self.cal_ppm = float(cal_ppm)

    def cal_offset_hz(self, f_hz):
        return float(f_hz) * self.cal_ppm * 1e-6

    @property
    def duration_s(self):
        return self.power.shape[1] * self.frame_s

    def row_for(self, tetra_channel):
        hits = np.flatnonzero(self.tetra_ch == tetra_channel)
        return int(hits[0]) if hits.size else None

    def noise_floor(self):
        """Per-channel noise floor: the 25th percentile over time.

        A percentile rather than a mean, because bursts must not drag the floor
        up. The 25th is low enough to ignore a channel busy a quarter of the
        time, and high enough to stay stable on a quiet channel.
        """
        return np.percentile(self.power, 25, axis=1)


def channelise(iq, fs, centre_hz, frame_ms=DEFAULT_FRAME_MS, apply_cal=True,
               usable_hz=USABLE_HZ, oversample=OVERSAMPLE):
    """Channelise with `oversample` FFT bins per 25 kHz channel, summed.

    One bin per channel is tempting but makes a poor filter: a Hann window's
    main lobe spans 4 bins, so at 25 kHz per bin the effective channel response
    is ~100 kHz and a strong burst shows up almost as brightly in its
    neighbours. Using finer bins and summing `oversample` of them per channel
    narrows the response to roughly the channel width.
    """
    base_bins = int(round(fs / CHANNEL_HZ))
    if abs(base_bins * CHANNEL_HZ - fs) > 1.0:
        raise ValueError(f"{fs/1e6:g} MSPS is not an integer multiple of 25 kHz; "
                         f"channelisation would straddle bins")
    n_fft = base_bins * oversample

    cal_ppm = load_calibration() if apply_cal else 0.0

    # The raster alignment is done by ROLLING BINS, not by rotating samples.
    # 12.5 kHz is exactly `oversample/2` fine bins, so the shift is an integer
    # number of bins and a roll is equivalent - and it removes a complex
    # exponential evaluated over every sample, which profiling showed was 1.3 s
    # of a 1.65 s run while the FFT itself was only 0.09 s.
    fine_hz = fs / n_fft
    align_bins = int(round(alignment_shift_hz(centre_hz) / fine_hz))
    if abs(align_bins * fine_hz - alignment_shift_hz(centre_hz)) > 1.0:
        raise ValueError("raster alignment is not an integer number of FFT bins; "
                         "increase `oversample`")
    shift = align_bins * fine_hz

    # Correct the receiver's measured offset before integrating channel power.
    # One short oscillator is reused per FFT frame; inter-frame phase resets
    # do not affect power. Reported frequencies stay on the physical raster.
    # Raw-IQ confirmation still tunes to raster + frequency-specific offset.
    # A real copy is taken on purpose. `x *= win` and the overwrite_x FFT below
    # both write in place; when `iq` is already complex64 and contiguous, an
    # ascontiguousarray would return a *view* and those in-place writes would
    # clobber the caller's samples with FFT output. detect() never noticed (it
    # uses the returned power), but signal_health() and the confirmation stage in
    # watch.py reuse the same `iq` afterwards.
    n_frames_in = len(iq) // n_fft
    x = np.array(iq[:n_frames_in * n_fft], dtype=np.complex64).reshape(
        n_frames_in, n_fft)
    win = np.hanning(n_fft).astype(np.float32)
    x *= win
    # Correct the receiver offset in each FFT frame. Resetting phase at a frame
    # boundary changes only its constant phase, which power discards. This costs
    # one short oscillator, not an exponential over millions of samples.
    if cal_ppm:
        offset = centre_hz * cal_ppm * 1e-6
        oscillator = np.exp(-2j * np.pi * offset * np.arange(n_fft) / fs).astype(np.complex64)
        x *= oscillator
    # scipy's pocketfft with workers=-1 spreads the transforms across cores and
    # keeps complex64 in complex64 - numpy.fft would promote to complex128 and
    # do twice the memory traffic for no extra precision we can use. This is
    # what makes the pipeline fit comfortably on a 4-core Raspberry Pi.
    spec = sfft.fft(x, axis=1, workers=-1, overwrite_x=True)
    p = (spec.real ** 2 + spec.imag ** 2)

    # Integrate to the requested time resolution.
    group = max(1, int(round(frame_ms * 1e-3 * fs / n_fft)))
    usable = (n_frames_in // group) * group
    p = p[:usable].reshape(-1, group, n_fft).mean(axis=1)     # [frames, bins]

    # Sum `oversample` neighbouring bins into each 25 kHz channel. Rolling the
    # spectrum by half a group first centres each channel on its bin block.
    p = np.roll(p, oversample // 2 + align_bins, axis=1)
    pf = p.reshape(p.shape[0], base_bins, oversample)         # [frames, ch, fine]
    pchan = pf.sum(axis=2)                                    # [frames, channels]
    occ = _occupancy_hz(pf, pchan, oversample)               # [channels]
    p = np.ascontiguousarray(pchan.T)                        # [channels, frames]

    bins = np.fft.fftfreq(base_bins, 1 / fs)
    true_hz = centre_hz + bins - shift
    ch_num = np.round((true_hz - raster_base(centre_hz)) / CHANNEL_HZ)

    # Only real channel numbers: at the full 5 MHz the grid also reaches one raster step past the band.
    keep = (np.abs(bins) <= usable_hz / 2) & (ch_num >= 0) & (ch_num < CHANNELS)
    order = np.argsort(true_hz[keep])
    return Channelised(p[keep][order], true_hz[keep][order],
                       ch_num[keep][order].astype(int),
                       group * n_fft / fs, centre_hz, fs,
                       occupancy_hz=occ[keep][order],
                       cal_ppm=cal_ppm)


def _occupancy_hz(pf, pchan, oversample):
    """Occupied bandwidth per channel, from its own busiest frames.

    For each channel, average the `oversample` fine-bin spectrum over the frames
    where that channel is most active (its top ~10 %), subtract the per-channel
    noise pedestal, and count how many fine bins sit above half the peak. A real
    TETRA carrier lights ~6-8 of 8 bins (~18-25 kHz); a tone lights one (~3 kHz).

    Noise is safe here: where there is no real burst the energy is spread across
    all bins, so the estimate reads *wide*, never falsely narrow - a weak carrier
    is therefore never rejected by mistake, only a genuinely narrow signal is.
    """
    n_frames, n_ch = pchan.shape
    fine_hz = CHANNEL_HZ / oversample
    if n_frames < 4:
        return np.full(n_ch, oversample * fine_hz, np.float32)   # "wide": no gate
    thr = np.percentile(pchan, 90, axis=0)                       # [ch]
    w = (pchan > thr[None, :]).astype(np.float32)                # busiest frames
    den = np.maximum(w.sum(axis=0), 1.0)                         # [ch]
    mean_fine = np.einsum("fc,fco->co", w, pf) / den[:, None]    # [ch, fine]
    ped = mean_fine.min(axis=1, keepdims=True)
    sig = np.clip(mean_fine - ped, 0.0, None)
    peak = np.maximum(sig.max(axis=1, keepdims=True), 1e-30)
    occupied = (sig > 0.5 * peak).sum(axis=1)                    # bins above -6 dB
    return (occupied * fine_hz).astype(np.float32)


# --------------------------------------------------------------------------- #

def selftest():
    import tetra_gen

    fs = 6_000_000.0
    centre = 382.5e6
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}{'  ' + detail if detail else ''}")

    print("channelize self-test")

    # --- raster alignment ----------------------------------------------------
    s = alignment_shift_hz(centre)
    ch_off = (BASE_UL_HZ - centre + s) % CHANNEL_HZ
    check("TETRA raster lands on bin centres",
          min(ch_off, CHANNEL_HZ - ch_off) < 1.0,
          f"shift {s:+.0f} Hz, residual {min(ch_off, CHANNEL_HZ-ch_off):.1f} Hz")

    # --- a synthetic burst lands in the right channel ------------------------
    rng = np.random.default_rng(0)
    n = int(fs * 0.4)
    noise = ((rng.normal(size=n) + 1j * rng.normal(size=n)) / np.sqrt(2)).astype(
        np.complex64)

    target_ch = 100
    target_hz = BASE_UL_HZ + target_ch * CHANNEL_HZ
    y = tetra_gen.inject(noise.copy(), fs, offset_hz=target_hz - centre,
                         snr_db=20.0, seed=7)

    c = channelise(y, fs, centre, apply_cal=False)
    row = c.row_for(target_ch)
    check("target channel exists in the grid", row is not None,
          f"channel {target_ch} at {target_hz/1e6:.4f} MHz")

    if row is not None:
        floor = c.noise_floor()
        excess = 10 * np.log10(c.power.max(axis=1) / floor)
        best = int(np.argmax(excess))
        check("burst appears in the injected channel", best == row,
              f"strongest row is channel {c.tetra_ch[best]}, "
              f"expected {target_ch}")
        check("burst stands well above the floor", excess[row] > 10,
              f"{excess[row]:.1f} dB above floor")

        neighbours = [r for r in (row - 1, row + 1) if 0 <= r < len(excess)]
        leak = max(excess[r] for r in neighbours)
        check("adjacent-channel leakage is bounded",
              leak < excess[row] - 6,
              f"neighbour {leak:.1f} dB vs target {excess[row]:.1f} dB")

    # --- an empty capture produces no strong channel -------------------------
    c2 = channelise(noise, fs, centre, apply_cal=False)
    excess2 = 10 * np.log10(c2.power.max(axis=1) / c2.noise_floor())
    check("pure noise stays near its floor", excess2.max() < 12,
          f"worst channel {excess2.max():.1f} dB above floor")

    # --- timing bookkeeping --------------------------------------------------
    # 0.5 ms is the *request*; the achievable value is a whole number of FFT
    # frames, so at 6 MSPS with n_fft=1920 it rounds to 2 frames = 0.64 ms.
    # Assert what the code really produces - the old +/-0.15 ms tolerance was
    # wide enough to pass while hiding a 28 % discrepancy from the docs.
    want_ms = round(DEFAULT_FRAME_MS * 1e-3 * fs / (240 * OVERSAMPLE))         * (240 * OVERSAMPLE) / fs * 1e3
    check("frame duration is the achievable quantum",
          abs(c.frame_s * 1e3 - want_ms) < 0.01,
          f"{c.frame_s*1e3:.3f} ms (requested {DEFAULT_FRAME_MS} ms)")
    check("duration preserved", abs(c.duration_s - 0.4) < 0.02,
          f"{c.duration_s:.3f} s of 0.4 s")
    check("channel count sensible", 180 <= c.power.shape[0] <= 200,
          f"{c.power.shape[0]} channels over {USABLE_HZ / 1e6:.1f} MHz")

    # Every reported channel frequency must be EXACTLY a TETRA raster point.
    # This is what the calibration sign bug broke: it put all of them 3.7 kHz
    # off, which a live run caught but no offline test did.
    cc = channelise(noise, fs, centre, apply_cal=True)
    resid = np.abs((cc.channel_hz - BASE_UL_HZ) % CHANNEL_HZ)
    resid = np.minimum(resid, CHANNEL_HZ - resid)
    check("reported channel frequencies are exactly on the raster (cal applied)",
          float(resid.max()) < 1.0, f"worst residual {resid.max():.1f} Hz")
    check("channel number matches the reported frequency",
          bool(np.all(cc.tetra_ch
                      == np.round((cc.channel_hz - BASE_UL_HZ) / CHANNEL_HZ))),
          "consistent")

    print("\n" + ("ALL PASS" if ok else "FAILURES ABOVE"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--frame-ms", type=float, default=DEFAULT_FRAME_MS)
    args = ap.parse_args()

    if args.selftest:
        raise SystemExit(selftest())
    if not args.file:
        ap.print_help()
        return

    iq, meta = load(args.file)
    c = channelise(iq, float(meta["sample_rate_hz"]), float(meta["centre_hz"]),
                   frame_ms=args.frame_ms)
    print(f"{c.power.shape[0]} channels x {c.power.shape[1]} frames "
          f"({c.frame_s*1e3:.2f} ms each, {c.duration_s:.1f} s)")

    # Two different questions need two different metrics, and conflating them
    # makes a working channelizer look broken:
    #
    #   CONTINUOUS  a base-station carrier transmits all the time, so it lifts
    #               its own time-percentile floor and shows almost no excess
    #               over it. Compare it with the *band* floor instead.
    #   BURSTY      a handheld sends ~14 ms per 56.67 ms frame, so it stands far
    #               above its own quiet-time floor. This is what the detector
    #               hunts for on the uplink.
    mean_p = c.power.mean(axis=1)
    continuous = 10 * np.log10(mean_p / np.percentile(mean_p, 25))
    bursty = 10 * np.log10(c.power.max(axis=1) / c.noise_floor())

    print("")
    print("CONTINUOUS carriers (mean power above the band floor):")
    print(f"  {'TETRA ch':>9}  {'MHz':>11}  {'dB':>7}")
    for r in np.argsort(-continuous)[:10]:
        print(f"  {c.tetra_ch[r]:9d}  {c.channel_hz[r]/1e6:11.4f}  {continuous[r]:7.1f}")

    print("")
    print("BURSTY channels (peak above that channel's own quiet floor):")
    print(f"  {'TETRA ch':>9}  {'MHz':>11}  {'dB':>7}")
    for r in np.argsort(-bursty)[:10]:
        print(f"  {c.tetra_ch[r]:9d}  {c.channel_hz[r]/1e6:11.4f}  {bursty[r]:7.1f}")


if __name__ == "__main__":
    main()
