import io
import sys
import time

import numpy as np
import pytest
from PIL import Image

from test_player_fakes import LOG, FakeAudio, FakeDriver, make_set
from trafficlight import config
from trafficlight.engine import Engine, FrameSlot, make_pattern
from trafficlight.model import Clip
from trafficlight.store import Store

F = config.FACE
C = F // 2


def solid(color, start=0.0, duration=2.0, tag="", **kw):
    return Clip(type="test:solid", start=start, duration=duration, params={"color": color, "tag": tag}, **kw)


def px(frame, face):
    return tuple(int(v) for v in frame[face * F + C, C])


class Rig:
    def __init__(self, tmp_path, **settings):
        self.store = Store(tmp_path)
        if settings:
            self.store.update_settings(settings)
        self.audio = FakeAudio()
        self.drivers: list[FakeDriver] = []
        self.fail_driver = False
        self.now = 100.0
        self.engine = Engine(self.store, None, self.audio, self._driver, clock=lambda: self.now)

    def _driver(self, out):
        d = FakeDriver(out, fail=self.fail_driver)
        self.drivers.append(d)
        return d

    def add(self, name, *clips_by_target, duration=2.0, **kw):
        s = make_set(*clips_by_target, duration=duration, name=name, **kw)
        return self.store.create_set(s.model_dump())

    def step(self, dt=1 / 60):
        self.now += dt
        self.engine.step(self.now)

    def run(self, seconds, dt=1 / 60):
        for _ in range(int(round(seconds / dt))):
            self.step(dt)

    @property
    def frame(self):
        return self.engine.sign_frame.get()[1]

    @property
    def now_state(self):
        return self.engine.state()["now"]


def test_idle_shows_unlit_lights(tmp_path):
    r = Rig(tmp_path)
    r.step()
    st = r.engine.state()
    assert st["now"]["source"] == "idle" and st["now"]["set_id"] is None
    assert px(r.frame, 0) == (0, 0, 0) and r.frame.max() > 0     # rings only
    assert r.drivers and r.drivers[0].frames == 1 and r.drivers[0].brightness == 0.5
    assert r.audio.cues == [] and r.audio.volume == 0.8 and r.audio.device is None
    assert st["output"]["ok"] and st["sets_rev"] == 0 and st["media_rev"] == 0 and st["version"]


def test_preview_frame_not_spoiled_by_driver(tmp_path):
    r = Rig(tmp_path, off_style="dark")
    r.add("A", ("face0", [solid("#ff0000")]))
    r.step()
    assert px(r.frame, 0) == (255, 0, 0)       # FakeDriver zeroes what it gets


def test_default_set_loops_without_recreating_renderers(tmp_path):
    LOG.clear()
    r = Rig(tmp_path)
    a = r.add("A", ("face0", [solid("#ff0000", 0, 2, tag="A")]))
    r.add("B", ("face0", [solid("#00ff00", 0, 2, tag="B")]))
    r.store.update_settings({"schedule": {"default_set": a.id, "random_minutes": 60}})
    r.step()
    assert r.now_state["set_id"] == a.id and r.now_state["source"] == "schedule"
    r.run(5.0)
    assert r.now_state["set_id"] == a.id
    assert 0.9 < r.now_state["t"] < 1.1         # no drift over the wraps
    assert [e for e in LOG if e[1] == "A"] == [("init", "A")]
    assert r.engine.state()["schedule"]["random_in"] == pytest.approx(3600 - 5, abs=0.2)


def test_random_set_then_back(tmp_path):
    r = Rig(tmp_path)
    a = r.add("A", ("face0", [solid("#ff0000")]))
    b = r.add("B", ("face0", [solid("#00ff00")]), duration=1.0)
    r.store.update_settings({"schedule": {"default_set": a.id, "random_minutes": 0}})
    seen = []
    for _ in range(int(6 / (1 / 60))):
        r.step()
        sid = r.now_state["set_id"]
        if not seen or seen[-1] != sid:
            seen.append(sid)
    assert seen[:4] == [a.id, b.id, a.id, b.id]


