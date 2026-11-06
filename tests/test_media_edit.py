"""Media edits (rotate, crop, trim), re-making the proxy while the old one is in use, duplicates and
link imports (with a fake downloader: no network in tests). All media is generated here."""
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

from test_media import make_audio, make_gif, make_video, read_index, upload
from trafficlight import config
from trafficlight.media import MediaLibrary
from trafficlight.media import library as library_mod
from trafficlight.media.download import DownloadError, Fetched, Probed, choose, explain, merge
from trafficlight.media.ingest import crop_rect
from trafficlight.model import MediaEdit

RED, GREEN, BLUE, WHITE = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)


def quad_picture(w: int, h: int) -> np.ndarray:
    """Top-left red, top-right green, bottom-left blue, bottom-right white."""
    a = np.zeros((h, w, 3), np.uint8)
    a[:h // 2, :w // 2] = RED
    a[:h // 2, w // 2:] = GREEN
    a[h // 2:, :w // 2] = BLUE
    a[h // 2:, w // 2:] = WHITE
    return a


def make_quad_video(path: Path, w: int = 320, h: int = 180, n: int = 30, fps: int = 30) -> Path:
    with av.open(str(path), "w") as c:
        s = c.add_stream("libx264", rate=Fraction(fps))
        s.width, s.height, s.pix_fmt = w, h, "yuv420p"
        s.options = {"preset": "ultrafast", "crf": "10"}
        pic = quad_picture(w, h)
        for i in range(n):
            f = av.VideoFrame.from_ndarray(pic, format="rgb24")
            f.pts, f.time_base = i, Fraction(1, fps)
            for p in s.encode(f):
                c.mux(p)
        for p in s.encode():
            c.mux(p)
    return path


def first_frame(path: str, t: float = 0.0) -> np.ndarray:
    with av.open(path) as c:
        for f in c.decode(video=0):
            if f.time + 1e-6 >= t:
                return f.to_ndarray(format="rgb24")
    raise AssertionError("no frame")


def colour_at(img: np.ndarray, fx: float, fy: float) -> tuple[int, ...]:
    h, w = img.shape[:2]
    return tuple(int(v) for v in img[int(fy * (h - 1)), int(fx * (w - 1)), :3])


def near(c, ref, tol=40) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(c, ref))


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(library_mod, "_SWEEP_SECONDS", 0.1)
    m = MediaLibrary(tmp_path / "media")
    yield m
    m.close()


def apply(lib: MediaLibrary, mid: str, edit: MediaEdit, timeout: float = 60):
    changed = lib.get(mid).edit != edit
    out = lib.update(mid, edit=edit)
    assert out.edit == edit and out.status == ("processing" if changed else "ready")
    m = lib.wait_ready(mid, timeout)
    assert m.status == "ready", m.error
    return m


# ------------------------------------------------------------------ the model
def test_media_edit_validation():
    assert MediaEdit(crop=[0.25, 0.5, 0.75, 0.5]).crop == [0.25, 0.5, 0.75, 0.5]
    assert MediaEdit(crop=[-1e-9, 0, 1.0000001, 1]).crop == [0.0, 0.0, 1.0, 1.0]   # rounding noise is fine
    for bad in ([0.5, 0, 0.6, 1], [0, 0, 0, 1], [0, 0, 1], [-0.1, 0, 0.5, 0.5]):
        with pytest.raises(ValueError):
            MediaEdit(crop=bad)
    with pytest.raises(ValueError):
        MediaEdit(start=3, end=2)
    with pytest.raises(ValueError):
        MediaEdit(rotate=45)
    with pytest.raises(ValueError):
        MediaEdit(start=-1)


def test_crop_rect_in_pixels():
    assert crop_rect(1000, 500, None) == (0, 0, 1000, 500)
    assert crop_rect(1000, 500, [0.5, 0.5, 0.5, 0.5]) == (500, 250, 500, 250)
    x, y, w, h = crop_rect(100, 100, [0.999, 0, 0.001, 1])     # never thinner than a pixel
    assert w == 1 and x == 99


# ------------------------------------------------------------------ video edits
def test_rotate_each_quarter(lib, tmp_path):
    m = upload(lib, make_quad_video(tmp_path / "q.mp4"))
    assert (m.source_width, m.source_height) == (320, 180) and m.version == 1
    # where red (top-left of the original) ends up after turning clockwise
    corners = {0: (0.1, 0.1), 90: (0.9, 0.1), 180: (0.9, 0.9), 270: (0.1, 0.9)}
    for deg, (fx, fy) in corners.items():
        r = apply(lib, m.id, MediaEdit(rotate=deg))
        img = first_frame(lib.video_path(m.id))
        size = (180, 320) if deg in (90, 270) else (320, 180)
        assert (r.width, r.height) == size and (r.source_width, r.source_height) == (320, 180)
        assert img.shape[1] / img.shape[0] == pytest.approx(size[0] / size[1], rel=0.03)
        assert near(colour_at(img, fx, fy), RED), deg
        assert near(colour_at(img, 1 - fx, 1 - fy), WHITE), deg


def test_crop_one_light_and_whole_sign(lib, tmp_path):
    m = upload(lib, make_quad_video(tmp_path / "q.mp4", w=640, h=360))
    # 1:1 box inside the green quadrant (top right): 160 x 160 px
    r = apply(lib, m.id, MediaEdit(crop=[0.7, 0.02, 0.25, 160 / 360], shape="light"))
    assert (r.width, r.height) == (160, 160)
    img = first_frame(lib.video_path(m.id))
    assert img.shape[:2] == (160, 160)                                 # smaller than 384: never scaled up
    assert near(colour_at(img, 0.5, 0.2), GREEN) and near(colour_at(img, 0.5, 0.8), GREEN)
    # 1:3 box straddling the left half: red on top, blue below
    r = apply(lib, m.id, MediaEdit(crop=[0.1, 0.0, 120 / 640, 1.0], shape="sign"))
    assert (r.width, r.height) == (120, 360) and r.edit.shape == "sign"
    img = first_frame(lib.video_path(m.id))
    assert near(colour_at(img, 0.5, 0.2), RED) and near(colour_at(img, 0.5, 0.8), BLUE)
    with Image.open(lib.thumb_path(m.id)) as th:                       # the card shows the result
        assert th.size[1] > th.size[0] * 2.5


def test_crop_of_a_large_source_is_scaled_to_cover_the_sign(lib, tmp_path):
    m = upload(lib, make_quad_video(tmp_path / "big.mp4", w=1920, h=1080, n=6))
    apply(lib, m.id, MediaEdit(crop=[0.5, 0.0, 0.5, 1.0]))
    with av.open(lib.video_path(m.id)) as c:
        v = c.streams.video[0]
        assert (v.width, v.height) == (342, 384)                       # 960x1080 -> just covers 128x384
    img = first_frame(lib.video_path(m.id))
    assert near(colour_at(img, 0.5, 0.25), GREEN) and near(colour_at(img, 0.5, 0.75), WHITE)


def test_rotate_then_crop(lib, tmp_path):
    m = upload(lib, make_quad_video(tmp_path / "q.mp4"))
    # turned clockwise, the original's left half (red over blue) is the top half: blue left, red right
    r = apply(lib, m.id, MediaEdit(rotate=90, crop=[0.0, 0.0, 1.0, 0.5]))
    assert (r.width, r.height) == (180, 160)
    img = first_frame(lib.video_path(m.id))
    assert near(colour_at(img, 0.2, 0.5), BLUE) and near(colour_at(img, 0.8, 0.5), RED)


def test_trim_cuts_picture_and_sound_together(lib, tmp_path):
    src = make_video(tmp_path / "count.mp4", w=160, h=90, n=60, fps=30, sound=True)
    m = upload(lib, src)
    assert m.duration == pytest.approx(2.0, abs=0.04) and m.source_duration == pytest.approx(2.0, abs=0.04)
    r = apply(lib, m.id, MediaEdit(start=0.5, end=1.5))
    assert r.duration == pytest.approx(1.0, abs=0.04) and r.source_duration == pytest.approx(2.0, abs=0.04)
    with av.open(lib.video_path(m.id)) as c:
        frames = [f.to_ndarray(format="rgb24") for f in c.decode(video=0)]
    assert len(frames) == 30
    assert read_index(frames[0]) == 15 and read_index(frames[-1]) == 44    # in at frame 15 (0.5 s)
    with av.open(lib.audio_path(m.id)) as c:
        samples = sum(f.samples for f in c.decode(audio=0))
    assert samples / 48000 == pytest.approx(r.duration, abs=0.02)
    peaks = lib.peaks(m.id)
    assert len(peaks["peaks"]) == pytest.approx(peaks["rate"] * r.duration, abs=2)
    # back to the whole video
    r = apply(lib, m.id, MediaEdit())
    assert r.duration == pytest.approx(2.0, abs=0.04)
    assert read_index(first_frame(lib.video_path(m.id))) == 0


def test_trim_far_into_a_video_seeks(lib, tmp_path):
    m = upload(lib, make_video(tmp_path / "long.mp4", w=96, h=64, n=63 * 1 + 1, fps=10))   # 6.4 s, 64 frames
    r = apply(lib, m.id, MediaEdit(start=4.2, end=5.0))
    with av.open(lib.video_path(m.id)) as c:
        frames = [f.to_ndarray(format="rgb24") for f in c.decode(video=0)]
    assert len(frames) == 8 and r.duration == pytest.approx(0.8, abs=0.01)
    assert [read_index(f) for f in frames] == list(range(42, 50))


def test_in_point_after_the_end_is_refused(lib, tmp_path):
    m = upload(lib, make_video(tmp_path / "v.mp4", n=30))
    with pytest.raises(ValueError):
        lib.update(m.id, edit=MediaEdit(start=5.0))
    assert lib.get(m.id).status == "ready" and lib.get(m.id).edit == MediaEdit()


def test_animation_edit_and_source(lib, tmp_path):
    m = upload(lib, make_gif(tmp_path / "a.gif", n=10, delay_ms=100, w=80, h=60))
    assert m.kind == "video" and m.source_duration == pytest.approx(1.0)
    assert lib.source_path(m.id).name == "source.mp4"
    with av.open(str(lib.source_path(m.id))) as c:
        assert c.streams.video[0].frames == 10 or sum(1 for _ in c.decode(video=0)) == 10
    r = apply(lib, m.id, MediaEdit(start=0.3, end=0.7, crop=[0.0, 0.0, 0.5, 1.0], rotate=180))
    assert r.duration == pytest.approx(0.4) and (r.width, r.height) == (40, 60)
    with av.open(lib.video_path(m.id)) as c:
        frames = [f.to_ndarray(format="rgb24") for f in c.decode(video=0)]
    # turned 180: the index stripes are upside down, and the left half after turning is the original right half
    assert [read_index(np.ascontiguousarray(f[::-1, ::-1])) for f in frames] == [3, 4, 5, 6]
    assert frames[0][:, :, 0].mean() < 100        # the red bar (original left edge) was cropped away


def test_image_rotate_and_crop(lib, tmp_path):
    Image.fromarray(quad_picture(400, 200)).save(tmp_path / "p.png")
    m = upload(lib, tmp_path / "p.png")
    assert (m.width, m.height, m.source_width, m.source_height) == (400, 200, 400, 200)
    assert lib.source_path(m.id).name == "original.png"
    out = lib.update(m.id, edit=MediaEdit(rotate=270, crop=[0.0, 0.5, 1.0, 0.5], start=3, end=4))
    assert out.edit.start == 0 and out.edit.end is None        # a still has no time to trim
    r = lib.wait_ready(m.id, 30)
    assert (r.width, r.height) == (200, 200)
    with Image.open(lib.image_path(m.id)) as im:
        a = np.asarray(im.convert("RGB"))
    # turned counter-clockwise, the bottom half is the original's left half: red left, blue right
    assert near(colour_at(a, 0.25, 0.5), RED) and near(colour_at(a, 0.75, 0.5), BLUE)


def test_audio_trim_and_no_crop(lib, tmp_path):
    m = upload(lib, make_audio(tmp_path / "s.wav", seconds=3.0))
    r = apply(lib, m.id, MediaEdit(start=1.0, end=2.5))
    assert r.duration == pytest.approx(1.5, abs=0.02) and r.source_duration == pytest.approx(3.0, abs=0.02)
    with pytest.raises(ValueError):
        lib.update(m.id, edit=MediaEdit(crop=[0, 0, 0.5, 0.5]))
    assert lib.source_path(m.id) is None


def test_source_is_original_or_a_browser_copy(lib, tmp_path):
    m = upload(lib, make_video(tmp_path / "ok.mp4", n=10))
    assert lib.source_path(m.id).name == "original.mp4"
    m2 = upload(lib, make_video(tmp_path / "old.mkv", w=200, h=100, n=10, codec="mpeg4"))
    p = lib.source_path(m2.id)
    assert p.name == "source.mp4"
    with av.open(str(p)) as c:
        v = c.streams.video[0]
        assert v.codec_context.name == "h264" and (v.width, v.height) == (200, 100)
    # source.mp4 is made once: a new edit does not make it again
    before = p.stat().st_mtime_ns
    apply(lib, m2.id, MediaEdit(rotate=90))
    assert p.stat().st_mtime_ns == before


# ------------------------------------------------------------------ re-making while in use
def test_old_proxy_plays_until_the_new_one_is_ready(lib, tmp_path, monkeypatch):
    m = upload(lib, make_quad_video(tmp_path / "q.mp4"))
    old_path = lib.video_path(m.id)
    gate = threading.Event()
    real = library_mod.ingest.ingest_video

    def slow(*a, **k):
        gate.wait(10)
        return real(*a, **k)

    monkeypatch.setattr(library_mod.ingest, "ingest_video", slow)
    out = lib.update(m.id, edit=MediaEdit(rotate=90))
    assert out.status == "processing" and out.edit.rotate == 90
    # while it is being made, everything still hands out the old files, and they are intact
    time.sleep(0.2)
    cur = lib.get(m.id)
    assert cur.status == "processing" and cur.stage == "converting" and cur.version == 1
    assert (cur.width, cur.height) == (320, 180)
    assert lib.video_path(m.id) == old_path and Path(old_path).is_file()
    assert lib.thumb_path(m.id) is not None
    gate.set()
    r = lib.wait_ready(m.id, 30)
    assert r.version == 2 and (r.width, r.height) == (180, 320)
    assert lib.video_path(m.id) != old_path and Path(lib.video_path(m.id)).is_file()
    end = time.monotonic() + 5
    while Path(old_path).parent.exists() and time.monotonic() < end:
        time.sleep(0.05)
    assert not Path(old_path).parent.exists()                  # the superseded version is cleaned up
    assert sorted(p.name for p in (lib.root / m.id).iterdir()) == ["meta.json", "original.mp4", "v2"]


def test_superseded_proxy_held_open_is_removed_once_closed(lib, tmp_path):
    """Windows cannot delete the old proxy while the renderer still decodes it: it must go later."""
    m = upload(lib, make_quad_video(tmp_path / "q.mp4"))
    old = lib.video_path(m.id)
    c = av.open(old)
    try:
        next(c.decode(video=0))
        apply(lib, m.id, MediaEdit(rotate=180))
        assert lib.video_path(m.id) != old
    finally:
        c.close()
    end = time.monotonic() + 5
    while Path(old).parent.exists() and time.monotonic() < end:
        time.sleep(0.05)
    assert not Path(old).parent.exists()


def test_a_newer_edit_cancels_the_running_one(lib, tmp_path, monkeypatch):
    m = upload(lib, make_quad_video(tmp_path / "q.mp4"))
    started = threading.Event()
    real = library_mod.ingest.ingest_video

    def watch(src, out, check, edit, progress):
        started.set()
        if edit.rotate == 90:
            end = time.monotonic() + 10
            while time.monotonic() < end:      # stands for a long conversion that checks for cancellation
                check()
                time.sleep(0.01)
        return real(src, out, check, edit, progress)

    monkeypatch.setattr(library_mod.ingest, "ingest_video", watch)
    lib.update(m.id, edit=MediaEdit(rotate=90))
    assert started.wait(5)
    lib.update(m.id, edit=MediaEdit(rotate=270))
    r = lib.wait_ready(m.id, 30)
    assert r.edit.rotate == 270 and r.version >= 2
    img = first_frame(lib.video_path(m.id))
    assert near(colour_at(img, 0.1, 0.9), RED)
    time.sleep(0.3)
    assert [p.name for p in (lib.root / m.id).glob("v*")] == [f"v{r.version}"]


def test_a_failed_re_edit_keeps_the_previous_result(lib, tmp_path):
    m = upload(lib, make_quad_video(tmp_path / "q.mp4"))
    path = lib.video_path(m.id)
    orig = next((lib.root / m.id).glob("original.*"))
    orig.write_bytes(b"\0" * 1000)                                   # the original went bad on disk
    lib.update(m.id, edit=MediaEdit(rotate=90))
    r = lib.wait_ready(m.id, 30)
    assert r.status == "ready" and r.edit == MediaEdit() and r.version == 1
    assert r.error and "could not be made" in r.error
    assert lib.video_path(m.id) == path and Path(path).is_file()


def test_progress_is_reported(lib, tmp_path):
    seen = set()
    src = make_video(tmp_path / "v.mp4", w=640, h=360, n=90, sound=True)
    tmp = tmp_path / "up.mp4"
    shutil.copyfile(src, tmp)
    m = lib.add(tmp, "v")
    end = time.monotonic() + 30
    while time.monotonic() < end:
        cur = lib.get(m.id)
        seen.add((cur.stage, round(cur.progress, 1)))
        if cur.status != "processing":
            break
        time.sleep(0.005)
    assert cur.status == "ready" and cur.progress == 1.0 and cur.stage == ""
    stages = {s for s, _ in seen}
    assert "converting" in stages
    assert any(0 < p < 1 for s, p in seen if s == "converting")


# ------------------------------------------------------------------ duplicates
def test_duplicate_shares_the_original(lib, tmp_path):
    m = upload(lib, make_quad_video(tmp_path / "q.mp4"), "Quad")
    apply(lib, m.id, MediaEdit(rotate=90))
    d = lib.duplicate(m.id)
    assert d.id != m.id and d.name == "Quad copy" and d.status == "ready" and d.version == 1
    assert d.edit.rotate == 90 and (d.width, d.height) == (180, 320)
    assert lib.video_path(d.id) and Path(lib.video_path(d.id)).is_file()
    a, b = next((lib.root / m.id).glob("original.*")), next((lib.root / d.id).glob("original.*"))
    assert a.read_bytes() == b.read_bytes()
    if os.stat(a).st_nlink == 2:                     # a hard link: one copy on disk
        assert os.path.samefile(a, b)
    # a different crop of the same source; the first item keeps its own
    r = apply(lib, d.id, MediaEdit(crop=[0.5, 0.5, 0.5, 0.5]))
    assert (r.width, r.height) == (160, 90) and lib.get(m.id).edit.rotate == 90
    lib.delete(m.id)
    assert b.is_file() and lib.get(d.id).status == "ready"
    with pytest.raises(KeyError):
        lib.duplicate("nope")


def test_duplicate_copies_where_links_are_impossible(lib, tmp_path, monkeypatch):
    m = upload(lib, make_quad_video(tmp_path / "q.mp4"))

    def no_links(*a, **k):
        raise OSError("hard links not supported (FAT / exFAT)")

    monkeypatch.setattr(library_mod.os, "link", no_links)
    d = lib.duplicate(m.id, "Other")
    assert d.name == "Other" and d.status == "ready"
    b = next((lib.root / d.id).glob("original.*"))
    assert os.stat(b).st_nlink == 1 and b.stat().st_size == next((lib.root / m.id).glob("original.*")).stat().st_size


# ------------------------------------------------------------------ restarts
def test_restart_drops_half_made_versions_and_resumes(tmp_path):
    root = tmp_path / "media"
    lib = MediaLibrary(root)
    m = upload(lib, make_quad_video(tmp_path / "q.mp4"))
    lib.close()
    d = root / m.id
    (d / "v7").mkdir()
    (d / "v7" / "proxy.tmp.mp4").write_bytes(b"x")
    (d / "meta.json.tmp").write_text("{}")
    meta = json.loads((d / "meta.json").read_text())
    meta.update(status="processing", edit={"rotate": 90})
    (d / "meta.json").write_text(json.dumps(meta))
    lib2 = MediaLibrary(root)
    try:
        r = lib2.wait_ready(m.id, 30)
        assert r.status == "ready" and r.edit.rotate == 90 and r.version >= 2
        time.sleep(0.2)
        assert sorted(p.name for p in d.iterdir()) == ["meta.json", "original.mp4", f"v{r.version}"]
    finally:
        lib2.close()


def test_items_from_before_versions_are_adopted(tmp_path):
    """An item prepared by the earlier layout (files at the top of its folder) keeps working."""
    root = tmp_path / "media"
    lib = MediaLibrary(root)
    m = upload(lib, make_video(tmp_path / "v.mp4", n=10, sound=True))
    lib.close()
    d = root / m.id
    for f in (d / "v1").iterdir():
        os.replace(f, d / f.name)
    (d / "v1").rmdir()
    meta = json.loads((d / "meta.json").read_text())
    for k in ("version", "source_width", "source_height", "source_duration", "edit", "progress", "stage"):
        meta.pop(k, None)
    (d / "meta.json").write_text(json.dumps(meta))
    lib2 = MediaLibrary(root)
    try:
        r = lib2.get(m.id)
        assert r.version == 1 and r.source_width == r.width and r.status == "ready"
        assert Path(lib2.video_path(m.id)).is_file() and Path(lib2.audio_path(m.id)).is_file()
    finally:
        lib2.close()


# ------------------------------------------------------------------ link imports
class FakeFetch:
    """Stands in for download.fetch: makes a video in the work folder, reporting progress."""

    def __init__(self, tmp: Path, *, fail: str | None = None, gate: threading.Event | None = None,
                 title: str = "A video", start: float = 0.0, end: float | None = None):
        self.tmp, self.fail, self.gate, self.title, self.start, self.end = tmp, fail, gate, title, start, end
        self.calls: list[dict] = []

    def __call__(self, url, work, *, max_height, start, duration, progress, check):
        self.calls.append({"url": url, "max_height": max_height, "start": start, "duration": duration})
        work.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            progress(i / 5)
            check()
            if self.gate is not None:
                self.gate.wait(0.05)
        if self.fail:
            raise DownloadError(self.fail)
        p = make_video(work / "merged.mp4", n=30, sound=True)
        progress(1.0)
        return Fetched(p, self.title, self.start, self.end)


def test_import_from_a_link(tmp_path, monkeypatch):
    monkeypatch.setattr(library_mod, "_PROGRESS_EVERY", 0.0)
    fake = FakeFetch(tmp_path, start=0.25, end=0.75)
    lib = MediaLibrary(tmp_path / "media", fetcher=fake)
    try:
        m = lib.import_url("https://video.example/watch?v=1", max_height=720, start=0.25, duration=0.5)
        assert m.status == "processing" and m.stage == "queued" and m.name == "video.example"
        assert m.source_url == "https://video.example/watch?v=1"
        r = lib.wait_ready(m.id, 30)
        assert r.status == "ready", r.error
        assert fake.calls == [{"url": m.source_url, "max_height": 720, "start": 0.25, "duration": 0.5}]
        assert r.name == "A video" and r.kind == "video" and r.has_audio
        assert r.edit.start == 0.25 and r.edit.end == 0.75 and r.duration == pytest.approx(0.5, abs=0.04)
        assert r.size > 0 and lib.source_path(m.id).name == "original.mp4"
        assert not (lib.root / m.id / "download").exists()
        meta = json.loads((lib.root / m.id / "meta.json").read_text())
        assert "import" not in meta
        with pytest.raises(ValueError):
            lib.import_url("file:///etc/passwd")
    finally:
        lib.close()


def test_import_failure_is_a_readable_error_and_can_be_retried(tmp_path):
    fake = FakeFetch(tmp_path, fail="No internet connection: video.example could not be reached")
    lib = MediaLibrary(tmp_path / "media", fetcher=fake)
    try:
        m = lib.import_url("https://video.example/x", name="Mine")
        r = lib.wait_ready(m.id, 30)
        assert r.status == "error" and r.error.startswith("No internet") and r.name == "Mine"
        fake.fail = None
        lib.update(m.id, edit=r.edit)                      # the same edit again: try again
        r = lib.wait_ready(m.id, 30)
        assert r.status == "ready" and len(fake.calls) == 2 and fake.calls[1] == fake.calls[0]
    finally:
        lib.close()


def test_delete_during_download(tmp_path):
    gate = threading.Event()
    fake = FakeFetch(tmp_path, gate=gate)
    lib = MediaLibrary(tmp_path / "media", fetcher=fake)
    try:
        m = lib.import_url("https://video.example/x")
        time.sleep(0.05)
        lib.delete(m.id)
        gate.set()
        end = time.monotonic() + 5
        while (lib.root / m.id).exists() and time.monotonic() < end:
            time.sleep(0.05)
        assert not (lib.root / m.id).exists() and lib.get(m.id) is None
    finally:
        lib.close()


def test_probe_without_a_site_knows_nothing():
    from trafficlight.media.download import probe
    assert probe("ftp://x.example/a.mp4") == Probed()


def test_links_download_one_at_a_time_in_order(tmp_path, monkeypatch):
    monkeypatch.setattr(library_mod, "_PROGRESS_EVERY", 0.0)
    gate = threading.Event()
    running, most = [0], [0]
    lock = threading.Lock()

    class Counting(FakeFetch):
        def __call__(self, *a, **k):
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
            try:
                gate.wait(10)                   # the first one holds the line until the test has looked
                return super().__call__(*a, **k)
            finally:
                with lock:
                    running[0] -= 1

    fake = Counting(tmp_path)
    lib = MediaLibrary(tmp_path / "media", fetcher=fake)
    try:
        ms = [lib.import_url(f"https://video.example/{i}") for i in range(3)]
        assert all(m.stage == "queued" for m in ms)
        end = time.monotonic() + 5
        while lib.get(ms[0].id).stage != "downloading" and time.monotonic() < end:
            time.sleep(0.01)
        assert [lib.get(m.id).stage for m in ms] == ["downloading", "queued", "queued"]
        gate.set()
        for m in ms:
            assert lib.wait_ready(m.id, 30).status == "ready"
        assert most[0] == 1
        assert [c["url"] for c in fake.calls] == [m.source_url for m in ms]
        assert all(c["max_height"] == config.IMPORT_HEIGHT for c in fake.calls)
    finally:
        gate.set()
        lib.close()


def test_restart_during_download_fetches_again(tmp_path):
    gate = threading.Event()

    class Stuck(FakeFetch):
        def __call__(self, *a, check, **k):
            while True:
                check()                         # until the library closes
                time.sleep(0.01)

    lib = MediaLibrary(tmp_path / "media", fetcher=Stuck(tmp_path, gate=gate))
    m = lib.import_url("https://video.example/x", start=0.0, duration=None, max_height=480)
    time.sleep(0.1)
    lib.close()
    fake = FakeFetch(tmp_path)
    lib2 = MediaLibrary(tmp_path / "media", fetcher=fake)
    try:
        r = lib2.wait_ready(m.id, 30)
        assert r.status == "ready" and fake.calls[0]["max_height"] == 480
    finally:
        lib2.close()


# ------------------------------------------------------------------ the downloader's own logic
def test_choose_streams():
    fmts = [
        {"format_id": "sb0", "ext": "mhtml", "vcodec": "none", "acodec": "none", "url": "u", "protocol": "mhtml"},
        {"format_id": "137", "vcodec": "avc1.640028", "acodec": "none", "height": 1080, "width": 1920, "url": "u", "protocol": "https", "tbr": 4000},
        {"format_id": "248", "vcodec": "vp9", "acodec": "none", "height": 1080, "width": 1920, "url": "u", "protocol": "https", "tbr": 3000},
        {"format_id": "313", "vcodec": "vp9", "acodec": "none", "height": 2160, "width": 3840, "url": "u", "protocol": "https", "tbr": 9000},
        {"format_id": "136", "vcodec": "avc1.4d401f", "acodec": "none", "height": 720, "width": 1280, "url": "u", "protocol": "https"},
        {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128, "url": "u", "protocol": "https"},
        {"format_id": "251", "vcodec": "none", "acodec": "opus", "abr": 130, "url": "u", "protocol": "https"},
        {"format_id": "251-drc", "vcodec": "none", "acodec": "opus", "abr": 140, "url": "u", "protocol": "https"},
        {"format_id": "18", "vcodec": "avc1", "acodec": "mp4a", "height": 360, "width": 640, "url": "u", "protocol": "https"},
    ]
    pic, snd = choose(fmts, 1080)
    assert pic["format_id"] == "137" and snd["format_id"] == "251"     # H.264 over VP9; Opus, not the DRC copy
    assert choose(fmts, 720)[0]["format_id"] == "136"
    assert choose(fmts, 100)[0]["format_id"] == "18"                    # nothing small enough: the smallest
    # portrait: the 1080 of "1080p" is the short side
    tall = [{"format_id": "v", "vcodec": "avc1", "acodec": "none", "height": 1920, "width": 1080, "url": "u", "protocol": "https"},
            {"format_id": "a", "vcodec": "none", "acodec": "opus", "url": "u", "protocol": "https"}]
    assert choose(tall, 1080)[0]["format_id"] == "v"
    # a direct file: codecs unknown, taken as picture and sound
    assert choose([{"format_id": "0", "url": "u", "ext": "mp4"}], 1080) == ({"format_id": "0", "url": "u", "ext": "mp4"}, None)
    assert choose([], 1080) == (None, None)


def test_explain_errors():
    url = "https://www.youtube.com/watch?v=x"
    assert explain(Exception("ERROR: [youtube] x: Unable to download webpage: <urlopen error [Errno -3] "
                             "Temporary failure in name resolution>"), url).startswith("No internet connection: www.youtube.com")
    assert "not available" in explain(Exception("ERROR: [youtube] x: Video unavailable"), url)
    assert explain(Exception("<urlopen error [Errno 111] Connection refused>"), "http://127.0.0.1:9/x").startswith(
        "127.0.0.1 could not be reached")
    assert "unsupported" in explain(Exception("ERROR: Unsupported URL: https://example.com/"), url)
    assert explain(Exception("\x1b[0;31mERROR:\x1b[0m odd"), url) == "The link could not be fetched (odd)"


def test_merge_keeps_picture_and_sound_in_step(tmp_path):
    """Two separate downloads (picture only, sound only) become one file; a part is cut at a keyframe."""
    pic = make_video(tmp_path / "pic.mp4", w=96, h=64, n=90, fps=30)          # keyframes every 250 frames: only 0
    snd = make_audio(tmp_path / "snd.m4a", seconds=3.0, codec="aac", fmt="ipod")
    out, cut = merge(pic, snd, tmp_path / "merged")
    assert out.suffix == ".mp4" and cut == 0
    with av.open(str(out)) as c:
        assert [s.type for s in c.streams] == ["video", "audio"]
        assert c.streams.video[0].codec_context.name == "h264" and c.streams.audio[0].codec_context.name == "aac"
        v = [p for p in c.demux(c.streams.video[0]) if p.size]
    assert len(v) == 90
    out2, cut2 = merge(pic, snd, tmp_path / "part", start=1.0, duration=1.0)
    assert cut2 == 0.0                                   # the only keyframe at or before 1 s is the first
    with av.open(str(out2)) as c:
        n = sum(1 for p in c.demux(c.streams.video[0]) if p.size)
    assert 60 <= n <= 90                                 # up to 2 s plus a little: the trim cuts exactly

