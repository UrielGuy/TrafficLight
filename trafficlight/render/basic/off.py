"""`off`: black. Put over a whole-sign clip it switches one light off: all of its two panels,
not only the disc, so nothing of the clip below is left in the corners."""
from __future__ import annotations

import numpy as np

from ..base import ClipRenderer, RenderContext, register


@register
class Off(ClipRenderer):
    type = "off"
    label = "Off"
    category = "basic"
    targets = ("face", "all")
    default_duration = 4.0
    description = ("Black: the light is dark. On a light during a whole-sign video it switches "
                   "that light off while the video carries on in the others.")
    params = []
    masked = False

    def render(self, ctx: RenderContext) -> np.ndarray:
        return np.zeros((ctx.height, ctx.width, 3), np.uint8)
