"""Beat effects drawn from a few shapes: quarters, pulse, wave, heart, ripples, chase, ball."""
from __future__ import annotations

import math
from functools import lru_cache

import numpy as np

from ..base import ParamSpec, RenderContext, register
from . import _common as c


# ---------------------------------------------------------------- quarters
_QUARTER_HELP = "Transparent: that quarter shows what is below."


@register
class Quarters(c.Fx):
    type = "fx:quarters"
    label = "Quarters"
    description = "Four colours in quarters that turn 30 degrees clockwise on every beat."
    params = [
        c.color_param("color", "Colour 1 (top left)", "face", _QUARTER_HELP, transparent=True),
        c.color_param("color2", "Colour 2 (top right)", "#ffffff", _QUARTER_HELP, transparent=True),
        c.color_param("color3", "Colour 3 (bottom right)", "#000000", _QUARTER_HELP, transparent=True),
        c.color_param("color4", "Colour 4 (bottom left)", "#ffffff", _QUARTER_HELP, transparent=True),
        c.hue_param("hue_color", "Hue jump per beat (°)"),
        c.num_param("turn", "Turn time (beats)", 0.2, 0.0, 1.0, 0.05,
                    "How long the 30 degree turn takes after each beat; 0 snaps."),
    ]

    def render(self, ctx: RenderContext) -> np.ndarray:
        w, h = c.box(ctx)
        n, ph = c.beat_state(ctx)
        base = self.col(ctx, "color", "hue_color", fallback=ctx.face_color)
        quarters = [base] + [self.col(ctx, k, "hue_color", fallback=c.WHITE) for k in ("color2", "color3", "color4")]
        # Blended premultiplied, so an edge between a clear quarter and a coloured one fades in alpha only.
        four = any(c.is_clear(q) for q in quarters)
        cols = np.array([c.vec(q, four) for q in quarters], np.float32)
        turn = self.num("turn")
        done = 1.0 if turn <= 0 else c.ease_out(ph / turn)
        rot = math.radians(30.0 * (n - (1.0 - done) if n > 0 else 0.0))
        g = c.grid(w, h)
        # Position in quarters, 0 at the left edge of colour 1 (9 o'clock turned by rot).
        s = np.mod((c.clockwise_angle(w, h) - rot + math.pi / 2) / (math.pi / 2), 4.0)
        edge = np.rint(s)
        b = edge.astype(np.intp) & 3
        # Arc distance in pixels to the nearest edge: blend the two neighbours over one pixel.
        wt = np.clip(0.5 + (s - edge) * (math.pi / 2) * g.d, 0.0, 1.0)[..., None]
        img = cols[(b - 1) & 3] * (1.0 - wt) + cols[b] * wt
        return c.to_u8(img)


# ---------------------------------------------------------------- pulse
@register
class Pulse(c.Fx):
    type = "fx:pulse"
    label = "Pulse"
    description = "A disc that is full size on the beat and eases back to a smaller one."
    params = [
        c.color_param("color", "Colour", "face"),
        c.color_param("bg", "Background", "#000000", "Transparent: only the disc is drawn, over what is below.",
                      transparent=True),
        c.hue_param("hue_color", "Hue jump per beat (°)"),
        c.num_param("min_size", "Smallest size (%)", 75, 10, 100, 5),
    ]

    def render(self, ctx: RenderContext) -> np.ndarray:
        w, h = c.box(ctx)
        _, ph = c.beat_state(ctx)
        lo = self.num("min_size") / 100.0
        rem = 1.0 - ph if ctx.bpm and ctx.bpm > 0 else 0.0
        radius = c.R * (lo + (1.0 - lo) * rem * rem)
        img = c.canvas(w, h, self.col(ctx, "bg", fallback=c.BLACK))
        c.paint(img, c.cov(c.grid(w, h).d - radius), self.col(ctx, "color", "hue_color", fallback=ctx.face_color))
        return c.to_u8(img)


