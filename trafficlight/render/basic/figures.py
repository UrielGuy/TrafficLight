"""The old sign's animated pictograms, drawn anti-aliased at the light's resolution.

Pose data and the jointed-figure drawing are the old sign's tools/gen_sprites.py.
There every frame was drawn large, all frames of a sprite were cropped to their
union box, scaled to fit 64 x 96 (a 128 px light) and thresholded to 1 bpp.
Here the same union box and scale are used, but each frame is drawn again at
SS times the final scale and box-filtered down: the coverage is the alpha.

A pose: angles in degrees, 0 = straight down, positive = clockwise as seen by
the viewer. lean = torso angle from vertical, head = extra head angle, la/lf
ra/rf = upper arm / forearm, ll/ls rl/rs = thigh / shin (absolute angles),
dx/dy = offset of the hip, "second" = a second figure, "shapes" = props:
("circle", cx, cy, r, width) / ("disc", cx, cy, r) / ("line", x1, y1, x2, y2, width)
in figure units relative to the main hip.
"""
from __future__ import annotations

import math
import threading

import numpy as np
from PIL import Image, ImageDraw

from ... import config

# (id, name, ms per frame, poses)
SPRITES = [
  ("standing", "Person standing", 1000, [
      dict(la=-28, lf=-14, ra=28, rf=14, ll=-14, ls=-12, rl=14, rs=12),
  ]),
  ("ready", "On your marks, get set", 700, [
      dict(la=-28, lf=-14, ra=28, rf=14, ll=-14, ls=-12, rl=14, rs=12),
      dict(lean=35, head=-15, la=-60, lf=-30, ra=-40, rf=-10, ll=-45, ls=35, rl=25, rs=-10),
      dict(lean=72, head=-40, la=-95, lf=-5, ra=-85, rf=5, ll=-70, ls=20, rl=55, rs=75),
  ]),
  ("breakdance", "Breakdancing", 200, [
      dict(lean=8,   la=-70,  lf=-20,  ra=60,   rf=20,   ll=-30, ls=10,  rl=30,  rs=-10),
      dict(lean=50,  head=-20, la=-20,  lf=10,   ra=-40,  rf=-10, ll=-45, ls=30,  rl=60,  rs=20),
      dict(lean=100, head=-30, la=10,   lf=0,    ra=-10,  rf=-5,  ll=-150, ls=-170, rl=-100, rs=-30),
      dict(lean=180, la=15,   lf=5,    ra=-15,  rf=-5,   ll=-155, ls=-135, rl=155, rs=135),
      dict(lean=180, la=20,   lf=10,   ra=-20,  rf=-10,  ll=-110, ls=-160, rl=175, rs=120),
      dict(lean=-180, la=-15, lf=-5,   ra=15,   rf=5,    ll=110,  ls=160,  rl=-175, rs=-120),
      dict(lean=-100, head=30, la=-10, lf=5,    ra=10,   rf=0,    ll=150,  ls=170,  rl=100,  rs=30),
      dict(lean=-45, head=20, la=40,   lf=10,   ra=20,   rf=-10,  ll=45,   ls=-30,  rl=-60,  rs=-20),
  ]),
  ("walking", "Walking", 220, [
      dict(lean=4, la=25,  lf=35,  ra=-25, rf=-15, ll=-30, ls=-20, rl=25,  rs=40),
      dict(lean=4, la=10,  lf=15,  ra=-10, rf=-5,  ll=-8,  ls=-4,  rl=8,   rs=30),
      dict(lean=4, la=-25, lf=-15, ra=25,  rf=35,  ll=25,  ls=40,  rl=-30, rs=-20),
      dict(lean=4, la=-10, lf=-5,  ra=10,  rf=15,  ll=8,   ls=30,  rl=-8,  rs=-4),
  ]),
  ("running", "Running", 150, [
      dict(lean=22, la=45,  lf=-30, ra=-60, rf=-150, ll=-50, ls=-15, rl=40,  rs=95),
      dict(lean=22, la=15,  lf=-60, ra=-30, rf=-120, ll=-15, ls=30,  rl=15,  rs=70),
      dict(lean=22, la=-60, lf=-150, ra=45, rf=-30,  ll=40,  ls=95,  rl=-50, rs=-15),
      dict(lean=22, la=-30, lf=-120, ra=15, rf=-60,  ll=15,  ls=70,  rl=-15, rs=30),
  ]),
  ("hug", "Two people hugging", 700, [
      dict(dx=-22, lean=4,  la=-30, lf=-20, ra=55,  rf=40,  ll=-8, ls=-8, rl=8, rs=8,
           second=dict(dx=22, lean=-4, la=-55, lf=-40, ra=30, rf=20, ll=-8, ls=-8, rl=8, rs=8)),
      dict(dx=-11, lean=10, la=-20, lf=-10, ra=75,  rf=100, ll=-8, ls=-8, rl=8, rs=8,
           second=dict(dx=11, lean=-10, la=-75, lf=-100, ra=20, rf=10, ll=-8, ls=-8, rl=8, rs=8)),
      dict(dx=-11, lean=12, head=-10, la=-20, lf=-10, ra=80, rf=110, ll=-8, ls=-8, rl=8, rs=8,
           second=dict(dx=11, lean=-12, head=10, la=-80, lf=-110, ra=20, rf=10, ll=-8, ls=-8, rl=8, rs=8)),
  ]),
  ("highfive", "High five", 350, [
      dict(dx=-20, la=-15, lf=-10, ra=100, rf=150, ll=-10, ls=-8, rl=10, rs=8,
           second=dict(dx=20, la=-100, lf=-150, ra=15, rf=10, ll=-10, ls=-8, rl=10, rs=8)),
      dict(dx=-18, la=-15, lf=-10, ra=130, rf=165, ll=-10, ls=-8, rl=10, rs=8,
           second=dict(dx=18, la=-130, lf=-165, ra=15, rf=10, ll=-10, ls=-8, rl=10, rs=8)),
      dict(dx=-16, lean=3, la=-15, lf=-10, ra=145, rf=175, ll=-10, ls=-8, rl=10, rs=8,
           second=dict(dx=16, lean=-3, la=-145, lf=-175, ra=15, rf=10, ll=-10, ls=-8, rl=10, rs=8)),
      dict(dx=-18, la=-15, lf=-10, ra=130, rf=165, ll=-10, ls=-8, rl=10, rs=8,
           second=dict(dx=18, la=-130, lf=-165, ra=15, rf=10, ll=-10, ls=-8, rl=10, rs=8)),
  ]),
  ("bow", "Taking a bow", 500, [
      dict(la=-15, lf=-10, ra=15, rf=10, ll=-10, ls=-8, rl=10, rs=8),
      dict(lean=40, head=-10, la=-20, lf=-10, ra=0, rf=-10, ll=-10, ls=-8, rl=10, rs=8),
      dict(lean=80, head=-20, la=-30, lf=-10, ra=-10, rf=-5, ll=-10, ls=-8, rl=10, rs=8),
      dict(lean=40, head=-10, la=-20, lf=-10, ra=0, rf=-10, ll=-10, ls=-8, rl=10, rs=8),
  ]),
  ("dj", "DJ at the decks", 260, [
      dict(dy=-6, head=-12, la=-130, lf=-60, ra=35, rf=70, ll=-10, ls=-8, rl=10, rs=8, shapes=[("line", -30, 22, 50, 22, 4), ("circle", 32, 16, 9, 3)]),
      dict(dy=-6, head=12,  la=-130, lf=-60, ra=45, rf=85, ll=-10, ls=-8, rl=10, rs=8, shapes=[("line", -30, 22, 50, 22, 4), ("circle", 32, 16, 9, 3)]),
      dict(dy=-6, head=-12, la=-130, lf=-60, ra=25, rf=60, ll=-10, ls=-8, rl=10, rs=8, shapes=[("line", -30, 22, 50, 22, 4), ("circle", 32, 16, 9, 3)]),
      dict(dy=-6, head=12,  la=-120, lf=-50, ra=45, rf=80, ll=-10, ls=-8, rl=10, rs=8, shapes=[("line", -30, 22, 50, 22, 4), ("circle", 32, 16, 9, 3)]),
  ]),
  ("jump", "Jumping", 180, [
      dict(dy=10, lean=12, la=40,  lf=70,  ra=20,  rf=50,  ll=-45, ls=30, rl=45, rs=-30),
      dict(dy=-10, la=-160, lf=-170, ra=160, rf=170, ll=-12, ls=-8, rl=12, rs=8),
      dict(dy=-24, la=-165, lf=-175, ra=165, rf=175, ll=-60, ls=40, rl=60, rs=-40),
      dict(dy=-10, la=-150, lf=-160, ra=150, rf=160, ll=-15, ls=-10, rl=15, rs=10),
      dict(dy=10, lean=12, la=40,  lf=70,  ra=20,  rf=50,  ll=-45, ls=30, rl=45, rs=-30),
  ]),
  ("sunsalute", "Sun salutation", 900, [
      dict(la=-12, lf=-8, ra=12, rf=8, ll=-8, ls=-8, rl=8, rs=8),
      dict(la=-170, lf=-175, ra=170, rf=175, ll=-8, ls=-8, rl=8, rs=8),
      dict(lean=85, head=-30, la=-10, lf=0, ra=10, rf=0, ll=-8, ls=-8, rl=8, rs=8),
      dict(lean=90, head=-45, dy=-14, la=0, lf=0, ra=5, rf=0, ll=-100, ls=-100, rl=-100, rs=-100),
      dict(lean=55, head=-30, dy=-6, la=0, lf=0, ra=5, rf=0, ll=-110, ls=-105, rl=-110, rs=-105),
      dict(lean=125, head=20, dy=-20, la=0, lf=0, ra=5, rf=0, ll=-150, ls=-160, rl=-150, rs=-160),
  ]),
  ("bike", "Riding a bike", 160, [
      dict(dy=-8, lean=35, head=-20, la=55, lf=85, ra=50, rf=80, ll=-25, ls=25, rl=35, rs=0, shapes="bike"),
      dict(dy=-8, lean=35, head=-20, la=55, lf=85, ra=50, rf=80, ll=5, ls=45, rl=25, rs=-25, shapes="bike"),
      dict(dy=-8, lean=35, head=-20, la=55, lf=85, ra=50, rf=80, ll=35, ls=0, rl=-25, rs=25, shapes="bike"),
      dict(dy=-8, lean=35, head=-20, la=55, lf=85, ra=50, rf=80, ll=25, ls=-25, rl=5, rs=45, shapes="bike"),
  ]),
  ("dancing", "Dancing", 220, [
      dict(lean=-10, la=-150, lf=-170, ra=70, rf=30, ll=-25, ls=-15, rl=20, rs=15),
      dict(lean=10, la=-70, lf=-30, ra=150, rf=170, ll=-20, ls=-15, rl=45, rs=-35),
      dict(lean=-6, la=-165, lf=-175, ra=165, rf=175, ll=-35, ls=25, rl=35, rs=-25),
      dict(lean=8, la=-100, lf=-165, ra=100, rf=165, ll=-50, ls=40, rl=15, rs=15),
      dict(lean=-12, la=-140, lf=-60, ra=-40, rf=-100, ll=-15, ls=-10, rl=55, rs=-70),
      dict(lean=14, la=40, lf=100, ra=140, rf=60, ll=-55, ls=70, rl=10, rs=10),
  ]),
]
_BIKE = [("circle", -26, 40, 14, 3), ("circle", 26, 40, 14, 3), ("line", -26, 40, 0, 40, 3), ("line", 0, 40, 26, 40, 3),
         ("line", -26, 40, 0, 0, 3), ("line", 0, 0, 24, 8, 3), ("line", 24, 8, 26, 40, 3), ("line", 20, 4, 34, 2, 3)]

