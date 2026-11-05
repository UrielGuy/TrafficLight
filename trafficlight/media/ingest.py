"""Turning an upload into what the sign plays: probing, proxies, sound tracks, thumbnails, peaks.

Everything here uses PyAV (its bundled FFmpeg) and Pillow; nothing shells out. Functions are
plain and blocking: the library runs them on its ingest thread. Failures raise IngestError with
a message meant for the person who uploaded the file.
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Callable, Iterator, Optional

import av
from av.video.reformatter import VideoReformatter
import numpy as np
from PIL import Image, ImageOps, ImageSequence

from .. import config

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac"}
ACCEPTED_EXTS = VIDEO_EXTS | IMAGE_EXTS | AUDIO_EXTS

# Formats Pillow reads that we treat as pictures (MPO = camera JPEG with a preview, not an animation).
_PIL_FORMATS = {"PNG", "JPEG", "MPO", "BMP", "GIF", "WEBP"}
_ANIMATED_FORMATS = {"PNG", "GIF", "WEBP"}

MAX_FPS = 60
AUDIO_RATE = 48000          # what sound cards run at natively; the mixer then rarely has to resample
PEAKS_RATE = 100.0          # waveform points per second, reduced for long files
MAX_PEAKS = 20000
THUMB_MAX = 256
GOP_SECONDS = 0.5           # keyframe distance of the proxy: bounds the cost of a seek

Check = Callable[[], None]  # raises to cancel a running ingest


class IngestError(Exception):
    """A readable reason why a file cannot be used."""


class Cancelled(Exception):
    """Raised by a Check when the item was deleted while it was being ingested."""


@dataclass
class Probe:
    kind: str                 # "video" | "image" | "audio"
    animated: bool = False    # a GIF / WebP / APNG animation (decoded with Pillow, played as a video)


@dataclass
class Result:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False


def _nothing() -> None:
    return None


def short_error(e: BaseException) -> str:
    msg = str(e).strip() or type(e).__name__
    return msg if len(msg) < 200 else msg[:197] + "..."


def kind_from_ext(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    return None


# ------------------------------------------------------------------ probing
def _visual_streams(container) -> list:
    """Video streams that are moving pictures, not cover art attached to a song."""
    out = []
    for s in container.streams.video:
        if s.disposition & av.stream.Disposition.attached_pic:
            continue
        out.append(s)
    return out


def probe(path: Path) -> Probe:
    """What the file is, from its content. IngestError if it is nothing we can play."""
    try:
        with Image.open(path) as im:
            if im.format in _PIL_FORMATS:
                animated = im.format in _ANIMATED_FORMATS and getattr(im, "n_frames", 1) > 1
                return Probe("video" if animated else "image", animated)
    except Exception:
        pass
    try:
        with av.open(str(path)) as c:
            if _visual_streams(c):
                return Probe("video")
            if c.streams.audio:
                return Probe("audio")
    except Exception as e:
        raise IngestError(f"Not a video, picture or sound file that can be read ({short_error(e)})") from e
    raise IngestError("The file has no picture and no sound")


# ------------------------------------------------------------------ geometry
def proxy_size(w: float, h: float) -> tuple[int, int]:
    """Smallest size that still covers the largest box the sign shows (the whole sign), never larger
    than the source, aspect kept. Even numbers: 4:2:0 video needs them."""
    s = min(1.0, max(config.W / w, config.H / h))
    pw = max(2, int(math.ceil(w * s / 2.0)) * 2)
    ph = max(2, int(math.ceil(h * s / 2.0)) * 2)
    return pw, ph


def _write_jpeg(img: Image.Image, dest: Path) -> None:
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGBA", img.size, (24, 24, 24, 255))
        img = Image.alpha_composite(bg, img)
    img = img.convert("RGB")
    img.thumbnail((THUMB_MAX, THUMB_MAX), Image.Resampling.LANCZOS)
    tmp = dest.with_name(dest.name + ".tmp")
    img.save(tmp, "JPEG", quality=85)
    os.replace(tmp, dest)


def _write_json(obj, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, dest)


# ------------------------------------------------------------------ images
def ingest_image(src: Path, out: Path) -> Result:
    """proxy.png (upright, RGB or RGBA, no larger than the sign needs) and thumb.jpg."""
    try:
        with Image.open(src) as im:
            im.load()
            im = ImageOps.exif_transpose(im)
            alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
            im = im.convert("RGBA" if alpha else "RGB")
    except Exception as e:
        raise IngestError(f"The picture cannot be read ({short_error(e)})") from e
    w, h = im.size
    pw, ph = proxy_size(w, h)
    scaled = im if (pw, ph) == (w, h) or pw >= w else im.resize((pw, ph), Image.Resampling.LANCZOS)
    tmp = out / "proxy.png.tmp"
    scaled.save(tmp, "PNG")
    os.replace(tmp, out / "proxy.png")
    _write_jpeg(im, out / "thumb.jpg")
    return Result(width=w, height=h)


# ------------------------------------------------------------------ video
class _ProxyWriter:
    """Encodes RGB frames at a constant frame rate into proxy.mp4: H.264, short GOP, no B-frames,
    no sound. Constant rate means frame n is at n / fps, which keeps the player's seeking simple."""

    def __init__(self, dest: Path, width: int, height: int, fps: Fraction):
        self.dest = dest
        self.tmp = dest.with_name("proxy.tmp.mp4")
        self.fps = fps
        self.count = 0
        self.c = av.open(str(self.tmp), "w", format="mp4")
        try:
            gop = max(1, round(float(fps) * GOP_SECONDS))
            codec = "libx264" if "libx264" in av.codecs_available else "mpeg4"
            s = self.c.add_stream(codec, rate=fps)
            s.width, s.height, s.pix_fmt = width, height, "yuv420p"
            s.codec_context.gop_size = gop
            s.codec_context.max_b_frames = 0
            if codec == "libx264":
                # Baseline + fastdecode: no CABAC, no B-frames, no deblocking -> the cheapest H.264 to
                # decode (three at once on a Pi), and every browser plays it.
                s.options = {"preset": "veryfast", "tune": "fastdecode", "profile": "baseline", "crf": "23",
                             "threads": "2",
                             "x264-params": f"keyint={gop}:min-keyint={gop}:scenecut=0:bframes=0"}
            else:
                s.options = {"qscale": "3", "g": str(gop), "bf": "0"}
            self.s = s
        except Exception:
            self.c.close()
            raise

    def add(self, rgb: np.ndarray) -> None:
        f = av.VideoFrame.from_ndarray(np.ascontiguousarray(rgb), format="rgb24")
        f.pts = self.count
        f.time_base = Fraction(1) / self.fps
        for p in self.s.encode(f):
            self.c.mux(p)
        self.count += 1

    def finish(self) -> None:
        for p in self.s.encode():
            self.c.mux(p)
        self.c.close()
        os.replace(self.tmp, self.dest)

    def abort(self) -> None:
        try:
            self.c.close()
        except Exception:
            pass
        try:
            self.tmp.unlink()
        except OSError:
            pass


