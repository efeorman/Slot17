#!/usr/bin/env python3
"""The app's sounds, rendered to WAV files so they can be heard before they are built in.

The same notes, timings and envelopes are in Slot17/Services/AudioAlerts.swift; keep them equal.

    sounds.py OUTPUT_DIR
"""
import math
from pathlib import Path
import struct
import sys
import wave

RATE = 44100

# Soft bell: a sine with a few quiet overtones that fade faster than the note, so it rings
# instead of buzzing. (multiple of the note, loudness, how fast it fades in seconds)
BELL = [(1.0, 1.0, 0.45), (2.0, 0.28, 0.22), (3.0, 0.10, 0.14), (4.07, 0.05, 0.09)]

SOUNDS = {
    # name: (notes as (Hz, start s), total s, peak level 0..1, fade s)
    "chime": ([(659.25, 0.0), (987.77, 0.18)], 1.5, 0.55, 0.45),             # E5 then B5
    "urgent": ([(880.0, 0.0), (1108.73, 0.14), (1318.51, 0.28)], 1.5, 0.70, 0.40),  # A5 C#6 E6
    "warning": ([(523.25, 0.0), (392.0, 0.32)], 1.6, 0.50, 0.55),            # C5 down to G4
    "linkLost": ([(659.25, 0.0), (523.25, 0.26), (392.0, 0.52)], 1.9, 0.50, 0.55),
}


def render(notes, seconds, peak, fade):
    frames = int(seconds * RATE)
    out = [0.0] * frames
    for frequency, start in notes:
        begin = int(start * RATE)
        for i in range(frames - begin):
            t = i / RATE
            attack = min(1.0, t / 0.008)          # 8 ms: soft, no click
            value = 0.0
            for multiple, loudness, partial_fade in BELL:
                value += loudness * math.exp(-t / (partial_fade * fade / 0.45)) * math.sin(2 * math.pi * frequency * multiple * t)
            out[begin + i] += attack * value
    top = max(abs(x) for x in out) or 1.0
    # Ten milliseconds of fade at the very end, so a cut tail cannot click.
    for i in range(min(441, frames)):
        out[frames - 1 - i] *= i / 441
    return [x / top * peak for x in out]


def write(path, samples):
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(RATE)
        fh.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, x)) * 32767)) for x in samples))


def old_urgent():
    """The beep the operator found harsh: three 2000 Hz square pulses."""
    frames = int(0.52 * RATE)
    out = [0.0] * frames
    for start in (0.0, 0.17, 0.34):
        for i in range(int(0.135 * RATE)):
            t = i / RATE
            level = t / 0.005 if t < 0.005 else (1.0 if t < 0.12 else (0.135 - t) / 0.015)
            out[int(start * RATE) + i] = 0.7 * level * (1 if (t * 2000) % 1 < 0.5 else -1)
    return out


if __name__ == "__main__":
    folder = Path(sys.argv[1])
    folder.mkdir(parents=True, exist_ok=True)
    names = {"urgent": "1 new - strong signal (was the hard beep)", "chime": "2 new - red signal (chime)",
             "warning": "3 new - receiver hears nothing", "linkLost": "4 new - lost the car"}
    for key, (notes, seconds, peak, fade) in SOUNDS.items():
        samples = render(notes, seconds, peak, fade)
        write(folder / f"{names[key]}.wav", samples)
        print(f"{names[key]}: {seconds} s, peak {20 * math.log10(max(abs(x) for x in samples)):.1f} dBFS")
    write(folder / "9 old - the hard beep, for comparison.wav", old_urgent())
