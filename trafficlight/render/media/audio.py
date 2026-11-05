"""The `audio` clip type: music or sound from the library (an audio item, or a video's sound track)."""
from __future__ import annotations

from typing import Any, Optional

from ..base import AudioCue, ClipRenderer, ParamSpec, RenderContext, RenderEnv, register
from .common import lookup, media_problems, number, playable


@register
class Sound(ClipRenderer):
    type = "audio"
    label = "Sound"
    category = "media"
    targets = ("audio",)
    default_duration = 30.0
    description = "Music or a sound from the library (a video's sound works too)."
    params = [
        ParamSpec("media", "Sound", "media", "", media_kind="audio"),
        ParamSpec("offset", "Start at", "number", 0.0, min=0, step=0.1,
                  help="Seconds into the sound where the clip starts"),
        ParamSpec("volume", "Volume", "number", 1.0, min=0, max=1, step=0.01),
        ParamSpec("loop", "Loop", "bool", False, help="Start again at the end of the sound"),
    ]

    def __init__(self, params: dict[str, Any], env: RenderEnv):
        super().__init__(params, env)
        self._src: Optional[tuple[str, float]] = None      # (audio file, its duration)

    def _source(self) -> Optional[tuple[str, float]]:
        mid = self.p.get("media")
        if self.env.media is None or not isinstance(mid, str) or not mid:
            return None
        # Asked every frame (a dictionary lookup): when the item's edit is re-made, the path changes and
        # the sound switches to the new file without the clip being restarted.
        path = self.env.media.audio_path(mid)
        if path is None:
            return self._src
        if self._src is None or self._src[0] != path:
            m = lookup(self.env, mid)
            if not playable(m) or not m.has_audio or m.duration <= 0:
                return self._src
            self._src = (path, m.duration)
        return self._src

    def audio(self, ctx: RenderContext) -> Optional[AudioCue]:
        src = self._source()
        if src is None:
            return None
        path, duration = src
        pos = number(self.p, "offset", 0.0, 0.0, 1e7) + max(0.0, ctx.t)
        if self.p.get("loop", False):
            pos %= duration
        elif pos >= duration:
            return None
        return AudioCue(key="", path=path, position=pos, gain=number(self.p, "volume", 1.0, 0.0, 1.0))

    @classmethod
    def validate(cls, params: dict[str, Any], target: str, env: RenderEnv) -> list[str]:
        return media_problems(cls.with_defaults(params), env, ("audio", "video"), need_sound=True)

    @classmethod
    def natural_duration(cls, params: dict[str, Any], env: RenderEnv) -> Optional[float]:
        p = cls.with_defaults(params)
        m = lookup(env, p.get("media"))
        if m is None or not m.has_audio or m.duration <= 0:
            return None
        rest = m.duration - number(p, "offset", 0.0, 0.0, 1e7)
        return rest if rest > 0 else m.duration