class _CfrResampler:
    """Feeds source frames with their own timestamps (seconds from the first frame) to the
    proxy writer as a constant-rate sequence: each output slot shows the source frame nearest it."""

    def __init__(self, writer: _ProxyWriter, thumb_at: float):
        self.w = writer
        self.prev: Optional[np.ndarray] = None
        self.thumb_at = thumb_at
        self.thumb: Optional[np.ndarray] = None

    def push(self, t: float, rgb: np.ndarray) -> None:
        if self.prev is not None:
            self._emit_until(round(t * float(self.w.fps)))
        self.prev = rgb
        if self.thumb is None or t <= self.thumb_at:
            self.thumb = rgb

    def _emit_until(self, slot: int) -> None:
        while self.w.count < slot:
            self.w.add(self.prev)

    def end(self, t_end: float) -> None:
        if self.prev is None:
            return
        self._emit_until(max(self.w.count + 1, round(t_end * float(self.w.fps))))


def _rational_fps(rate) -> Optional[Fraction]:
    try:
        f = Fraction(rate)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if f <= 0 or f > 1000:
        return None
    return f


def ingest_video(src: Path, out: Path, check: Check = _nothing) -> Result:
    """proxy.mp4, audio.wav (if the file has sound), thumb.jpg, peaks.json."""
    try:
        c = av.open(str(src))
    except Exception as e:
        raise IngestError(f"The video cannot be opened ({short_error(e)})") from e
    try:
        streams = _visual_streams(c)
        if not streams:
            raise IngestError("The file has no video picture")
        vs = streams[0]
        cc = vs.codec_context
        cc.thread_type = "AUTO"
        cc.thread_count = 2          # an upload during a show must leave CPU for the render thread
        if not cc.width or not cc.height:
            raise IngestError("The video has no picture size")
        src_fps = _rational_fps(vs.average_rate) or _rational_fps(vs.guessed_rate) or Fraction(30)
        fps = src_fps if src_fps <= MAX_FPS else Fraction(MAX_FPS)
        est = float(vs.duration * vs.time_base) if vs.duration else (c.duration or 0) / 1e6
        thumb_at = min(1.0, est / 3.0) if est > 0 else 0.0

        writer: Optional[_ProxyWriter] = None
        cfr: Optional[_CfrResampler] = None
        t0: Optional[float] = None
        last_t = 0.0
        frame_dur = 1.0 / float(src_fps)
        rot = 0
        size = (0, 0)
        scaled = (0, 0)
        decoded = 0
        sws = VideoReformatter()
        try:
            for frame in _decode_frames(c, vs):
                check()
                t = frame.time if frame.time is not None else decoded / float(src_fps)
                if t0 is None:
                    t0 = t
                    rot = int(round((getattr(frame, "rotation", 0) or 0) / 90.0)) % 4
                    sar = vs.sample_aspect_ratio or cc.sample_aspect_ratio
                    sar_f = float(sar) if sar and float(sar) > 0 else 1.0
                    dw, dh = frame.width * sar_f, float(frame.height)
                    if rot % 2:
                        dw, dh = dh, dw
                    size = (int(round(dw)), int(round(dh)))
                    pw, ph = proxy_size(dw, dh)
                    scaled = (ph, pw) if rot % 2 else (pw, ph)
                    writer = _ProxyWriter(out / "proxy.mp4", pw, ph, fps)
                    cfr = _CfrResampler(writer, thumb_at)
                if t < last_t - 1e-6 and decoded:
                    continue            # a timestamp going backwards: drop rather than reorder
                rgb = sws.reformat(frame, width=scaled[0], height=scaled[1], format="rgb24",
                                   interpolation="AREA", threads=1).to_ndarray()
                if rot:
                    # FFmpeg's rotation is counter-clockwise in degrees; np.rot90 turns counter-clockwise.
                    rgb = np.rot90(rgb, rot)
                cfr.push(t - t0, rgb)
                last_t = t
                if frame.duration and frame.time_base:
                    frame_dur = float(frame.duration * frame.time_base) or frame_dur
                decoded += 1
            if writer is None or cfr is None:
                raise IngestError("The video has no picture that can be decoded")
            cfr.end(last_t - t0 + frame_dur)
            writer.finish()
        except BaseException:
            if writer is not None:
                writer.abort()
            raise
        n = writer.count
        duration = n / float(fps)
        _write_jpeg(Image.fromarray(cfr.thumb), out / "thumb.jpg")
        has_audio = False
        if c.streams.audio:
            try:
                has_audio = extract_audio(src, out, start=t0, duration=duration, check=check)
            except IngestError as e:
                log.warning("%s: sound track skipped: %s", src.name, e)
        return Result(duration=duration, width=size[0], height=size[1], fps=float(fps), has_audio=has_audio)
    finally:
        c.close()


