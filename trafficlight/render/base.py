"""The clip renderer contract and registry.

A renderer class draws one kind of clip (text, a beat effect, a video...). The
player makes one instance per clip while that clip is on screen and calls
render() once per output frame. Renderers know nothing about tracks, masks,
fades or the sign as a whole: they fill a box (128x128 for a light, 128x384
for the whole sign) and the player composites it.

    @register
    class Pulse(ClipRenderer):
        type = "fx:pulse"
        label = "Pulse"
        category = "beat"
        params = [ParamSpec("color", "Colour", "color", "face"), ...]
        def render(self, ctx): ...

Rules for render():
  * Return a uint8 array of shape (ctx.height, ctx.width, 3) (opaque RGB) or
    (ctx.height, ctx.width, 4) (RGBA, straight alpha). Do not keep a reference
    the player could corrupt: return a fresh array or one you no longer write.
  * It is a function of ctx: the player may call it with any time, in any
    order (scrubbing, pause, seek). Cache what is expensive, keyed on what it
    depends on.
  * It runs on the render thread at up to 60 fps next to other clips: aim for
    well under 2 ms on a PC. Use numpy on whole arrays, never per-pixel loops.
  * Never raise for bad params: draw something sensible. Report problems
    through validate().
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional, Protocol

import numpy as np

from ..model import MediaItem, Settings


# ---------------------------------------------------------------- parameters
@dataclass
class ParamSpec:
    """One editable parameter; the web UI builds the clip inspector from these.

    kind:
      "string"  one line of text          "text"   multi-line text
      "number"  float (min/max/step)      "int"    integer (min/max/step)
      "bool"    checkbox                  "select" one of `options` [{value,label}]
      "color"   "#rrggbb", or "face" (the light's own colour from Settings.face_colors),
                or "random" (a new colour every time the clip starts). See render/color.py.
      "media"   id of a MediaItem of `media_kind` ("video" | "image" | "audio")
      "font"    font id from /api/catalog fonts ("" = automatic)
    """
    key: str
    label: str
    kind: str
    default: Any = None
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    options: Optional[list[dict[str, Any]]] = None
    media_kind: Optional[str] = None
    help: str = ""

    def to_json(self) -> dict[str, Any]:
        d = {"key": self.key, "label": self.label, "kind": self.kind, "default": self.default}
        for k in ("min", "max", "step", "options", "media_kind"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        if self.help:
            d["help"] = self.help
        return d


# ---------------------------------------------------------------- context
@dataclass
class RenderContext:
    """Everything a renderer may depend on for one frame."""
    t: float                       # seconds since the clip's start (0 <= t < clip_duration, roughly)
    clip_duration: float           # the clip's length in seconds
    set_time: float                # seconds since the set's start
    bpm: float                     # the set's tempo
    beat: float                    # fractional beats on the set's grid: (set_time - beat_offset) * bpm / 60
    clip_beat: float               # fractional beats since the clip's start: t * bpm / 60 (effects count from here)
    width: int                     # box to fill: 128
    height: int                    # 128 for a light, 384 for the whole sign
    face: Optional[int]            # 0 top, 1 middle, 2 bottom; None on an "all" track
    face_color: tuple[int, int, int]   # this light's own colour (white on an "all" track)
    seed: int = 0                  # stable while this clip plays once; differs on the next pass (random colours)
    wall_time: _dt.datetime = field(default_factory=_dt.datetime.now)   # local time, for real clocks
    still: bool = False            # a one-off picture (/api/render.png, thumbnails), not live playback: take the
                                   # time to be exact (wait for a decoder's frame) instead of drawing a stand-in


@dataclass
class AudioCue:
    """"This sound should be playing right now, at this position." The player sends
    the full list of cues every frame; the audio engine starts, seeks and stops
    voices to match (audio/base.py)."""
    key: str                       # unique per sounding clip (the clip id)
    path: str                      # an audio file (or any file with an audio stream) on disk
    position: float                # seconds into the file that should be heard now
    gain: float = 1.0              # 0..1, fades included


class MediaLookup(Protocol):
    """What renderers may ask the media library (media/library.py implements it)."""

    def get(self, media_id: str) -> Optional[MediaItem]: ...
    def video_path(self, media_id: str) -> Optional[str]:
        """The file to decode for playback (a small proxy, not the original)."""
    def image_path(self, media_id: str) -> Optional[str]: ...
    def audio_path(self, media_id: str) -> Optional[str]:
        """The audio of an audio item, or the extracted sound track of a video; None if silent."""


@dataclass
class RenderEnv:
    """Long-lived services handed to every renderer instance."""
    settings: Settings
    media: Optional[MediaLookup] = None


# ---------------------------------------------------------------- renderer
class ClipRenderer:
    type: ClassVar[str] = ""                     # clip type id, e.g. "text", "fx:pulse", "video"
    label: ClassVar[str] = ""                    # name in the UI
    category: ClassVar[str] = "basic"            # palette group: "basic" | "text" | "figure" | "beat" | "media"
    targets: ClassVar[tuple[str, ...]] = ("face", "all")   # where it may go: "face", "all", "audio"
    default_duration: ClassVar[float] = 4.0      # seconds, when dropped on the timeline
    params: ClassVar[list[ParamSpec]] = []
    description: ClassVar[str] = ""

    def __init__(self, params: dict[str, Any], env: RenderEnv):
        self.env = env
        self.p = self.with_defaults(params)

    # -- drawing
    def render(self, ctx: RenderContext) -> Optional[np.ndarray]:
        """The clip's picture for this frame (see the module docstring); None = nothing (fully transparent)."""
        return None

    def audio(self, ctx: RenderContext) -> Optional[AudioCue]:
        """The sound this clip makes now, or None. `key` and fades are filled in by the player:
        return path, position and the clip's own volume as gain."""
        return None

    def close(self) -> None:
        """Release files and decoders. Called when the clip leaves the screen."""

    # -- class helpers
    @classmethod
    def with_defaults(cls, params: dict[str, Any]) -> dict[str, Any]:
        out = {s.key: s.default for s in cls.params}
        out.update({k: v for k, v in (params or {}).items() if v is not None})
        return out

    @classmethod
    def validate(cls, params: dict[str, Any], target: str, env: RenderEnv) -> list[str]:
        """Human-readable problems with these params on this target ("face0".."face2", "all", "audio");
        [] = fine. Example: "The text does not fit in the light"."""
        return []

    @classmethod
    def natural_duration(cls, params: dict[str, Any], env: RenderEnv) -> Optional[float]:
        """The clip's own length in seconds if it has one (a video, a song); None otherwise."""
        return None

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "type": cls.type, "label": cls.label, "category": cls.category,
            "targets": list(cls.targets), "default_duration": cls.default_duration,
            "description": cls.description, "params": [s.to_json() for s in cls.params],
        }


# ---------------------------------------------------------------- registry
_REGISTRY: dict[str, type[ClipRenderer]] = {}


def register(cls: type[ClipRenderer]) -> type[ClipRenderer]:
    if not cls.type:
        raise ValueError(f"{cls.__name__} has no type")
    _REGISTRY[cls.type] = cls
    return cls


def get_renderer(type_id: str) -> Optional[type[ClipRenderer]]:
    return _REGISTRY.get(type_id)


def all_renderers() -> list[type[ClipRenderer]]:
    return list(_REGISTRY.values())


def catalog() -> list[dict[str, Any]]:
    return [c.describe() for c in _REGISTRY.values()]
