"""Beat animations and the clocks: registration, shapes, determinism, timing properties, speed."""
import datetime
import math
import time

import numpy as np
import pytest

from trafficlight import config, model, render
from trafficlight.render.base import RenderContext, RenderEnv

ENV = RenderEnv(settings=model.Settings())
BPM = 120.0                      # a beat is half a second
BEAT_TYPES = ["fx:quarters", "fx:pulse", "fx:wave", "fx:clock", "fx:ecg", "fx:ecgsweep", "fx:heart",
              "fx:ripples", "fx:eq", "fx:chase", "fx:ball", "fx:sparkle", "fx:fillv", "fx:fillx"]
ALL_TARGET = {"fx:sparkle", "fx:ripples", "fx:eq", "fx:fillv", "fx:fillx"}
ALL_TYPES = BEAT_TYPES + ["clock"]
C = config.FACE // 2
R = config.FACE_RADIUS
RED = (255, 0, 0)


def ctx(beat=0.0, face=0, w=config.FACE, h=config.FACE, dur=4.0, color=RED, bpm=BPM, **kw):
    t = beat * 60.0 / bpm if bpm else 0.0
    d = dict(t=t, clip_duration=dur, set_time=t, bpm=bpm, beat=beat, clip_beat=beat, width=w, height=h,
             face=face, face_color=color, seed=3, wall_time=datetime.datetime(2026, 1, 1, 3, 0, 0))
    d.update(kw)
    return RenderContext(**d)


def make(typ, **params):
    return render.get_renderer(typ)(params, ENV)


def draw(typ, beat, params=None, **kw):
    return make(typ, **(params or {})).render(ctx(beat, **kw))


def is_color(frame, rgb, tol=40):
    return np.abs(frame[..., :3].astype(int) - np.array(rgb)).max(axis=-1) <= tol


def disc(w=config.FACE, h=config.FACE, k=0.9):
    y, x = np.mgrid[0:h, 0:w]
    return np.hypot(x + .5 - w / 2, (y + .5) % (h / max(1, round(h / w))) - (h / max(1, round(h / w))) / 2) < R * k


# ---------------------------------------------------------------- registry
def test_registered_with_valid_catalog():
    cat = {e["type"]: e for e in render.catalog()}
    for typ in ALL_TYPES:
        e = cat[typ]
        assert e["label"] and e["description"], typ
        assert e["category"] == ("basic" if typ == "clock" else "beat")
        assert "face" in e["targets"]
        assert (("all" in e["targets"]) == (typ in ALL_TARGET)), typ
        keys = [p["key"] for p in e["params"]]
        assert len(keys) == len(set(keys))
        for p in e["params"]:
            assert p["label"] and p["kind"] in ("number", "int", "bool", "color", "select", "string")
            if p["kind"] == "color":
                assert p["default"] in ("face", "random") or str(p["default"]).startswith("#")
            if "min" in p:
                assert p["min"] <= p["default"] <= p["max"], (typ, p["key"])


@pytest.mark.parametrize("typ", ALL_TYPES)
def test_shape_dtype_and_determinism(typ):
    r = make(typ)
    for beat in (0.0, 0.4, 1.0, 3.7):
        f = r.render(ctx(beat))
        assert f.dtype == np.uint8 and f.shape == (config.FACE, config.FACE, 3)
        assert np.array_equal(f, make(typ).render(ctx(beat)))        # same context -> same picture
        assert np.array_equal(f, r.render(ctx(beat)))                # and in any order
    a = r.render(ctx(0.4))
    a2 = r.render(ctx(0.4))
    a[:] = 0
    assert a2.any()                                                  # a fresh array every time


@pytest.mark.parametrize("typ", sorted(ALL_TARGET))
def test_whole_sign(typ):
    f = make(typ).render(ctx(1.3, face=None, w=config.W, h=config.H, color=(255, 255, 255)))
    assert f.shape == (config.H, config.W, 3) and f.dtype == np.uint8
    assert f.any()


@pytest.mark.parametrize("typ", ALL_TYPES)
def test_bad_params_never_raise(typ):
    junk = {"color": "bogus", "ink": 12, "color2": None, "hue_color": "x", "hue_ink": [], "amplitude": "a",
            "bars": -4, "count": 1e9, "minutes": 0, "thickness": float("nan"), "min_size": 1e9, "life": 0,
            "turn": -3, "tick": "no", "window": 0, "width": -2, "seconds": "x", "unlit": 999}
    r = make(typ, **junk)
    for beat in (0.0, 2.5, -1.0):
        assert r.render(ctx(beat)).shape == (config.FACE, config.FACE, 3)
    assert r.render(ctx(0.0, bpm=0.0)).dtype == np.uint8
    assert r.render(ctx(5.0, dur=0.0)).dtype == np.uint8