# ---------------------------------------------------------------- wave
@register
class Wave(c.Fx):
    type = "fx:wave"
    label = "Wave"
    description = "A line bent into a sine wave that swings through flat to the other side on every beat."
    params = c.colour_params() + [
        c.num_param("amplitude", "Swing (% of radius)", 40, 5, 90, 5),
        c.num_param("thickness", "Line thickness (px)", 6, 1, 20, 0.5),
    ]

    def render(self, ctx: RenderContext) -> np.ndarray:
        w, h = c.box(ctx)
        base, ink = self.pair(ctx)
        g = c.grid(w, h)
        amp = c.R * self.num("amplitude") / 100.0
        # cos(pi * beats): +1 on beat 1 (left up), 0 mid beat, -1 on beat 2, continuous across beats.
        a = amp * math.cos(math.pi * ctx.clip_beat) if ctx.bpm and ctx.bpm > 0 else 0.0
        k = 2.0 * math.pi / w
        phase = (g.x + w / 2.0) * k                           # one period across the box from its left edge
        f = -a * np.sin(phase)                                # line's y offset from the middle
        slope = -a * k * np.cos(phase)
        sd = np.abs(g.yb - f) / np.sqrt(1.0 + slope * slope) - self.num("thickness") / 2.0
        img = c.canvas(w, h, base)
        c.paint(img, c.cov(sd), ink)
        return c.to_u8(img)


# ---------------------------------------------------------------- heart
@register
class Heart(c.Fx):
    type = "fx:heart"
    label = "Heart"
    description = "A heart that is full size on the beat and eases back to 80 percent."
    params = [
        c.color_param("color", "Heart", "face"),
        c.color_param("bg", "Background", "#000000", "Transparent: only the heart is drawn, over what is below.",
                      transparent=True),
        c.hue_param("hue_color", "Hue jump per beat (°)"),
        c.num_param("min_size", "Smallest size (%)", 80, 20, 100, 5),
    ]

    def render(self, ctx: RenderContext) -> np.ndarray:
        w, h = c.box(ctx)
        _, ph = c.beat_state(ctx)
        lo = self.num("min_size") / 100.0
        rem = 1.0 - ph if ctx.bpm and ctx.bpm > 0 else 0.0
        z = c.U * (lo + (1.0 - lo) * rem * rem)
        g = c.grid(w, h)
        # A square on its corner (half diagonal 94, centre 10 below the middle) plus two discs
        # on its upper sides, in the old sign's units.
        ax = np.abs(g.x)
        diamond = (ax + np.abs(g.y - 10.0 * z) - 94.0 * z) * math.sqrt(0.5)
        lobes = np.hypot(ax - 47.0 * z, g.y - (10.0 - 47.0) * z) - 94.0 * z * math.sqrt(0.5)
        bg = self.col(ctx, "bg", fallback=c.BLACK)
        color = self.col(ctx, "color", "hue_color", fallback=ctx.face_color)
        if not c.is_clear(bg) and color == bg:
            color = c.WHITE if bg == c.BLACK else c.BLACK
        img = c.canvas(w, h, bg)
        c.paint(img, c.cov(np.minimum(diamond, lobes)), color)
        return c.to_u8(img)


# ---------------------------------------------------------------- ripples
@register
class Ripples(c.Fx):
    type = "fx:ripples"
    label = "Ripples"
    description = "A ring leaves the centre on every beat, widens to the rim and fades."
    targets = ("face", "all")
    params = c.colour_params() + [
        c.num_param("life", "Beats to the rim", 2.5, 1, 6, 0.5),
        c.num_param("width", "Ring width (px)", 5, 1, 12, 0.5),
    ]

    def render(self, ctx: RenderContext) -> np.ndarray:
        w, h = c.box(ctx)
        base, ink = self.pair(ctx)
        g = c.grid(w, h)
        life = self.num("life")
        wmax = self.num("width") * c.R / 64.0
        img = c.canvas(w, h, base)
        if ctx.bpm and ctx.bpm > 0:
            cb = max(0.0, ctx.clip_beat) if math.isfinite(ctx.clip_beat) else 0.0
            ages = [cb - k for k in range(max(0, int(cb - life)), int(cb) + 1) if cb - k < life]
        else:
            ages = [0.2 * life, 0.6 * life]
        for age in ages:
            u = age / life
            r = c.R * u * (3.0 - u) / 2.0          # starts 1.5x the average speed, ends 0.5x
            width = wmax * (1.0 - 0.4 * u)          # 5 px thinning to 3
            opacity = 1.0 - 0.8 * float(c.smoothstep(0.55, 1.0, u))
            c.paint(img, c.cov(np.abs(g.d - r) - width / 2.0) * opacity, ink)
        return c.to_u8(img)


