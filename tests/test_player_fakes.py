"""Test renderers and fakes (driver, audio, media) shared by the core tests.

Imported by test_player/test_engine/test_server as `test_player_fakes`; the
renderers register under "test:*" types so they never clash with real ones.
"""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

from trafficlight import config
from trafficlight.audio.base import AudioDevice, AudioEngine
from trafficlight.model import MediaItem, Set, Track, Clip
from trafficlight.output.base import OutputDriver, OutputError
from trafficlight.render import color
from trafficlight.render.base import AudioCue, ClipRenderer, ParamSpec, register

LOG: list[tuple[str, str]] = []      # (event, clip id or type) from the test renderers
_log_lock = threading.Lock()


def note(event: str, who: str) -> None:
    with _log_lock:
        LOG.append((event, who))


@register
class Solid(ClipRenderer):
    type = "test:solid"
    label = "Solid"
    params = [ParamSpec("color", "Colour", "color", "#ffffff"), ParamSpec("tag", "Tag", "string", "")]

    def __init__(self, params, env):
        super().__init__(params, env)
        note("init", self.p["tag"])

    def render(self, ctx):
        out = np.empty((ctx.height, ctx.width, 3), np.uint8)
        out[:] = color.resolve(self.p["color"], ctx, "c")
        return out

    def close(self):
        note("close", self.p["tag"])

    @classmethod
    def validate(cls, params, target, env):
        return ["tag says bad"] if params.get("tag") == "bad" else []


@register
class HalfAlpha(ClipRenderer):
    type = "test:rgba"
    label = "Half alpha"
    params = [ParamSpec("color", "Colour", "color", "#ffffff")]

    def render(self, ctx):
        out = np.empty((ctx.height, ctx.width, 4), np.uint8)
        out[..., :3] = color.parse_hex(self.p["color"])
        out[..., 3] = 128
        return out


@register
class Boom(ClipRenderer):
    type = "test:boom"
    label = "Boom"
    params = [ParamSpec("where", "Where", "string", "render")]

    def __init__(self, params, env):
        super().__init__(params, env)
        if self.p["where"] == "init":
            raise RuntimeError("init boom")

    def render(self, ctx):
        if self.p["where"] == "render":
            raise RuntimeError("render boom")
        if self.p["where"] == "shape":
            return np.zeros((3, 3, 3), np.uint8)
        return None

    def audio(self, ctx):
        if self.p["where"] == "audio":
            raise RuntimeError("audio boom")
        return None


@register
class Sound(ClipRenderer):
    type = "test:sound"
    label = "Sound"
    targets = ("audio", "face", "all")
    params = [ParamSpec("media", "Media", "media", "", media_kind="audio")]

    def audio(self, ctx):
        return AudioCue(key="ignored", path=f"/sounds/{self.p['media']}.wav", position=ctx.t, gain=0.5)


@register
class TimeProbe(ClipRenderer):
    """Draws its ctx into pixel values so tests can read what the player passed."""
    type = "test:probe"
    label = "Probe"
    targets = ("face", "all")

    def render(self, ctx):
        out = np.zeros((ctx.height, ctx.width, 3), np.uint8)
        out[..., 0] = min(255, int(ctx.t * 10))
        out[..., 1] = ctx.seed % 256
        out[..., 2] = min(255, int(ctx.beat * 10))
        return out


@register
class SlowInit(ClipRenderer):
    type = "test:slow"
    label = "Slow"
    params = [ParamSpec("tag", "Tag", "string", "")]

    def __init__(self, params, env):
        super().__init__(params, env)
        note("init-thread:" + threading.current_thread().name, self.p["tag"])
        time.sleep(0.05)

    def render(self, ctx):
        return np.full((ctx.height, ctx.width, 3), 200, np.uint8)


def make_set(*clips_by_target: tuple[str, list[Clip]], duration: float = 10.0, name: str = "T", **kw) -> Set:
    tracks = [Track(name=t, target=t, clips=list(cs)) for t, cs in clips_by_target]
    return Set(name=name, duration=duration, tracks=tracks, **kw)


