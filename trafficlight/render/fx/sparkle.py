"""`fx:sparkle`: twinkling stars, not tied to the beat."""
from __future__ import annotations


import numpy as np

from .. import color as colors
from ..base import ParamSpec, RenderContext, register
from . import _common as c

_PALETTE = [(255, 255, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255), (255, 170, 0), (0, 255, 0), (170, 255, 255)]
_REACH = 8.5            # arm length of a full star, pixels at the old sign's size
_HALF = 11              # the window painted around a spark: 2 * _HALF + 1 pixels square
_OFF = np.arange(-_HALF, _HALF + 1, dtype=np.float32)


def _unit(h: np.ndarray) -> np.ndarray:
    return (h >> np.uint32(8)).astype(np.float32) / float(1 << 24)


@register
class Sparkle(c.Fx):
    type = "fx:sparkle"
    label = "Sparkle"
    description = "Stars twinkle in and out at random places, in their own rhythm rather than the beat."
    targets = ("face", "all")
    params = [
        c.color_param("color", "Colour", "face"),
        c.color_param("ink", "Star colour", "#ffffff", "Used when multi-coloured is off."),
        ParamSpec("multicolor", "Multi-coloured", "bool", True),
        ParamSpec("count", "Stars per light", "int", 20, 3, 60, 1),
        c.hue_param("hue_color", "Colour: hue jump per beat (°)"),
        c.hue_param("hue_ink", "Stars: hue jump per beat (°)"),
    ]

    def render(self, ctx: RenderContext) -> np.ndarray:
        w, h = c.box(ctx)
        g = c.grid(w, h)
        base = self.col(ctx, "color", "hue_color", fallback=ctx.face_color)
        n, _ = c.beat_state(ctx)
        hue = self.num("hue_ink") * n
        if self.flag("multicolor"):
            pal = [colors.hue_rotate(p, hue) for p in _PALETTE]
            pal = [p for p in pal if sum(abs(a - b) for a, b in zip(p, base)) > 150] or [c.WHITE]
        else:
            pal = [self.col(ctx, "ink", "hue_ink", avoid=base, fallback=c.WHITE)]
        palette = np.array(pal, np.float32)
        img = c.canvas(w, h, base)

        k = int(self.num("count")) * g.tiles
        i = np.arange(k, dtype=np.uint32)
        seed = np.uint32(((ctx.seed * 0x9E3779B1) + (3 if ctx.face is None else ctx.face) * 0x85EBCA6B) & 0xFFFFFFFF)
        with np.errstate(over="ignore"):
            period = 0.56 + 0.44 * _unit(c.mix32(i * np.uint32(977) + np.uint32(1) + seed))
            tt = max(0.0, ctx.t) + _unit(c.mix32(i * np.uint32(131) + np.uint32(7) + seed)) * period
            gen = np.floor(tt / period).astype(np.uint32)
            tau = tt - gen.astype(np.float32) * period
            h1 = c.mix32(i * np.uint32(7919) + gen * np.uint32(104729) + np.uint32(12345) + seed)
            h2 = c.mix32(h1)
            h3 = c.mix32(h2)
        life = np.minimum(0.32 + 0.44 * _unit(h1), period - 0.08)
        on = tau < life
        if not on.any():
            return c.to_u8(img)
        s = (tau / life)[on]
        h1, h2, h3 = h1[on], h2[on], h3[on]
        env = np.sin(np.pi * s) ** 0.7                                  # twinkle in, peak, out
        size = (0.35 + 0.65 * env) * (c.R / 64.0)
        rho = 0.84 * c.R * np.maximum(_unit(h2 << np.uint32(3)), _unit(h2 >> np.uint32(2)))
        ang = 2.0 * np.pi * _unit(h1 << np.uint32(5))
        tile = (h3 % np.uint32(g.tiles)).astype(np.float32)
        cx = w / 2.0 + rho * np.cos(ang)
        cy = (tile + 0.5) * g.pitch + rho * np.sin(ang)
        star = ((h2 >> np.uint32(20)) & np.uint32(1)).astype(bool)
        col = palette[(h3 >> np.uint32(8)) % np.uint32(len(palette))]

        # A small window around every spark, as arrays (K, 23, 23).
        px = np.floor(cx)[:, None, None] + _OFF[None, None, :]          # pixel x index
        py = np.floor(cy)[:, None, None] + _OFF[None, :, None]
        ex = px + 0.5 - cx[:, None, None]
        ey = py + 0.5 - cy[:, None, None]
        L = (_REACH * size)[:, None, None]
        core = np.exp(-(ex * ex + ey * ey) / (2.0 * (0.9 * size[:, None, None]) ** 2))
        arm_h = np.clip(1.0 - np.abs(ex) / L, 0.0, 1.0) ** 1.6 * np.exp(-(ey * ey) / (2.0 * 0.75 ** 2))
        arm_v = np.clip(1.0 - np.abs(ey) / L, 0.0, 1.0) ** 1.6 * np.exp(-(ex * ex) / (2.0 * 0.75 ** 2))
        star_shape = np.maximum(core, np.maximum(arm_h, arm_v))
        blob = np.clip(2.6 * size[:, None, None] - np.hypot(ex, ey) + 0.5, 0.0, 1.0)
        alpha = np.where(star[:, None, None], star_shape, blob) * env[:, None, None]
        valid = (px >= 0) & (px < w) & (py >= 0) & (py < h)
        idx = (np.clip(py, 0, h - 1) * w + np.clip(px, 0, w - 1)).astype(np.intp)
        alpha = np.where(valid, alpha, 0.0).astype(np.float32)
        flat = idx.ravel()
        a_sum = np.bincount(flat, alpha.ravel(), minlength=w * h)
        acc = np.stack([np.bincount(flat, (alpha * col[:, ch, None, None]).ravel(), minlength=w * h)
                        for ch in range(3)], axis=1)
        a_sum = a_sum.reshape(h, w).astype(np.float32)
        acc = acc.reshape(h, w, 3).astype(np.float32)
        a_c = np.clip(a_sum, 0.0, 1.0)
        img = img * (1.0 - a_c[..., None]) + acc / np.maximum(a_sum, 1e-6)[..., None] * a_c[..., None]
        return c.to_u8(img)