def _decode_frames(c, vs) -> Iterator[av.VideoFrame]:
    """Frames of one stream; a damaged tail ends the video instead of failing it."""
    got = 0
    try:
        for packet in c.demux(vs):
            try:
                frames = packet.decode()
            except av.error.FFmpegError:
                continue         # one bad packet: skip it, the decoder resyncs at the next keyframe
            for f in frames:
                got += 1
                yield f
    except av.error.FFmpegError as e:
        if not got:
            raise IngestError(f"The video cannot be decoded ({short_error(e)})") from e
        log.warning("video ends early after %d frames: %s", got, e)


def ingest_animation(src: Path, out: Path, check: Check = _nothing) -> Result:
    """An animated GIF / WebP / PNG, decoded with Pillow (frame delays, disposal, transparency),
    flattened on black into a proxy like any video."""
    try:
        im = Image.open(src)
    except Exception as e:
        raise IngestError(f"The animation cannot be read ({short_error(e)})") from e
    writer: Optional[_ProxyWriter] = None
    try:
        delays = []
        for fr in ImageSequence.Iterator(im):
            fr.load()        # WebP reports a frame's delay only once the frame is decoded
            d = int(fr.info.get("duration") or 0)
            delays.append(100 if d <= 10 else d)       # what browsers do with 0 / tiny delays
        if not delays:
            raise IngestError("The animation has no frames")
        fps = min(Fraction(MAX_FPS), Fraction(1000, min(delays)))
        w, h = im.size
        pw, ph = proxy_size(w, h)
        writer = _ProxyWriter(out / "proxy.mp4", pw, ph, fps)
        cfr = _CfrResampler(writer, thumb_at=min(1.0, sum(delays) / 3000.0))
        t = 0.0
        for fr, d in zip(ImageSequence.Iterator(im), delays):
            check()
            rgba = fr.convert("RGBA")
            if rgba.size != (pw, ph):
                rgba = rgba.resize((pw, ph), Image.Resampling.LANCZOS)
            a = np.asarray(rgba, dtype=np.uint16)
            rgb = (a[..., :3] * a[..., 3:4] // 255).astype(np.uint8)
            cfr.push(t, rgb)
            t += d / 1000.0
        cfr.end(t)
        writer.finish()
    except (IngestError, Cancelled):
        if writer is not None:
            writer.abort()
        raise
    except Exception as e:
        if writer is not None:
            writer.abort()
        raise IngestError(f"The animation cannot be decoded ({short_error(e)})") from e
    finally:
        im.close()
    _write_jpeg(Image.fromarray(cfr.thumb), out / "thumb.jpg")
    return Result(duration=writer.count / float(fps), width=w, height=h, fps=float(fps))


# ------------------------------------------------------------------ audio
class _Peaks:
    def __init__(self):
        self.per = AUDIO_RATE / PEAKS_RATE
        self.n = int(self.per)
        self.rest = np.zeros(0, np.float32)
        self.values: list[float] = []

    def add(self, mono_abs: np.ndarray) -> None:
        buf = np.concatenate([self.rest, mono_abs]) if len(self.rest) else mono_abs
        k = len(buf) // self.n
        if k:
            self.values.extend(buf[:k * self.n].reshape(k, self.n).max(axis=1).tolist())
        self.rest = buf[k * self.n:]

    def result(self) -> dict:
        vals = list(self.values)
        if len(self.rest):
            vals.append(float(self.rest.max()))
        rate = PEAKS_RATE
        arr = np.asarray(vals, np.float32)
        if len(arr) > MAX_PEAKS:
            f = int(math.ceil(len(arr) / MAX_PEAKS))
            pad = (-len(arr)) % f
            arr = np.pad(arr, (0, pad)).reshape(-1, f).max(axis=1)
            rate = PEAKS_RATE / f
        return {"rate": rate, "peaks": [round(float(v), 3) for v in np.clip(arr, 0, 1)]}


def extract_audio(src: Path, out: Path, start: Optional[float] = None, duration: Optional[float] = None,
                  check: Check = _nothing) -> bool:
    """Decode the first sound stream of `src` into audio.wav (48 kHz stereo 16-bit PCM: seeking is
    exact and instant) and peaks.json. For a video, `start` is the time of its first picture and
    `duration` its length: the sound is shifted and cut so that second x of audio.wav is second x
    of the proxy. Returns False if the stream holds no samples."""
    try:
        c = av.open(str(src))
    except Exception as e:
        raise IngestError(f"The sound cannot be opened ({short_error(e)})") from e
    tmp = out / "audio.tmp.wav"
    o = None
    try:
        if not c.streams.audio:
            raise IngestError("The file has no sound")
        st = c.streams.audio[0]
        res = av.AudioResampler(format="s16", layout="stereo", rate=AUDIO_RATE)
        o = av.open(str(tmp), "w", format="wav")
        os_ = o.add_stream("pcm_s16le", rate=AUDIO_RATE)
        os_.layout = "stereo"
        peaks = _Peaks()
        limit = None if duration is None else int(round(duration * AUDIO_RATE))
        written = 0
        first_time: Optional[float] = None
        skip = 0          # samples to drop because the sound starts before the first picture
        lead_done = False

        def emit(pcm: np.ndarray) -> None:
            nonlocal written
            if limit is not None:
                pcm = pcm[:max(0, limit - written)]
            if not len(pcm):
                return
            peaks.add(np.abs(pcm.astype(np.float32)).max(axis=1) / 32768.0)
            fr = av.AudioFrame.from_ndarray(np.ascontiguousarray(pcm.reshape(1, -1)), format="s16",
                                            layout="stereo")
            fr.sample_rate = AUDIO_RATE
            fr.pts = written
            for p in os_.encode(fr):
                o.mux(p)
            written += len(pcm)

        def feed(frames) -> None:
            nonlocal skip, lead_done
            for rf in frames:
                pcm = rf.to_ndarray().reshape(-1, 2)
                if not lead_done:
                    lead_done = True
                    if start is not None and first_time is not None:
                        lead = int(round((first_time - start) * AUDIO_RATE))
                        for i in range(0, max(0, lead), AUDIO_RATE):
                            emit(np.zeros((min(AUDIO_RATE, lead - i), 2), np.int16))
                        skip = max(0, -lead)
                if skip:
                    d = min(skip, len(pcm))
                    pcm, skip = pcm[d:], skip - d
                emit(pcm)

        for packet in c.demux(st):
            check()
            try:
                frames = packet.decode()
            except av.error.FFmpegError:
                continue
            for f in frames:
                if first_time is None:
                    first_time = f.time
                f.pts = None         # let the resampler run continuously across damaged stretches
                feed(res.resample(f))
            if limit is not None and written >= limit:
                break
        feed(res.resample(None))
        for p in os_.encode(None):
            o.mux(p)
        o.close()
        o = None
        if written == 0:
            tmp.unlink(missing_ok=True)
            return False
        os.replace(tmp, out / "audio.wav")
        _write_json(peaks.result(), out / "peaks.json")
        return True
    except (IngestError, Cancelled):
        raise
    except Exception as e:
        raise IngestError(f"The sound cannot be decoded ({short_error(e)})") from e
    finally:
        if o is not None:
            try:
                o.close()
            except Exception:
                pass
            tmp.unlink(missing_ok=True)
        c.close()


def ingest_audio(src: Path, out: Path, check: Check = _nothing) -> Result:
    """audio.wav, peaks.json and a waveform picture as thumb.jpg."""
    if not extract_audio(src, out, check=check):
        raise IngestError("The file holds no sound")
    with av.open(str(out / "audio.wav")) as c:
        st = c.streams.audio[0]
        duration = float(st.duration * st.time_base) if st.duration else (c.duration or 0) / 1e6
    peaks = json.loads((out / "peaks.json").read_text(encoding="utf-8"))
    waveform_image(peaks["peaks"]).save(out / "thumb.jpg", "JPEG", quality=85)
    return Result(duration=duration, has_audio=True)


def waveform_image(peaks: list[float], w: int = THUMB_MAX, h: int = THUMB_MAX // 2) -> Image.Image:
    img = np.empty((h, w, 3), np.uint8)
    img[:] = (20, 22, 28)
    if peaks:
        p = np.asarray(peaks, np.float32)
        starts = (np.arange(w) * len(p) // w).astype(np.intp)
        cols = np.maximum.reduceat(p, starts)       # max over each column's share of the peaks
        half = np.maximum(cols * (h / 2 - 2), 0.5)
        y = np.abs(np.arange(h, dtype=np.float32) + 0.5 - h / 2)[:, None]
        cover = np.clip(half[None, :] - y + 0.5, 0, 1)[..., None]
        ink = np.array((80, 200, 255), np.float32)
        img = (img * (1 - cover) + ink * cover).astype(np.uint8)
    return Image.fromarray(img)


# ------------------------------------------------------------------ what a browser plays
_BROWSER_VIDEO = {"h264", "vp8", "vp9", "av1"}
_BROWSER_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac", "pcm_s16le", "pcm_s24le", "pcm_u8", "pcm_f32le"}


def browser_playable(path: Path, kind: str) -> bool:
    """Whether the original can go straight into <video> / <audio> in common browsers."""
    ext = path.suffix.lower()
    if kind == "image":
        return ext in IMAGE_EXTS
    try:
        with av.open(str(path)) as c:
            vids = _visual_streams(c)
            auds = [s.codec_context.codec.canonical_name for s in c.streams.audio]
            if kind == "video":
                if ext not in (".mp4", ".m4v", ".mov", ".webm") or not vids:
                    return False
                v = vids[0].codec_context
                name = v.codec.canonical_name
                if name not in _BROWSER_VIDEO or (name == "h264" and v.format and v.format.name != "yuv420p"):
                    return False
                return all(a in _BROWSER_AUDIO for a in auds)
            if kind == "audio":
                return ext in AUDIO_EXTS and bool(auds) and auds[0] in _BROWSER_AUDIO and not vids
    except Exception:
        return False
    return False
