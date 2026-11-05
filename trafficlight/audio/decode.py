"""Streaming file decoder: a background thread keeps a ring buffer filled ahead of the play position.

`TrackBuffer` is what the audio callback reads (it never waits on slow code: the lock is only held
for index updates and memcpy). `TrackDecoder` is the thread that fills it, with seeking.
Positions are absolute frame numbers at the mixer's sample rate.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# A forward skip shorter than this is cheaper to decode through than to seek.
_SKIP_AHEAD_SECONDS = 1.5


class TrackBuffer:
    """Ring buffer holding the contiguous frames [start, end); `rpos` is the play position."""

    def __init__(self, rate: int, channels: int, seconds: float = 6.0):
        self.rate = rate
        self.channels = channels
        self.cap = max(int(seconds * rate), 4096)
        self.ring = np.zeros((self.cap, channels), np.float32)
        self.cond = threading.Condition(threading.Lock())
        self.start = 0
        self.end = 0
        self.rpos = 0
        self.eof: Optional[int] = None      # total frames of the file, once the decoder has reached its end
        self.error: Optional[str] = None
        self.closed = False
        self.gen = 0                        # bumped by every real seek; the decoder restarts when it changes
        self.seek_target = 0
        self._skip = 0                      # frames the callback could not read because the lock was busy

    # -- consumer side (audio callback)
    def read_into(self, dest: np.ndarray) -> tuple[int, int]:
        """Fill `dest` (n, ch; must be zeros) from the play position and advance it by n.
        Returns (lo, hi): the valid range inside dest; the rest stays silent."""
        n = len(dest)
        # The holders (decoder/sync) only copy a few KB, but they may be descheduled mid-copy: wait a
        # moment (releases the GIL so they can finish) rather than drop a whole block.
        if not self.cond.acquire(timeout=0.002):
            self._skip += n
            return 0, 0
        try:
            a = self.rpos + self._skip
            self._skip = 0
            b = a + n
            lo_abs, hi_abs = max(a, self.start), min(b, self.end)
            lo = hi = 0
            if hi_abs > lo_abs:
                lo, hi = lo_abs - a, hi_abs - a
                self._copy_out(lo_abs, hi_abs, dest[lo:hi])
            self.rpos = b
            if b >= self.end:
                self.start = self.end = b
            else:
                self.start = max(self.start, b)
            return lo, hi
        finally:
            self.cond.release()

    def _copy_out(self, a: int, b: int, dest: np.ndarray) -> None:
        i = a % self.cap
        first = min(b - a, self.cap - i)
        dest[:first] = self.ring[i:i + first]
        if b - a > first:
            dest[first:] = self.ring[:b - a - first]

    # -- control side
    def seek(self, target: int, timeout: float = -1) -> bool:
        if not self.cond.acquire(timeout=timeout):
            return False
        try:
            target = max(0, int(target))
            self.rpos = target
            self._skip = 0
            if self.start <= target <= self.end:
                self.start = target
            elif self.eof is None and self.end < target <= self.end + int(_SKIP_AHEAD_SECONDS * self.rate):
                self.start = self.end = target      # the decoder is about to get there anyway
            else:
                self.start = self.end = target
                self.eof = None
                self.gen += 1
                self.seek_target = target
                self.cond.notify_all()
            return True
        finally:
            self.cond.release()

    def position(self) -> int:
        return self.rpos + self._skip

    def buffered(self) -> int:
        with self.cond:
            return max(0, self.end - max(self.start, self.rpos))

    def ready(self, seconds: float = 0.25) -> bool:
        """Enough data to play, or nothing more will ever come."""
        with self.cond:
            if self.error or self.closed:
                return True
            have = self.end - max(self.start, self.rpos)
            return have >= int(seconds * self.rate) or (self.eof is not None and self.rpos >= self.eof - 1) \
                or (self.eof is not None and have >= self.eof - self.rpos)

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    # -- producer side (decoder thread)
    def write(self, gen: int, start: int, data: np.ndarray) -> Optional[int]:
        """Append `data` that begins at absolute frame `start`. Returns how many frames were consumed
        (stale or trimmed ones count), or None when a newer seek made this data obsolete."""
        with self.cond:
            if self.closed or gen != self.gen:
                return None
            n = len(data)
            if start + n <= self.end:
                return n
            skipped = 0
            if start < self.end:
                skipped = self.end - start
                data, start = data[skipped:], self.end
            if start > self.end:
                if self.end == self.start:
                    self.start = self.end = start       # nothing buffered: just begin later (silence before)
                else:
                    gap = min(start - self.end, self.cap - (self.end - self.start))
                    if gap > 0:
                        self._copy_in(self.end, np.zeros((gap, self.channels), np.float32))
                        self.end += gap
                    if start > self.end:
                        return skipped
            free = self.cap - (self.end - self.start)
            k = min(len(data), free)
            if k > 0:
                self._copy_in(self.end, data[:k])
                self.end += k
            return skipped + k

    def _copy_in(self, a: int, data: np.ndarray) -> None:
        i = a % self.cap
        first = min(len(data), self.cap - i)
        self.ring[i:i + first] = data[:first]
        if len(data) > first:
            self.ring[:len(data) - first] = data[first:]

    def finish(self, gen: int, total: int) -> None:
        with self.cond:
            if gen == self.gen:
                self.eof = total

    def fail(self, message: str) -> None:
        with self.cond:
            self.error = message


class TrackDecoder(threading.Thread):
    def __init__(self, path: str, buf: TrackBuffer, target_frame: int):
        super().__init__(name="audio-decode", daemon=True)
        self.path = path
        self.buf = buf
        with buf.cond:                       # the first "seek" is the initial positioning
            buf.rpos = buf.start = buf.end = max(0, target_frame)
            buf.seek_target = buf.rpos
            buf.gen += 1
        self.start()

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:             # a corrupt file must only silence this voice
            log.warning("decoding %s failed: %s", self.path, exc)
            self.buf.fail(f"{type(exc).__name__}: {exc}")

    def _run(self) -> None:
        import av
        buf = self.buf
        container = av.open(self.path)
        try:
            if not container.streams.audio:
                raise ValueError("no audio stream")
            stream = container.streams.audio[0]
            my_gen = -1
            while True:
                with buf.cond:
                    while not buf.closed and buf.gen == my_gen and buf.eof is not None:
                        buf.cond.wait(0.5)
                    if buf.closed:
                        return
                    if buf.gen != my_gen:
                        my_gen, target = buf.gen, buf.seek_target
                    else:
                        target = None
                if target is not None:
                    self._decode_from(container, stream, my_gen, target)
        finally:
            container.close()

    def _decode_from(self, container, stream, gen: int, target: int) -> None:
        import av
        buf = self.buf
        rate, ch = buf.rate, buf.channels
        mono_source = stream.codec_context.layout.nb_channels == 1
        # A mono source is duplicated by hand: the resampler's mono->stereo downmix matrix is -3 dB.
        layout = "mono" if (mono_source and ch == 2) else ("stereo" if ch == 2 else "mono")
        resampler = av.AudioResampler(format="flt", layout=layout, rate=rate)
        try:
            container.seek(int(target / rate / stream.time_base), stream=stream, backward=True, any_frame=False) \
                if target > 0 else container.seek(0, stream=stream)
        except Exception:
            container.seek(0)
        cursor: Optional[int] = None

        def emit(frames) -> bool:
            nonlocal cursor
            for f in frames:
                arr = f.to_ndarray().reshape(-1, 1 if layout == "mono" else ch).astype(np.float32, copy=False)
                if layout == "mono" and ch == 2:
                    arr = np.repeat(arr, 2, axis=1)
                if not self._put(gen, cursor, arr):
                    return False
                cursor += len(arr)
            return True

        for frame in container.decode(stream):
            if buf.closed or buf.gen != gen:
                return
            if cursor is None:
                cursor = int(round(frame.pts * stream.time_base * rate)) if frame.pts is not None else target
            if not emit(resampler.resample(frame)):
                return
        if cursor is None:
            cursor = target
        if not emit(resampler.resample(None)):
            return
        buf.finish(gen, cursor)

    def _put(self, gen: int, cursor: int, arr: np.ndarray) -> bool:
        """Write a chunk, waiting for room. False = obsolete (seek or close)."""
        buf = self.buf
        if cursor < 0:                       # leading padding before time zero
            arr, cursor = arr[-cursor:], 0
        while len(arr):
            k = buf.write(gen, cursor, arr)
            if k is None:
                return False
            arr, cursor = arr[k:], cursor + k
            if len(arr):
                with buf.cond:
                    if buf.closed or buf.gen != gen:
                        return False
                    buf.cond.wait(0.05)
        return True
