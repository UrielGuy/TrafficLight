"""Shared helpers of the beat effects: geometry grids, anti-aliased painting, colours, beats.

Everything is drawn in float32 and converted once at the end. Shapes are described by a
signed distance in pixels (negative inside) and painted with `cov(sd)`, a one pixel wide
linear ramp, which is what makes the edges smooth.

Sizes of the old sign are kept as proportions: it measured in "doubled pixels" on a disc of
radius 128, so `U` converts one of those to pixels on today's disc.
"""
from __future__ import annotations

import math
from functools import lru_cache
from typing import Optional

import numpy as np

from ... import config
from .. import color as colors
from ..base import ClipRenderer, ParamSpec, RenderContext

R = float(config.FACE_RADIUS)
U = R / 128.0
BLACK = colors.BLACK
WHITE = colors.WHITE


# ---------------------------------------------------------------- geometry
class Grid:
    """Pixel-centre coordinates of a w x h box, relative to the centre of the box (x right, y down).

    `y` is relative to the centre of the light the pixel belongs to (a 128x384 box is three
    stacked lights), `yb` to the centre of the whole box. `d` = distance from the light's centre.
    """
    __slots__ = ("w", "h", "tiles", "pitch", "x", "y", "yb", "d", "ys")

    def __init__(self, w: int, h: int):
        self.w, self.h = w, h
        self.tiles = max(1, int(round(h / w)))
        self.pitch = h / self.tiles
        yc = np.arange(h, dtype=np.float64) + 0.5
        self.x = (np.arange(w, dtype=np.float32) + 0.5 - w / 2.0)[None, :]
        self.yb = (yc - h / 2.0).astype(np.float32)[:, None]
        self.y = ((yc % self.pitch) - self.pitch / 2.0).astype(np.float32)[:, None]
        self.ys = yc.astype(np.float32)[:, None]
        self.d = np.hypot(self.x, self.y).astype(np.float32)
        for a in (self.x, self.y, self.yb, self.d, self.ys):
            a.flags.writeable = False


@lru_cache(maxsize=12)
def grid(w: int, h: int) -> Grid:
    return Grid(w, h)


@lru_cache(maxsize=12)
def clockwise_angle(w: int, h: int) -> np.ndarray:
    """Angle of every pixel from 12 o'clock, clockwise, radians (-pi, pi]."""
    g = grid(w, h)
    a = np.arctan2(g.x, -g.y).astype(np.float32)
    a.flags.writeable = False
    return a


def box(ctx: RenderContext) -> tuple[int, int]:
    return max(1, int(ctx.width)), max(1, int(ctx.height))


# ---------------------------------------------------------------- painting
def cov(sd: np.ndarray) -> np.ndarray:
    """Coverage of a pixel whose centre is `sd` pixels outside a shape's edge."""
    return np.clip(0.5 - sd, 0.0, 1.0)


def canvas(w: int, h: int, rgb: colors.RGB) -> np.ndarray:
    img = np.empty((h, w, 3), np.float32)
    img[:] = np.asarray(rgb, np.float32)
    return img


def paint(img: np.ndarray, alpha: np.ndarray, rgb: colors.RGB) -> None:
    """Blend `rgb` over img where alpha (h, w) says so."""
    img += alpha[..., None] * (np.asarray(rgb, np.float32) - img)


def to_u8(img: np.ndarray) -> np.ndarray:
    return (np.clip(img, 0.0, 255.0) + 0.5).astype(np.uint8)


def mix(a: colors.RGB, b: colors.RGB, k: float) -> colors.RGB:
    k = max(0.0, min(1.0, k))
    return (a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k)  # type: ignore[return-value]


def capsule_sd(x: np.ndarray, y: np.ndarray, ax: float, ay: float, bx: float, by: float,
               ra: float, rb: float) -> np.ndarray:
    """Distance to a capsule from a (radius ra) to b (radius rb): a tapered hand with a round tip."""
    bax, bay = bx - ax, by - ay
    t = np.clip(((x - ax) * bax + (y - ay) * bay) / max(bax * bax + bay * bay, 1e-9), 0.0, 1.0)
    return np.hypot(x - ax - t * bax, y - ay - t * bay) - (ra + (rb - ra) * t)


def box_sd(qx: np.ndarray, qy: np.ndarray, hx: float, hy: float) -> np.ndarray:
    """Distance to an axis-aligned box of half sizes (hx, hy) centred on the origin."""
    dx, dy = np.abs(qx) - hx, np.abs(qy) - hy
    return np.hypot(np.maximum(dx, 0.0), np.maximum(dy, 0.0)) + np.minimum(np.maximum(dx, dy), 0.0)


