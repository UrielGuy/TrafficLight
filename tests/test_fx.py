"""Beat animations and the clocks: registration, shapes, determinism, timing properties, speed."""
import datetime
import hashlib
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


# ---------------------------------------------------------------- no regression for opaque colours
GOLDEN_VARIANTS = ({}, {"color": "#3070ff", "ink": "#ffe000", "bg": "#101040", "hue_color": 30})
# Recorded before the transparent colours were added; identical on Windows and Linux. The clocks
# are not hashed: sin/cos differ in the last bit between platforms, so their pixel sums are compared.
GOLDEN = {
    "fx:quarters": "0e67d6b3e7db9b4b",
    "fx:pulse": "198ec167461144d7",
    "fx:wave": "5ed19b7af535fdd0",
    "fx:ecg": "8f4e1f325f69fe99",
    "fx:ecgsweep": "b0ac92ee4b642cb6",
    "fx:heart": "9bc7605e2af7aebb",
    "fx:ripples": "54c45df83278534f",
    "fx:eq": "63473145da782a64",
    "fx:chase": "155e9ee42a003ab9",
    "fx:ball": "c3f7848dfae77e85",
    "fx:sparkle": "43790bed4607b0b2",
    "fx:fillv": "1cb04c7e2f718959",
    "fx:fillx": "7e92d24fd20fbfb5"
}
GOLDEN_SUMS = {
    "fx:clock": [2762032, 2762032, 2762032, 2714594, 7154672, 7154672, 6894750, 8007560],
    "clock": [2718662, 2718662, 2718662, 2718662, 7108232, 7108232, 7108232, 7108232],
}


@pytest.mark.parametrize("typ", sorted(GOLDEN))
def test_opaque_pictures_are_unchanged(typ):
    h = hashlib.sha1()
    for p in GOLDEN_VARIANTS:
        for beat in (0.0, 0.37, 1.0, 2.6):
            h.update(draw(typ, beat, p).tobytes())
    if typ in ALL_TARGET:
        h.update(make(typ).render(ctx(1.3, face=None, w=config.W, h=config.H)).tobytes())
    assert h.hexdigest()[:16] == GOLDEN[typ]


@pytest.mark.parametrize("typ", sorted(GOLDEN_SUMS))
def test_opaque_clocks_are_unchanged(typ):
    sums = [int(draw(typ, b, p).astype(np.int64).sum()) for p in GOLDEN_VARIANTS for b in (0.0, 0.37, 1.0, 2.6)]
    assert np.allclose(sums, GOLDEN_SUMS[typ], rtol=2e-3)


# ---------------------------------------------------------------- transparent colours
# The param that is the background, and the others that give a visible, background-independent picture.
SEE_THROUGH = {
    "fx:quarters": ("color", {}),
    "fx:pulse": ("bg", {"color": "#3070ff"}), "fx:heart": ("bg", {"color": "#3070ff"}),
    "fx:eq": ("bg", {"color": "#ff00ff"}),
    "fx:sparkle": ("color", {"multicolor": False, "ink": "#ffe000"}),
}
for _t in ("fx:wave", "fx:clock", "fx:ecg", "fx:ecgsweep", "fx:ripples", "fx:chase", "fx:ball", "fx:fillv",
           "fx:fillx", "clock"):
    SEE_THROUGH[_t] = ("color", {"ink": "#ffe000", "second_color": "#00c0ff"} if _t == "clock" else {"ink": "#ffe000"})


def over(frame, bg):
    """RGBA straight alpha over a flat colour -> RGB."""
    a = frame[..., 3:].astype(np.float64) / 255.0
    return frame[..., :3] * a + np.asarray(bg, np.float64) * (1.0 - a)


def test_every_effect_offers_a_transparent_colour():
    seen = set()
    for cls in render.all_renderers():
        if not (cls.type.startswith("fx:") or cls.type == "clock"):
            continue
        seen.add(cls.type)
        flagged = [s.key for s in cls.params if s.kind == "color" and s.transparent]
        assert flagged, cls.type
        r = cls({flagged[0]: "transparent"}, ENV)
        f = r.render(ctx(0.4, face=None, w=config.W, h=config.H) if cls.type in ALL_TARGET else ctx(0.4))
        assert f.dtype == np.uint8 and f.shape[-1] == 4, cls.type
        assert (f[..., 3] == 0).any() and (f[..., 3] == 255).any(), cls.type
    assert seen == set(ALL_TYPES)


def test_transparent_flag_in_catalog():
    cat = {e["type"]: {p["key"]: p for p in e["params"]} for e in render.catalog()}
    wanted = {"fx:quarters": {"color", "color2", "color3", "color4"}, "fx:eq": {"color", "bg"},
              "fx:pulse": {"bg"}, "fx:heart": {"bg"}}
    for typ in ALL_TYPES:
        flagged = {k for k, p in cat[typ].items() if p.get("transparent")}
        assert flagged == wanted.get(typ, {"color"}), typ
        assert all(cat[typ][k]["kind"] == "color" for k in flagged)


