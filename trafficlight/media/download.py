"""Fetching a video from a link (YouTube and whatever else yt-dlp knows, or a direct file URL).

yt-dlp is used as a library, and only to find the streams: there is no ffmpeg executable on the
sign's machines, so yt-dlp can neither merge separate picture and sound downloads nor cut them.
Instead:

* the picture and the sound are chosen here (the best picture up to `max_height`, H.264 preferred:
  cheapest to decode on a Pi and every browser plays it; the best sound, Opus preferred) and fetched
  as two files, over plain HTTP range requests where the site allows it (YouTube serves short ranges
  at full speed but paces one long request at about twice playback speed), else with yt-dlp's own
  downloader;
* when only part of a long video is wanted (`duration`), only about that much of the file is fetched
  (the formats YouTube serves can be read up to wherever they were cut off);
* PyAV then copies both streams, packet for packet and with their timestamps, into one file (MP4 for
  H.264 / AAC): that file is the item's original, at full quality, with picture and sound in sync as
  the site served them. Nothing is re-encoded. (MP4 holds H.264 with Opus too, and browsers play it.)

`fetch` is what MediaLibrary calls on its download thread; tests pass a fake instead.
"""
from __future__ import annotations

import heapq
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from urllib.parse import unquote, urlparse

import av

from .ingest import ACCEPTED_EXTS, Cancelled, IngestError, _visual_streams, short_error

log = logging.getLogger(__name__)

CHUNK = 8 << 20             # bytes per range request
TIMEOUT = 20                # seconds without an answer before a network request fails
MARGIN_SECONDS = 10.0       # fetched past the wanted end of a part, for the bitrate guess being off
CUT_PAD = 1.0               # seconds of stream kept after the wanted end (the trim cuts exactly)

Check = Callable[[], None]
Progress = Callable[[float], None]


class DownloadError(IngestError):
    """A readable reason why a link could not be fetched."""


@dataclass
class Fetched:
    path: Path                      # the file to keep as the original
    title: str
    start: float = 0.0              # where the wanted part begins in it (seconds from its first picture)
    end: Optional[float] = None     # where it ends; None = the end of the file


Fetcher = Callable[..., Fetched]


def _nothing(*_a) -> None:
    return None