def test_in_order_without_default(tmp_path):
    r = Rig(tmp_path)
    ids = [r.add(n, ("face0", [solid("#ff0000")]), duration=1.0).id for n in "XYZ"]
    seen = []
    for _ in range(int(4 / (1 / 60))):
        r.step()
        if not seen or seen[-1] != r.now_state["set_id"]:
            seen.append(r.now_state["set_id"])
    assert seen == ids + [ids[0]]


def test_show_now_pause_next(tmp_path):
    r = Rig(tmp_path, off_style="dark")
    a = r.add("A", ("face0", [solid("#ff0000")]), duration=3)
    b = r.add("B", ("face1", [solid("#00ff00")]), duration=1)
    r.store.update_settings({"schedule": {"default_set": a.id}})
    r.run(0.5)
    assert r.engine.show_set(b.id) and not r.engine.show_set("nope")
    r.step()
    assert r.now_state["source"] == "show" and r.now_state["set_id"] == b.id
    assert px(r.frame, 1) == (0, 255, 0)
    r.run(1.0)
    assert r.now_state["set_id"] == a.id and r.now_state["source"] == "schedule"
    # pause: the clock stands, the audio engine is told
    r.engine.pause()
    r.step()
    t = r.now_state["t"]
    r.run(1.0)
    assert r.now_state["t"] == t and r.now_state["paused"] and r.audio.playing is False
    r.engine.play()
    r.run(0.5)
    assert r.now_state["t"] > t and r.audio.playing is True
    r.engine.next()
    r.step()
    assert r.now_state["set_id"] == a.id and r.now_state["t"] < 0.1


def test_temp_show_and_edits_and_delete_while_playing(tmp_path):
    r = Rig(tmp_path, off_style="dark")
    a = r.add("A", ("face0", [solid("#ff0000", 0, 10)]), duration=10)
    r.add("B", ("face0", [solid("#0000ff", 0, 10)]), duration=10)
    r.run(1.0)
    t = r.now_state["t"]
    s = r.store.get_set(a.id)
    s.tracks[0].clips[0].params["color"] = "#ffffff"
    r.store.save_set(s)
    r.step()
    assert px(r.frame, 0) == (255, 255, 255) and r.now_state["t"] > t        # picked up, kept playing
    r.store.delete_set(a.id)
    r.step()
    assert r.now_state["set_name"] == "B" and px(r.frame, 0) == (0, 0, 255)
    temp = make_set(("face2", [solid("#00ff00", 0, 1)]), duration=1, name="clip.mp4")
    r.engine.show_temp(temp)
    r.step()
    assert r.now_state == {**r.now_state, "source": "show", "set_id": None, "set_name": "clip.mp4"}
    assert px(r.frame, 2) == (0, 255, 0)
    r.run(1.1)
    assert r.now_state["set_name"] == "B"


def test_editor_live_and_draft_updates(tmp_path):
    LOG.clear()
    r = Rig(tmp_path, off_style="dark")
    r.add("A", ("face0", [solid("#ff0000", 0, 10)]), duration=10)
    r.run(0.5)
    sign_t = r.now_state["t"]
    ed = r.engine.open_editor()
    draft = make_set(("face1", [solid("#00ff00", 0, 5, tag="E1")]), ("face2", [solid("#0000ff", 0, 5, tag="E2")]),
                     duration=5)
    ed.set_draft(draft, [])
    r.step()
    assert r.now_state["source"] == "editor"
    assert px(r.frame, 1) == (0, 255, 0) and px(r.frame, 0) == (0, 0, 0)
    assert ed.status()["t"] == 0 and not ed.status()["playing"]          # opens paused
    ed.play()
    r.run(1.0)
    assert ed.status()["t"] == pytest.approx(1.0, abs=0.02)
    # edit one clip: playback position kept, the other clip's renderer untouched
    d2 = draft.model_copy(deep=True)
    d2.tracks[1].clips[0].params["color"] = "#ffffff"
    ed.set_draft(d2, [{"clip_id": "x", "track_id": "y", "message": "m"}])
    r.step()
    assert ed.status()["t"] > 1.0 and ed.status()["problems"][0]["message"] == "m"
    assert px(r.frame, 2) == (255, 255, 255)
    assert [e for e in LOG if e[1] == "E1"] == [("init", "E1")]
    # seek while paused shows the frame at t
    ed.pause()
    d3 = d2.model_copy(deep=True)
    d3.tracks[0].clips[0].start, d3.tracks[0].clips[0].duration = 3, 1
    ed.set_draft(d3, [])
    ed.seek(1.0)
    r.step()
    assert px(r.frame, 1) == (0, 0, 0)
    ed.seek(3.5)
    r.step()
    assert px(r.frame, 1) == (0, 255, 0)
    # the sign was suspended meanwhile
    r.engine.close_editor(ed)
    r.step()
    assert r.now_state["source"] == "schedule"
    assert r.now_state["t"] == pytest.approx(sign_t + 1 / 60, abs=0.03)


