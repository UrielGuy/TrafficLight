"""Clip renderers. Importing this package registers every built-in renderer.

Sub-packages (each registers its renderers on import):
    basic  - light (plain disc), text, figure (animated pictograms)
    fx     - beat animations and the clock
    media  - video, image, audio
"""
import importlib
import logging

from .base import (AudioCue, ClipRenderer, MediaLookup, ParamSpec, RenderContext, RenderEnv,  # noqa: F401
                   all_renderers, catalog, get_renderer, register)

_log = logging.getLogger(__name__)

for _name in ("basic", "fx", "media"):
    try:
        importlib.import_module(f"{__name__}.{_name}")
    except Exception:  # a broken renderer package must not take the sign down
        _log.exception("renderer package %s failed to load", _name)
