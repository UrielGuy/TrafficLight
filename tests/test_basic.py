"""render/basic: registration, catalog entries and the light renderer."""
import time

import numpy as np
import pytest

from trafficlight import config, model, render
from trafficlight.render import get_renderer
from trafficlight.render.base import RenderContext, RenderEnv
from trafficlight.render.color import parse_hex

ENV = RenderEnv(settings=model.Settings())


def ctx(t=0.0, face=0, all_=False, bpm=120.0, seed=1):
    return RenderContext(t=t, clip_duration=4.0, set_time=t, bpm=bpm, beat=t * bpm / 60, clip_beat=t * bpm / 60,
                         width=config.W, height=config.H if all_ else config.FACE,
                         face=None if all_ else face,
                         face_color=(255, 255, 255) if all_ else (255, 0, 0), seed=seed)


def test_registered_with_catalog_entries():
    entries = {c["type"]: c for c in render.catalog()}
    assert entries["light"]["category"] == "basic" and entries["light"]["targets"] == ["face", "all"]
    assert entries["text"]["category"] == "text" and entries["text"]["targets"] == ["face"]
    assert entries["figure"]["category"] == "figure" and entries["figure"]["targets"] == ["face"]
    text_params = {p["key"]: p for p in entries["text"]["params"]}
    assert text_params["font"]["kind"] == "select"
    fonts = [o["value"] for o in text_params["font"]["options"]]
    assert fonts[0] == "auto"
    for stem in ("High-School", "Reisinger-Michal-Regular", "heshbon-bold-alefalefalef", "heshbon-regular-alefalefalef"):
        assert stem in fonts
    assert text_params["bg"]["default"] == "face" and text_params["ink"]["default"] == "#000000"
    fig = {p["key"]: p for p in entries["figure"]["params"]}["figure"]
    ids = [o["value"] for o in fig["options"]]
    for sid in ("standing", "ready", "breakdance", "walking", "running", "hug", "highfive", "bow", "dj", "jump",
                "sunsalute", "bike", "dancing", "eclipse-small-lr", "eclipse-small-tb", "eclipse-lr", "eclipse-tb"):
        assert sid in ids


def test_light_on_a_face_fills_the_box():
    r = get_renderer("light")({}, ENV)
    f = r.render(ctx())
    assert f.shape == (config.FACE, config.FACE, 3) and f.dtype == np.uint8
    assert (f == (255, 0, 0)).all()
    half = get_renderer("light")({"color": "#0000ff", "brightness": 0.5}, ENV).render(ctx())
    assert tuple(half[0, 0]) == (0, 0, 128)


def test_light_hue_jump_per_beat():
    r = get_renderer("light")({"color": "#ff0000", "hue": 120}, ENV)
    assert tuple(r.render(ctx(t=0.0))[5, 5]) == (255, 0, 0)
    assert tuple(r.render(ctx(t=0.5))[5, 5]) == (0, 255, 0)     # one beat at 120 bpm


def test_light_on_all_draws_each_light_in_its_own_colour():
    f = get_renderer("light")({}, ENV).render(ctx(all_=True))
    assert f.shape == (config.H, config.W, 4)
    c = config.FACE // 2
    for i, hexcol in enumerate(ENV.settings.face_colors):
        px = f[i * config.FACE + c, c]
        assert px[3] == 255 and tuple(px[:3]) == parse_hex(hexcol)
        assert f[i * config.FACE, 0, 3] == 0                    # the corner is outside the disc
    white = get_renderer("light")({"color": "#ffffff"}, ENV).render(ctx(all_=True))
    assert (white[..., :3][white[..., 3] > 0] == 255).all()


@pytest.mark.parametrize("params", [{"color": None}, {"color": "nonsense", "hue": "x", "brightness": "bright"},
                                    {"color": 12, "brightness": -3}])
def test_light_bad_params_do_not_raise(params):
    for c in (ctx(), ctx(all_=True)):
        f = get_renderer("light")(params, ENV).render(c)
        assert f.shape[:2] == (c.height, c.width)


def test_light_speed():
    r = get_renderer("light")({"hue": 30}, ENV)
    for all_ in (False, True):
        r.render(ctx(all_=all_))
        n = 200
        t0 = time.perf_counter()
        for i in range(n):
            r.render(ctx(t=i / 60, all_=all_))
        assert (time.perf_counter() - t0) / n < 0.002
