"""Heart monitor traces: `fx:ecg` scrolls, `fx:ecgsweep` stands still behind a sweeping window."""
from __future__ import annotations

import math

import numpy as np

from ..base import RenderContext, register
from . import _common as c

# One PQRST complex, pixels above the baseline per table column (the old sign's table).
_ECG = np.array([
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 3, 3, 4, 5, 6, 6,
    6, 6, 5, 4, 3, 3, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, -1, -2, -4, -4, -1, 5, 15, 25, 35,
    35, 25, 15, 5, -3, -8, -9, -6, -2, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 2, 3, 4, 6, 7, 8, 9, 9, 10, 10, 10, 10,
    9, 9, 8, 7, 6, 4, 3, 2, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], np.float32)
_N = len(_ECG)
_TABLE = np.append(_ECG, _ECG[0])
_SUB = 4          # horizontal sub-samples per pixel for the anti-aliasing


def _trace(w: int, h: int, scroll: float, thickness: float) -> np.ndarray:
    """Coverage (h, w) of the trace; `scroll` in table columns (the trace moves left by that much)."""
    g = c.grid(w, h)
    xs = (np.arange(w * _SUB, dtype=np.float32) + 0.5) / _SUB                    # pixel units
    pos = np.mod(xs * (_N / w) - 0.5 + scroll, _N)
    y = -np.interp(pos, np.arange(_N + 1), _TABLE) * (c.R / 64.0)                # above baseline -> up
    half = thickness / 2.0
    # Every sub-sample covers the vertical run between itself and its neighbour, and the stroke
    # reaches `half` to both sides, so steep edges stay connected and are as thick as the flat.
    lo, hi = np.minimum(y[:-1], y[1:]), np.maximum(y[:-1], y[1:])
    lo, hi = np.append(lo, lo[-1]), np.append(hi, hi[-1])
    reach = max(1, int(math.ceil(half * _SUB)))
    from numpy.lib.stride_tricks import sliding_window_view as win
    lo = win(np.pad(lo, reach, mode="edge"), 2 * reach + 1).min(axis=1) - half
    hi = win(np.pad(hi, reach, mode="edge"), 2 * reach + 1).max(axis=1) + half
    yy = g.yb                                                                    # (h, 1)
    over = np.minimum(yy + 0.5, hi[None, :]) - np.maximum(yy - 0.5, lo[None, :])
    return np.clip(over, 0.0, 1.0).reshape(h, w, _SUB).mean(axis=2)


class _Ecg(c.Fx):
    sweep = False
    targets = ("face",)

    def render(self, ctx: RenderContext) -> np.ndarray:
        w, h = c.box(ctx)
        base, ink = self.pair(ctx)
        _, ph = c.beat_state(ctx)
        beats = bool(ctx.bpm and ctx.bpm > 0)
        phase = ph if beats else 0.0
        coverage = _trace(w, h, 0.0 if self.sweep else phase * _N, self.num("thickness"))
        if self.sweep and beats:
            # The trace shows through a window whose leading edge is on the spike (the middle)
            # on the beat and sweeps right; its oldest `fade` columns fade out.
            win_w = self.num("window") * w / 128.0
            fade = min(win_w, 16.0 * w / 128.0)
            lead = w / 2.0 + phase * w
            age = np.mod(lead - (np.arange(w, dtype=np.float32) + 0.5), w)
            alpha = np.clip(age, 0.0, 1.0) * np.clip((win_w - age) / fade, 0.0, 1.0)
            coverage = coverage * alpha[None, :]
        img = c.canvas(w, h, base)
        c.paint(img, coverage, ink)
        return c.to_u8(img)


@register
class Ecg(_Ecg):
    type = "fx:ecg"
    label = "ECG"
    description = "A heart monitor trace scrolling right to left, its spike in the middle on every beat."
    params = c.colour_params() + [c.num_param("thickness", "Line thickness (px)", 4, 1, 12, 0.5)]


@register
class EcgSweep(_Ecg):
    type = "fx:ecgsweep"
    label = "ECG sweep"
    description = "A standing heart monitor trace revealed by a window sweeping left to right once per beat."
    sweep = True
    params = c.colour_params() + [
        c.num_param("thickness", "Line thickness (px)", 4, 1, 12, 0.5),
        c.num_param("window", "Window width (px of 128)", 64, 16, 128, 4),
    ]
