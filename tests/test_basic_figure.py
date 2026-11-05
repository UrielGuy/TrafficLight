"""render/basic: the figure clip (the old sign's pictograms and eclipse discs)."""
import time

import numpy as np
import pytest

from trafficlight import config, model
from trafficlight.render import get_renderer
from trafficlight.render.base import RenderContext, RenderEnv
from trafficlight.render.basic import figures

ENV = RenderEnv(settings=model.Settings())


def ctx(t=0.0, face=1):
    return RenderContext(t=t, clip_duration=4.0, set_time=t, bpm=120.0, beat=t * 2, clip_beat=t * 2,
                         width=config.FACE, height=config.FACE, face=face, face_color=(255, 176, 0), seed=1)


def ink(frame):
    return (frame[..., :3] == 0).all(axis=2)


@pytest.mark.parametrize("sid", [s[0] for s in figures.SPRITES])
def test_every_figure_animates_at_the_old_pace(sid):
    _, _, ms, poses = figures.BY_ID[sid]
    r = get_renderer("figure")({"figure": sid}, ENV)
    shots = [r.render(ctx(t=(k * ms + ms / 2) / 1000)) for k in range(len(poses))]
    for f in shots:
        assert f.shape == (config.FACE, config.FACE, 3) and ink(f).sum() > 300
        assert len(np.unique(f.reshape(-1, 3), axis=0)) > 5                # anti-aliased edges
    for a, b in zip(shots, shots[1:]):
        assert (a != b).any()                                               # a new pose every `ms`
    again = r.render(ctx(t=(len(poses) * ms + ms / 2) / 1000))
    assert (again == shots[0]).all()                                        # and round again
    within = r.render(ctx(t=(ms * 0.9) / 1000))
    assert (within == shots[0]).all()


def test_figures_fit_the_old_frame_box():
    for sid, _, _, _ in figures.SPRITES:
        for a in figures.frames(sid, config.FACE, config.FACE):
            rows, cols = np.flatnonzero(a.any(axis=1)), np.flatnonzero(a.any(axis=0))
            assert cols[0] >= config.FACE // 4 - 1 and cols[-1] <= config.FACE * 3 // 4
            assert rows[0] >= config.FACE // 8 - 1 and rows[-1] <= config.FACE * 7 // 8


def test_eclipse_crosses_the_light():
    r = get_renderer("figure")({"figure": "eclipse-small-lr"}, ENV)
    xs = []
    for k in range(1, 23):
        f = ink(r.render(ctx(t=k * 65 / 1000)))
        cols = np.flatnonzero(f.any(axis=0))
        xs.append(cols.mean())
    assert all(b > a for a, b in zip(xs, xs[1:]))                           # left to right
    assert not ink(r.render(ctx(t=0.0))).any()                               # starts outside
    tb = ink(get_renderer("figure")({"figure": "eclipse-tb"}, ENV).render(ctx(t=12 * 65 / 1000)))
    c = config.FACE // 2
    assert tb[c, c]


def test_colours_and_transparency():
    F = get_renderer("figure")
    f = F({"figure": "standing", "bg": "#0000ff", "ink": "#ffffff"}, ENV).render(ctx())
    assert tuple(f[0, 0]) == (0, 0, 255) and ((f == 255).all(axis=2)).sum() > 300
    rgba = F({"figure": "walking", "ink": "#ff00ff", "transparent": True}, ENV).render(ctx())
    assert rgba.shape == (config.FACE, config.FACE, 4)
    assert rgba[0, 0, 3] == 0 and rgba[..., 3].max() == 255 and (rgba[..., :3] == (255, 0, 255)).all()
    hue = F({"figure": "standing", "bg": "#ff0000", "bg_hue": 120}, ENV)
    assert tuple(hue.render(ctx(t=0.5))[0, 0]) == (0, 255, 0)


def test_speed():
    F = get_renderer("figure")
    ms = figures.BY_ID["walking"][2]
    slow = F({"figure": "walking", "speed": 0.5}, ENV)
    normal = F({"figure": "walking"}, ENV)
    assert (slow.render(ctx(t=2 * ms / 1000 + 0.001)) == normal.render(ctx(t=ms / 1000 + 0.001))).all()
    frozen = F({"figure": "walking", "speed": 0}, ENV)
    assert (frozen.render(ctx(t=3.3)) == normal.render(ctx(t=0))).all()


def test_validate_and_bad_params():
    F = get_renderer("figure")
    assert F.validate({"figure": "dj"}, "face2", ENV) == []
    assert F.validate({"figure": "moonwalk"}, "face2", ENV)
    assert F.validate({"figure": "dj"}, "all", ENV)
    for p in ({"figure": "moonwalk"}, {"figure": None, "speed": "fast"}, {"figure": 3, "bg": 5, "ink_hue": "x"},
              {"speed": -2}):
        f = F(p, ENV).render(ctx(t=-1.0))
        assert f.shape[:2] == (config.FACE, config.FACE)


def test_frames_are_fast():
    for sid in ("breakdance", "eclipse-lr"):
        for transparent in (False, True):
            r = get_renderer("figure")({"figure": sid, "transparent": transparent, "ink_hue": 30}, ENV)
            r.render(ctx())
            n = 200
            t0 = time.perf_counter()
            for i in range(n):
                r.render(ctx(t=i / 60))
            assert (time.perf_counter() - t0) / n < 0.002