# ---------------------------------------------------------------- beats and time
def beat_state(ctx: RenderContext) -> tuple[int, float]:
    """(whole beats since the clip started, fraction of the current beat). Without a tempo: (0, 0)."""
    cb = ctx.clip_beat if ctx.bpm and ctx.bpm > 0 and math.isfinite(ctx.clip_beat) else 0.0
    cb = max(0.0, cb)
    n = int(cb)
    return n, cb - n


def ease_out(k: float) -> float:
    """1 - (1 - k)^2 on 0..1: fast start, soft landing (the old sign's tick and pulse curve)."""
    k = max(0.0, min(1.0, k))
    return 1.0 - (1.0 - k) * (1.0 - k)


def smoothstep(a: float, b: float, x):
    t = np.clip((x - a) / (b - a), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def mix32(x):
    """The old sign's integer hash (lowbias32), on Python ints or uint32 arrays."""
    if isinstance(x, np.ndarray):
        x = x.astype(np.uint32)
        with np.errstate(over="ignore"):
            x ^= x >> np.uint32(16)
            x *= np.uint32(0x7FEB352D)
            x ^= x >> np.uint32(15)
            x *= np.uint32(0x846CA68B)
            x ^= x >> np.uint32(16)
        return x
    x &= 0xFFFFFFFF
    x ^= x >> 16
    x = (x * 0x7FEB352D) & 0xFFFFFFFF
    x ^= x >> 15
    x = (x * 0x846CA68B) & 0xFFFFFFFF
    x ^= x >> 16
    return x


# ---------------------------------------------------------------- params and base class
def color_param(key: str, label: str, default: str, help: str = "") -> ParamSpec:
    return ParamSpec(key, label, "color", default, help=help)


def hue_param(key: str, label: str) -> ParamSpec:
    return ParamSpec(key, label, "number", 0, -180, 180, 5,
                     help="Degrees the hue turns on every beat; 0 keeps the colour.")


def num_param(key: str, label: str, default: float, lo: float, hi: float, step: float, help: str = "") -> ParamSpec:
    return ParamSpec(key, label, "number", default, lo, hi, step, help=help)


class Fx(ClipRenderer):
    """Base of the effects: safe parameter access and the usual colour pair."""
    category = "beat"
    targets = ("face",)
    default_duration = 4.0

    @classmethod
    def _spec(cls, key: str) -> Optional[ParamSpec]:
        for s in cls.params:
            if s.key == key:
                return s
        return None

    def num(self, key: str) -> float:
        """A numeric param clamped to its spec's range; the default if it is not a number."""
        s = self._spec(key)
        d = float(s.default) if s is not None and s.default is not None else 0.0
        try:
            v = float(self.p.get(key, d))
            if not math.isfinite(v):
                v = d
        except (TypeError, ValueError):
            v = d
        if s is not None:
            if s.min is not None:
                v = max(float(s.min), v)
            if s.max is not None:
                v = min(float(s.max), v)
        return v

    def flag(self, key: str) -> bool:
        v = self.p.get(key)
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)

    def col(self, ctx: RenderContext, key: str, hue_key: str = "", avoid: Optional[colors.RGB] = None,
            fallback: colors.RGB = WHITE) -> colors.RGB:
        hue = self.num(hue_key) if hue_key else 0.0
        return colors.resolve(self.p.get(key), ctx, key, hue, avoid, fallback)

    def pair(self, ctx: RenderContext) -> tuple[colors.RGB, colors.RGB]:
        """(circle colour, ink); the ink is never the same as the circle (the old sign's rule)."""
        base = self.col(ctx, "color", "hue_color", fallback=ctx.face_color)
        ink = self.col(ctx, "ink", "hue_ink", avoid=base, fallback=BLACK)
        if ink == base:
            ink = WHITE if base == BLACK else BLACK
        return base, ink


def colour_params(color: str = "face", ink: Optional[str] = "#000000",
                  color_label: str = "Colour", ink_label: str = "Ink") -> list[ParamSpec]:
    out = [color_param("color", color_label, color)]
    if ink is not None:
        out.append(color_param("ink", ink_label, ink))
    out.append(hue_param("hue_color", f"{color_label}: hue jump per beat (°)"))
    if ink is not None:
        out.append(hue_param("hue_ink", f"{ink_label}: hue jump per beat (°)"))
    return out