@pytest.mark.parametrize("typ", sorted(SEE_THROUGH))
def test_transparent_background_composites_to_the_opaque_picture(typ):
    key, extra = SEE_THROUGH[typ]
    inked = False
    for beat in (0.0, 0.37, 1.0, 2.6):
        t = draw(typ, beat, {**extra, key: "transparent"})
        assert t.dtype == np.uint8 and t.shape == (config.FACE, config.FACE, 4), typ
        assert (t[..., 3] == 0).any(), (typ, beat)                       # the background is not drawn
        inked |= bool((t[..., 3] > 0).any())                             # (a fill is still empty on the first beat)
        for bg in ((0, 0, 0), (255, 255, 255)):
            o = draw(typ, beat, {**extra, key: "#%02x%02x%02x" % bg})
            assert o.shape == (config.FACE, config.FACE, 3)
            err = np.abs(over(t, bg) - o).max()
            assert err <= 2.0, (typ, beat, bg, err)
    assert inked, typ


@pytest.mark.parametrize("typ", sorted(ALL_TARGET))
def test_transparent_whole_sign(typ):
    key, extra = SEE_THROUGH[typ]
    f = make(typ, **extra, **{key: "transparent"}).render(ctx(1.3, face=None, w=config.W, h=config.H))
    assert f.shape == (config.H, config.W, 4) and f.dtype == np.uint8 and (f[..., 3] == 0).any() and f[..., 3].any()


def test_transparent_where_not_allowed_is_an_invalid_colour():
    g = draw("fx:wave", 0.5, {"ink": "transparent"})                  # an ink colour
    assert g.shape[-1] == 3 and np.array_equal(g, draw("fx:wave", 0.5, {"ink": "bogus"}))
    assert np.array_equal(draw("fx:pulse", 0.5, {"color": "transparent"}), draw("fx:pulse", 0.5, {"color": "bogus"}))


def test_transparent_circle_ink_does_not_depend_on_the_circle():
    # With an opaque circle the ink must differ from it (swapped when equal); with none, black ink stays black.
    f = draw("fx:wave", 0.0, {"color": "transparent", "ink": "#000000"})
    solid = f[..., 3] > 200
    assert solid.any() and not f[solid][:, :3].any()
    assert (draw("fx:wave", 0.0, {"color": "transparent", "ink": "random"})[..., 3] > 200).any()


def test_transparent_alpha_is_coverage():
    f = draw("fx:pulse", 0.0, {"bg": "transparent", "color": "#ff0000"})        # a full disc
    a = f[..., 3]
    assert a[C, C] == 255 and a[0, 0] == 0
    assert ((a > 0) & (a < 255)).sum() > 100                              # anti-aliased rim
    assert abs(a.astype(float).sum() / 255.0 - math.pi * R * R) < 0.02 * math.pi * R * R
    assert (np.abs(f[a > 0][:, :3].astype(int) - RED).max()) <= 1         # no halo: edge pixels keep the colour


def test_transparent_quarter_shows_what_is_below():
    f = draw("fx:quarters", 0.5, {"color": "transparent", "color2": "#00ff00", "color3": "#0000ff", "color4": "#ff0000"})
    assert f[C - 40, C - 40, 3] == 0 and f[C - 40, C + 40, 3] == 255 and f[C + 40, C + 40, 3] == 255
    assert tuple(f[C - 40, C + 40, :3]) == (0, 255, 0)


def test_transparent_clock_pin_is_punched_out():
    f = draw("fx:clock", 0.0, {"color": "transparent"})
    assert f[C, C, 3] == 0
    assert f[C, C + int(10 * R / 128), 3] == 255                          # the hub ring around the pin


def test_transparent_unlit_bulbs_are_partly_see_through():
    f = draw("fx:chase", 0.5, {"color": "transparent", "unlit": 30})          # one bulb lit
    cx, cy = C + 100 * R / 128 * math.sin(math.radians(60)), C - 100 * R / 128 * math.cos(math.radians(60))
    assert abs(int(f[int(cy), int(cx), 3]) - 77) <= 1                     # 30 % of 255
    assert f[C - int(100 * R / 128), C, 3] == 255


def test_transparent_eq_rim_leaves_outside_empty():
    f = draw("fx:eq", 0.0, {"color": "transparent", "bg": "#000000"})
    assert f[0, 0, 3] == 0 and f[C, C, 3] == 255


def test_transparent_speed():
    # The see-through path has four channels instead of three but must stay in the same class.
    for typ, (key, extra) in SEE_THROUGH.items():
        opaque, clear = make(typ, **extra), make(typ, **extra, **{key: "transparent"})
        times = []
        for r in (opaque, clear):
            r.render(ctx(0.0))
            t0 = time.perf_counter()
            for i in range(20):
                r.render(ctx(i * 0.137))
            times.append((time.perf_counter() - t0) / 20 * 1000)
        assert times[1] < max(4.0 * times[0], 3.0), (typ, times)