def test_editor_loop_and_end(tmp_path):
    r = Rig(tmp_path)
    ed = r.engine.open_editor()
    ed.set_draft(make_set(("face0", [solid("#ff0000", 0, 4)]), duration=4), [])
    ed.loop(True, 1.0, 2.0)
    ed.seek(1.5)
    ed.play()
    r.run(0.6)
    assert 1.0 <= ed.status()["t"] < 1.2
    ed.loop(False)
    ed.seek(3.5)
    r.run(1.0)
    st = ed.status()
    assert st["t"] == 4.0 and not st["playing"]
    ed.play()                                   # play at the end starts over
    r.step()
    assert ed.status()["t"] < 0.1
    # loop on, whole set: wraps at the end
    ed.loop(True)
    ed.seek(3.9)
    r.run(0.2)
    assert ed.status()["t"] < 0.2 and ed.status()["playing"]


def test_editor_not_live_previews_only(tmp_path):
    r = Rig(tmp_path, off_style="dark")
    r.add("A", ("face0", [solid("#ff0000", 0, 10)]), duration=10)
    snd = Clip(type="test:sound", start=0, duration=5, params={"media": "x"})
    ed = r.engine.open_editor()
    ed.set_live(False)
    ed.set_draft(make_set(("face1", [solid("#00ff00", 0, 5)]), ("audio", [snd]), duration=5), [])
    ed.play()
    r.run(0.5)
    assert r.now_state["source"] == "schedule" and r.now_state["t"] > 0.4
    assert px(r.frame, 0) == (255, 0, 0) and px(r.frame, 1) == (0, 0, 0)
    ef = ed.frame.get()[1]
    assert px(ef, 1) == (0, 255, 0)
    assert r.audio.cues == []           # no sound from a preview-only editor
    ed.set_live(True)
    r.step()
    assert [c.key for c in r.audio.cues] == [snd.id] and r.audio.playing


def test_second_editor_closes_first(tmp_path):
    r = Rig(tmp_path)
    e1 = r.engine.open_editor()
    e2 = r.engine.open_editor()
    assert e1.closed_reason and e2.closed_reason is None
    r.engine.close_editor(e1)          # the old socket's cleanup must not end the new session
    assert r.engine.editor is e2


def test_test_patterns(tmp_path):
    r = Rig(tmp_path)
    r.add("A", ("face0", [solid("#ff0000", 0, 10)]), duration=10)
    r.engine.test_pattern("blue")
    r.step()
    assert r.now_state["source"] == "test" and (r.frame == (0, 0, 255)).all()
    assert r.audio.cues == []
    with pytest.raises(ValueError):
        r.engine.test_pattern("plaid")
    r.engine.test_pattern("off")
    r.step()
    assert r.now_state["source"] == "schedule"
    for name in ("panels", "white", "red", "green", "blue", "gradient"):
        assert make_pattern(name).shape == (config.H, config.W, 3)


def test_settings_applied_live(tmp_path):
    r = Rig(tmp_path)
    r.step()
    assert len(r.drivers) == 1
    r.store.update_settings({"output": {"brightness": 0.9}})
    r.step()
    assert len(r.drivers) == 1 and r.drivers[0].brightness == 0.9
    r.store.update_settings({"output": {"rotate180": True}})
    r.step()
    assert len(r.drivers) == 2 and r.drivers[0].closed and r.drivers[1].settings.rotate180
    r.store.update_settings({"audio": {"device": "dev1", "volume": 0.2}})
    r.step()
    assert r.audio.device == "dev1" and r.audio.volume == 0.2
    r.store.update_settings({"face_colors": ["#0000ff", "#0000ff", "#0000ff"]})
    r.step()
    assert r.engine.env.settings.face_colors[0] == "#0000ff"


