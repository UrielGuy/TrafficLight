"""Shared by the media clip types: fitting a picture into the box, looking items up, checking params."""
from __future__ import annotations

import math
from typing import Any, Optional

from ...model import MediaItem
from ..base import ParamSpec, RenderEnv

FITS = ("cover", "contain", "stretch")

FIT_PARAM = ParamSpec("fit", "Fit", "select", "cover", options=[
    {"value": "cover", "label": "Cover (fill the box, crop the edges)"},
    {"value": "contain", "label": "Contain (whole picture, empty bars)"},
    {"value": "stretch", "label": "Stretch (fill the box, distort)"},
])

_KIND_WORD = {"video": "video", "image": "picture", "audio": "sound"}


def fit_of(p: dict[str, Any]) -> str:
    f = p.get("fit")
    return f if f in FITS else "cover"


def number(p: dict[str, Any], key: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(p.get(key, default))
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return min(hi, max(lo, v))


def geometry(src_w: int, src_h: int, box_w: int, box_h: int, fit: str) -> tuple[int, int, int, int]:
    """How to place a src_w x src_h picture in the box: (scaled_w, scaled_h, x, y).
    cover: scale to at least the box, then crop at (x, y) inside the scaled picture.
    contain: scale to fit inside, then paste at (x, y) inside the box (the rest stays transparent).
    stretch: scale to the box exactly; x = y = 0."""
    if fit == "stretch" or src_w <= 0 or src_h <= 0:
        return box_w, box_h, 0, 0
    if fit == "contain":
        s = min(box_w / src_w, box_h / src_h)
        sw = min(box_w, max(1, round(src_w * s)))
        sh = min(box_h, max(1, round(src_h * s)))
        return sw, sh, (box_w - sw) // 2, (box_h - sh) // 2
    s = max(box_w / src_w, box_h / src_h)
    sw = max(box_w, round(src_w * s))
    sh = max(box_h, round(src_h * s))
    return sw, sh, (sw - box_w) // 2, (sh - box_h) // 2


def playable(m: Optional[MediaItem]) -> bool:
    """Prepared files exist: the item is ready, or a changed edit is being applied while the previous
    result still plays."""
    return m is not None and (m.status == "ready" or (m.status == "processing" and m.version > 0))


def lookup(env: RenderEnv, media_id: Any) -> Optional[MediaItem]:
    if env.media is None or not isinstance(media_id, str) or not media_id:
        return None
    try:
        return env.media.get(media_id)
    except Exception:
        return None


def media_problems(p: dict[str, Any], env: RenderEnv, kinds: tuple[str, ...], need_sound: bool = False) -> list[str]:
    """What is wrong with the `media` param: nothing chosen, gone, still processing, failed, wrong kind."""
    word = _KIND_WORD[kinds[0]]
    mid = p.get("media")
    if not mid:
        return [f"No {word} chosen"]
    if env.media is None:
        return ["The media library is not available"]
    m = lookup(env, mid)
    if m is None:
        return [f"The {word} is no longer in the library"]
    if m.kind not in kinds:
        return [f'"{m.name}" is a {_KIND_WORD[m.kind]}, not a {word}']
    if m.status == "processing" and not playable(m):
        return [f'"{m.name}" is still being processed']
    if m.status == "error":
        return [f'"{m.name}" could not be read: {m.error or "unknown error"}']
    if need_sound and not m.has_audio:
        return [f'"{m.name}" has no sound']
    return []