# A filled disc crossing the light from fully outside to fully outside: (id, name, ms per step,
# radius in px of the old FACE-px light, axis). The old sprites had 24 steps; here the disc
# glides between them.
DISCS = [
  ("eclipse-small-lr", "Eclipse, small, left to right", 65, 22, "x"),
  ("eclipse-small-tb", "Eclipse, small, top to bottom", 65, 22, "y"),
  ("eclipse-lr",       "Eclipse, full, left to right",  65, 64, "x"),
  ("eclipse-tb",       "Eclipse, full, top to bottom",  65, 64, "y"),
]
DISC_STEPS = 24

BY_ID = {s[0]: s for s in SPRITES}
DISC_BY_ID = {d[0]: d for d in DISCS}
OPTIONS = [{"value": s[0], "label": s[1]} for s in SPRITES] + [{"value": d[0], "label": d[1]} for d in DISCS]

_BIG = 4                 # the old generator's scale (px per figure unit) and canvas: the union box is measured there
_CANVAS = 640
_SS = 8                  # oversampling of the final drawing


def _pt(x, y, ang, length):
    a = math.radians(ang)
    return (x + length * math.sin(a), y + length * math.cos(a))


def _draw_figure(d: ImageDraw.ImageDraw, pose: dict, hip, k: float) -> None:
    t = max(1, round(7 * k))
    lean = pose.get("lean", 0)
    neck = _pt(hip[0], hip[1], 180 + lean, 30 * k)
    head = _pt(neck[0], neck[1], 180 + lean + pose.get("head", 0), 11 * k)
    d.line([hip, neck], fill=255, width=max(1, round(10 * k)))
    r = 7.5 * k
    d.ellipse([head[0] - r, head[1] - r, head[0] + r, head[1] + r], fill=255)

    def limb(origin, a1, l1, a2, l2):
        j = _pt(origin[0], origin[1], a1, l1 * k)
        e = _pt(j[0], j[1], a2, l2 * k)
        d.line([origin, j], fill=255, width=t)
        d.line([j, e], fill=255, width=t)
        for q in (origin, j, e):
            d.ellipse([q[0] - t / 2, q[1] - t / 2, q[0] + t / 2, q[1] + t / 2], fill=255)

    sh = _pt(neck[0], neck[1], lean, 4 * k)
    limb(sh, pose.get("la", -20), 17, pose.get("lf", -20), 15)
    limb(sh, pose.get("ra", 20), 17, pose.get("rf", 20), 15)
    limb(hip, pose.get("ll", -8), 20, pose.get("ls", -8), 19)
    limb(hip, pose.get("rl", 8), 20, pose.get("rs", 8), 19)


