"""`fx:eq`: rainbow equaliser bars of LED segments."""
from __future__ import annotations

import colorsys

import numpy as np

from ..base import ParamSpec, RenderContext, register
from . import _common as c


def _bar_colors(n: int, hue_shift: float) -> np.ndarray:
    """A rainbow from red to magenta, one colour per bar."""
    out = []
    for i in range(n):
        hue = (300.0 * i / max(1, n - 1) + hue_shift) % 360.0 / 360.0
        out.append([v * 255.0 for v in colorsys.hsv_to_rgb(hue, 1.0, 1.0)])
    return np.array(out, np.float32)


def _span(coord: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Anti-aliased coverage of the pixels (centres in `coord`) by the interval [lo, hi]."""
    return np.clip(np.minimum(coord + 0.5, hi) - np.maximum(coord - 0.5, lo), 0.0, 1.0)


@register
class Eq(c.Fx):
    type = "fx:eq"
    label = "Equaliser"
    description = "Rainbow equaliser bars of LED segments that jump to a new height on every beat and sink back."
    targets = ("face", "all")
    params = [
        c.color_param("color", "Rim", "face"),
        c.color_param("bg", "Background", "#000000"),
        c.hue_param("hue_color", "Rim: hue jump per beat (°)"),
        c.hue_param("hue_bars", "Bars: hue jump per beat (°)"),
        ParamSpec("bars", "Bars", "int", 7, 3, 16, 1),
    ]

    def render(self, ctx: RenderContext) -> np.ndarray:
        w, h = c.box(ctx)
        n, ph = c.beat_state(ctx)
        rim = self.col(ctx, "color", "hue_color", fallback=ctx.face_color)
        bg = self.col(ctx, "bg", fallback=c.BLACK)
        bars = int(self.num("bars"))
        sc = w / 128.0
        g = c.grid(w, h)
        rem = 1.0 - ph if ctx.bpm and ctx.bpm > 0 else 0.0
        floor, seg_pitch, seg_h = h - 20.0 * sc, 8.0 * sc, 6.0 * sc
        segs = max(1, int(floor // seg_pitch))
        x0, x1 = 10.0 * sc, w - 10.0 * sc
        pitch = (x1 - x0) / bars
        bar_w = pitch * 0.75
        # Each bar jumps to a hashed height on the beat and sinks back to one segment; a peak segment stays.
        peaks = np.array([3 + c.mix32(n * bars + i + 0x5EED) % max(1, segs - 2) for i in range(bars)], np.float32)
        heights = 1.0 + (peaks - 1.0) * rem * rem
        # Per-segment brightness: full below the height, the segment at the top partly lit; peak on.
        k = np.arange(segs, dtype=np.float32)[None, :]
        level = np.clip(heights[:, None] - k, 0.0, 1.0)
        level = np.maximum(level, (k == (peaks[:, None] - 1.0)).astype(np.float32))      # (bars, segs)
        xs = g.x[0] + w / 2.0
        bar = np.clip(np.floor((xs - x0) / pitch), 0, bars - 1).astype(np.intp)
        hcov = _span(xs, x0 + bar * pitch + (pitch - bar_w) / 2.0, x0 + bar * pitch + (pitch + bar_w) / 2.0)
        hcov[(xs < x0) | (xs > x1)] = 0.0
        ys = g.ys[:, 0]
        seg = np.floor((floor - ys) / seg_pitch)                    # segment 0 is the bottom one
        seg_i = np.clip(seg, 0, segs - 1).astype(np.intp)
        top = floor - seg * seg_pitch
        vcov = _span(ys, top - seg_h, top)
        vcov[(seg < 0) | (seg >= segs)] = 0.0
        colors = _bar_colors(bars, self.num("hue_bars"))
        lit = level[bar[None, :], seg_i[:, None]]                    # (h, w)
        alpha = (lit * vcov[:, None] * hcov[None, :]).astype(np.float32)
        img = c.canvas(w, h, bg)
        img += alpha[..., None] * (colors[bar][None, :, :] - img)
        # The rim of the circle colour is drawn last, over the bars.
        rim_w = 2.5 * c.R / 64.0
        c.paint(img, c.cov((c.R - rim_w) - g.d), rim)
        return c.to_u8(img)