# ---------------------------------------------------------------- chase
@lru_cache(maxsize=8)
def _bulbs(w: int, h: int, radius: float, bulb: float):
    """Coverage of the 12 bulbs and which bulb (0 at 12 o'clock, clockwise) each pixel is nearest."""
    g = c.grid(w, h)
    theta = c.clockwise_angle(w, h)
    step = math.pi / 6
    k = np.rint(theta / step)
    dth = theta - k * step
    dist = np.sqrt(np.maximum(g.d * g.d + radius * radius - 2.0 * g.d * radius * np.cos(dth), 0.0))
    coverage = c.cov(dist - bulb).astype(np.float32)
    index = (k.astype(np.intp)) % 12
    coverage.flags.writeable = False
    index.flags.writeable = False
    return coverage, index


@register
class Chase(c.Fx):
    type = "fx:chase"
    label = "Chase"
    description = "Twelve bulbs around the rim: one more lights up on every beat, clockwise from 12, then all go out."
    params = c.colour_params() + [
        c.num_param("unlit", "Unlit bulb contrast (%)", 30, 0, 100, 5,
                    "How far the unlit bulbs are from the circle colour toward the ink."),
    ]

    def render(self, ctx: RenderContext) -> np.ndarray:
        w, h = c.box(ctx)
        base, ink = self.pair(ctx)
        n, _ = c.beat_state(ctx)
        lit = (n + 1) % 13 if ctx.bpm and ctx.bpm > 0 else 12
        coverage, index = _bulbs(w, h, 100.0 * c.U, 16.0 * c.U)
        img = c.canvas(w, h, base)
        c.paint(img, coverage * (index >= lit), c.mix(base, ink, self.num("unlit") / 100.0))
        c.paint(img, coverage * (index < lit), ink)
        return c.to_u8(img)


# ---------------------------------------------------------------- ball
@register
class Ball(c.Fx):
    type = "fx:ball"
    label = "Ball"
    description = "A ball that lands on a floor line on every beat, squashing as it hits."
    params = c.colour_params()

    def render(self, ctx: RenderContext) -> np.ndarray:
        w, h = c.box(ctx)
        base, ink = self.pair(ctx)
        g = c.grid(w, h)
        u = c.U
        radius, floor, high, squash, window = 22.0 * u, 97.0 * u, 128.0 * u, 6.0 * u, 0.05
        _, ph = c.beat_state(ctx)
        if ctx.bpm and ctx.bpm > 0:
            d = min(ph, 1.0 - ph)
            if d < window:           # on the floor, squashed most right on the beat
                lift, sq = 0.0, squash * (window - d) / window
            else:                    # a parabola from the floor up to `high` mid beat and back
                lift = high * 4.0 * (ph - window) * (1.0 - window - ph) / (1.0 - 2.0 * window) ** 2
                sq = 0.0
        else:
            lift, sq = 0.0, 0.0
        rx, ry = radius + sq, radius - sq
        cy = floor - ry - lift
        k = np.hypot(g.x / rx, (g.yb - cy) / ry)
        img = c.canvas(w, h, base)
        c.paint(img, c.cov(np.abs(g.yb - (floor + 1.0 * u)) - 1.0), ink)
        c.paint(img, c.cov((k - 1.0) * min(rx, ry)), ink)
        return c.to_u8(img)