@pytest.mark.parametrize("typ", ALL_TYPES)
def test_colour_params(typ):
    f = draw(typ, 0.5, {"color": "random"})
    g = draw(typ, 0.5, {"color": "#00ff00"})
    assert f.shape == g.shape and not np.array_equal(f, g)


@pytest.mark.parametrize("typ", ALL_TYPES)
def test_speed(typ):
    r = make(typ)
    r.render(ctx(0.0))
    for kw in ({}, {"face": None, "w": config.W, "h": config.H}) if typ in ALL_TARGET else ({},):
        t0 = time.perf_counter()
        n = 30
        for i in range(n):
            r.render(ctx(i * 0.137, **kw))
        ms = (time.perf_counter() - t0) / n * 1000
        assert ms < 12.0, (typ, ms)       # the goal is ~2 ms; this only catches accidents on a slow CI box


# ---------------------------------------------------------------- per effect
def test_quarters_turn_30_degrees_per_beat():
    cols = ["#ff0000", "#00ff00", "#0000ff", "#ffff00"]
    r = make("fx:quarters", color=cols[0], color2=cols[1], color3=cols[2], color4=cols[3])
    probes = [15, 105, 195, 285]       # degrees clockwise from 12 o'clock, away from every edge at 30 deg steps
    rgbs = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
    for n in range(0, 14):
        f = r.render(ctx(n + 0.5))      # settled mid-beat
        for deg in probes:
            a = math.radians(deg)
            x, y = int(C + 40 * math.sin(a)), int(C - 40 * math.cos(a))
            expect = rgbs[int(((deg - 30 * n + 90 + 3600) % 360) // 90)]
            assert is_color(f[y:y + 1, x:x + 1], expect, 10).all(), (n, deg)
    assert not np.array_equal(r.render(ctx(0.5)), r.render(ctx(1.5)))
    # It is turning right after the beat: between the old and the new position.
    assert not np.array_equal(r.render(ctx(1.05)), r.render(ctx(1.5)))


def test_quarters_hue_jump_changes_per_beat():
    a = draw("fx:quarters", 0.5, {"hue_color": 90})
    b = draw("fx:quarters", 1.5, {"hue_color": 90})
    assert tuple(a[C - 40, C - 40]) != tuple(b[C - 40, C - 40])


def test_pulse_full_on_beat_smaller_mid_beat():
    full = (draw("fx:pulse", 0.0)[..., 0] > 128).sum()
    mid = (draw("fx:pulse", 0.5)[..., 0] > 128).sum()
    late = (draw("fx:pulse", 0.99)[..., 0] > 128).sum()
    assert abs(full - math.pi * R * R) < 0.02 * math.pi * R * R
    assert mid < full and late <= mid
    assert abs(late / full - 0.75 ** 2) < 0.03
    assert np.array_equal(draw("fx:pulse", 0.0), draw("fx:pulse", 1.0))      # periodic
    assert tuple(draw("fx:pulse", 0.0)[0, 0]) == (0, 0, 0)


def test_wave_swings_left_up_on_beat_one():
    def ink_rows(beat):
        f = draw("fx:wave", beat)
        col = (f[:, C // 2, 0] < 128) & (f[:, C // 2, 1] < 128)
        return np.where(col)[0]
    assert ink_rows(0.0).mean() < C - 8          # left half up on beat 1
    assert abs(ink_rows(0.5).mean() - C) < 2     # flat mid beat
    assert ink_rows(1.0).mean() > C + 8          # and down on beat 2
    assert ink_rows(2.0).mean() < C - 8          # up again on beat 3


def test_heart_full_on_beat_and_black_corners():
    full = (draw("fx:heart", 0.0)[..., 0] > 128).sum()
    mid = (draw("fx:heart", 0.5)[..., 0] > 128).sum()
    assert mid < full
    f = draw("fx:heart", 0.0)
    assert tuple(f[C, C]) == RED and tuple(f[2, 2]) == (0, 0, 0)
    assert tuple(f[config.FACE - 4, C]) == (0, 0, 0) or f[config.FACE - 4, C, 0] < 255   # heart point is above the rim


def test_clock_hands_advance_five_minutes_per_beat():
    def ink_at(f, x, y):
        return bool(f[y, x, 0] < 100)
    f = draw("fx:clock", 3.5)                                    # 3 beats = 15 minutes: minute hand at 3 o'clock
    assert ink_at(f, C + 25, C)
    assert ink_at(draw("fx:clock", 0.5), C, C - 30) and not ink_at(draw("fx:clock", 0.5), C + 30, C)
    assert ink_at(draw("fx:clock", 6.5), C, C + 30)              # 30 minutes: straight down
    assert not np.array_equal(draw("fx:clock", 1.02), draw("fx:clock", 1.5))      # still ticking over


def test_wall_clock_shows_real_time():
    def frame(h, m, s, **p):
        t = datetime.datetime(2026, 5, 5, h, m, s)
        return make("clock", **p).render(ctx(0.0, wall_time=t))
    dark = lambda f, x, y: f[y, x, 0] < 100
    f = frame(3, 0, 0, seconds=False)
    assert dark(f, C + 20, C) and dark(f, C, C - 45) and not dark(f, C - 25, C) and not dark(f, C, C + 25)
    f = frame(9, 30, 0, seconds=False)
    assert dark(f, C, C + 45) and dark(f, C - 20, C - 5)       # minute hand down, hour hand left
    assert np.array_equal(frame(3, 0, 0, seconds=False), frame(3, 0, 0, seconds=False))
    assert not np.array_equal(frame(3, 0, 0), frame(3, 0, 20))                 # the second hand moves
    s30 = frame(3, 0, 30, seconds=True)
    assert dark(s30, C, C + 50)
    assert not np.array_equal(frame(3, 0, 0, seconds=True), frame(3, 0, 0, seconds=False))
    now = make("clock").render(ctx(0.0, wall_time=datetime.datetime.now()))
    assert now.shape == (config.FACE, config.FACE, 3)
    assert frame(3, 0, 0, color="#ffffff", ink="#0000ff")[C, C - 50, 2] > 0


def _ink_cols(f):
    return np.where((f[..., 0] < 100).any(axis=0))[0]


def test_ecg_spike_in_the_middle_on_the_beat():
    f = draw("fx:ecg", 0.0)
    ink = f[..., 0] < 100
    top = np.where(ink.any(axis=0), ink.argmax(axis=0), 10 ** 6)
    spike = int(top.argmin())
    assert abs(np.where(top == top[spike])[0].mean() - C) <= 2
    assert C - top[spike] > 0.5 * R * 35 / 64 * 1.0                      # a tall R spike
    for b in range(1, 5):
        g = draw("fx:ecg", float(b))
        assert np.abs(g.astype(int) - f).max() <= 2                    # the same picture on every beat
    mid = draw("fx:ecg", 0.5)
    ink = mid[..., 0] < 100
    top = np.where(ink.any(axis=0), ink.argmax(axis=0), 10 ** 6)
    assert abs(int(top.argmin()) - C) > 20                           # the spike has moved away


def test_ecgsweep_window_reaches_the_spike_on_the_beat():
    f = draw("fx:ecgsweep", 0.0)
    cols = _ink_cols(f)
    assert cols.max() <= C + 3 and cols.min() >= C - 66              # nothing right of the leading edge
    g = draw("fx:ecgsweep", 0.25)
    assert _ink_cols(g).max() > C + 20                               # the window has swept on


def test_ripples_grow_each_beat():
    def outer(beat):
        f = draw("fx:ripples", beat)
        row = f[C, C:, 0] < 100
        return np.where(row)[0].max() if row.any() else 0
    assert outer(0.1) < outer(0.4) < outer(0.8)
    assert outer(0.8) < R
    inner = lambda b: np.where(draw("fx:ripples", b)[C, C:, 0] < 100)[0].min()
    assert inner(1.1) < 10 and inner(0.9) > 20                     # a new ring starts on the beat
    assert (draw("fx:ripples", 3.0)[C, C:, 0] < 100).any()


def test_ripples_whole_sign_has_a_ring_in_every_light():
    f = make("fx:ripples").render(ctx(0.8, face=None, w=config.W, h=config.H, color=(255, 255, 255)))
    a, b, c3 = (f[i * config.FACE:(i + 1) * config.FACE] for i in range(3))
    assert np.array_equal(a, b) and np.array_equal(b, c3)


def test_eq_bars_jump_on_the_beat_and_sink():
    def lit(beat, **p):
        f = draw("fx:eq", beat, p)
        return (f[..., 1:].max(axis=-1) > 60).sum()
    assert lit(0.0) > lit(0.9)
    assert np.array_equal(draw("fx:eq", 2.3), draw("fx:eq", 2.3))
    # more bars -> more distinct bar columns
    def columns(n):
        f = draw("fx:eq", 0.0, {"bars": n})
        on = (f[40:100, :, 1:].max(axis=-1) > 60).any(axis=0)
        return int(((on[1:].astype(int) - on[:-1].astype(int)) == 1).sum())
    assert columns(12) > columns(5)


def test_chase_lights_one_more_bulb_per_beat():
    centres = [(C + 100 * config.FACE_RADIUS / 128 * math.sin(math.radians(30 * k)),
                C - 100 * config.FACE_RADIUS / 128 * math.cos(math.radians(30 * k))) for k in range(12)]
    def lit(beat):
        f = draw("fx:chase", beat)
        return [bool(f[int(y), int(x), 0] < 60) for x, y in centres]
    for n in range(0, 14):
        got = lit(n + 0.5)
        want = (n + 1) % 13
        assert sum(got) == want, n
        assert got == [k < want for k in range(12)]                # clockwise from 12 o'clock


def test_ball_lands_on_the_beat():
    def top(beat):
        ink = draw("fx:ball", beat)[..., 0] < 100
        return np.where(ink[:, C - 8:C + 8].any(axis=1))[0].min()      # the ball's top edge
    assert top(0.5) < top(0.0) - 30              # high mid beat, low on the beat
    assert abs(top(1.0) - top(0.0)) < 2
    assert top(0.25) < top(0.0) and top(0.25) > top(0.5)
    wide = lambda beat: (draw("fx:ball", beat)[..., 0] < 100)[config.FACE - 40:config.FACE - 25].sum()
    assert wide(0.0) > wide(0.5)                 # squashed (wider) on the floor, only the floor line later


def test_sparkle_follows_time_not_beat():
    a = make("fx:sparkle").render(ctx(1.0, bpm=120.0))
    b = make("fx:sparkle").render(ctx(0.5, bpm=60.0))               # same t = 0.5 s
    assert np.array_equal(a, b)
    assert not np.array_equal(draw("fx:sparkle", 0.4), draw("fx:sparkle", 0.9))
    base = draw("fx:sparkle", 0.0, {"count": 3})
    busy = draw("fx:sparkle", 1.3, {"count": 60})
    assert (np.abs(busy.astype(int) - np.array(RED)).sum(axis=-1) > 60).sum() > 30
    assert base.shape == busy.shape
    assert not np.array_equal(make("fx:sparkle").render(ctx(0.7, face=0)), make("fx:sparkle").render(ctx(0.7, face=1)))


@pytest.mark.parametrize("typ", ["fx:fillv", "fx:fillx"])
def test_fills_build_up_and_complete_before_the_end(typ):
    dur = 4.0
    mask = disc()
    def left(t):
        f = make(typ).render(ctx(t * BPM / 60, t=t, dur=dur))
        return (is_color(f, RED, 10) & mask).sum()                 # circle colour still showing
    total = mask.sum()
    assert left(0.01) > 0.5 * total                                    # starts (almost) empty
    seen = [left(0.25 + 0.5 * k) for k in range(7)]                    # mid beat, every beat
    assert all(a >= b for a, b in zip(seen, seen[1:])) and seen[0] > seen[-1]
    assert left(dur - 0.05) == 0                                       # complete just before the end
    assert left(dur - 0.5) > 0                                         # but not a beat earlier
    # without a tempo everything comes at once, 100 ms before the end
    f = make(typ).render(ctx(0.0, bpm=0.0, t=dur - 0.2, dur=dur))
    assert (is_color(f, RED, 10) & mask).sum() == total
    f = make(typ).render(ctx(0.0, bpm=0.0, t=dur - 0.05, dur=dur))
    assert (is_color(f, RED, 10) & mask).sum() == 0


def test_fills_whole_sign_complete_before_the_end():
    f = make("fx:fillx").render(ctx(0, face=None, w=config.W, h=config.H, t=3.95, dur=4.0, color=(255, 255, 255)))
    assert (f[disc(config.W, config.H)] == 0).all()
    g = make("fx:fillx").render(ctx(0, face=None, w=config.W, h=config.H, t=1.2, dur=4.0, color=(255, 255, 255)))
    assert (g[disc(config.W, config.H)] > 0).any()
