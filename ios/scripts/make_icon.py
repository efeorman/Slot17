#!/usr/bin/env python3
"""Draws the app icon: a dark tile with signal arcs and three level bars. Needs Pillow."""
import sys
from PIL import Image, ImageDraw, ImageFilter

S = 1024
SCALE = 4  # drawn large and reduced, for smooth edges
N = S * SCALE


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def main(path):
    tile = Image.new("RGB", (N // 4, N // 4))
    px = tile.load()
    size = N // 4
    for y in range(size):
        for x in range(size):
            t = min(1.0, (x * 0.35 + y * 0.65) / size)
            px[x, y] = lerp((22, 30, 44), (6, 9, 14), t)
    tile = tile.resize((N, N), Image.BILINEAR)

    glow = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    cx, cy = int(N * 0.5), int(N * 0.60)
    arcs = [(0.17, (47, 210, 107)), (0.29, (245, 197, 66)), (0.41, (255, 77, 79))]
    for radius, colour in arcs:
        r = int(N * radius)
        draw.arc((cx - r, cy - r, cx + r, cy + r), 222, 318, fill=colour + (255,), width=int(N * 0.052))
    blurred = glow.filter(ImageFilter.GaussianBlur(N * 0.022))
    tile.paste(blurred, (0, 0), blurred)
    tile.paste(glow, (0, 0), glow)

    draw = ImageDraw.Draw(tile)
    r = int(N * 0.058)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(232, 237, 244))

    width, gap = int(N * 0.095), int(N * 0.05)
    base = int(N * 0.86)
    left = cx - width - gap - width // 2
    bars = [(0.075, (47, 210, 107)), (0.115, (245, 197, 66)), (0.155, (255, 77, 79))]
    for i, (height, colour) in enumerate(bars):
        x = left + i * (width + gap)
        draw.rounded_rectangle((x, base - int(N * height), x + width, base), radius=int(N * 0.02), fill=colour)

    # No alpha and square corners: iOS applies its own mask and rejects transparency.
    tile.resize((S, S), Image.LANCZOS).convert("RGB").save(path, optimize=True)


if __name__ == "__main__":
    main(sys.argv[1])
