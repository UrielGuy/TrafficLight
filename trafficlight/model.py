"""The data model: sets (timelines), settings, media items. Pydantic v2.

These classes are the contract between the store, the player, the HTTP API and
the web UI (web/src/api/types.ts mirrors them). Change them only together with
docs/ARCHITECTURE.md.
"""
from __future__ import annotations

import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

Target = Literal["face0", "face1", "face2", "all", "audio"]
FACE_TARGETS = ("face0", "face1", "face2")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class Clip(BaseModel):
    """One block on a track. `type` names a registered ClipRenderer (render/base.py)."""
    id: str = Field(default_factory=new_id)
    type: str
    start: float = 0.0                 # seconds from the start of the set
    duration: float = 4.0              # seconds, > 0
    params: dict[str, Any] = Field(default_factory=dict)   # renderer-specific, see /api/catalog
    # Common properties, applied by the player to every visual clip:
    mask: Optional[bool] = None        # clip to the round lights (one disc, three on the whole sign). None = default: True.
                                       # On an "all" track True clips to the three circles.
    opacity: float = 1.0               # 0..1
    fade_in: float = 0.0               # seconds; for audio clips these fade the volume
    fade_out: float = 0.0


class Track(BaseModel):
    """A row of the timeline. Clips on one track never overlap in time."""
    id: str = Field(default_factory=new_id)
    name: str = ""
    target: Target
    muted: bool = False                # a muted track is skipped (picture and sound)
    clips: list[Clip] = Field(default_factory=list)


class Set(BaseModel):
    """A timeline. tracks[0] is the top row in the editor and the top layer on the sign:
    visual tracks are composited from the last to the first."""
    id: str = Field(default_factory=new_id)
    name: str = "Untitled"
    bpm: float = 120.0                 # tempo of the beat grid and the beat animations (10..400)
    beat_offset: float = 0.0           # seconds from the set's start to beat 0
    duration: float = 30.0             # seconds; the set ends here even if clips run longer
    in_rotation: bool = True           # may be picked as the random set
    tracks: list[Track] = Field(default_factory=list)


def default_tracks() -> list[Track]:
    return [
        Track(name="Whole sign", target="all"),
        Track(name="Top", target="face0"),
        Track(name="Middle", target="face1"),
        Track(name="Bottom", target="face2"),
        Track(name="Audio", target="audio"),
    ]


class SetSummary(BaseModel):
    id: str
    name: str
    bpm: float
    duration: float
    in_rotation: bool


# ---------------------------------------------------------------- settings
class OutputSettings(BaseModel):
    driver: Literal["colorlight", "none"] = "none"
    interface: Optional[str] = None    # network interface id from /api/output/interfaces
    brightness: float = 0.5            # 0..1
    gamma: float = 1.0                 # applied in software before sending (1.0 = leave to the receiver card)
    rotate180: bool = False            # whole sign upside down
    panel_order: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5])   # canvas panel (top..bottom) -> position in the card's image
    panel_flip: list[bool] = Field(default_factory=lambda: [False] * 6)          # panel mounted rotated 180 degrees
    color_order: Literal["rgb", "rbg", "grb", "gbr", "brg", "bgr"] = "rgb"   # byte order on the wire; this card probably needs "bgr" (docs/HARDWARE.md)


class AudioSettings(BaseModel):
    device: Optional[str] = None       # output device id from /api/audio/devices; None = system default
    volume: float = 0.8                # master, 0..1


class ScheduleSettings(BaseModel):
    default_set: Optional[str] = None  # the set the sign loops; None = play all sets in order
    random_minutes: float = 6.0        # one random other set this often (measured start to start)
    random_enabled: bool = True


class Settings(BaseModel):
    output: OutputSettings = Field(default_factory=OutputSettings)
    audio: AudioSettings = Field(default_factory=AudioSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    face_colors: list[str] = Field(default_factory=lambda: ["#ff0000", "#ffb000", "#00ff40"])  # top, middle, bottom
    off_style: Literal["ring", "dark"] = "ring"   # a light with nothing on it: dim outline ring or black
    fps: int = 60                      # render rate of the engine


# ---------------------------------------------------------------- media
class MediaEdit(BaseModel):
    """How an item's original is prepared for the sign. Never destructive: the original
    file is kept as uploaded, and changing the edit re-makes the proxy from it."""
    rotate: Literal[0, 90, 180, 270] = 0   # degrees clockwise, applied first
    crop: Optional[list[float]] = None     # [x, y, w, h] as fractions (0..1) of the rotated picture; None = all of it
    start: float = Field(0.0, ge=0)        # trim: seconds into the original (0 = its first picture)
    end: Optional[float] = None            # seconds into the original; None = to its end
    shape: Literal["free", "light", "sign"] = "free"   # what the crop was drawn for: one light (1:1), the whole
                                           # sign (1:3), or no fixed shape. A hint for the crop tool only.

    @field_validator("crop")
    @classmethod
    def _crop_inside(cls, v: Optional[list[float]]) -> Optional[list[float]]:
        if v is None:
            return None
        if len(v) != 4:
            raise ValueError("crop is [x, y, w, h]")
        x, y, w, h = (float(a) for a in v)
        eps = 1e-6
        if not (x >= -eps and y >= -eps and w > 0 and h > 0 and x + w <= 1 + eps and y + h <= 1 + eps):
            raise ValueError("crop must lie inside the picture: 0 <= x, y and x + w, y + h <= 1")
        x, y = max(0.0, x), max(0.0, y)
        return [x, y, min(w, 1.0 - x), min(h, 1.0 - y)]

    @model_validator(mode="after")
    def _end_after_start(self) -> "MediaEdit":
        if self.end is not None and self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class MediaItem(BaseModel):
    id: str
    name: str
    kind: Literal["video", "image", "audio"]
    status: Literal["processing", "ready", "error"] = "processing"
    error: Optional[str] = None        # with status "error": why. With "ready": the last change of the edit
                                       # failed (the previous result still plays and `edit` is back to it).
    duration: float = 0.0              # seconds (0 for a still image)
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    size: int = 0                      # bytes of the original file
    added: float = 0.0                 # unix time
    # duration / width / height / fps / has_audio describe the item as prepared (after rotate, crop, trim; width
    # and height in pixels of the original). While a changed edit is being applied they still describe the
    # previous result, which keeps playing; they change together with `version`.
    edit: MediaEdit = Field(default_factory=MediaEdit)
    version: int = 0                   # number of the prepared files in use (proxy, sound, thumbnail); grows each
                                       # time they are re-made. 0 = nothing prepared yet.
    source_duration: float = 0.0       # the original: seconds,
    source_width: int = 0              # and display size before rotate and crop
    source_height: int = 0
    source_url: Optional[str] = None   # where it was imported from, if from a link
    progress: float = 0.0              # 0..1 while status is "processing"
    stage: str = ""                    # what is happening while processing: "waiting", "downloading", "converting",
                                       # "preparing the crop view"
