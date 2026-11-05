"""The `image` clip type: a still picture from the library, fitted to the box (RGBA if it has alpha)."""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Any, Optional

import numpy as np
from PIL import Image

from ..base import ClipRenderer, ParamSpec, RenderContext, RenderEnv, register
from .common import FIT_PARAM, fit_of, geometry, media_problems

log = logging.getLogger(__name__)

# Scaled pictures by (file, box, fit): a clip that comes round again on every pass of the set, or the
# same picture on three lights, is scaled once.
_CACHE: "OrderedDict[tuple, np.ndarray]" = OrderedDict()
_CACHE_SIZE = 32
_CACHE_LOCK = threading.Lock()


def _scaled(path: str, box_w: int, box_h: int, fit: str) -> np.ndarray:
    key = (path, box_w, box_h, fit)
    with _CACHE_LOCK:
        arr = _CACHE.get(key)
        if arr is not None:
            _CACHE.move_to_end(key)
            return arr
    with Image.open(path) as im:
        im.load()
        alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
        im = im.convert("RGBA" if alpha else "RGB")
    sw, sh, x, y = geometry(im.width, im.height, box_w, box_h, fit)
    if (sw, sh) != im.size:
        im = im.resize((sw, sh), Image.Resampling.LANCZOS)
    src = np.asarray(im)
    if fit == "contain" and (sw, sh) != (box_w, box_h):
        arr = np.zeros((box_h, box_w, 4), np.uint8)
        arr[y:y + sh, x:x + sw, :src.shape[2]] = src
        if not alpha:
            arr[y:y + sh, x:x + sw, 3] = 255
    else:
        arr = np.ascontiguousarray(src[y:y + box_h, x:x + box_w])
    arr.flags.writeable = False
    with _CACHE_LOCK:
        _CACHE[key] = arr
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)
    return arr


@register
class Picture(ClipRenderer):
    type = "image"
    label = "Picture"
    category = "media"
    targets = ("face", "all")
    default_duration = 4.0
    description = "A still picture from the library, fitted to the light or the whole sign."
    params = [
        ParamSpec("media", "Picture", "media", "", media_kind="image"),
        FIT_PARAM,
    ]

    def __init__(self, params: dict[str, Any], env: RenderEnv):
        super().__init__(params, env)
        self._path: Optional[str] = None
        self._failed = False

    def render(self, ctx: RenderContext) -> Optional[np.ndarray]:
        try:
            if self._path is None:
                mid = self.p.get("media")
                if self.env.media is None or not isinstance(mid, str) or not mid:
                    return None
                self._path = self.env.media.image_path(mid)
                if self._path is None:
                    return None
            return _scaled(self._path, ctx.width, ctx.height, fit_of(self.p)).copy()
        except Exception:
            if not self._failed:
                self._failed = True
                log.exception("picture clip failed to render")
            return None

    @classmethod
    def validate(cls, params: dict[str, Any], target: str, env: RenderEnv) -> list[str]:
        return media_problems(cls.with_defaults(params), env, ("image",))
