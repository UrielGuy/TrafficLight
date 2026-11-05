"""Turning an ink coverage mask and two colours into a frame (text and figures)."""
from __future__ import annotations

from typing import Any

import numpy as np

from .. import color
from ..base import ParamSpec, RenderContext

_RAMP = np.arange(256, dtype=np.float32)[:, None] / 255.0


def hue_param(key: str, label: str) -> ParamSpec:
    return ParamSpec(key, label, "int", 0, min=0, max=359, step=1,
                     help="Degrees the hue turns on every beat since the clip started (0 = steady).")


def colour_params(bg_default: str = "face", ink_default: str = "#000000") -> list[ParamSpec]:
    """Background and ink colours, their hue jumps, and the see-through background switch."""
    return [
        ParamSpec("bg", "Background", "color", bg_default),
        ParamSpec("ink", "Ink", "color", ink_default),
        hue_param("bg_hue", "Background hue jump per beat"),
        hue_param("ink_hue", "Ink hue jump per beat"),
        ParamSpec("transparent", "No background", "bool", False,
                  help="Only the ink is drawn; tracks below show through around it."),
    ]


def number(value: Any, default: float, lo: float | None = None, hi: float | None = None) -> float:
    """A numeric param, clamped; anything that is not a finite number gives the default."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def flag(value: Any) -> bool:
    """A bool param that may arrive as a bool, a number or a string."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value) if isinstance(value, (bool, int, float)) else False


def colours(p: dict[str, Any], ctx: RenderContext) -> tuple[color.RGB, color.RGB]:
    """(background, ink) for this frame. Ink the same as the background is never invisible:
    it turns black, or white on black (the old sign's rule)."""
    bg = color.resolve(p.get("bg"), ctx, "bg", number(p.get("bg_hue"), 0.0), fallback=ctx.face_color)
    ink = color.resolve(p.get("ink"), ctx, "ink", number(p.get("ink_hue"), 0.0), avoid=bg, fallback=color.BLACK)
    if ink == bg and not flag(p.get("transparent")):
        ink = color.WHITE if bg == color.BLACK else color.BLACK
    return bg, ink


def compose(alpha: np.ndarray, bg: color.RGB, ink: color.RGB, transparent: bool) -> np.ndarray:
    """alpha (uint8 coverage, h x w) -> a fresh RGB frame (ink over bg) or RGBA (ink, alpha = coverage)."""
    h, w = alpha.shape
    if transparent:
        out = np.empty((h, w, 4), np.uint8)
        out[..., :3] = ink
        out[..., 3] = alpha
        return out
    b = np.asarray(bg, np.float32)
    lut = np.rint(b + (np.asarray(ink, np.float32) - b) * _RAMP).astype(np.uint8)   # (256, 3)
    return lut[alpha]