def _draw_pose(size, pose: dict, ox: float, oy: float, k: float) -> Image.Image:
    """One frame on an L canvas: (ox, oy) = where the main figure's hip goes before dx/dy, k = px per unit."""
    img = Image.new("L", size, 0)
    d = ImageDraw.Draw(img)
    _draw_figure(d, pose, (ox + pose.get("dx", 0) * k, oy + pose.get("dy", 0) * k), k)
    if "second" in pose:
        q = pose["second"]
        _draw_figure(d, q, (ox + q.get("dx", 0) * k, oy + q.get("dy", 0) * k), k)
    shapes = _BIKE if pose.get("shapes") == "bike" else pose.get("shapes", [])
    for s in shapes:
        if s[0] == "circle":
            _, cx, cy, r, w = s
            d.ellipse([ox + (cx - r) * k, oy + (cy - r) * k, ox + (cx + r) * k, oy + (cy + r) * k],
                      outline=255, width=max(1, round(w * k)))
        elif s[0] == "disc":
            _, cx, cy, r = s
            d.ellipse([ox + (cx - r) * k, oy + (cy - r) * k, ox + (cx + r) * k, oy + (cy + r) * k], fill=255)
        elif s[0] == "line":
            _, x1, y1, x2, y2, w = s
            d.line([(ox + x1 * k, oy + y1 * k), (ox + x2 * k, oy + y2 * k)], fill=255, width=max(1, round(w * k)))
    return img


