"""The video / image / audio clip types against a library holding generated media."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from test_media import frame_picture, make_audio, make_video, read_index, upload
from trafficlight import config
from trafficlight.media import MediaLibrary
from trafficlight.model import Settings
from trafficlight.render import get_renderer
from trafficlight.render.base import RenderContext, RenderEnv

FPS = 30
N = 40                     # frames in the test video: 1.333 s
SRC_W, SRC_H = 160, 90     # landscape, so each fit mode looks different in both boxes


def ctx(t: float, height: int = config.FACE) -> RenderContext:
    return RenderContext(t=t, clip_duration=10.0, set_time=t, bpm=120.0, beat=0.0, clip_beat=0.0,
                         width=config.W, height=height, face=0 if height == config.FACE else None,
                         face_color=(255, 0, 0), seed=1)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("media")
    lib = MediaLibrary(tmp / "lib")
    items = {
        "video": upload(lib, make_video(tmp / "count.mp4", w=SRC_W, h=SRC_H, n=N, fps=FPS, sound=True)),
        "silent": upload(lib, make_video(tmp / "silent.mp4", n=10)),
        "song": upload(lib, make_audio(tmp / "song.wav", seconds=3.0)),
    }
    a = np.zeros((60, 80, 4), np.uint8)
    a[..., 1] = 255
    a[:, 40:, 3] = 255
    Image.fromarray(a, "RGBA").save(tmp / "half.png")
    items["alpha"] = upload(lib, tmp / "half.png")
    Image.fromarray(frame_picture(5, 300, 300)).save(tmp / "square.jpg")
    items["photo"] = upload(lib, tmp / "square.jpg")
    for k, m in items.items():
        assert m.status == "ready", (k, m.error)
    yield lib, items
    lib.close()


@pytest.fixture
def env(media):
    return RenderEnv(settings=Settings(), media=media[0])


def make(env, type_id, **params):
    return get_renderer(type_id)(params, env)


def settle(r, c, expect_from=None, timeout=3.0):
    """Call render() like the render thread would until the decoder has caught up. Every frame shown
    on the way must be the one asked for or the one shown before (never a wrong frame)."""
    end = time.monotonic() + timeout
    seen = []
    while True:
        out = r.render(c)
        if out is not None:
            seen.append(out)
        if seen and r._dec is not None and r._dec.get(r._want_idx) is not None:
            out = r.render(c)
            return out, seen
        assert time.monotonic() < end, "decoder did not deliver"
        time.sleep(0.002)


def picture_rows(out: np.ndarray) -> tuple[int, int]:
    """Rows of the box covered by the picture (alpha > 0) in a contain-fitted frame."""
    if out.shape[2] == 3:
        return 0, out.shape[0]
    rows = np.nonzero(out[:, :, 3].max(axis=1))[0]
    return int(rows[0]), int(rows[-1]) + 1


def index_of(out: np.ndarray) -> int:
    top, bottom = picture_rows(out)
    return read_index(out, top, bottom - top)


@pytest.mark.parametrize("height", [config.FACE, config.H])
@pytest.mark.parametrize("fit", ["cover", "contain", "stretch"])
def test_video_frame_for_time(env, media, height, fit):
    vid = media[1]["video"]
    r = make(env, "video", media=vid.id, fit=fit)
    try:
        prev = None
        # forward, a jump ahead, the loop wrap, a jump back (scrubbing)
        for t, expect in [(0.0, 0), (0.5, 15), (0.52, 15), (1.2, 36), (1.30, 39), (1.40, 2),
                          (0.1, 3), (1.0 / FPS * 7 + 0.001, 7)]:
            out, seen = settle(r, ctx(t, height))
            assert out.shape[:2] == (height, config.W) and out.dtype == np.uint8
            assert index_of(out) == expect % N, (t, fit, height)
            for s in seen:
                assert index_of(s) in (expect % N, prev), (t, [index_of(x) for x in seen])
            prev = expect % N
        # geometry of each fit
        if fit == "contain":
            assert out.shape[2] == 4
            top, bottom = picture_rows(out)
            expect_h = round(SRC_H * config.W / SRC_W)
            assert bottom - top == expect_h and out[0, :, 3].max() == 0
            assert tuple(out[(top + bottom) // 2, 2, :3]) == pytest.approx((255, 0, 0), abs=40)
        elif fit == "stretch":
            assert out.shape[2] == 3
            assert out[height // 2, 2, 0] > 200 and out[height // 2, 2, 1] < 60      # red bar kept
        else:
            assert out.shape[2] == 3
            assert not ((out[..., 0] > 200) & (out[..., 1] < 60)).any()                # red bar cropped away
    finally:
        r.close()


def test_video_params(env, media):
    vid = media[1]["video"]
    for params, t, expect in [
        (dict(offset=0.5), 0.0, 15),
        (dict(speed=2.0), 0.5, 30),
        (dict(speed=0.5), 1.0, 15),
        (dict(loop=False), 5.0, N - 1),            # past the end: the last frame stays
        (dict(offset=1.0, loop=True), 0.5, 5),     # 1.5 s wraps to frame 5
    ]:
        r = make(env, "video", media=vid.id, **params)
        try:
            out, _ = settle(r, ctx(t))
            assert index_of(out) == expect, params
        finally:
            r.close()


def test_video_realtime_playback_is_smooth(env, media):
    """At 60 fps on the render thread, a 30 fps file shows the right frame nearly every time, through
    two loop wraps, and render() itself stays fast."""
    vid = media[1]["video"]
    r = make(env, "video", media=vid.id)
    try:
        settle(r, ctx(0.0))
        start = time.perf_counter()
        wrong, worst, calls = 0, 0.0, 0
        while (t := time.perf_counter() - start) < 3.0:
            c = ctx(t)
            t0 = time.perf_counter()
            out = r.render(c)
            worst = max(worst, time.perf_counter() - t0)
            calls += 1
            if index_of(out) != int(t * FPS + 1e-6) % N:
                wrong += 1
            time.sleep(max(0.0, (calls / 60.0) - (time.perf_counter() - start)))
        assert calls > 150
        assert wrong <= calls * 0.05, (wrong, calls, r.stats)
        assert worst < 0.02
    finally:
        r.close()


def test_video_preroll_has_the_first_frame_ready(env, media):
    """The player makes renderers ahead of their clip: the first render() must not wait or miss."""
    r = make(env, "video", media=media[1]["video"].id, offset=0.5)
    try:
        end = time.monotonic() + 2
        while not r._dec.raw and time.monotonic() < end:
            time.sleep(0.005)
        t0 = time.perf_counter()
        out = r.render(ctx(0.0, config.H))
        assert time.perf_counter() - t0 < 0.01
        assert out is not None and index_of(out) == 15 and r.stats["late"] == 0
    finally:
        r.close()


def test_video_close_stops_the_thread(env, media):
    r = make(env, "video", media=media[1]["video"].id)
    settle(r, ctx(0.0))
    th = r._dec.thread
    r.close()
    th.join(2)
    assert not th.is_alive()
    r.close()          # twice is fine


def test_video_sound_cue(env, media):
    vid = media[1]["video"]
    lib = media[0]
    r = make(env, "video", media=vid.id, volume=0.4)
    cue = r.audio(ctx(0.5))
    assert cue.path == lib.audio_path(vid.id) and Path(cue.path).is_file()
    assert cue.position == pytest.approx(0.5) and cue.gain == pytest.approx(0.4)
    assert r.audio(ctx(vid.duration + 0.25)).position == pytest.approx(0.25, abs=1e-6)    # loop
    assert make(env, "video", media=vid.id, offset=0.3).audio(ctx(0.2)).position == pytest.approx(0.5)
    assert make(env, "video", media=vid.id, sound=False).audio(ctx(0.5)) is None
    assert make(env, "video", media=vid.id, speed=2).audio(ctx(0.5)) is None
    assert make(env, "video", media=vid.id, loop=False).audio(ctx(vid.duration + 1)) is None
    assert make(env, "video", media=media[1]["silent"].id).audio(ctx(0.1)) is None


def test_video_validate_and_duration(env, media):
    V = get_renderer("video")
    items = media[1]
    assert V.validate({"media": items["video"].id}, "face0", env) == []
    assert V.validate({}, "face0", env) == ["No video chosen"]
    assert "no longer" in V.validate({"media": "gone"}, "all", env)[0]
    assert "not a video" in V.validate({"media": items["song"].id}, "face1", env)[0]
    assert "speed 1" in V.validate({"media": items["video"].id, "speed": 2}, "face1", env)[0]
    assert V.validate({"media": items["video"].id}, "face0", RenderEnv(settings=Settings())) != []
    assert V.natural_duration({"media": items["video"].id}, env) == pytest.approx(N / FPS)
    assert V.natural_duration({"media": items["video"].id, "speed": 2}, env) == pytest.approx(N / FPS / 2)
    assert V.natural_duration({}, env) is None
    assert make(env, "video").render(ctx(0)) is None
    assert make(env, "video", media="gone").render(ctx(0)) is None
    bad = make(env, "video", media=items["video"].id, speed="fast", offset=None, fit="odd", loop=None)
    out, _ = settle(bad, ctx(0.5))                     # bad params never raise: defaults instead
    assert index_of(out) == 15
    bad.close()


def test_video_three_at_once(env, media):
    vid = media[1]["video"]
    rs = [make(env, "video", media=vid.id, offset=k * 0.3) for k in range(3)]
    try:
        outs = [settle(r, ctx(0.2))[0] for r in rs]
        assert [index_of(o) for o in outs] == [6, 15, 24]
    finally:
        for r in rs:
            r.close()


def test_image(env, media):
    items = media[1]
    r = make(env, "image", media=items["alpha"].id, fit="stretch")
    out = r.render(ctx(0))
    assert out.shape == (config.FACE, config.W, 4)
    assert out[64, 10, 3] == 0 and out[64, 120, 3] == 255 and out[64, 120, 1] == 255
    out[:] = 0                                            # the player may write into it
    assert r.render(ctx(0))[64, 120, 3] == 255

    photo = items["photo"]
    out = make(env, "image", media=photo.id).render(ctx(0, config.H))
    assert out.shape == (config.H, config.W, 3)                 # cover: square photo in a tall box
    out = make(env, "image", media=photo.id, fit="contain").render(ctx(0, config.H))
    assert out.shape == (config.H, config.W, 4)
    top, bottom = picture_rows(out)
    assert bottom - top == config.W and read_index(out, top, bottom - top) == 5
    assert make(env, "image", media="gone").render(ctx(0)) is None
    I = get_renderer("image")
    assert I.validate({"media": photo.id}, "face0", env) == []
    assert I.validate({"media": items["video"].id}, "face0", env) != []
    assert I.describe()["category"] == "media"


def test_audio_clip(env, media):
    items, lib = media[1], media[0]
    song = items["song"]
    A = get_renderer("audio")
    assert A.targets == ("audio",)
    r = make(env, "audio", media=song.id, offset=1.0, volume=0.5)
    assert r.render(ctx(0)) is None
    cue = r.audio(ctx(0.5))
    assert cue.path == lib.audio_path(song.id) and cue.position == pytest.approx(1.5) and cue.gain == 0.5
    assert r.audio(ctx(2.5)) is None                                     # past the end, no loop
    assert make(env, "audio", media=song.id, loop=True).audio(ctx(3.5)).position == pytest.approx(0.5, abs=0.01)
    # a video's sound track
    vcue = make(env, "audio", media=items["video"].id).audio(ctx(0.2))
    assert vcue.path == lib.audio_path(items["video"].id)
    assert A.validate({"media": items["video"].id}, "audio", env) == []
    assert "no sound" in A.validate({"media": items["silent"].id}, "audio", env)[0]
    assert A.validate({}, "audio", env) == ["No sound chosen"]
    assert A.natural_duration({"media": song.id}, env) == pytest.approx(3.0, abs=0.02)
    assert A.natural_duration({"media": song.id, "offset": 1}, env) == pytest.approx(2.0, abs=0.02)
    assert make(env, "audio", media="gone").audio(ctx(0)) is None


# ------------------------------------------------------------------ a re-made crop while the clip plays
def _is_red(out: np.ndarray) -> bool:
    rgb = out[..., :3].astype(int)
    return rgb[..., 0].mean() > 200 and rgb[..., 1].mean() < 60 and rgb[..., 2].mean() < 60


def test_playing_clips_switch_to_a_re_made_crop(tmp_path, monkeypatch):
    """The edit changes while a video, its sound and a picture play: they carry on without a gap and
    take the new files as soon as those are complete; the old files are let go and removed."""
    from trafficlight.media import library as library_mod
    from trafficlight.model import MediaEdit
    monkeypatch.setattr(library_mod, "_SWEEP_SECONDS", 0.1)
    lib = MediaLibrary(tmp_path / "lib")
    try:
        vid = upload(lib, make_video(tmp_path / "count.mp4", w=SRC_W, h=SRC_H, n=N, fps=FPS, sound=True))
        Image.fromarray(frame_picture(9, 300, 200)).save(tmp_path / "pic.png")
        pic = upload(lib, tmp_path / "pic.png")
        env = RenderEnv(settings=Settings(), media=lib)
        v = make(env, "video", media=vid.id, fit="stretch")
        snd = make(env, "audio", media=vid.id)
        im = make(env, "image", media=pic.id, fit="stretch")
        t = 0.2
        out, _ = settle(v, ctx(t))
        assert not _is_red(out) and not _is_red(im.render(ctx(t)))
        old_dec, old_video = v._dec, lib.video_path(vid.id)
        old_sound = snd.audio(ctx(t)).path
        assert v.audio(ctx(t)).path == old_sound

        red_bar = MediaEdit(crop=[0.0, 0.0, 0.08, 1.0])       # only the red bar on the left edge
        lib.update(vid.id, edit=red_bar)
        lib.update(pic.id, edit=red_bar)
        # while the new files are made, the clips go on with the old ones
        while lib.get(vid.id).status == "processing" or lib.get(pic.id).status == "processing":
            t += 1 / 60
            out = v.render(ctx(t))
            # (the new video may be published between the status check above and this frame)
            assert out is not None and (not _is_red(out) or lib.get(vid.id).version == 2)
            time.sleep(0.002)
        assert lib.get(vid.id).version == 2 and lib.get(pic.id).version == 2
        end = time.monotonic() + 3
        while True:
            t += 1 / 60
            out = v.render(ctx(t))
            assert out is not None                              # never a gap on the way
            if _is_red(out):
                break
            assert time.monotonic() < end, "the video never switched to the new crop"
            time.sleep(0.002)
        assert v._dec is not old_dec and old_dec.closed and v._dec.path == lib.video_path(vid.id) != old_video
        assert v.audio(ctx(t)).path == lib.audio_path(vid.id) != old_sound
        assert snd.audio(ctx(t)).path == lib.audio_path(vid.id)
        assert _is_red(im.render(ctx(t)))
        # nothing holds the old files any more: they go (on Windows too)
        end = time.monotonic() + 5
        while Path(old_video).parent.exists() and time.monotonic() < end:
            time.sleep(0.05)
        assert not Path(old_video).parent.exists()
        v.close()
    finally:
        lib.close()
