#!/usr/bin/env python3
"""Validate the detector against a REAL TETRA recording, not a synthetic one.

The recording (sigidwiki TETRA_IQ, 48 kHz complex baseband, one continuous
downlink channel) carries genuine TETRA modulation. Uplink and downlink use the
identical waveform - pi/4-DQPSK, 18000 sym/s, 25 kHz - so the only thing that
makes an uplink burst is the on/off gating. We therefore:

  1. upsample the real channel 48 kHz -> 6 MSPS (x125, exact), preserving its
     24 kHz occupied bandwidth;
  2. gate it into 14.17 ms bursts once per 56.67 ms frame - real modulation,
     real TETRA framing timing;
  3. drop it into our REAL recorded uplink noise at a chosen channel and SNR;
  4. run the real detector.

If the detector fires on this, it fires on real TETRA modulation - the one thing
tetra_gen could not prove.
"""
import os, sys, wave
import numpy as np
from scipy.signal import resample_poly

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
from channelize import channelise, CHANNEL_HZ
from detect import detect, load_downlink_channels, FRAME_S, SLOT_S
from tetra_gen import snr_bandwidth_hz

WAV_NAME = "TETRA IQ.wav"


def _find_real_wav():
    """Locate the real TETRA recording, portably across bench and Pi.

    Order: $SLOT17_TETRA_WAV, then the project's data/ dir (where it is deployed),
    then the original bench scratchpad location as a legacy fallback. Returns the
    first that exists, else the project path so the error message is the useful one.
    """
    candidates = []
    if os.environ.get("SLOT17_TETRA_WAV"):
        candidates.append(os.environ["SLOT17_TETRA_WAV"])
    candidates.append(os.path.join(paths.ROOT, "data", WAV_NAME))
    for c in candidates:
        if os.path.isfile(c):
            return c
    return candidates[-1]


REAL_WAV = _find_real_wav()


def load_real_tetra(fs_out, max_seconds=None):
    """Load the recording and upsample to `fs_out`.

    `max_seconds` trims the raw recording *before* upsampling. This matters on the
    Pi: the full ~42 s recording upsampled to 6 MSPS is ~250M complex samples
    (~2 GB), which OOM-kills an 8 GB board; trimming first keeps memory bounded.
    """
    if not os.path.isfile(REAL_WAV):
        raise FileNotFoundError(
            f"real TETRA recording not found: {REAL_WAV}\n"
            f"  put '{WAV_NAME}' in {os.path.join(paths.ROOT, 'data')} "
            f"or set SLOT17_TETRA_WAV to its path.")
    w = wave.open(REAL_WAV, "rb")
    fs_in, n = w.getframerate(), w.getnframes()
    if max_seconds:
        n = min(n, int(max_seconds * fs_in))
    a = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32).reshape(-1, 2)
    iq = a[:, 0] + 1j * a[:, 1]
    iq -= iq.mean()
    iq /= np.sqrt(np.mean(np.abs(iq) ** 2))
    g = np.gcd(int(fs_out), int(fs_in))
    return resample_poly(iq, int(fs_out) // g, int(fs_in) // g).astype(np.complex64)


def gate_to_bursts(iq, fs, burst_s=SLOT_S, frame_s=FRAME_S):
    out = np.zeros_like(iq)
    bn = int(burst_s * fs)
    fn = int(frame_s * fs)
    ramp = int(0.0003 * fs)
    w = np.ones(bn)
    w[:ramp] = np.linspace(0, 1, ramp); w[-ramp:] = np.linspace(1, 0, ramp)
    for start in range(0, len(iq) - bn, fn):
        out[start:start + bn] = iq[start:start + bn] * w
    return out


def real_noise(fs, want_s):
    import glob
    for pat in ("*uplink-baseline*.iq", "*verify-382.5*.iq"):
        fs_files = sorted(glob.glob(os.path.join(paths.IQ_DIR, pat)))
        if fs_files:
            raw = np.fromfile(fs_files[-1], dtype=np.int16, count=int(want_s * fs) * 2)
            return (raw[0::2].astype(np.float32) + 1j * raw[1::2].astype(np.float32)
                    ).astype(np.complex64), os.path.basename(fs_files[-1])
    rng = np.random.default_rng(0)
    n = int(want_s * fs)
    return ((rng.normal(size=n) + 1j*rng.normal(size=n))/np.sqrt(2)).astype(np.complex64), "synthetic"


def main():
    fs = 6_000_000.0
    centre = 382.5e6
    target_ch = 100
    target_hz = 380.0125e6 + target_ch * CHANNEL_HZ

    real = load_real_tetra(fs)
    print(f"real TETRA upsampled to 6 MSPS: {len(real)/fs:.1f} s")

    noise, src = real_noise(fs, min(len(real)/fs, 20.0))
    real = real[:len(noise)]
    print(f"noise bed: {src} ({len(noise)/fs:.1f} s)\n")

    bursts = gate_to_bursts(real, fs)
    t = np.arange(len(bursts)) / fs
    shift = np.exp(2j * np.pi * (target_hz - centre) * t).astype(np.complex64)

    noise_p = float(np.mean(np.abs(noise) ** 2))
    burst_p = float(np.mean(np.abs(bursts[np.abs(bursts) > 0]) ** 2))
    dl = load_downlink_channels()

    print(f"{'SNR dB':>7}  {'detected':>8}  {'burst ms':>8}  {'period ms':>9}  "
          f"{'perio':>5}  {'paired':>6}")
    detected_at = []
    for snr in (4, 6, 8, 10, 14, 20):
        scale = np.sqrt(noise_p * 10**(snr/10.0) * (snr_bandwidth_hz()/fs) / burst_p)
        y = noise + bursts * shift * scale
        hits = [h for h in detect(channelise(y, fs, centre, apply_cal=False),
                                  downlink_channels=dl)
                if h["tetra_channel"] == target_ch]
        if hits:
            detected_at.append(snr)
            h = hits[0]
            print(f"{snr:7d}  {'YES':>8}  {h['median_burst_ms']:8.1f}  "
                  f"{str(h['measured_period_ms']):>9}  {h['periodicity']:5.2f}  "
                  f"{str(h['downlink_paired']):>6}")
        else:
            print(f"{snr:7d}  {'no':>8}")

    print()
    if detected_at:
        print(f"RESULT: detector fires on REAL TETRA modulation down to "
              f"{min(detected_at)} dB SNR.")
    else:
        print("RESULT: detector did NOT fire on real TETRA - the model is wrong "
              "somewhere. Investigate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
