"""Voices and the mixer: pure numpy, no sound card. The PortAudio callback just calls `Mixer.render(n)`.

Timing model: a voice's play position (`rpos`) is the position of the next frame handed to the
device, so what is heard now is `rpos - latency`. A voice runs on its own sample clock and is
only re-seeked when that heard position drifts from the cue by more than `DRIFT_SECONDS`.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import numpy as np

from ..render.base import AudioCue
from .decode import TrackBuffer, TrackDecoder

log = logging.getLogger(__name__)

DRIFT_SECONDS = 0.08
GAIN_SLEW_SECONDS = 0.02       # a full-scale gain change takes this long
FADE_OUT_SECONDS = 0.01        # dropped voices, pause, watchdog
EDGE_SECONDS = 0.002           # ramp where a voice's data starts or stops (seek, underrun)
WATCHDOG_SECONDS = 0.5
LIMIT_KNEE = 0.8


def _ramp(cur: float, target: float, n: int, rate: int, seconds: float) -> tuple[np.ndarray, float]:
    """Per-sample gain moving from `cur` toward `target` no faster than full scale per `seconds`."""
    step = n / (seconds * rate)
    new = min(max(target, cur - step), cur + step)
    return np.linspace(cur, new, n + 1, dtype=np.float32)[1:], new


def soft_limit(x: np.ndarray) -> None:
    """In place: linear up to the knee, then tanh-compressed toward +-1 (smooth, never harsh)."""
    a = np.abs(x)
    over = a > LIMIT_KNEE
    if over.any():
        k = 1.0 - LIMIT_KNEE
        x[over] = np.sign(x[over]) * (LIMIT_KNEE + k * np.tanh((a[over] - LIMIT_KNEE) / k))


class Voice:
    def __init__(self, cue: AudioCue, rate: int, channels: int, start_frame: int):
        self.key = cue.key
        self.path = cue.path
        self.rate = rate
        self.buf = TrackBuffer(rate, channels)
        self.decoder = TrackDecoder(cue.path, self.buf, start_frame)
        self.gain_target = 0.0
        self.gain_cur = 0.0
        self.dying = False
        self.dying_since = 0.0
        self.pending_seek: Optional[int] = None
        self.muted = False                  # between the fade-out of a seek and the seek taking effect
        self.had_prev = False
        self.edge = max(int(EDGE_SECONDS * rate), 8)

    @property
    def error(self) -> Optional[str]:
        return self.buf.error

    def close(self) -> None:
        self.buf.close()

    # -- audio thread
    def mix_into(self, acc: np.ndarray) -> None:
        n = len(acc)
        if self.dying and self.gain_cur == 0.0:
            return
        blk = np.zeros_like(acc)
        buf = self.buf
        first = 0
        pend = self.pending_seek
        if pend is not None:
            if not self.muted:
                # Fade the old position out over a few ms, then jump: no click on seek.
                m = min(n, self.edge)
                lo, hi = buf.read_into(blk[:m])
                self._edges(blk[:m], lo, hi, self.had_prev, force_out=True)
                self.muted = True
                first = m
            if buf.seek(pend + first, timeout=0.001):
                self.pending_seek = None
                self.muted = False
                self.had_prev = False
            else:
                self.had_prev = False
                self._accumulate(acc, blk)
                return
        lo, hi = buf.read_into(blk[first:])
        self._edges(blk[first:], lo, hi, self.had_prev and first == 0)
        self.had_prev = hi == n - first and hi > 0
        self._accumulate(acc, blk)

    def _edges(self, seg: np.ndarray, lo: int, hi: int, prev: bool, force_out: bool = False) -> None:
        """Short ramps where valid data begins/ends inside the block so gaps never click."""
        if hi <= lo:
            return
        if lo > 0 or not prev:
            k = min(self.edge, hi - lo)
            seg[lo:lo + k] *= np.linspace(0.0, 1.0, k, endpoint=False, dtype=np.float32)[:, None]
        if hi < len(seg) or force_out:
            k = min(self.edge, hi - lo)
            seg[hi - k:hi] *= np.linspace(1.0, 0.0, k, endpoint=False, dtype=np.float32)[:, None]

    def _accumulate(self, acc: np.ndarray, blk: np.ndarray) -> None:
        seconds = FADE_OUT_SECONDS if self.dying else GAIN_SLEW_SECONDS
        g, self.gain_cur = _ramp(self.gain_cur, self.gain_target, len(acc), self.rate, seconds)
        acc += blk * g[:, None]


class Mixer:
    def __init__(self, rate: int = 48000, channels: int = 2,
                 clock: Callable[[], float] = time.monotonic, watchdog: float = WATCHDOG_SECONDS):
        self.rate = rate
        self.channels = channels
        self.latency = 0.0                  # seconds between handing a frame over and hearing it
        self._clock = clock
        self._watchdog = watchdog
        self._sync_lock = threading.Lock()  # sync-side only; the audio thread never takes it
        self._live: dict[str, Voice] = {}
        self._dying: list[Voice] = []
        self._voices: tuple[Voice, ...] = ()
        self._playing = False
        self._last_sync: Optional[float] = None
        self._volume = 1.0
        self._vol_cur = 1.0
        self._pause_cur = 0.0
        self.underruns = 0

    # -- configuration
    def configure(self, rate: int, channels: int, latency: float = 0.0) -> None:
        """New output format: all voices are dropped (the next sync() recreates them)."""
        with self._sync_lock:
            self._clear()
            self.rate, self.channels, self.latency = rate, channels, latency

    def set_volume(self, volume: float) -> None:
        self._volume = min(max(float(volume), 0.0), 1.0)

    @property
    def volume(self) -> float:
        return self._volume

    def voice_count(self) -> int:
        return len(self._live)

    def close(self) -> None:
        with self._sync_lock:
            self._clear()

    def _clear(self) -> None:
        voices = self._voices
        self._live, self._dying, self._voices = {}, [], ()
        for v in voices:
            v.close()

    # -- control side
    def sync(self, cues: list[AudioCue], playing: bool = True) -> None:
        with self._sync_lock:
            now = self._clock()
            self._last_sync = now
            self._playing = bool(playing)
            wanted = {c.key: c for c in cues}
            changed = False
            for key in [k for k in self._live if k not in wanted]:
                self._retire(self._live.pop(key), now)
                changed = True
            lat = int(round(self.latency * self.rate))
            limit = int(DRIFT_SECONDS * self.rate)
            for key, cue in wanted.items():
                v = self._live.get(key)
                if v is not None and v.path != cue.path:
                    self._retire(self._live.pop(key), now)
                    v, changed = None, True
                expected = max(0, int(round(cue.position * self.rate)) + lat)
                gain = min(max(float(cue.gain), 0.0), 1.0)
                if v is None:
                    v = Voice(cue, self.rate, self.channels, expected)
                    self._live[key] = v
                    changed = True
                v.gain_target = gain
                if v.error is None:
                    cur = v.pending_seek if v.pending_seek is not None else v.buf.position()
                    if abs(cur - expected) > limit:
                        v.pending_seek = expected
            keep = [v for v in self._dying if now - v.dying_since <= 4 * FADE_OUT_SECONDS + 0.2]
            if len(keep) != len(self._dying):
                for v in self._dying:
                    if v not in keep:
                        v.close()
                self._dying, changed = keep, True
            if changed:
                self._voices = tuple(self._live.values()) + tuple(self._dying)

    def _retire(self, v: Voice, now: float) -> None:
        v.dying, v.dying_since, v.gain_target = True, now, 0.0
        self._dying.append(v)

    def voice_position(self, key: str) -> Optional[float]:
        """Seconds of the file being heard right now for this voice (None if unknown)."""
        v = self._live.get(key)
        if v is None:
            return None
        frame = v.pending_seek if v.pending_seek is not None else v.buf.position()
        return (frame - self.latency * self.rate) / self.rate

    def wait_ready(self, timeout: float = 5.0, seconds: float = 0.25) -> bool:
        """Block until every voice has buffered some audio (or failed). For tests and tools only."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if all(v.buf.ready(seconds) for v in self._voices):
                return True
            time.sleep(0.005)
        return False

    # -- audio thread
    def render(self, n: int) -> np.ndarray:
        """The next n frames as float32 (n, channels). Never blocks, never decodes."""
        out = np.zeros((n, self.channels), np.float32)
        last = self._last_sync
        active = self._playing and last is not None and (self._clock() - last) <= self._watchdog
        target = 1.0 if active else 0.0
        if self._pause_cur == 0.0 and target == 0.0:
            return out                      # paused or stalled: voices stand still where they are
        pause, self._pause_cur = _ramp(self._pause_cur, target, n, self.rate, FADE_OUT_SECONDS)
        vol, self._vol_cur = _ramp(self._vol_cur, self._volume, n, self.rate, GAIN_SLEW_SECONDS)
        for v in self._voices:
            v.mix_into(out)
        out *= (pause * vol)[:, None]
        soft_limit(out)
        return out
