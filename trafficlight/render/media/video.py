"""The `video` clip type: a library video fitted to the box, decoded ahead on a background thread.

The render thread never decodes. Each clip owns a _Decoder thread that keeps the frames the clip is
about to show (the current one and a short look-ahead, wrapping round at a loop) converted to the
box size. render() picks the frame for its time from that buffer; when it is not there yet (a jump,
a slow machine) it shows the last frame it showed instead of waiting. The proxy has a constant
frame rate (media/ingest.py), so frame n is at n / fps and seeking is arithmetic.
"""
from __future__ import annotations

import logging
import math
import threading
from typing import Any, Optional

import av
import numpy as np
from av.video.reformatter import VideoReformatter

from ..base import AudioCue, ClipRenderer, ParamSpec, RenderContext, RenderEnv, register
from .common import FIT_PARAM, fit_of, geometry, lookup, media_problems, number

log = logging.getLogger(__name__)

LOOKAHEAD_SECONDS = 0.15     # frames decoded ahead of the play position (times the speed)
MAX_AHEAD = 48
FIRST_FRAME_WAIT = 0.004     # the only wait on the render thread: a clip's very first frame
SEEK_SECONDS = 0.5           # a forward jump shorter than this decodes through (the proxy's GOP)


class _Decoder:
    """Decodes one file on its own thread into box-sized RGB / RGBA arrays for the wanted frame indices.

    It starts as soon as the clip's renderer is made (the player does that ahead of the clip, off the
    render thread), before the box is known: frames decoded then are kept as they are and converted
    when the first render() says how big the box is."""

    def __init__(self, path: str, fps: float, nframes: int):
        self.path = path
        self.fps = fps
        self.n = max(1, nframes)          # shrinks if the file turns out shorter than its metadata says
        self.box: Optional[tuple[int, int]] = None
        self.fit = "cover"
        self.cond = threading.Condition()
        self.frames: dict[int, np.ndarray] = {}       # converted for (box, fit)
        self.raw: dict[int, av.VideoFrame] = {}       # decoded while the box was not known yet
        self.want: tuple[int, ...] = ()
        self._want_set: frozenset[int] = frozenset()
        self.closed = False
        self.error: Optional[str] = None
        self.thread = threading.Thread(target=self._run, name=f"video-decode {path}", daemon=True)
        self.thread.start()

    # -- render thread
    def configure(self, box: tuple[int, int], fit: str) -> bool:
        """Set the box and fit; True if they changed (converted frames are then dropped)."""
        with self.cond:
            if (box, fit) == (self.box, self.fit):
                return False
            self.box, self.fit = box, fit
            self.frames.clear()
            self.cond.notify_all()
            return True

    def request(self, want: tuple[int, ...]) -> None:
        with self.cond:
            if want != self.want:
                self.want = want
                self._want_set = frozenset(want)
                self.cond.notify_all()

    def get(self, i: int, wait: float = 0.0) -> Optional[np.ndarray]:
        with self.cond:
            arr = self.frames.get(i)
            if arr is None and wait > 0:
                self.cond.wait_for(lambda: i in self.frames or self.closed or self.error is not None, wait)
                arr = self.frames.get(i)
            return arr

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    # -- decoder thread (methods below that read shared state are called with the lock held)
    def _job(self) -> Optional[tuple[str, int]]:
        if self.box is not None:
            for i in self.want:
                if i in self.raw:
                    return "convert", i
        for i in self.want:
            if i < self.n and i not in self.frames and i not in self.raw:
                return "decode", i
        return None

    def _run(self) -> None:
        try:
            with av.open(self.path) as c:
                self._loop(c)
        except Exception as e:
            log.warning("video %s: decoding stopped: %s", self.path, e)
            with self.cond:
                self.error = str(e) or type(e).__name__
                self.cond.notify_all()

    def _loop(self, c) -> None:
        vs = c.streams.video[0]
        vs.codec_context.thread_count = 1      # tiny proxies: threads cost more than they save
        tb = float(vs.time_base)
        sws = VideoReformatter()
        jump = max(2, int(self.fps * SEEK_SECONDS) + 1)
        gen = None
        pos: Optional[int] = None              # index of the frame `gen` yields next, if known
        while True:
            with self.cond:
                self.cond.wait_for(lambda: self.closed or self._job() is not None, 1.0)
                if self.closed:
                    return
                for store in (self.frames, self.raw):
                    for k in [k for k in store if k not in self._want_set]:
                        del store[k]
                job = self._job()
                box, fit = self.box, self.fit
                raw = self.raw.pop(job[1], None) if job is not None and job[0] == "convert" else None
            if job is None:
                continue
            if raw is not None:
                self._store([job[1]], self._convert(sws, raw, box, fit), box, fit)
                continue
            target = job[1]
            if gen is None or pos is None or target < pos or target - pos > jump:
                c.seek(int(target / self.fps / tb), stream=vs, backward=True, any_frame=False)
                gen = c.decode(vs)
                pos = None
            try:
                frame = next(gen)
            except StopIteration:
                with self.cond:
                    end = pos if pos is not None else target
                    if end == 0:
                        raise RuntimeError("the video has no frames")
                    if end < self.n:
                        self.n = end
                    self.cond.notify_all()
                gen = None
                continue
            idx = round(frame.time * self.fps) if frame.time is not None else (pos if pos is not None else target)
            lo = pos if pos is not None else target
            pos = idx + 1
            # This frame also stands in for wanted indices the file skipped (a gap, or a first frame
            # later than the seek target); otherwise they would be sought for ever.
            with self.cond:
                fill = [j for j in self.want if j == idx or (lo <= j < idx and j not in self.frames)]
            if not fill:
                continue
            if box is None:
                with self.cond:
                    for j in fill:
                        self.raw[j] = frame
                continue
            self._store(fill, self._convert(sws, frame, box, fit), box, fit)

    def _store(self, idxs: list[int], arr: np.ndarray, box, fit) -> None:
        with self.cond:
            if (box, fit) != (self.box, self.fit):
                return                         # converted for a box that is no longer asked for
            for j in idxs:
                if j in self._want_set:
                    self.frames[j] = arr
            self.cond.notify_all()

    @staticmethod
    def _convert(sws: VideoReformatter, frame: av.VideoFrame, box: tuple[int, int], fit: str) -> np.ndarray:
        bw, bh = box
        sw, sh, x, y = geometry(frame.width, frame.height, bw, bh, fit)
        rgb = sws.reformat(frame, width=sw, height=sh, format="rgb24", interpolation="AREA",
                           threads=1).to_ndarray()
        if fit == "contain":
            if (sw, sh) == (bw, bh):
                return rgb
            out = np.zeros((bh, bw, 4), np.uint8)
            out[y:y + sh, x:x + sw, :3] = rgb
            out[y:y + sh, x:x + sw, 3] = 255
            return out
        if (sw, sh) == (bw, bh):
            return rgb
        return np.ascontiguousarray(rgb[y:y + bh, x:x + bw])


