"""Timeline evaluation and compositing for one set (docs/ARCHITECTURE.md,
"Compositing (player)").

A Player is used by one thread (the engine's render thread, or a request
handler for a one-off thumbnail). It can be asked for any time in any order:
renderer instances live from shortly before their clip starts (PRE_ROLL) to
shortly after it ends (LINGER) around the time last asked for, are made on a
background thread when there is time (pre-roll) and closed there too, so slow
constructors (video decoders) do not stall the frame.

An unknown clip type, a renderer that raises or one that returns a malformed
picture never breaks the frame: that clip is left out and the problem logged
once.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import random
import threading
import zlib
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Optional

import numpy as np

from . import config
from .model import FACE_TARGETS, Clip, Set, Settings, Track
from .render import color as _color
from .render.base import AudioCue, ClipRenderer, RenderContext, RenderEnv, get_renderer

log = logging.getLogger(__name__)

PRE_ROLL = 1.0       # seconds before a clip starts that its renderer is made
LINGER = 0.5         # seconds after it ends that it is kept (a quick seek back costs nothing)
RING_LEVEL = 0.15    # brightness of the unlit-light outline ring

_logged: set[Any] = set()
_logged_lock = threading.Lock()


def log_once(key: Any, msg: str, *args: Any, exc: bool = False) -> None:
    with _logged_lock:
        if key in _logged:
            return
        _logged.add(key)
    (log.exception if exc else log.warning)(msg, *args)


_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()


def _worker() -> ThreadPoolExecutor:
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="clip")
        return _executor


# ---------------------------------------------------------------- geometry
def target_box(target: str) -> tuple[int, int, Optional[int]]:
    """(first row, height, face index or None) of a visual target."""
    if target in FACE_TARGETS:
        f = int(target[-1])
        return f * config.FACE, config.FACE, f
    return 0, config.H, None


def _coverage(d: np.ndarray, r: float) -> np.ndarray:
    """Anti-aliased inside-ness of a circle of radius r at distance d from its centre."""
    return np.clip(r + 0.5 - d, 0.0, 1.0)


@lru_cache(maxsize=1)
def _face_dist() -> np.ndarray:
    c = (config.FACE - 1) / 2.0
    y, x = np.mgrid[0:config.FACE, 0:config.FACE].astype(np.float32)
    return np.hypot(x - c, y - c)


@lru_cache(maxsize=1)
def face_mask() -> np.ndarray:
    """(FACE, FACE) float32 0..1: the lit disc of one light."""
    m = _coverage(_face_dist(), config.FACE_RADIUS).astype(np.float32)
    m.flags.writeable = False
    return m


@lru_cache(maxsize=1)
def sign_mask() -> np.ndarray:
    """(H, W) float32: the three discs."""
    m = np.concatenate([face_mask()] * config.FACES, axis=0)
    m.flags.writeable = False
    return m


@lru_cache(maxsize=1)
def _sign_mask3() -> np.ndarray:
    """(H, W, 3) float32: sign_mask() for each colour."""
    m = np.repeat(sign_mask()[..., None], 3, axis=2)
    m.flags.writeable = False
    return m


@lru_cache(maxsize=1)
def _ring() -> np.ndarray:
    d = _face_dist()
    return (_coverage(d, config.FACE_RADIUS) - _coverage(d, config.FACE_RADIUS - config.RING_WIDTH)).astype(np.float32)


def face_rgb(settings: Settings, face: Optional[int]) -> tuple[int, int, int]:
    if face is None:
        return _color.WHITE
    cols = settings.face_colors
    return _color.parse_hex(cols[face] if face < len(cols) else None, _color.WHITE)


@lru_cache(maxsize=8)
def _background(off_style: str, colors: tuple[tuple[int, int, int], ...]) -> np.ndarray:
    bg = np.zeros((config.H, config.W, 3), np.float32)
    if off_style == "ring":
        ring = _ring()
        for f, rgb in enumerate(colors):
            bg[f * config.FACE:(f + 1) * config.FACE] = ring[..., None] * (np.array(rgb, np.float32) * RING_LEVEL)
    bg.flags.writeable = False
    return bg


def background(settings: Settings) -> np.ndarray:
    """float32 (H, W, 3): the sign with nothing on it."""
    return _background(settings.off_style, tuple(face_rgb(settings, f) for f in range(config.FACES)))


def idle_frame(settings: Settings) -> np.ndarray:
    return np.rint(background(settings)).astype(np.uint8)


# ---------------------------------------------------------------- timeline helpers
def paint_order(tracks: list[Track]) -> list[Track]:
    """Tracks from the bottom layer to the top. A whole-sign clip is the backdrop: a clip on one
    light always shows over it, wherever the tracks sit in the list. Within each kind the first
    track in the list is the top layer."""
    back = list(reversed(tracks))
    return [t for t in back if t.target == "all"] + [t for t in back if t.target != "all"]


def clip_at(track: Track, t: float) -> Optional[Clip]:
    """The clip covering t (start <= t < start + duration). Clips should not overlap;
    if they do, the one that started last wins."""
    best = None
    for c in track.clips:
        if c.duration > 0 and c.start <= t < c.start + c.duration and (best is None or c.start >= best.start):
            best = c
    return best


def fade_gain(clip: Clip, local_t: float) -> float:
    k = max(0.0, min(1.0, float(clip.opacity)))
    if clip.fade_in > 0:
        k *= max(0.0, min(1.0, local_t / clip.fade_in))
    if clip.fade_out > 0:
        k *= max(0.0, min(1.0, (clip.duration - local_t) / clip.fade_out))
    return k


def _signature(clip: Clip) -> tuple[str, str]:
    """What a renderer instance depends on: a change here means a new instance."""
    return clip.type, json.dumps(clip.params, sort_keys=True, default=str)


def target_kind(target: str) -> str:
    return "face" if target in FACE_TARGETS else target


# ---------------------------------------------------------------- validation
def validate_clip(clip: Clip, target: str, env: RenderEnv) -> list[str]:
    cls = get_renderer(clip.type)
    if cls is None:
        return [f"Unknown clip type '{clip.type}'"]
    out = []
    if target_kind(target) not in cls.targets:
        where = {"face": "a light", "all": "the whole sign", "audio": "an audio track"}[target_kind(target)]
        out.append(f"{cls.label or cls.type} cannot go on {where}")
    try:
        out.extend(str(m) for m in cls.validate(cls.with_defaults(clip.params), target, env) or [])
    except Exception as e:  # a renderer bug must not hide the other problems
        log_once(("validate", clip.type, repr(e)), "validate() of %s raised", clip.type, exc=True)
        out.append(f"Could not check this clip: {e}")
    return out


def validate_set(s: Set, env: RenderEnv, cache: Optional[dict] = None) -> list[dict[str, str]]:
    """Problems with a set: [{"clip_id", "track_id", "message"}]. `cache` (any dict kept by
    the caller between calls) skips re-validating clips whose type, params and target did
    not change."""
    problems: list[dict[str, str]] = []

    def add(track: Track, clip: Clip, msg: str) -> None:
        problems.append({"clip_id": clip.id, "track_id": track.id, "message": msg})

    for track in s.tracks:
        prev_end = None
        prev_clip = None
        for clip in sorted(track.clips, key=lambda c: c.start):
            if clip.duration <= 0:
                add(track, clip, "The clip has no length")
            if clip.start >= s.duration:
                add(track, clip, "The clip starts after the set ends")
            if prev_end is not None and clip.start < prev_end - 1e-6:
                add(track, clip, f"Overlaps the clip before it ({prev_clip.type})")
            prev_end, prev_clip = max(prev_end or 0.0, clip.start + clip.duration), clip
            key = (_signature(clip), track.target)
            if cache is not None and key in cache:
                msgs = cache[key]
            else:
                msgs = validate_clip(clip, track.target, env)
                if cache is not None:
                    cache[key] = msgs
            for m in msgs:
                add(track, clip, m)
    return problems


# ---------------------------------------------------------------- player
@dataclass
class _Instance:
    sig: tuple[str, str]
    renderer: Optional[ClipRenderer] = None
    future: Optional[Future] = None
    failed: bool = False


def _close_renderer(r: ClipRenderer) -> None:
    try:
        r.close()
    except Exception:
        log_once(("close", type(r).__name__), "close() of %s raised", type(r).__name__, exc=True)


def _close_when_made(f: Future) -> None:
    r = f.result()   # _make never raises
    if r is not None:
        _close_renderer(r)


class Player:
    def __init__(self, env: RenderEnv, set_: Optional[Set] = None, *, background_init: bool = True,
                 seed: Optional[int] = None):
        self.env = env
        self.set: Set = set_ if set_ is not None else Set(name="", tracks=[])
        self._background_init = background_init
        self._insts: dict[str, _Instance] = {}
        self._pass_seed = seed if seed is not None else random.getrandbits(31)
        self._closed = False

    # -- set and passes
    def set_set(self, s: Set) -> None:
        """Replace the set (an edit in the editor, or the stored set changed). Renderers of
        clips whose type and params are unchanged keep running; the others are closed."""
        self.set = s
        live = {c.id: _signature(c) for t in s.tracks for c in t.clips}
        for cid in [cid for cid, inst in self._insts.items() if live.get(cid) != inst.sig]:
            self._retire(cid)

    def new_pass(self) -> None:
        """The set starts over: "random" colours re-roll."""
        self._pass_seed = random.getrandbits(31)

    def clip_seed(self, clip: Clip) -> int:
        return (self._pass_seed ^ zlib.crc32(clip.id.encode())) & 0x7FFFFFFF

    # -- instances
    def _retire(self, cid: str) -> None:
        inst = self._insts.pop(cid, None)
        if inst is None:
            return
        if inst.renderer is not None:
            if self._background_init:
                _worker().submit(_close_renderer, inst.renderer)
            else:
                _close_renderer(inst.renderer)
        elif inst.future is not None:
            inst.future.add_done_callback(_close_when_made)

    def _make(self, cls: type[ClipRenderer], clip: Clip) -> Optional[ClipRenderer]:
        try:
            return cls(clip.params, self.env)
        except Exception:
            log_once(("init", clip.id, _signature(clip)), "renderer %s for clip %s failed to start",
                     clip.type, clip.id, exc=True)
            return None

    def _instance(self, clip: Clip, now: bool) -> Optional[ClipRenderer]:
        """The renderer of a clip; made at once if `now`, else (pre-roll) in the background."""
        sig = _signature(clip)
        inst = self._insts.get(clip.id)
        if inst is not None and inst.sig != sig:
            self._retire(clip.id)
            inst = None
        if inst is None:
            cls = get_renderer(clip.type)
            if cls is None:
                log_once(("type", clip.type), "unknown clip type %r: clips of it are skipped", clip.type)
                return None
            inst = _Instance(sig)
            self._insts[clip.id] = inst
            if now or not self._background_init:
                inst.renderer = self._make(cls, clip)
                inst.failed = inst.renderer is None
            else:
                inst.future = _worker().submit(self._make, cls, clip)
        if inst.future is not None and (now or inst.future.done()):
            inst.renderer = inst.future.result()
            inst.failed = inst.renderer is None
            inst.future = None
        return inst.renderer

    def _ctx(self, clip: Clip, t: float, box_h: int, face: Optional[int], wall: _dt.datetime,
             still: bool = False) -> RenderContext:
        s = self.set
        local = t - clip.start
        bpm = float(s.bpm)
        return RenderContext(
            t=local, clip_duration=clip.duration, set_time=t, bpm=bpm,
            beat=(t - s.beat_offset) * bpm / 60.0, clip_beat=local * bpm / 60.0,
            width=config.W, height=box_h, face=face, face_color=face_rgb(self.env.settings, face),
            seed=self.clip_seed(clip), wall_time=wall, still=still)

    # -- the frame
    def _window(self, t: Optional[float], audio: bool) -> list[Clip]:
        """Clips that should have a renderer around set time t (pre-roll .. linger)."""
        if t is None:
            return []
        return [c for track in self.set.tracks
                if not track.muted and (audio or track.target != "audio")
                for c in track.clips
                if c.duration > 0 and c.start - PRE_ROLL <= t < c.start + c.duration + LINGER]

    def prepare(self, t: float, audio: bool = True) -> None:
        """Make, in the background, the renderers needed shortly before set time t (a
        player that will take over at t)."""
        for c in self._window(t, audio):
            if c.id not in self._insts:
                self._instance(c, now=False)

    def render(self, t: float, *, audio: bool = True, lookahead: Optional[float] = None,
               wall_time: Optional[_dt.datetime] = None, still: bool = False) -> tuple[np.ndarray, list[AudioCue]]:
        """The sign at set time t: uint8 (H, W, 3) and the sounds that should play now.
        lookahead: the set time playback jumps to next (a loop back to the start), so the
        renderers needed there are kept or made ahead."""
        wall = wall_time or _dt.datetime.now()
        # Two pictures: `lit` is what is seen through the round lights (every clip, uncut), `rest`
        # what is seen outside them (only clips with the mask off). They are joined through the
        # lights' discs at the end. Cutting each clip to the disc as it is blended would let a
        # lower clip show through the soft rim of the one above it.
        bg = background(self.env.settings)
        lit = bg.copy()
        rest: Optional[np.ndarray] = None       # the background until an unmasked clip paints there
        cues: list[AudioCue] = []

        near = self._window(t, audio)
        ahead = self._window(lookahead, audio)
        wanted = {c.id for c in near} | {c.id for c in ahead}
        for cid in [cid for cid in self._insts if cid not in wanted]:
            self._retire(cid)
        for c in [c for c in near if c.start > t] + ahead:   # pre-roll what starts soon
            if c.id not in self._insts:
                self._instance(c, now=False)

        for track in paint_order(self.set.tracks):
            if track.muted or (track.target == "audio" and not audio):
                continue
            clip = clip_at(track, t)
            if clip is None:
                continue
            r = self._instance(clip, now=True)
            if r is None:
                continue
            if track.target == "audio":
                self._audio(r, clip, t, config.H, None, wall, cues)
                continue
            y0, h, face = target_box(track.target)
            ctx = self._ctx(clip, t, h, face, wall, still)
            if audio:
                self._audio(r, clip, t, h, face, wall, cues, ctx)
            k = fade_gain(clip, ctx.t)
            if k <= 0:
                continue
            try:
                img = r.render(ctx)
            except Exception:
                log_once(("render", clip.id, _signature(clip)), "%s clip %s raised in render()",
                         clip.type, clip.id, exc=True)
                continue
            if img is None:
                continue
            masked = r.masked and (clip.mask is None or clip.mask)
            if not masked and rest is None:
                rest = bg.copy()
            self._blend(lit[y0:y0 + h], None if masked else rest[y0:y0 + h], img, clip, k)

        # canvas = outside + (lit - outside) * mask, in place in `lit` (a fresh copy every frame). The mask has
        # its three channels spelled out: broadcasting it over a last axis of 3 makes numpy's inner loops
        # 3 long, several times slower on a Raspberry Pi.
        outside = bg if rest is None else rest
        np.subtract(lit, outside, out=lit)
        np.multiply(lit, _sign_mask3(), out=lit)
        np.add(lit, outside, out=lit)
        np.clip(lit, 0, 255, out=lit)
        np.rint(lit, out=lit)
        return lit.astype(np.uint8), cues

    def _audio(self, r: ClipRenderer, clip: Clip, t: float, h: int, face: Optional[int], wall: _dt.datetime,
               cues: list[AudioCue], ctx: Optional[RenderContext] = None) -> None:
        ctx = ctx or self._ctx(clip, t, h, face, wall)
        try:
            cue = r.audio(ctx)
        except Exception:
            log_once(("audio", clip.id, _signature(clip)), "%s clip %s raised in audio()",
                     clip.type, clip.id, exc=True)
            return
        if cue is None:
            return
        cues.append(AudioCue(key=clip.id, path=cue.path, position=cue.position,
                             gain=max(0.0, float(cue.gain)) * fade_gain(clip, ctx.t)))

    def _blend(self, box: np.ndarray, outside: Optional[np.ndarray], img: Any, clip: Clip, k: float) -> None:
        """Blend a clip's picture over its box of the lit picture, and over `outside` (its box of
        the picture outside the lights) when the clip is not cut to the lights."""
        img = np.asarray(img)
        h, w = box.shape[:2]
        if img.ndim != 3 or img.shape[0] != h or img.shape[1] != w or img.shape[2] not in (3, 4):
            log_once(("shape", clip.id, _signature(clip)), "%s clip %s returned shape %s, expected (%d, %d, 3|4)",
                     clip.type, clip.id, getattr(img, "shape", None), h, w)
            return
        if img.dtype != np.uint8:
            img = np.clip(img, 0, 255).astype(np.uint8)
        alpha: Any = None
        if img.shape[2] == 4:
            alpha = img[..., 3].astype(np.float32) * (1.0 / 255.0)
        if k < 1.0:
            alpha = k if alpha is None else alpha * k
        rgb = img[..., :3].astype(np.float32)
        for dst in (box,) if outside is None else (box, outside):
            if alpha is None:
                dst[:] = rgb
            elif np.isscalar(alpha):
                dst += (rgb - dst) * alpha
            else:
                dst += (rgb - dst) * alpha[..., None]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for cid in list(self._insts):
            self._retire(cid)


def render_still(s: Set, t: float, env: RenderEnv) -> np.ndarray:
    """One frame of a set (thumbnails): a throwaway player."""
    p = Player(env, s, background_init=False, seed=0)
    try:
        frame, _ = p.render(t, audio=False, still=True)
        return frame
    finally:
        p.close()
