"""The watch face: `fx:clock` (jumps 5 minutes a beat) and `clock` (the real time)."""
from __future__ import annotations

import datetime as _dt
import math
from functools import lru_cache
from typing import Optional

import numpy as np

from ..base import ParamSpec, RenderContext, register
from . import _common as c

U = c.U


@lru_cache(maxsize=8)
def _dial(w: int, h: int) -> np.ndarray:
    """Coverage of the bezel ring and the twelve hour marks (they never move)."""
    g = c.grid(w, h)
    theta = c.clockwise_angle(w, h)
    step = math.pi / 6
    k = np.rint(theta / step)
    dth = theta - k * step
    along, across = g.d * np.cos(dth), g.d * np.sin(dth)
    main = (k.astype(np.intp) % 3) == 0
    inner = np.where(main, 84.0 * U, 98.0 * U)
    half_w = np.where(main, 5.0 * U, 3.0 * U)
    outer = 114.0 * U
    mark = c.box_sd(across, along - (inner + outer) / 2.0, half_w, (outer - inner) / 2.0)
    bezel = 122.0 * U - g.d
    out = np.maximum(c.cov(mark), c.cov(bezel)).astype(np.float32)
    out.flags.writeable = False
    return out


def _dir(deg: float) -> tuple[float, float]:
    a = math.radians(deg)
    return math.sin(a), -math.cos(a)


def draw_watch(img: np.ndarray, w: int, h: int, base, ink: c.colors.RGB,
               hour_deg: float, minute_deg: float, second_deg: Optional[float] = None,
               second_color: Optional[c.colors.RGB] = None) -> None:
    g = c.grid(w, h)
    c.paint(img, _dial(w, h), ink)
    # Tapered hands from the centre to a round tip (half widths in the old sign's units).
    for deg, length, w0, w1 in ((hour_deg, 64.0, 9.0, 5.0), (minute_deg, 102.0, 7.0, 4.0)):
        dx, dy = _dir(deg)
        c.paint(img, c.cov(c.capsule_sd(g.x, g.yb, 0.0, 0.0, dx * length * U, dy * length * U, w0 * U, w1 * U)), ink)
    # Hub with a pin in the face colour; the second hand runs over it.
    c.paint(img, c.cov(g.d - 14.0 * U), ink)
    c.paint(img, c.cov(g.d - 4.0 * U), base)      # a transparent base punches the pin out
    if second_deg is not None:
        sc = second_color or ink
        dx, dy = _dir(second_deg)
        thin = 0.9
        c.paint(img, c.cov(c.capsule_sd(g.x, g.yb, -dx * 22.0 * U, -dy * 22.0 * U, dx * 108.0 * U, dy * 108.0 * U,
                                        thin, thin)), sc)


@register
class BeatClock(c.Fx):
    type = "fx:clock"
    label = "Clock (beat)"
    description = "A watch face that starts at 12:00 and gains 5 minutes on every beat, hands ticking like a quartz watch."
    params = c.colour_params() + [
        c.num_param("minutes", "Minutes per beat", 5, 1, 60, 1),
        c.num_param("tick", "Tick time (beats)", 0.125, 0.0, 1.0, 0.025,
                    "How long the hands take to jump after the beat; 0 snaps."),
    ]

    def render(self, ctx: RenderContext) -> np.ndarray:
        w, h = c.box(ctx)
        base, ink = self.pair(ctx)
        n, ph = c.beat_state(ctx)
        step = self.num("minutes")
        tick = self.num("tick")
        back = 0.0                      # how far the hands have yet to go to this beat's position
        if n > 0 and tick > 0 and ph < tick:
            back = (1.0 - ph / tick) ** 2
        minutes = (n - back) * step
        img = c.canvas(w, h, base)
        draw_watch(img, w, h, base, ink, minutes * 0.5, minutes * 6.0)
        return c.to_u8(img)


@register
class WallClock(c.Fx):
    type = "clock"
    label = "Clock"
    category = "basic"
    description = "A watch face showing the real local time, with an optional second hand."
    params = [
        c.color_param("color", "Colour", "face", "Transparent: only the ink is drawn, over what is below.",
                      transparent=True),
        c.color_param("ink", "Ink", "#000000"),
        c.color_param("second_color", "Second hand", "#000000"),
        ParamSpec("seconds", "Second hand", "bool", True),
        ParamSpec("smooth", "Sweep the second hand", "bool", True,
                  help="Off: it jumps once a second like a quartz watch."),
    ]

    def render(self, ctx: RenderContext) -> np.ndarray:
        w, h = c.box(ctx)
        base = self.col(ctx, "color", fallback=ctx.face_color)
        clear = c.is_clear(base)
        ink = self.col(ctx, "ink", avoid=None if clear else base, fallback=c.BLACK)
        if not clear and ink == base:
            ink = c.WHITE if base == c.BLACK else c.BLACK
        t = ctx.wall_time if isinstance(ctx.wall_time, _dt.datetime) else _dt.datetime(2000, 1, 1, 10, 10, 30)
        sec = t.second + (t.microsecond / 1e6 if self.flag("smooth") else 0.0)
        minute = t.minute + sec / 60.0
        hour = (t.hour % 12) + minute / 60.0
        second = sec * 6.0 if self.flag("seconds") else None
        sc = self.col(ctx, "second_color", fallback=ink)
        img = c.canvas(w, h, base)
        draw_watch(img, w, h, base, ink, hour * 30.0, minute * 6.0, second, sc)
        return c.to_u8(img)