@register
class Video(ClipRenderer):
    type = "video"
    label = "Video"
    category = "media"
    targets = ("face", "all")
    default_duration = 10.0
    description = "A video from the library, fitted to the light or the whole sign; optional sound."
    params = [
        ParamSpec("media", "Video", "media", "", media_kind="video"),
        ParamSpec("offset", "Start at", "number", 0.0, min=0, step=0.1,
                  help="Seconds into the video where the clip starts"),
        ParamSpec("loop", "Loop", "bool", True, help="Start again at the end of the video"),
        ParamSpec("speed", "Speed", "number", 1.0, min=0, max=8, step=0.05,
                  help="1 = normal; sound plays only at 1"),
        FIT_PARAM,
        ParamSpec("sound", "Sound", "bool", True, help="Play the video's own sound"),
        ParamSpec("volume", "Volume", "number", 1.0, min=0, max=1, step=0.01),
    ]

    def __init__(self, params: dict[str, Any], env: RenderEnv):
        super().__init__(params, env)
        self._item = None
        self._dec: Optional[_Decoder] = None
        self._last: Optional[np.ndarray] = None
        self._want_idx: Optional[int] = None
        self._failed = False
        # Counters for diagnostics and tests: frames asked for, shown late (an older frame instead),
        # and shown again although the clock had moved on to a new frame.
        self.stats = {"calls": 0, "late": 0, "repeated": 0}
        self._start(0.0)

    # -- timing
    def _media_time(self, t: float) -> float:
        return number(self.p, "offset", 0.0, 0.0, 1e7) + max(0.0, t) * number(self.p, "speed", 1.0, 0.0, 8.0)

    def _wanted(self, t: float, n: int, fps: float) -> tuple[int, tuple[int, ...]]:
        """The frame index for clip time t, and it followed by the look-ahead (wrapping at a loop)."""
        loop = self.p.get("loop", True)
        i = int(math.floor(self._media_time(t) * fps + 1e-6))
        i = i % n if loop else min(max(i, 0), n - 1)
        speed = number(self.p, "speed", 1.0, 0.0, 8.0)
        ahead = 0 if speed <= 0 else min(MAX_AHEAD, max(3, math.ceil(fps * speed * LOOKAHEAD_SECONDS)))
        if loop:
            return i, tuple(dict.fromkeys((i + k) % n for k in range(ahead + 1)))
        return i, tuple(range(i, min(n, i + ahead + 1)))

    def _ready_item(self):
        if self._item is None:
            m = lookup(self.env, self.p.get("media"))
            if m is not None and m.status == "ready" and m.kind == "video" and m.fps > 0:
                self._item = m
        return self._item

    def _start(self, t: float) -> Optional[_Decoder]:
        """Open the decoder and ask for the frames around t (from __init__ that is the clip's start,
        so they are ready before the clip is first drawn)."""
        if self._dec is None:
            m = self._ready_item()
            path = self.env.media.video_path(m.id) if m is not None and self.env.media is not None else None
            if path is None:
                return None
            self._dec = _Decoder(path, m.fps, max(1, round(m.duration * m.fps)))
            self._dec.request(self._wanted(t, self._dec.n, m.fps)[1])
        return self._dec

    # -- drawing
    def render(self, ctx: RenderContext) -> Optional[np.ndarray]:
        try:
            return self._render(ctx)
        except Exception:
            if not self._failed:
                self._failed = True
                log.exception("video clip failed to render")
            return None if self._last is None else self._last.copy()

    def _render(self, ctx: RenderContext) -> Optional[np.ndarray]:
        dec = self._start(ctx.t)
        if dec is None:
            return None
        if dec.configure((ctx.width, ctx.height), fit_of(self.p)):
            self._last = None
        i, want = self._wanted(ctx.t, dec.n, self._item.fps)
        dec.request(want)
        arr = dec.get(i, FIRST_FRAME_WAIT if self._last is None else 0.0)
        self.stats["calls"] += 1
        if arr is None:
            self.stats["late"] += 1
            if self._last is not None and self._want_idx != i:
                self.stats["repeated"] += 1
            self._want_idx = i
            return None if self._last is None else self._last.copy()
        self._want_idx = i
        self._last = arr
        return arr.copy()

    def audio(self, ctx: RenderContext) -> Optional[AudioCue]:
        if not self.p.get("sound", True) or abs(number(self.p, "speed", 1.0, 0.0, 8.0) - 1.0) > 1e-3:
            return None
        m = self._ready_item()
        if m is None or not m.has_audio or m.duration <= 0 or self.env.media is None:
            return None
        path = self.env.media.audio_path(m.id)
        if path is None:
            return None
        mt = self._media_time(ctx.t)
        if self.p.get("loop", True):
            mt %= m.duration
        elif mt >= m.duration:
            return None
        return AudioCue(key="", path=path, position=mt, gain=number(self.p, "volume", 1.0, 0.0, 1.0))

    def close(self) -> None:
        if self._dec is not None:
            self._dec.close()
            self._dec = None

    # -- class helpers
    @classmethod
    def validate(cls, params: dict[str, Any], target: str, env: RenderEnv) -> list[str]:
        p = cls.with_defaults(params)
        problems = media_problems(p, env, ("video",))
        m = lookup(env, p.get("media"))
        if not problems and m is not None and m.has_audio and p.get("sound", True) \
                and abs(number(p, "speed", 1.0, 0.0, 8.0) - 1.0) > 1e-3:
            problems.append("The sound plays only at speed 1")
        return problems

    @classmethod
    def natural_duration(cls, params: dict[str, Any], env: RenderEnv) -> Optional[float]:
        p = cls.with_defaults(params)
        m = lookup(env, p.get("media"))
        if m is None or m.kind != "video" or m.duration <= 0:
            return None
        speed = number(p, "speed", 1.0, 0.0, 8.0)
        rest = max(0.0, m.duration - number(p, "offset", 0.0, 0.0, 1e7))
        rest = rest if rest > 0 else m.duration
        return rest / speed if speed > 0 else m.duration