def test_driver_failure_reported_and_retried(tmp_path):
    r = Rig(tmp_path)
    r.fail_driver = True
    r.step()
    out = r.engine.state()["output"]
    assert out["ok"] is False and "no such interface" in out["error"]
    n = len(r.drivers)
    r.run(1.0)
    assert len(r.drivers) == n                  # not hammering
    r.fail_driver = False
    r.run(3.0)
    assert r.engine.state()["output"]["ok"] is True


def test_frame_slot_encoding():
    slot = FrameSlot()
    assert slot.encoded() == (0, None)
    flat = np.zeros((config.H, config.W, 3), np.uint8)
    flat[:F] = (255, 0, 0)
    slot.put(flat)
    no, data = slot.encoded()
    assert no == 1 and data[:4] == b"\x89PNG"
    assert slot.encoded()[1] is data            # encoded once per frame
    img = Image.open(io.BytesIO(data))
    assert img.size == (config.W, config.H) and np.array_equal(np.asarray(img), flat)
    noise = np.random.default_rng(1).integers(0, 255, (config.H, config.W, 3), dtype=np.uint8)
    slot.put(noise)
    no, data = slot.encoded()
    assert no == 2 and data[:2] == b"\xff\xd8"


def _real_engine(tmp_path, fps):
    r = Rig(tmp_path, fps=fps)
    clips = [("all", [solid("#ff0000", 0, 30, mask=True, opacity=0.5)])]
    clips += [(f"face{i}", [solid("#00ff00", 0, 30, fade_in=60)]) for i in range(3)]
    r.add("A", *clips, duration=30)
    r.add("Boom", ("face0", [Clip(type="test:boom", duration=30, params={"where": "render"})]), duration=30)
    r.engine = Engine(r.store, None, r.audio, r._driver)     # the real clock
    return r


@pytest.mark.parametrize("fps", [60, 30])
def test_render_thread_holds_fps(tmp_path, fps):
    r = _real_engine(tmp_path, fps)
    e = r.engine
    e.start()
    try:
        time.sleep(0.5)
        f0, t0 = e.frames, time.perf_counter()
        time.sleep(2.0)
        f1, t1 = e.frames, time.perf_counter()
        e.next()     # onto the set whose renderer raises: the thread lives on
        time.sleep(0.3)
        f2 = e.frames
    finally:
        e.stop()
    measured = (f1 - f0) / (t1 - t0)
    assert abs(measured - fps) < fps * 0.03, measured
    assert abs(e.fps - fps) < fps * 0.05
    assert f2 > f1 + fps * 0.2
    assert r.drivers[-1].closed


def test_render_thread_survives_step_errors(tmp_path, monkeypatch):
    r = Rig(tmp_path)
    e = r.engine = Engine(r.store, None, r.audio, r._driver)
    calls = {"n": 0}
    real = e._advance_sign

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] % 2:
            raise RuntimeError("frame bug")
        return real(*a, **kw)

    monkeypatch.setattr(e, "_advance_sign", flaky)
    e.start()
    time.sleep(0.3)
    e.stop()
    assert calls["n"] > 10 and e.frames > 5


@pytest.mark.skipif(sys.platform != "win32", reason="Windows timer check")
def test_sleep_resolution_windows():
    t0 = time.perf_counter()
    for _ in range(50):
        time.sleep(0.002)
    assert (time.perf_counter() - t0) / 50 < 0.004


def test_next_set_warmed_up_before_switch(tmp_path):
    LOG.clear()
    r = Rig(tmp_path)
    r.add("A", ("face0", [solid("#ff0000", 0, 2, tag="A0")]), duration=2)
    r.add("B", ("face0", [solid("#00ff00", 0, 2, tag="B0")]), duration=2)
    r.step()
    r.run(1.5)                       # inside the last second of A: B's first clip is being made
    deadline = time.time() + 2
    while ("init", "B0") not in LOG and time.time() < deadline:
        time.sleep(0.01)
    assert ("init", "B0") in LOG and r.now_state["set_name"] == "A"
    r.run(0.6)
    assert r.now_state["set_name"] == "B"
    assert LOG.count(("init", "B0")) == 1 and LOG.count(("init", "A0")) == 1   # A did not pre-roll itself
