"""Colour values of clip params: "#rrggbb", "face" or "random", plus the per-beat hue jump.

All colours are 24-bit (r, g, b) tuples of ints 0..255.
"""
from __future__ import annotations

import colorsys
import random
from typing import Any

from .base import RenderContext

RGB = tuple[int, int, int]
BLACK: RGB = (0, 0, 0)
WHITE: RGB = (255, 255, 255)


def parse_hex(value: Any, fallback: RGB = WHITE) -> RGB:
    """"#rrggbb" or "#rgb" -> (r, g, b); anything else -> fallback."""
    if isinstance(value, (tuple, list)) and len(value) == 3:
        return tuple(max(0, min(255, int(v))) for v in value)  # type: ignore[return-value]
    if not isinstance(value, str):
        return fallback
    s = value.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        return fallback
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return fallback


def to_hex(rgb: RGB) -> str:
    return "#%02x%02x%02x" % rgb


def hue_rotate(rgb: RGB, degrees: float) -> RGB:
    """Turn the hue, keeping saturation and value. Greys stay grey."""
    if not degrees:
        return rgb
    h, s, v = colorsys.rgb_to_hsv(rgb[0] / 255.0, rgb[1] / 255.0, rgb[2] / 255.0)
    r, g, b = colorsys.hsv_to_rgb((h + degrees / 360.0) % 1.0, s, v)
    return (round(r * 255), round(g * 255), round(b * 255))


def scale(rgb: RGB, k: float) -> RGB:
    return (max(0, min(255, round(rgb[0] * k))), max(0, min(255, round(rgb[1] * k))), max(0, min(255, round(rgb[2] * k))))


def random_color(seed: int, salt: str = "", avoid: RGB | None = None) -> RGB:
    """A bright saturated colour, the same for the same (seed, salt). Never black;
    if `avoid` is given the hue stays at least 60 degrees away from it."""
    rnd = random.Random(f"{seed}:{salt}")
    h = rnd.random()
    if avoid is not None and max(avoid) > 0:
        ah, as_, _ = colorsys.rgb_to_hsv(avoid[0] / 255.0, avoid[1] / 255.0, avoid[2] / 255.0)
        if as_ > 0.2:
            h = (ah + 1 / 6 + rnd.random() * (4 / 6)) % 1.0
    r, g, b = colorsys.hsv_to_rgb(h, 1.0, 1.0)
    return (round(r * 255), round(g * 255), round(b * 255))


def resolve(value: Any, ctx: RenderContext, salt: str = "", hue_per_beat: float = 0.0,
            avoid: RGB | None = None, fallback: RGB = WHITE) -> RGB:
    """A colour param's value -> the colour to draw now.

    value: "#rrggbb" | "face" (ctx.face_color) | "random" (from ctx.seed and salt; pass a
    different salt per param so two random colours of one clip differ).
    hue_per_beat: degrees the hue jumps on every whole beat since the clip started
    (the old sign's "hue jump").
    """
    if value == "face":
        rgb = ctx.face_color
    elif value == "random":
        rgb = random_color(ctx.seed, salt, avoid)
    else:
        rgb = parse_hex(value, fallback)
    if hue_per_beat:
        rgb = hue_rotate(rgb, hue_per_beat * int(max(0.0, ctx.clip_beat)))
    return rgb
