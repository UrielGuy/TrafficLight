"""`figure`: the old sign's animated pictograms."""
from __future__ import annotations

from typing import Any

import numpy as np

from ..base import ClipRenderer, ParamSpec, RenderContext, RenderEnv, register
from . import figures
from .paint import colour_params, colours, compose, number, see_through

DEFAULT = "walking"


def _figure_id(value: Any) -> str:
    return value if isinstance(value, str) and (value in figures.BY_ID or value in figures.DISC_BY_ID) else DEFAULT


@register
class Figure(ClipRenderer):
    type = "figure"
    label = "Figure"
    category = "figure"
    targets = ("face",)
    default_duration = 4.0
    description = "An animated pictogram (walking, dancing, DJ…) or a disc crossing the light, in the ink on the background."
    params = [
        ParamSpec("figure", "Figure", "select", DEFAULT, options=figures.OPTIONS),
        *colour_params("face", "#000000"),
        ParamSpec("speed", "Speed", "number", 1.0, min=0.0, max=4.0, step=0.05,
                  help="1 = the old sign's pace; 0 holds the first frame."),
    ]

    def __init__(self, params: dict[str, Any], env: RenderEnv):
        super().__init__(params, env)
        self._id = _figure_id(self.p.get("figure"))
        self._speed = number(self.p.get("speed"), 1.0, 0.0, 16.0)
        self._transparent = see_through(self.p)
        self._field: figures.DiscField | None = None

    def _alpha(self, ctx: RenderContext) -> np.ndarray:
        ms = max(0.0, ctx.t) * 1000.0 * self._speed
        if self._id in figures.DISC_BY_ID:
            if self._field is None or self._field.x.size != ctx.width or self._field.y.size != ctx.height:
                self._field = figures.DiscField(ctx.width, ctx.height)
            return self._field.disc(*figures.disc_position(self._id, ms, ctx.width, ctx.height))
        fr = figures.frames(self._id, ctx.width, ctx.height)
        step_ms = figures.BY_ID[self._id][2]
        return fr[int(ms // step_ms) % len(fr)]

    def render(self, ctx: RenderContext) -> np.ndarray:
        bg, ink = colours(self.p, ctx)
        return compose(self._alpha(ctx), bg, ink, self._transparent)

    @classmethod
    def validate(cls, params: dict[str, Any], target: str, env: RenderEnv) -> list[str]:
        p = cls.with_defaults(params)
        problems = []
        if target not in ("face0", "face1", "face2"):
            problems.append("A figure goes on a light's track, not the whole sign")
        f = p.get("figure")
        if _figure_id(f) != f:
            problems.append(f"Unknown figure {f!r}: showing {DEFAULT}")
        return problems