class FakeDriver(OutputDriver):
    instances: list["FakeDriver"] = []

    def __init__(self, settings=None, fail: bool = False):
        self.settings = settings
        self.fail = fail
        self.frames = 0
        self.last: Optional[np.ndarray] = None
        self.brightness: Optional[float] = None
        self.opened = self.closed = False
        FakeDriver.instances.append(self)

    def open(self):
        if self.fail:
            raise OutputError("no such interface")
        self.opened = True

    def send(self, frame):
        assert frame.shape == (config.H, config.W, 3) and frame.dtype == np.uint8
        frame[:] = 0          # a driver that works in place must not spoil the preview
        self.frames += 1

    def set_brightness(self, value):
        self.brightness = value

    def close(self):
        self.closed = True

    def status(self):
        return {"driver": "fake", "ok": True, "error": None, "interface": None}


class FakeAudio(AudioEngine):
    def __init__(self):
        self.cues: list[AudioCue] = []
        self.playing = True
        self.device = "unset"
        self.volume = None
        self.syncs = 0
        self.closed = False

    def list_devices(self):
        return [AudioDevice("dev1", "Speakers", True, 2)]

    def set_device(self, device_id):
        self.device = device_id

    def set_volume(self, volume):
        self.volume = volume

    def sync(self, cues, playing=True):
        self.cues, self.playing = list(cues), playing
        self.syncs += 1

    def status(self):
        return {"ok": True, "error": None, "device": self.device, "device_name": "Fake",
                "volume": self.volume, "voices": len(self.cues)}


class FakeMedia:
    """In-memory MediaLibrary stand-in."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.items: dict[str, MediaItem] = {}
        self.rev = 0
        self.closed = False

    def put(self, item: MediaItem, file_bytes: bytes = b"data") -> MediaItem:
        d = self.root / item.id
        d.mkdir(exist_ok=True)
        (d / "original.bin").write_bytes(file_bytes)
        (d / "thumb.jpg").write_bytes(b"\xff\xd8thumb")
        self.items[item.id] = item
        self.rev += 1
        return item

    def list(self):
        return sorted(self.items.values(), key=lambda m: -m.added)

    def get(self, media_id):
        return self.items.get(media_id)

    def add(self, src: Path, name: str) -> MediaItem:
        if src.suffix.lower() not in (".mp4", ".png", ".mp3"):
            raise ValueError(f"Cannot take {src.suffix} files")
        kind = {".mp4": "video", ".png": "image", ".mp3": "audio"}[src.suffix.lower()]
        item = MediaItem(id=f"m{len(self.items):011d}", name=name, kind=kind, status="ready",
                         size=src.stat().st_size, duration=5.0 if kind != "image" else 0.0, added=time.time())
        d = self.root / item.id
        d.mkdir()
        shutil.move(str(src), d / ("original" + src.suffix))
        return self.put(item, (d / ("original" + src.suffix)).read_bytes())

    def rename(self, media_id, name):
        if media_id not in self.items:
            raise KeyError(media_id)
        self.items[media_id] = self.items[media_id].model_copy(update={"name": name})
        self.rev += 1
        return self.items[media_id]

    def update(self, media_id, name=None, edit=None):
        if media_id not in self.items:
            raise KeyError(media_id)
        upd = {k: v for k, v in (("name", name), ("edit", edit)) if v is not None}
        self.items[media_id] = self.items[media_id].model_copy(update=upd)
        self.rev += 1
        return self.items[media_id]

    def delete(self, media_id):
        if media_id not in self.items:
            raise KeyError(media_id)
        del self.items[media_id]
        shutil.rmtree(self.root / media_id, ignore_errors=True)
        self.rev += 1

    def video_path(self, media_id):
        return None

    image_path = audio_path = video_path

    def thumb_path(self, media_id):
        return self.root / media_id / "thumb.jpg" if media_id in self.items else None

    def file_path(self, media_id):
        return self.root / media_id / "original.bin" if media_id in self.items else None

    def peaks(self, media_id):
        item = self.items.get(media_id)
        return {"rate": 10.0, "peaks": [0.0, 0.5, 1.0]} if item and item.kind != "image" else None

    def wait_ready(self, media_id, timeout: float = 30) -> Any:
        return self.items[media_id]

    def close(self):
        self.closed = True


def test_fakes_register_renderers():
    from trafficlight.render import get_renderer
    assert get_renderer("test:solid") is Solid and get_renderer("test:probe") is TimeProbe
