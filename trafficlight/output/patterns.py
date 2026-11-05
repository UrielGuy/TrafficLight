"""Test frames for bring-up (`make(name)`), drawn in logical sign coordinates.

"panels": each of the six panels in its own colour with its number (1 = top)
drawn large and upright, a white triangle in its top-left corner, a one-pixel
frame round its edge (missing edge rows or columns show a wrong scan setting)
and three swatches labelled R G B (colour order). How these appear on the
sign says which setting to change; see docs/HARDWARE.md.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .. import config

NAMES = ("panels", "white", "red", "green", "blue", "gradient")

# Panel colours, top to bottom: far apart in hue, dimmed so a whole sign of
# them stays well within the supply.
PANEL_COLORS = [(160, 0, 0), (150, 80, 0), (130, 130, 0), (0, 140, 0), (0, 90, 170), (120, 0, 150)]


def make(name: str) -> np.ndarray | None:
    """A (H, W, 3) uint8 test frame, or None for an unknown name."""
    if name not in NAMES:
        return None
    return _cached(name).copy()


@lru_cache(maxsize=None)
def _cached(name: str) -> np.ndarray:
    H, W = config.H, config.W
    f = np.zeros((H, W, 3), np.uint8)
    solid = {"white": (255, 255, 255), "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255)}
    if name in solid:
        f[:] = solid[name]
    elif name == "gradient":
        ramp = np.rint(np.linspace(0, 255, W)).astype(np.uint8)
        bands = [(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 1)]
        edges = np.linspace(0, H, len(bands) + 1).astype(int)
        for (a, b), mask in zip(zip(edges[:-1], edges[1:]), bands):
            f[a:b] = ramp[None, :, None] * np.array(mask, np.uint8)
    elif name == "panels":
        f = _panels()
    f.flags.writeable = False
    return f


def _font(size: int):
    return ImageFont.load_default(size=size)


def _panels() -> np.ndarray:
    W, PH = config.PANEL_W, config.PANEL_H
    img = Image.new("RGB", (config.W, config.H))
    d = ImageDraw.Draw(img)
    big = _font(int(PH * 0.9))
    small = _font(max(8, PH // 6))
    corner = PH // 3
    sw = PH // 5                                   # swatch size
    for i in range(config.PANELS):
        top = i * PH
        d.rectangle([0, top, W - 1, top + PH - 1], fill=PANEL_COLORS[i], outline=(255, 255, 255))
        d.polygon([(0, top), (corner, top), (0, top + corner)], fill=(255, 255, 255))
        label = str(i + 1)
        l, t, r, b = d.textbbox((0, 0), label, font=big, stroke_width=2)
        cx = W * 0.38
        d.text((cx - (l + r) / 2, top + PH / 2 - (t + b) / 2), label, font=big, fill=(255, 255, 255),
               stroke_width=2, stroke_fill=(0, 0, 0))
        x0 = W - 3 * sw - 6
        y0 = top + (PH - sw) // 2 - 4
        for k, (letter, col) in enumerate(zip("RGB", [(255, 0, 0), (0, 255, 0), (0, 0, 255)])):
            x = x0 + k * (sw + 1)
            d.rectangle([x, y0, x + sw - 1, y0 + sw - 1], fill=col, outline=(0, 0, 0))
            l, t, r, b = d.textbbox((0, 0), letter, font=small)
            d.text((x + sw / 2 - (l + r) / 2, y0 + sw + 2 - t), letter, font=small, fill=(255, 255, 255))
    return np.asarray(img, dtype=np.uint8).copy()
