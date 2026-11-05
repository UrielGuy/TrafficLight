"""`fx:fillv` / `fx:fillx`: lines appear on the beats until the picture is filled just before the clip ends."""
from __future__ import annotations

from functools import lru_cache

import numpy as np

from ..base import RenderContext, register
from . import _common as c

_LEAD = 0.1            # seconds: the last lines are in this long before the clip ends
_STRIP = 2             # px per line, as on the old sign
_FADE = 0.12           # beats a new line takes to come in


@lru_cache(maxsize=16)
def _ranks(total: int) -> np.ndarray:
    """When each line appears: its position in a fixed shuffle."""
    order = np.random.default_rng(1000 + total).permutation(total)
    rank = np.empty(total, np.intp)
    rank[order] = np.arange(total)
    rank.flags.writeable = False
    return rank


def _shown(k: int, moments: int, lines: int) -> int:
    """Lines visible after the k-th of `moments` beat moments (k >= 1)."""
    if moments <= lines:
        return -(-lines * k // moments)
    return 1 + (lines - 1) * (k - 1) // (moments - 1)


class _Fill(c.Fx):
    horizontal = False
    targets = ("face", "all")
    params = c.colour_params()

    def render(self, ctx: RenderContext) -> np.ndarray:
        w, h = c.box(ctx)
        base, ink = self.pair(ctx)
        vlines = -(-w // _STRIP)
        hlines = -(-h // _STRIP) if self.horizontal else 0
        total = vlines + hlines
        rank = _ranks(total)
        end = max(0.0, ctx.clip_duration - _LEAD)
        moments = int(end * ctx.bpm / 60.0 + 1e-9) + 1 if ctx.bpm and ctx.bpm > 0 and ctx.clip_duration > _LEAD else 0
        prev, cur, fade = 0, 0, 1.0
        if moments == 0 or ctx.bpm <= 0:
            cur = prev = total if ctx.t >= end else 0
        elif ctx.t >= end:
            cur = prev = total
        else:
            k = min(moments, int(max(0.0, ctx.clip_beat)) + 1)
            cur = _shown(k, moments, total)
            prev = _shown(k - 1, moments, total) if k > 1 else 0
            fade = c.smoothstep(0.0, _FADE, max(0.0, ctx.clip_beat) - (k - 1))
        # Rank -> opacity: complete lines are 1, the ones that arrive on this beat fade in.
        def opacity(r: np.ndarray) -> np.ndarray:
            return np.where(r < prev, 1.0, np.where(r < cur, fade, 0.0)).astype(np.float32)
        av = opacity(rank[:vlines])[np.arange(w) // _STRIP]
        alpha = av[None, :]
        if self.horizontal:
            ah = opacity(rank[vlines:])[np.arange(h) // _STRIP]
            alpha = np.maximum(alpha, ah[:, None])
        img = c.canvas(w, h, base)
        c.paint(img, np.broadcast_to(alpha, (h, w)), ink)
        return c.to_u8(img)


@register
class FillV(_Fill):
    type = "fx:fillv"
    label = "Fill (vertical)"
    description = "Vertical lines appear on the beats until the light is filled just before the clip ends."


@register
class FillX(_Fill):
    type = "fx:fillx"
    label = "Fill (cross)"
    description = "Vertical and horizontal lines appear on the beats until the light is filled just before the clip ends."
    horizontal = True