def render_frames(sprite_id: str, width: int, height: int) -> list[np.ndarray]:
    """Every frame of a figure as uint8 coverage (height x width), centred in the box."""
    _, _, _, poses = BY_ID[sprite_id]
    # The old frame for a FACE-px light was 64 x 96 px, centred in the light.
    fw, fh = width // 2, width * 3 // 4
    ox0, oy0 = _CANVAS / 2, _CANVAS * 0.6
    boxes = [_draw_pose((_CANVAS, _CANVAS), p, ox0, oy0, _BIG).getbbox() for p in poses]
    boxes = [b for b in boxes if b]
    if not boxes:
        return [np.zeros((height, width), np.uint8)]
    x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
    x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
    scale = min((fw - 2) / (x1 - x0), (fh - 2) / (y1 - y0))     # final px per old canvas px
    tw, th = max(1, round((x1 - x0) * scale)), max(1, round((y1 - y0) * scale))
    left = (width - fw) // 2 + (fw - tw) // 2
    top = (height - fh) // 2 + (fh - th) // 2
    k = _BIG * scale * _SS
    out = []
    for p in poses:
        big = _draw_pose((tw * _SS, th * _SS), p, (ox0 - x0) * scale * _SS, (oy0 - y0) * scale * _SS, k)
        small = np.asarray(big.reduce(_SS), dtype=np.uint8)
        frame = np.zeros((height, width), np.uint8)
        xa, ya = max(left, 0), max(top, 0)
        xb, yb = min(left + tw, width), min(top + th, height)
        if xa < xb and ya < yb:
            frame[ya:yb, xa:xb] = small[ya - top:yb - top, xa - left:xb - left]
        out.append(frame)
    return out