class _Log:
    """yt-dlp's chatter goes to our log, not to the console."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    info = debug

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp warning: %s", msg)

    def error(self, msg: str) -> None:
        log.info("yt-dlp error: %s", msg)


def _options(**extra: Any) -> dict:
    return {"quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True, "playlist_items": "1",
            "socket_timeout": TIMEOUT, "retries": 2, "extractor_retries": 1, "fragment_retries": 3,
            "logger": _Log(), "fixup": "never", "postprocessors": [], **extra}


# ------------------------------------------------------------------ errors
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# no internet at all: names do not resolve, no route
_OFFLINE = ("getaddrinfo", "name or service not known", "temporary failure in name resolution",
            "nodename nor servname", "network is unreachable", "no route to host", "failed to resolve",
            "no address associated", "errno 11001", "errno 10065")
# the address is known but nothing answers
_UNREACHABLE = ("timed out", "connection refused", "unable to connect", "connection reset", "errno 10060",
                "errno 10061")
_GONE = ("video unavailable", "private video", "has been removed", "no longer available", "account associated",
         "http error 404", "http error 410", "does not exist", "is not available", "members-only",
         "this video is unavailable", "copyright")


def explain(e: BaseException, url: str) -> str:
    """What went wrong, for the person who pasted the link."""
    raw = _ANSI.sub("", str(e)).strip()
    raw = re.sub(r"^(ERROR:\s*)+", "", raw)
    raw = re.sub(r"^\[[^\]]+\]\s*[^:]*:\s*", "", raw)     # "[youtube] abc123: "
    low = raw.lower()
    host = urlparse(url).hostname or url
    if any(k in low for k in _OFFLINE):
        return f"No internet connection: {host} could not be reached"
    if any(k in low for k in _UNREACHABLE):
        return f"{host} could not be reached (the site does not answer, or there is no internet)"
    if "unsupported url" in low:
        return "This link does not lead to a video that can be fetched (unsupported site or page)"
    if any(k in low for k in _GONE):
        return f"The video is not available ({raw[:160]})"
    if "sign in" in low or "confirm you" in low:
        return f"The site refuses to hand out this video without signing in ({raw[:160]})"
    return f"The link could not be fetched ({raw[:200] or type(e).__name__})"


# ------------------------------------------------------------------ choosing streams
_PROTOCOLS_OWN = ("http", "https")
_PROTOCOLS_YTDLP = ("m3u8", "m3u8_native", "http_dash_segments", "http_dash_segments_generator")


def _has(f: dict, key: str) -> Optional[bool]:
    v = f.get(key)
    if v is None:
        return None          # unknown (a direct link): may well have it
    return v != "none"


def _usable(f: dict) -> bool:
    if f.get("has_drm") or f.get("ext") in ("mhtml", "f4m", "f4v"):
        return False
    proto = f.get("protocol") or "https"
    return bool(f.get("url") or f.get("fragments")) and proto in _PROTOCOLS_OWN + _PROTOCOLS_YTDLP


def _size_class(f: dict) -> int:
    """The "1080" of 1080p: the shorter side (a portrait video's height is its long side)."""
    w, h = f.get("width") or 0, f.get("height") or 0
    return min(w, h) if w and h else h


def _vcodec_rank(f: dict) -> int:
    v = (f.get("vcodec") or "").lower()
    if v.startswith(("avc", "h264")):
        return 3
    if v.startswith(("vp09", "vp9")) or not v:
        return 2
    if v.startswith("av01"):
        return 1
    return 0


def _acodec_rank(f: dict) -> int:
    # Opus first: its header says how many priming samples to drop, so it decodes exactly in time. The
    # AAC that YouTube serves (fragmented M4A without an edit list) decodes about 35 ms late: measured
    # against two flash-and-beep sync test clips, Opus was within 2 ms of the picture.
    a = (f.get("acodec") or "").lower()
    return 2 if a.startswith("opus") else 1 if a.startswith(("mp4a", "aac")) else 0


def choose(formats: list[dict], max_height: int) -> tuple[Optional[dict], Optional[dict]]:
    """(picture format, separate sound format or None). The picture format may carry sound itself."""
    usable = [f for f in formats if _usable(f)]
    pics = [f for f in usable if _has(f, "vcodec") is not False]
    sounds = [f for f in usable if _has(f, "acodec") is not False and _has(f, "vcodec") is False]

    def pic_key(f: dict):
        c = _size_class(f)
        fits = c <= max_height
        return (fits, c if fits else -c, _vcodec_rank(f), (f.get("protocol") or "https") in _PROTOCOLS_OWN,
                f.get("tbr") or 0)

    def sound_key(f: dict):
        return (not str(f.get("format_id", "")).endswith("-drc"), _acodec_rank(f), f.get("abr") or f.get("tbr") or 0)

    pic = max(pics, key=pic_key) if pics else None
    if pic is None:
        return (max(sounds, key=sound_key), None) if sounds else (None, None)
    if _has(pic, "acodec") is not False:
        return pic, None
    if sounds:
        return pic, max(sounds, key=sound_key)
    both = [f for f in pics if _has(f, "acodec") is not False]
    return (max(both, key=pic_key), None) if both else (pic, None)


# ------------------------------------------------------------------ fetching one stream
def _seconds_in(path: Path) -> float:
    """How many seconds of its first stream a (possibly cut-off) file holds, from the packets."""
    try:
        with av.open(str(path)) as c:
            s = (c.streams.video or c.streams.audio)[0]
            last = 0.0
            try:
                for p in c.demux(s):
                    if p.pts is not None:
                        last = max(last, float(p.pts * s.time_base))
            except av.error.FFmpegError:
                pass
            return last
    except Exception:
        return 0.0


def _ranged(y, f: dict, dest: Path, upto: Optional[float], progress: Progress, check: Check) -> bool:
    """Fetch f over HTTP range requests into dest; with `upto` (seconds), stop once the file holds that
    much. True when the file is complete."""
    from yt_dlp.networking import Request
    url = f["url"]
    headers = dict(f.get("http_headers") or {})
    size = int(f.get("filesize") or f.get("filesize_approx") or 0)
    length = float(f.get("duration") or 0)
    if upto is not None and size and length:
        want = int(size * min(1.0, (upto + MARGIN_SECONDS) / length) * 1.2) + (512 << 10)
    elif upto is not None:
        want = 2 * CHUNK          # no size or length to estimate from: grow until the file holds enough
    else:
        want = size or (1 << 62)
    got, total = 0, 0
    with open(dest, "wb") as out:
        while True:
            if total:
                want = min(want, total)
            while got < want:
                check()
                end = min(want, got + CHUNK) - 1
                r = y.urlopen(Request(url, headers={**headers, "Range": f"bytes={got}-{end}"}))
                try:
                    if r.status == 200 and got:
                        raise DownloadError("The server stopped taking range requests")
                    m = re.search(r"/(\d+)\s*$", r.headers.get("Content-Range", "") or "")
                    if m:
                        total = int(m.group(1))
                    elif r.status == 200:
                        cl = r.headers.get("Content-Length")
                        total = int(cl) if cl and cl.isdigit() else 0
                    before = got
                    while True:
                        b = r.read(1 << 18)
                        if not b:
                            break
                        out.write(b)
                        got += len(b)
                        progress(min(1.0, got / max(1, min(want, total or want))))
                        check()
                finally:
                    r.close()
                if r.status == 200 and not total:
                    total = got             # no length given: it ended when the answer ended
                if got == before or (total and got >= total):
                    break
            out.flush()
            complete = bool(total) and got >= total
            if complete or upto is None:
                return complete or upto is None
            if _seconds_in(dest) >= upto:
                return False
            if got < want:          # the server ended early: that is all there is
                return True
            want = got + max(2 << 20, got // 2)


def _with_ytdlp(info: dict, f: dict, dest_stem: Path, progress: Progress, check: Check) -> Path:
    """Fetch f with yt-dlp's own downloader (HLS / DASH fragments; it needs no ffmpeg for these)."""
    import yt_dlp
    done: list[str] = []

    def hook(d: dict) -> None:
        check()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                progress(min(1.0, d.get("downloaded_bytes", 0) / total))
            elif d.get("fragment_count"):
                progress(min(1.0, (d.get("fragment_index") or 0) / d["fragment_count"]))
        elif d.get("status") == "finished" and d.get("filename"):
            done.append(d["filename"])

    opts = _options(format=str(f["format_id"]), outtmpl=str(dest_stem) + ".%(ext)s", progress_hooks=[hook],
                    overwrites=True, continuedl=False)
    with yt_dlp.YoutubeDL(opts) as y:
        y.process_ie_result(dict(info), download=True)
    paths = [Path(p) for p in done if Path(p).is_file()] or sorted(dest_stem.parent.glob(dest_stem.name + ".*"))
    if not paths:
        raise DownloadError("The download produced no file")
    return paths[-1]


def _fetch_format(y, info: dict, f: dict, dest_stem: Path, upto: Optional[float], progress: Progress,
                  check: Check) -> tuple[Path, bool]:
    """(file, complete)."""
    if (f.get("protocol") or "https") in _PROTOCOLS_OWN and f.get("url"):
        dest = dest_stem.with_suffix("." + (f.get("ext") or "bin"))
        return dest, _ranged(y, f, dest, upto, progress, check)
    return _with_ytdlp(info, f, dest_stem, progress, check), True


# ------------------------------------------------------------------ one file from two
_MP4_VIDEO = {"h264", "hevc", "av1", "vp9", "mpeg4"}
_MP4_AUDIO = {"aac", "mp3", "opus", "ac3", "eac3", "alac", "flac"}
_WEBM_VIDEO = {"vp8", "vp9", "av1"}
_WEBM_AUDIO = {"opus", "vorbis"}


def _container_for(vcodec: Optional[str], acodec: Optional[str]) -> tuple[str, str]:
    if (vcodec is None or vcodec in _MP4_VIDEO) and (acodec is None or acodec in _MP4_AUDIO) and vcodec:
        return "mp4", ".mp4"
    if (vcodec is None or vcodec in _WEBM_VIDEO) and (acodec is None or acodec in _WEBM_AUDIO):
        return "webm", ".webm"
    return "matroska", ".mkv"


def _packets(c, streams: list, t_from: float, t_to: Optional[float]) -> Iterator[tuple[float, int, Any]]:
    """(time in seconds, order, packet) of the given streams of one container between t_from and
    t_to, read lazily (one demuxer per container: two readers of one file would steal each other's
    packets)."""
    try:
        for p in c.demux(*streams):
            if p.dts is None and p.pts is None:
                continue
            s = p.stream
            tb = s.time_base
            t = float((p.dts if p.dts is not None else p.pts) * tb)
            if t_to is not None and t > t_to:
                if s.type == "video" or len(streams) == 1:
                    return
                continue
            pt = float((p.pts if p.pts is not None else p.dts) * tb)
            if pt < t_from - 1e-6 and (s.type != "video" or t < t_from - 1e-6):
                continue
            yield t, 0 if s.type == "video" else 1, p
    except av.error.FFmpegError as e:
        log.info("%s ends early (cut off): %s", c.name, e)


def _first_keyframe_at_or_before(path: Path, t: float) -> float:
    """Time of the last keyframe of the first picture stream at or before t (seconds), or 0."""
    with av.open(str(path)) as c:
        s = c.streams.video[0]
        best = None
        try:
            for p in c.demux(s):
                if p.pts is None:
                    continue
                pt = float(p.pts * s.time_base)
                if pt > t + 1e-6:
                    break
                if p.is_keyframe:
                    best = pt
        except av.error.FFmpegError:
            pass
        return best if best is not None else 0.0


def merge(picture: Path, sound: Optional[Path], dest_stem: Path, start: float = 0.0,
          duration: Optional[float] = None) -> tuple[Path, float]:
    """Copy the first picture stream of `picture` and the first sound stream of `sound` (or of
    `picture` when None) into one file, packets and timestamps unchanged except for a common shift.
    With start / duration only that part is kept, from the keyframe at or before `start` (a stream
    copy can only start on a keyframe). Returns (the file, seconds the result begins after the
    original's start)."""
    cut = _first_keyframe_at_or_before(picture, start) if start > 0 else 0.0
    t_to = None if duration is None else start + duration + CUT_PAD
    ins = [av.open(str(picture))]
    if sound is not None:
        ins.append(av.open(str(sound)))
    o = None
    tmp: Optional[Path] = None
    try:
        vs = _visual_streams(ins[0])[0]
        a_in = ins[-1]
        as_ = a_in.streams.audio[0] if a_in.streams.audio else None
        vname = vs.codec_context.name
        aname = as_.codec_context.name if as_ is not None else None
        fmt, ext = _container_for(vname, aname)
        dest = dest_stem.with_suffix(ext)
        tmp = dest.with_name(dest.stem + ".tmp" + ext)
        o = av.open(str(tmp), "w", format=fmt, options={"movflags": "+faststart"} if fmt == "mp4" else None)
        ov = o.add_stream_from_template(vs)
        oa = o.add_stream_from_template(as_) if as_ is not None else None
        # Both streams move by the same amount, so whatever offset the site gave them is kept.
        v0 = _first_time(ins[0], vs)
        base = cut if start > 0 else v0
        if as_ is not None and a_in is ins[0]:
            streams = [_packets(ins[0], [vs, as_], cut, t_to)]
        else:
            streams = [_packets(ins[0], [vs], cut, t_to)]
            if as_ is not None:
                streams.append(_packets(a_in, [as_], cut, t_to))
        for t, _, p in heapq.merge(*streams, key=lambda x: (x[0], x[1])):
            src_s = p.stream
            shift = int(round(base / src_s.time_base))
            if p.pts is not None:
                p.pts -= shift
            if p.dts is not None:
                p.dts -= shift
            p.stream = ov if src_s.type == "video" else oa
            o.mux(p)
        o.close()
        o = None
        os.replace(tmp, dest)
        return dest, cut
    except av.error.FFmpegError as e:
        raise DownloadError(f"The downloaded streams could not be put together ({short_error(e)})") from e
    finally:
        if o is not None:
            try:
                o.close()
            except Exception:
                pass
        if tmp is not None and tmp.exists() and o is None:
            try:
                tmp.unlink()
            except OSError:
                pass
        for c in ins:
            c.close()


def _first_time(c, s) -> float:
    if s.start_time is not None:
        return float(s.start_time * s.time_base)
    return 0.0


# ------------------------------------------------------------------ the whole job
USER_AGENT = "TrafficLight media import (Python urllib)"


def _direct(url: str, work: Path, progress: Progress, check: Check) -> Optional[Path]:
    """A plain link to a media file that yt-dlp did not take (a picture, or a host that turns away
    yt-dlp's browser-like requests). None if the link is not a media file or cannot be read."""
    import urllib.request
    ext = Path(urlparse(url).path).suffix.lower()
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=TIMEOUT)
    except Exception as e:
        log.info("direct fetch of %s failed: %s", url, e)
        return None
    try:
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if not (ctype.startswith(("image/", "video/", "audio/")) or ext in ACCEPTED_EXTS):
            return None
        if ext not in ACCEPTED_EXTS:
            ext = "." + (ctype.split("/")[1].split("+")[0] if "/" in ctype else "bin")
            ext = {".jpeg": ".jpg", ".quicktime": ".mov", ".mpeg": ".mp3", ".x-matroska": ".mkv"}.get(ext, ext)
        total = int(r.headers.get("Content-Length") or 0)
        dest = work / f"download{ext[:8]}"
        got = 0
        with open(dest, "wb") as out:
            while True:
                check()
                b = r.read(1 << 18)
                if not b:
                    break
                out.write(b)
                got += len(b)
                if total:
                    progress(min(1.0, got / total))
        return dest
    except (Cancelled, DownloadError):
        raise
    except Exception as e:
        log.info("direct fetch of %s failed: %s", url, e)
        return None
    finally:
        r.close()


def fetch(url: str, work: Path, *, max_height: int = 1080, start: float = 0.0, duration: Optional[float] = None,
          progress: Progress = _nothing, check: Check = _nothing) -> Fetched:
    """Download the video behind `url` into the folder `work` (it is created). DownloadError with a
    readable message when that is not possible; Cancelled when `check` says so."""
    import yt_dlp
    from yt_dlp.utils import DownloadError as YtError, ExtractorError

    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise DownloadError("Paste a web link (starting with http:// or https://)")
    work.mkdir(parents=True, exist_ok=True)
    start = max(0.0, float(start or 0.0))
    upto = None if duration is None else start + float(duration)
    try:
        with yt_dlp.YoutubeDL(_options()) as y:
            try:
                info = y.extract_info(url, download=False)
            except (YtError, ExtractorError):
                path = _direct(url, work, progress, check)
                if path is not None:
                    return Fetched(path, unquote(Path(urlparse(url).path).stem).replace("_", " ") or "Download", start,
                                   None if duration is None else start + duration)
                raise
            check()
            if info.get("entries") is not None:
                entries = [e for e in info["entries"] if e]
                if not entries:
                    raise DownloadError("The link is an empty playlist")
                info = entries[0]
            title = str(info.get("title") or info.get("id") or "Download")
            formats = info.get("formats") or ([info] if info.get("url") else [])
            pic, snd = choose(formats, int(max_height))
            if pic is None:
                raise DownloadError("The link has no picture or sound that can be fetched")
            log.info("fetching %s: %s%s", url, pic.get("format_id"), f" + {snd.get('format_id')}" if snd else "")
            for f in (pic, snd):
                if f is not None and not f.get("duration"):
                    f["duration"] = info.get("duration")
            share = 1.0 if snd is None else 0.85
            pic_path, pic_whole = _fetch_format(y, info, pic, work / "picture", upto,
                                                lambda x: progress(share * x), check)
            snd_path, snd_whole = None, True
            if snd is not None:
                snd_path, snd_whole = _fetch_format(y, info, snd, work / "sound", upto,
                                                    lambda x: progress(share + (1 - share) * x), check)
    except (Cancelled, DownloadError):
        raise
    except Exception as e:
        raise DownloadError(explain(e, url)) from e
    check()
    progress(1.0)
    if not _has_picture(pic_path):
        # A sound-only link: keep the file as it is (an audio item); the trim is the edit's.
        return Fetched(pic_path, title, start, upto)
    if snd_path is None and start == 0 and duration is None and pic_whole:
        return Fetched(pic_path, title, 0.0, None)
    merged, cut = merge(pic_path, snd_path, work / "merged", start, duration)
    for p in (pic_path, snd_path):
        if p is not None and p != merged:
            try:
                p.unlink()
            except OSError:
                pass
    return Fetched(merged, title, start - cut, None if duration is None else start - cut + duration)


def _has_picture(path: Path) -> bool:
    try:
        with av.open(str(path)) as c:
            return bool(_visual_streams(c))
    except Exception:
        return False

