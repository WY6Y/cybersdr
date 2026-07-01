#!/usr/bin/env python3
"""
generate_icons.py — Generate CyberSDR PWA icons using Pillow.

Design:
  • Dark navy background (#080810) with rounded corners (safe-zone inset)
  • Spectrum analyser: ~20 vertical bars, cyan (#00f5ff) → deep blue gradient
  • Thin antenna/tower silhouette above the bars in cyan
  • "WY6Y" label in neon cyan below the bars
  • Outer cyan glow border

Run once to create icon-192.png and icon-512.png in this directory.
Requires: pip install Pillow
"""

import math
import os
import random

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
except ImportError:
    raise SystemExit("Pillow is required: pip install Pillow")

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))

# Fixed bar heights (0.0–1.0) for a nice spectrum shape — deterministic
_BAR_PROFILE = [
    0.25, 0.40, 0.55, 0.72, 0.85, 0.78, 0.92, 0.88, 0.65, 0.50,
    0.70, 0.95, 0.80, 0.60, 0.75, 0.45, 0.58, 0.38, 0.30, 0.20,
]

BG_COLOR   = (8, 8, 16)
CYAN       = (0, 245, 255)
DEEP_BLUE  = (0, 30, 80)
BORDER     = (0, 100, 130)


def _lerp_color(c1, c2, t):
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def make_icon(size: int, path: str) -> None:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Background rounded rect
    pad = size // 12
    r   = size // 6
    bg_box = [pad, pad, size - pad, size - pad]
    draw.rounded_rectangle(bg_box, radius=r, fill=(*BG_COLOR, 255))

    # Subtle cyan border glow
    draw.rounded_rectangle(bg_box, radius=r, outline=(*BORDER, 200), width=max(1, size // 96))

    inner_w  = bg_box[2] - bg_box[0]
    inner_h  = bg_box[3] - bg_box[1]
    n_bars   = len(_BAR_PROFILE)

    # Spectrum bars region: occupy the lower 55% of the inner box
    bar_region_top    = bg_box[1] + int(inner_h * 0.38)
    bar_region_bottom = bg_box[3] - int(inner_h * 0.14)
    bar_region_h      = bar_region_bottom - bar_region_top
    bar_region_left   = bg_box[0] + int(inner_w * 0.06)
    bar_region_right  = bg_box[2] - int(inner_w * 0.06)
    bar_region_w      = bar_region_right - bar_region_left

    bar_gap   = max(1, bar_region_w // (n_bars * 6))
    bar_w     = max(1, (bar_region_w - bar_gap * (n_bars - 1)) // n_bars)

    for i, h_frac in enumerate(_BAR_PROFILE):
        x0 = bar_region_left + i * (bar_w + bar_gap)
        bar_h = max(2, int(bar_region_h * h_frac))
        y0 = bar_region_bottom - bar_h
        y1 = bar_region_bottom
        # Gradient: cyan at top, deep blue at bottom
        for y in range(y0, y1):
            t = (y - y0) / max(1, y1 - y0)
            color = _lerp_color(CYAN, DEEP_BLUE, t)
            draw.rectangle([x0, y, x0 + bar_w - 1, y], fill=(*color, 230))

    # Antenna: thin tower above bars
    cx = (bg_box[0] + bg_box[2]) // 2
    ant_bottom = bar_region_top - int(inner_h * 0.02)
    ant_top    = bg_box[1] + int(inner_h * 0.04)
    ant_w      = max(1, size // 64)

    # Vertical mast
    draw.rectangle([cx - ant_w, ant_top + int((ant_bottom - ant_top) * 0.1),
                    cx + ant_w, ant_bottom], fill=(*CYAN, 180))

    # Horizontal cross-arms (3 pairs, narrowing toward top)
    for frac, arm_frac in [(0.25, 0.30), (0.55, 0.20), (0.80, 0.12)]:
        y_arm  = ant_top + int((ant_bottom - ant_top) * frac)
        arm_hw = int(inner_w * arm_frac)
        draw.rectangle([cx - arm_hw, y_arm - ant_w,
                        cx + arm_hw, y_arm + ant_w], fill=(*CYAN, 160))

    # "WY6Y" label
    label_y = bar_region_bottom + int(inner_h * 0.03)
    label_size = max(8, size // 14)

    try:
        from PIL import ImageFont
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", label_size)
    except (IOError, OSError):
        font = ImageFont.load_default()

    text = "WY6Y"
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
    except AttributeError:
        tw = len(text) * label_size // 2

    tx = cx - tw // 2
    draw.text((tx, label_y), text, font=font, fill=(*CYAN, 220))

    # Save
    img.save(path, "PNG")
    print(f"  Wrote {os.path.basename(path)} ({size}×{size})")


def main():
    print("[generate_icons] Creating CyberSDR PWA icons…")
    for size in (192, 512):
        out = os.path.join(OUTPUT_DIR, f"icon-{size}.png")
        make_icon(size, out)
    print("[generate_icons] Done.")


if __name__ == "__main__":
    main()
