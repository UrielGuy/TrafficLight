"""`text`: text fitted inside a light."""
from __future__ import annotations

from typing import Any

import numpy as np

from ... import config
from ..base import ClipRenderer, ParamSpec, RenderContext, RenderEnv, register
from .fonts import font_options
from .paint import colour_params, colours, compose, flag, number
from .textlayout import LADDER, get_layout


def _params(p: dict[str, Any]) -> tuple[str, str, int]:
    text = p.get("text")
    text = text if isinstance(text, str) else ("" if text is None else str(text))
    font = p.get("font") if isinstance(p.get("font"), str) else "auto"
    return text, font, int(number(p.get("max_size"), 0, 0, LADDER[0]))


@register
class Text(ClipRenderer):
    type = "text"
    label = "Text"
    category = "text"
    targets = ("face",)
    default_duration = 4.0
    description = ("Text fitted in the light, as large as it fits: line breaks are chosen automatically "
                   "(a new line in the text forces one). Hebrew, English and both mixed.")
    params = [
        ParamSpec("text", "Text", "text", "STOP"),
        ParamSpec("font", "Font", "select", "auto", options=font_options(),
                  help="Automatic: Latin letters in High-School, Hebrew in Reisinger Michal, "
                       "digits and signs in DejaVu Sans Bold. A chosen font is used for every "
                       "character it has."),
        ParamSpec("max_size", "Largest size", "int", 0, min=0, max=LADDER[0], step=1,
                  help="Cap on the letter size in pixels (em); 0 = as large as fits."),
        *colour_params("face", "#000000"),
    ]

    def __init__(self, params: dict[str, Any], env: RenderEnv):
        super().__init__(params, env)
        self._key = _params(self.p)
        self._transparent = flag(self.p.get("transparent"))
        get_layout(*self._key, config.FACE, config.FACE)    # lay out now rather than on the first frame

    def render(self, ctx: RenderContext) -> np.ndarray:
        lay = get_layout(*self._key, ctx.width, ctx.height)
        bg, ink = colours(self.p, ctx)
        alpha = lay.alpha if lay.ok else np.zeros((ctx.height, ctx.width), np.uint8)
        return compose(alpha, bg, ink, self._transparent)

    @classmethod
    def validate(cls, params: dict[str, Any], target: str, env: RenderEnv) -> list[str]:
        p = cls.with_defaults(params)
        text, font, cap = _params(p)
        problems = []
        if target not in ("face0", "face1", "face2"):
            problems.append("Text goes on a light's track, not the whole sign")
        lay = get_layout(text, font, cap, config.FACE, config.FACE)
        if lay.missing:
            problems.append(f"No font has these characters, they are left out: {lay.missing}")
        if lay.error == "empty":
            problems.append("The text is empty")
        elif not lay.ok:
            problems.append("The text does not fit in the light, even at the smallest size: shorten it")
        return problems
