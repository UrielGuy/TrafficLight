"""`light`: a plain lit light."""
from __future__ import annotations

import dataclasses
import threading

import numpy as np

from ... import config
from .. import color
from ..base import ClipRenderer, ParamSpec, RenderContext, register
from .paint import hue_param, number

_DISCS: dict[tuple[int, int], np.ndarray] = {}
_DISCS_LOCK = threading.Lock()


def discs_alpha(width: int, height: int) -> np.ndarray:
    """Anti-aliased coverage (uint8, height x width) of the lit discs of the lights stacked in a
    box `width` wide: one per width x width square, radius FACE_RADIUS scaled to the width."""
    key = (width, height)
    with _DISCS_LOCK:
        a = _DISCS.get(key)
    if a is None:
        r = config.FACE_RADIUS * width / config.FACE
        x = np.arange(width, dtype=np.float32) + 0.5 - width / 2
        y = (np.arange(height, dtype=np.float32) + 0.5) % width - width / 2
        d = np.sqrt(x[None, :] ** 2 + y[:, None] ** 2)
        a = np.rint(np.clip(r + 0.5 - d, 0.0, 1.0) * 255).astype(np.uint8)
        a[(height // width) * width:] = 0          # a partial square at the bottom is not a light
        with _DISCS_LOCK:
            _DISCS[key] = a
    return a


@register
class Light(ClipRenderer):
    type = "light"
    label = "Light"
    category = "basic"
    targets = ("face", "all")
    default_duration = 4.0
    description = ("A lit light in one colour. On a light's track it fills the box (the light's mask "
                   "makes it a disc). On the whole-sign track it lights all three lights as discs, "
                   "each in its own colour when the colour is \"face\".")
    params = [
        ParamSpec("color", "Colour", "color", "face"),
        hue_param("hue", "Hue jump per beat"),
        ParamSpec("brightness", "Brightness", "number", 1.0, min=0.0, max=1.0, step=0.05),
    ]

    def _colour(self, ctx: RenderContext) -> color.RGB:
        c = color.resolve(self.p.get("color"), ctx, "color", number(self.p.get("hue"), 0.0), fallback=ctx.face_color)
        return color.scale(c, number(self.p.get("brightness"), 1.0, 0.0, 1.0))

    def render(self, ctx: RenderContext) -> np.ndarray:
        if ctx.face is not None:
            out = np.empty((ctx.height, ctx.width, 3), np.uint8)
            out[:] = self._colour(ctx)
            return out
        alpha = discs_alpha(ctx.width, ctx.height)
        out = np.empty((ctx.height, ctx.width, 4), np.uint8)
        out[..., 3] = alpha
        faces = self.env.settings.face_colors
        for i in range(max(1, ctx.height // ctx.width)):
            own = color.parse_hex(faces[i], color.WHITE) if i < len(faces) else color.WHITE
            out[i * ctx.width:(i + 1) * ctx.width, :, :3] = self._colour(dataclasses.replace(ctx, face_color=own))
        out[(ctx.height // ctx.width) * ctx.width:, :, :3] = 0
        return out