_FRAMES: dict[tuple[str, int, int], list[np.ndarray]] = {}
_FRAMES_LOCK = threading.Lock()


def frames(sprite_id: str, width: int, height: int) -> list[np.ndarray]:
    """render_frames, cached. The arrays are shared: do not write to them."""
    key = (sprite_id, width, height)
    with _FRAMES_LOCK:
        f = _FRAMES.get(key)
    if f is None:
        f = render_frames(sprite_id, width, height)
        with _FRAMES_LOCK:
            _FRAMES[key] = f
    return f


class DiscField:
    """Distances from every pixel centre, for drawing the eclipse disc at any position."""

    def __init__(self, width: int, height: int):
        self.x = np.arange(width, dtype=np.float32) + 0.5
        self.y = np.arange(height, dtype=np.float32) + 0.5

    def disc(self, cx: float, cy: float, r: float) -> np.ndarray:
        dx2 = (self.x - cx) ** 2
        dy2 = (self.y - cy) ** 2
        d = np.sqrt(dy2[:, None] + dx2[None, :])
        return np.rint(np.clip(r + 0.5 - d, 0.0, 1.0) * 255).astype(np.uint8)


def disc_position(disc_id: str, ms: float, width: int, height: int) -> tuple[float, float, float]:
    """(cx, cy, r) of an eclipse disc `ms` into its cycle: step i of the old sprite at i x its ms,
    gliding in between; the last step (fully outside) holds until the cycle restarts."""
    _, _, step_ms, radius, axis = DISC_BY_ID[disc_id]
    r = radius * width / config.FACE
    p = min((ms / step_ms) % DISC_STEPS, DISC_STEPS - 1) / (DISC_STEPS - 1)
    span = width if axis == "x" else height
    c = -r + p * (span + 2 * r)
    return (c, height / 2, r) if axis == "x" else (width / 2, c, r)
