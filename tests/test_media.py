"""Media library: ingest of each kind, metadata, proxies, errors, restart, delete.

All media is generated here with PyAV / Pillow in a temp directory. The generators are also used by
test_media_render.py.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
import pytest
from PIL import Image

from trafficlight import config
from trafficlight.media import MediaLibrary
from trafficlight.media import library as library_mod
from trafficlight.media.ingest import proxy_size

BITS = 6          # frame index encoded as 6 horizontal stripes: up to 64 frames


# ------------------------------------------------------------------ generators
def frame_picture(i: int, w: int, h: int) -> np.ndarray:
    """Frame i: stripe k (top = bit 0) is white when bit k of i is set; a red bar on the left 10 %."""
    a = np.zeros((h, w, 3), np.uint8)
    for k in range(BITS):
        if (i >> k) & 1:
            a[k * h // BITS:(k + 1) * h // BITS] = 255
    a[:, :max(1, w // 10)] = (255, 0, 0)
    return a


def read_index(img: np.ndarray, top: int = 0, height: int | None = None) -> int:
    """The index frame_picture drew, read along the centre column of rows top..top+height."""
    height = img.shape[0] - top if height is None else height
    x = img.shape[1] // 2
    i = 0
    for k in range(BITS):
        y = top + int((k + 0.5) * height / BITS)
        if img[y, x, :3].astype(int).mean() > 128:
            i |= 1 << k
    return i


def make_video(path: Path, w: int = 160, h: int = 90, n: int = 40, fps=30, codec: str = "libx264",
               sound: bool = False, rotation: int = 0, fmt: str | None = None, audio_codec: str = "aac") -> Path:
    with av.open(str(path), "w", format=fmt) as c:
        s = c.add_stream(codec, rate=Fraction(fps))
        s.width, s.height = w, h
        s.pix_fmt = "yuvj420p" if codec == "mjpeg" else "yuv420p"
        if codec == "libx264":
            s.options = {"preset": "ultrafast", "crf": "10"}
        if rotation:
            s.set_display_rotation(rotation)
        a = None
        if sound:
            a = c.add_stream(audio_codec, rate=48000)
            a.layout = "stereo"
        for i in range(n):
            f = av.VideoFrame.from_ndarray(frame_picture(i, w, h), format="rgb24")
            f.pts = i
            f.time_base = Fraction(1) / Fraction(fps)
            for p in s.encode(f):
                c.mux(p)
        for p in s.encode():
            c.mux(p)
        if a is not None:
            _write_tone(c, a, n / float(fps))
    return path


def _write_tone(c, stream, seconds: float, rate: int = 48000) -> None:
    t = np.arange(int(seconds * rate)) / rate
    tone = (0.5 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
    pcm = np.repeat(tone[:, None], 2, axis=1)
    chunk = 1024
    for start in range(0, len(pcm), chunk):
        part = np.ascontiguousarray(pcm[start:start + chunk].reshape(1, -1))
        fr = av.AudioFrame.from_ndarray(part, format="s16", layout="stereo")
        fr.sample_rate = rate
        fr.pts = start
        for p in stream.encode(fr):
            c.mux(p)
    for p in stream.encode():
        c.mux(p)


def make_audio(path: Path, seconds: float = 2.0, codec: str = "pcm_s16le", fmt: str | None = None) -> Path:
    with av.open(str(path), "w", format=fmt) as c:
        s = c.add_stream(codec, rate=48000)
        s.layout = "stereo"
        _write_tone(c, s, seconds)
    return path


def make_gif(path: Path, n: int = 8, delay_ms: int = 50, w: int = 64, h: int = 48) -> Path:
    frames = [Image.fromarray(frame_picture(i, w, h)) for i in range(n)]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=delay_ms, loop=0, lossless=True)
    return path


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAFFICLIGHT_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(library_mod, "_SWEEP_SECONDS", 0.1)
    m = MediaLibrary(tmp_path / "data" / "media")
    yield m
    m.close()


def upload(lib: MediaLibrary, src: Path, name: str = "x", timeout: float = 60):
    """Like the server: copy to a temp file with the original extension, add, wait."""
    tmp = src.with_name("upload-" + src.name)
    shutil.copyfile(src, tmp)
    item = lib.add(tmp, name)
    assert item.status == "processing"
    assert not tmp.exists()
    return lib.wait_ready(item.id, timeout)


def stream_info(path: str):
    with av.open(path) as c:
        v = c.streams.video[0] if c.streams.video else None
        packets = [(p.pts, p.dts, p.is_keyframe) for p in c.demux(v) if p.size] if v else []
        return {
            "streams": [s.type for s in c.streams],
            "codec": v.codec_context.name if v else None,
            "size": (v.width, v.height) if v else None,
            "rate": v.average_rate if v else None,
            "keyframes": [i for i, p in enumerate(packets) if p[2]],
            "frames": len(packets),
            "in_order": all(p[0] == p[1] for p in packets),
        }


# ------------------------------------------------------------------ tests
def test_proxy_size_rules():
    assert proxy_size(1920, 1080) == (684, 384)              # covers 128x384, aspect kept
    assert proxy_size(1080, 1920) == (216, 384)
    assert proxy_size(160, 90) == (160, 90)                  # never upscaled
    assert proxy_size(4000, 100) == (4000, 100)
    # an exact 1:3 crop (as the crop tool rounds it) gives exactly the sign's box, both ways of rounding
    assert proxy_size(854 / 3, 854) == (128, 384) and proxy_size(0.593056 * 480, 854) == (128, 384)
    w, h = proxy_size(1280, 720)
    assert w >= config.W and h >= config.H and w % 2 == 0 and h % 2 == 0


def test_video_with_sound(lib, tmp_path):
    src = make_video(tmp_path / "clip.mp4", w=1280, h=720, n=45, fps=30, sound=True)
    m = upload(lib, src, "Clip")
    assert m.status == "ready", m.error
    assert m.kind == "video" and m.name == "Clip"
    assert (m.width, m.height) == (1280, 720)
    assert m.fps == 30 and m.duration == pytest.approx(1.5, abs=0.04) and m.has_audio
    assert m.size == src.stat().st_size

    info = stream_info(lib.video_path(m.id))
    assert info["streams"] == ["video"]                       # no sound in the proxy
    assert info["codec"] == "h264"
    assert info["size"] == (684, 384)
    assert info["rate"] == 30 and info["frames"] == 45
    assert info["keyframes"] == [0, 15, 30]                   # GOP of half a second
    assert info["in_order"]                                   # no B-frames

    with av.open(lib.audio_path(m.id)) as c:
        a = c.streams.audio[0]
        assert c.format.name == "wav" and a.codec_context.name == "pcm_s16le"
        assert a.rate == 48000 and a.channels == 2
        samples = sum(f.samples for f in c.decode(a))
    assert samples / 48000 == pytest.approx(m.duration, abs=0.03)   # cut to the picture

    peaks = lib.peaks(m.id)
    assert peaks["rate"] > 0 and len(peaks["peaks"]) == pytest.approx(peaks["rate"] * m.duration, abs=2)
    assert max(peaks["peaks"]) == pytest.approx(0.5, abs=0.05)
    with Image.open(lib.thumb_path(m.id)) as im:
        assert im.format == "JPEG" and max(im.size) <= 256
    assert lib.file_path(m.id).name == "original.mp4"         # H.264 + AAC in MP4: browsers play it
    assert lib.image_path(m.id) is None


def test_video_proxy_caps_frame_rate_and_converts_for_browser(lib, tmp_path):
    src = make_video(tmp_path / "fast.mkv", w=200, h=400, n=96, fps=120, codec="mpeg4")
    m = upload(lib, src)
    assert m.status == "ready", m.error
    assert m.fps == 60 and m.duration == pytest.approx(0.8, abs=0.03) and not m.has_audio
    info = stream_info(lib.video_path(m.id))
    assert info["rate"] == 60 and info["keyframes"][:2] == [0, 30]
    assert info["size"] == (192, 384)
    assert lib.file_path(m.id).name == "source.mp4"           # MPEG-4 part 2 in MKV: not for a browser
    assert lib.audio_path(m.id) is None and lib.peaks(m.id) is None


def test_rotated_phone_video(lib, tmp_path):
    src = make_video(tmp_path / "phone.mov", w=320, h=180, n=10, rotation=90, fmt="mov")
    m = upload(lib, src)
    assert m.status == "ready", m.error
    assert (m.width, m.height) == (180, 320)
    assert stream_info(lib.video_path(m.id))["size"] == (180, 320)
    with av.open(lib.video_path(m.id)) as c:
        first = next(c.decode(video=0)).to_ndarray(format="rgb24")
    # 90 degrees counter-clockwise (FFmpeg's display matrix convention): the red left edge is now at the bottom
    assert first[-3:, :, 0].mean() > 200 and first[-3:, :, 1].mean() < 60
    assert first[:3, :, 0].mean() < 60


def test_avi_mjpeg(lib, tmp_path):
    m = upload(lib, make_video(tmp_path / "old.avi", n=12, codec="mjpeg"))
    assert m.status == "ready", m.error
    assert m.kind == "video" and m.duration == pytest.approx(0.4, abs=0.04)


@pytest.mark.parametrize("ext", ["gif", "webp"])
def test_animation_is_a_video(lib, tmp_path, ext):
    m = upload(lib, make_gif(tmp_path / f"anim.{ext}", n=8, delay_ms=50))
    assert m.status == "ready", m.error
    assert m.kind == "video" and m.fps == 20 and m.duration == pytest.approx(0.4, abs=0.01)
    info = stream_info(lib.video_path(m.id))
    assert info["frames"] == 8
    assert lib.file_path(m.id).name == "source.mp4"           # a browser cannot seek in a GIF


def test_image_keeps_alpha_and_is_scaled(lib, tmp_path):
    a = np.zeros((1000, 2000, 4), np.uint8)
    a[..., 0] = 255
    a[:, 1000:, 3] = 255
    Image.fromarray(a, "RGBA").save(tmp_path / "logo.png")
    m = upload(lib, tmp_path / "logo.png")
    assert m.status == "ready", m.error
    assert m.kind == "image" and (m.width, m.height) == (2000, 1000) and m.duration == 0
    with Image.open(lib.image_path(m.id)) as im:
        assert im.mode == "RGBA" and im.size == (768, 384)
    assert lib.file_path(m.id).name == "original.png"
    with Image.open(lib.thumb_path(m.id)) as im:
        assert im.format == "JPEG"
    assert lib.video_path(m.id) is None and lib.audio_path(m.id) is None


def test_kind_comes_from_content_not_extension(lib, tmp_path):
    Image.new("RGB", (40, 30), (0, 200, 0)).save(tmp_path / "pic.jpg", "JPEG")
    os.replace(tmp_path / "pic.jpg", tmp_path / "pic.mp4")
    m = upload(lib, tmp_path / "pic.mp4")
    assert m.status == "ready" and m.kind == "image"
    with Image.open(lib.image_path(m.id)) as im:
        assert im.size == (40, 30)
    # An unknown extension is fine when the content is readable...
    s = make_audio(tmp_path / "s.wav")
    os.replace(s, tmp_path / "s.bin")
    m2 = upload(lib, tmp_path / "s.bin")
    assert m2.status == "ready" and m2.kind == "audio"
    assert lib.file_path(m.id).name == "proxy.png"          # not served as an "mp4"
    assert lib.file_path(m2.id).name == "audio.wav"


@pytest.mark.parametrize("ext,codec,fmt", [
    ("wav", "pcm_s16le", None), ("flac", "flac", None), ("mp3", "libmp3lame", None),
    ("ogg", "libopus", "ogg"), ("m4a", "aac", "ipod"),
])
def test_audio_formats(lib, tmp_path, ext, codec, fmt):
    if codec not in av.codecs_available:
        pytest.skip(f"no {codec} encoder here")
    m = upload(lib, make_audio(tmp_path / f"song.{ext}", seconds=2.0, codec=codec, fmt=fmt))
    assert m.status == "ready", m.error
    assert m.kind == "audio" and m.has_audio
    assert m.duration == pytest.approx(2.0, abs=0.06)
    with av.open(lib.audio_path(m.id)) as c:
        assert c.streams.audio[0].codec_context.name == "pcm_s16le"
    assert lib.peaks(m.id)["peaks"]
    with Image.open(lib.thumb_path(m.id)) as im:
        assert im.format == "JPEG" and im.size == (256, 128)
    assert lib.file_path(m.id).name == f"original.{ext}"
    assert lib.video_path(m.id) is None


def test_corrupt_upload_ends_as_error(lib, tmp_path):
    (tmp_path / "junk.mp4").write_bytes(os.urandom(50000))
    m = upload(lib, tmp_path / "junk.mp4")
    assert m.status == "error" and m.error and "read" in m.error
    assert m.kind == "video"
    assert lib.video_path(m.id) is None and lib.file_path(m.id) is None and lib.thumb_path(m.id) is None

    (tmp_path / "empty.png").write_bytes(b"")
    m = upload(lib, tmp_path / "empty.png")
    assert m.status == "error"

    good = make_video(tmp_path / "good.mp4", n=60).read_bytes()
    (tmp_path / "cut.mp4").write_bytes(good[: len(good) // 2])     # an upload cut short
    m = upload(lib, tmp_path / "cut.mp4")
    assert m.status in ("ready", "error")


def test_unsupported_file_is_refused(lib, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    with pytest.raises(ValueError):
        lib.add(p, "notes")
    assert p.exists() and lib.list() == []


def test_list_rename_rev(lib, tmp_path):
    r0 = lib.rev
    a = upload(lib, make_audio(tmp_path / "a.wav", 0.5), "A")
    time.sleep(0.01)
    b = upload(lib, make_audio(tmp_path / "b.wav", 0.5), "B")
    assert [m.id for m in lib.list()] == [b.id, a.id]          # newest first
    r1 = lib.rev
    assert r1 >= r0 + 4                                       # two adds, two status changes
    assert lib.rename(a.id, "Renamed").name == "Renamed"
    assert lib.get(a.id).name == "Renamed" and lib.rev == r1 + 1
    with pytest.raises(KeyError):
        lib.rename("nope", "x")
    with pytest.raises(KeyError):
        lib.delete("nope")
    assert lib.get("nope") is None


def test_items_survive_a_restart(tmp_path, monkeypatch):
    root = tmp_path / "media"
    lib = MediaLibrary(root)
    m = upload(lib, make_video(tmp_path / "v.mp4", n=10), "V")
    lib.close()

    # A crash in the middle of an ingest leaves "processing" behind; a delete interrupted by a crash
    # leaves its marker.
    pending = MediaLibrary(root)
    p = upload(pending, make_audio(tmp_path / "s.wav", 0.5), "S")
    pending.close()
    meta = json.loads((root / p.id / "meta.json").read_text())
    meta.update(status="processing", duration=0)
    (root / p.id / "meta.json").write_text(json.dumps(meta))
    (root / "deadbeef0000").mkdir()
    (root / "deadbeef0000" / "meta.json").write_text(json.dumps(dict(meta, id="deadbeef0000")))
    (root / "deadbeef0000" / ".deleted").touch()
    (root / "orphan000000").mkdir()

    lib2 = MediaLibrary(root)
    try:
        again = lib2.get(m.id)
        assert again == m
        assert lib2.video_path(m.id) and Path(lib2.video_path(m.id)).is_file()
        assert lib2.wait_ready(p.id, 30).status == "ready"
        assert lib2.get(p.id).duration == pytest.approx(0.5, abs=0.03)
        assert {i.id for i in lib2.list()} == {m.id, p.id}
        assert not (root / "deadbeef0000").exists() and not (root / "orphan000000").exists()
    finally:
        lib2.close()


def test_delete_removes_files(lib, tmp_path):
    m = upload(lib, make_video(tmp_path / "v.mp4", n=10, sound=True))
    d = Path(lib.video_path(m.id)).parent
    r = lib.rev
    lib.delete(m.id)
    assert lib.get(m.id) is None and lib.video_path(m.id) is None and lib.rev == r + 1
    assert not d.exists()
    with pytest.raises(KeyError):
        lib.delete(m.id)


def test_delete_while_the_proxy_is_open(lib, tmp_path):
    """On Windows the open proxy cannot be deleted at once: the item must vanish anyway, a restart
    must not bring it back, and the folder must go once the file is closed."""
    m = upload(lib, make_video(tmp_path / "v.mp4", n=10))
    path = lib.video_path(m.id)
    d = Path(path).parent
    c = av.open(path)
    try:
        next(c.decode(video=0))
        lib.delete(m.id)
        assert lib.get(m.id) is None and m.id not in [i.id for i in lib.list()]
        again = MediaLibrary(lib.root)
        try:
            assert again.get(m.id) is None
        finally:
            again.close()
    finally:
        c.close()
    end = time.monotonic() + 5
    while d.exists() and time.monotonic() < end:
        time.sleep(0.05)
    assert not d.exists()


def test_delete_during_ingest(lib, tmp_path):
    src = make_video(tmp_path / "big.mp4", w=640, h=360, n=60, sound=True)
    tmp = tmp_path / "up.mp4"
    shutil.copyfile(src, tmp)
    m = lib.add(tmp, "big")
    lib.delete(m.id)
    end = time.monotonic() + 10
    while (lib.root / m.id).exists() and time.monotonic() < end:
        time.sleep(0.05)
    assert not (lib.root / m.id).exists()
    assert lib.get(m.id) is None
    # the worker is still alive
    assert upload(lib, make_audio(tmp_path / "after.wav", 0.3)).status == "ready"


def test_concurrent_adds(lib, tmp_path):
    srcs = [make_audio(tmp_path / f"s{i}.wav", 0.3) for i in range(6)]
    ids, errors = [], []

    def go(s):
        try:
            tmp = s.with_name("up-" + s.name)
            shutil.copyfile(s, tmp)
            ids.append(lib.add(tmp, s.stem).id)
        except Exception as e:      # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=go, args=(s,)) for s in srcs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(set(ids)) == 6
    assert all(lib.wait_ready(i, 30).status == "ready" for i in ids)
    assert len(lib.list()) == 6
