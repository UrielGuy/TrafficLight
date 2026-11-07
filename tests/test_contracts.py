"""The shared contracts import and behave on both platforms."""
import numpy as np

from trafficlight import config, model, render
from trafficlight.audio.base import NullAudio
from trafficlight.output.base import NullDriver, create_driver
from trafficlight.render import color
from trafficlight.render.base import ClipRenderer, ParamSpec, RenderContext, RenderEnv


def ctx(**kw):
    d = dict(t=0.0, clip_duration=4.0, set_time=0.0, bpm=120.0, beat=0.0, clip_beat=0.0,
             width=config.FACE, height=config.FACE, face=0, face_color=(255, 0, 0), seed=1)
    d.update(kw)
    return RenderContext(**d)


def test_geometry():
    assert config.H == config.FACES * config.FACE == config.PANELS * config.PANEL_H
    assert config.W == config.FACE == config.PANEL_W


def test_set_round_trip():
    s = model.Set(name="x", tracks=model.default_tracks())
    s.tracks[1].clips.append(model.Clip(type="light", params={"color": "face"}))
    again = model.Set.model_validate_json(s.model_dump_json())
    assert again == s
    assert [t.target for t in again.tracks] == ["all", "face0", "face1", "face2", "audio"]


def test_renderer_defaults_and_catalog():
    class Solid(ClipRenderer):
        type = "test:solid"
        label = "Solid"
        params = [ParamSpec("color", "Colour", "color", "face")]

        def render(self, c):
            out = np.empty((c.height, c.width, 3), np.uint8)
            out[:] = color.resolve(self.p["color"], c)
            return out

    r = Solid({}, RenderEnv(settings=model.Settings()))
    frame = r.render(ctx())
    assert frame.shape == (config.FACE, config.FACE, 3) and tuple(frame[0, 0]) == (255, 0, 0)
    assert Solid.describe()["params"][0]["kind"] == "color"
    assert isinstance(render.catalog(), list)


def test_colors():
    assert color.parse_hex("#0f8") == (0, 255, 136)
    assert color.hue_rotate((255, 0, 0), 120) == (0, 255, 0)
    c = ctx(seed=7)
    assert color.resolve("random", c, "a") == color.resolve("random", c, "a")
    assert color.resolve("random", c, "a") != (0, 0, 0)
    assert color.resolve("#ff0000", ctx(clip_beat=2.5), hue_per_beat=120) == (0, 0, 255)


def test_null_backends():
    assert isinstance(create_driver(model.OutputSettings(driver="none")), NullDriver)
    NullAudio().sync([], True)
