"""The media routes for importing, editing, duplicating and the crop tool's source, against a real
MediaLibrary (generated media, a fake downloader: no network)."""
from __future__ import annotations

import io
import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from test_media import make_video
from test_media_edit import GREEN, RED, WHITE, FakeFetch, colour_at, make_quad_video, near
from test_player_fakes import FakeAudio, FakeDriver
from trafficlight import config
from trafficlight.media import MediaLibrary
from trafficlight.media import library as library_mod
from trafficlight.server import create_app


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(library_mod, "_SWEEP_SECONDS", 0.1)
    fetch = FakeFetch(tmp_path)
    lib = MediaLibrary(tmp_path / "data" / "media", fetcher=fetch)
    app = create_app(tmp_path / "data", media=lib, audio=FakeAudio(), driver_factory=lambda s: FakeDriver(s),
                     web_dist=tmp_path / "dist")
    with TestClient(app) as client:
        client.lib = lib
        client.fetch = fetch
        client.tmp = tmp_path
        yield client


def wait(rig, mid, timeout=30.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        m = rig.get(f"/api/media/{mid}").json()
        if m["status"] != "processing":
            return m
        time.sleep(0.05)
    raise AssertionError("still processing")


def upload(rig, path, name="clip") -> dict:
    with open(path, "rb") as f:
        r = rig.post("/api/media", files={"file": (path.name, f, "video/mp4")}, data={"name": name})
    assert r.status_code == 200, r.text
    return wait(rig, r.json()["id"])


def frame_of(rig, set_id: str, t: float = 0.1) -> np.ndarray:
    r = rig.get(f"/api/render.png?set={set_id}&t={t}")
    assert r.status_code == 200
    return np.asarray(Image.open(io.BytesIO(r.content)).convert("RGB"))


def test_import_route(rig):
    r = rig.post("/api/media/import", json={"url": "https://video.example/v", "max_height": 720, "start": 1.5,
                                            "duration": 4})
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["status"] == "processing" and m["stage"] == "downloading" and m["source_url"] == "https://video.example/v"
    done = wait(rig, m["id"])
    assert done["status"] == "ready" and done["name"] == "A video"
    assert rig.fetch.calls[-1] == {"url": "https://video.example/v", "max_height": 720, "start": 1.5, "duration": 4.0}
    for bad in ({}, {"url": ""}, {"url": "ftp://x/y"}, {"url": "https://x/y", "max_height": 10},
                {"url": "https://x/y", "duration": 0}, {"url": "https://x/y", "start": "soon"}):
        assert rig.post("/api/media/import", json=bad).status_code == 422, bad


def test_import_failure_route(rig):
    rig.fetch.fail = "The video is not available (Video unavailable)"
    m = rig.post("/api/media/import", json={"url": "https://video.example/gone"}).json()
    done = wait(rig, m["id"])
    assert done["status"] == "error" and done["error"] == "The video is not available (Video unavailable)"


def test_patch_edit_and_render(rig):
    m = upload(rig, make_quad_video(rig.tmp / "q.mp4", w=384, h=384, n=10))
    assert m["edit"] == {"rotate": 0, "crop": None, "start": 0.0, "end": None, "shape": "free"}
    assert (m["source_width"], m["source_height"], m["version"]) == (384, 384, 1)
    s = rig.post(f"/api/media/{m['id']}/make-set", json={"target": "face1"}).json()
    f = frame_of(rig, s["id"])
    box = f[config.FACE:2 * config.FACE]
    assert near(colour_at(box, 0.3, 0.3), RED) and near(colour_at(box, 0.7, 0.7), WHITE)

    # one light's worth from the bottom-right quadrant, turned a quarter clockwise
    edit = {"rotate": 90, "crop": [0.5, 0.5, 0.5, 0.5], "start": 0, "end": None, "shape": "light"}
    r = rig.patch(f"/api/media/{m['id']}", json={"edit": edit, "name": "Quad"})
    assert r.status_code == 200 and r.json()["status"] == "processing" and r.json()["name"] == "Quad"
    done = wait(rig, m["id"])
    assert done["status"] == "ready" and done["edit"] == edit and (done["width"], done["height"]) == (192, 192)
    assert done["version"] == 2
    # turned clockwise, the original's top-right (green) quadrant is the bottom-right one
    f = frame_of(rig, s["id"])
    assert near(colour_at(f[config.FACE:2 * config.FACE], 0.5, 0.5), GREEN)

    for bad in ({"edit": {"crop": [0.5, 0.5, 0.6, 0.5]}}, {"edit": {"rotate": 45}},
                {"edit": {"start": 2, "end": 1}}, {"edit": {"start": 999}}, {}, {"name": ""}):
        assert rig.patch(f"/api/media/{m['id']}", json=bad).status_code == 422, bad
    assert rig.patch("/api/media/nope", json={"edit": {}}).status_code == 404


def test_duplicate_route(rig):
    m = upload(rig, make_quad_video(rig.tmp / "q.mp4", n=10), "Quad")
    r = rig.post(f"/api/media/{m['id']}/duplicate", json={"name": "Quad, top"})
    assert r.status_code == 200
    d = r.json()
    assert d["id"] != m["id"] and d["name"] == "Quad, top" and d["status"] == "ready"
    assert rig.post(f"/api/media/{m['id']}/duplicate").json()["name"] == "Quad copy"
    assert rig.post("/api/media/nope/duplicate").status_code == 404
    assert len(rig.get("/api/media").json()) == 3
    r = rig.patch(f"/api/media/{d['id']}", json={"edit": {"crop": [0, 0, 1, 0.5]}})
    assert wait(rig, d["id"])["height"] == 90 and rig.get(f"/api/media/{m['id']}").json()["height"] == 180


def test_source_route(rig):
    m = upload(rig, make_video(rig.tmp / "v.mp4", n=10))
    r = rig.get(f"/api/media/{m['id']}/source")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    r = rig.get(f"/api/media/{m['id']}/source", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and len(r.content) == 10
    old = upload(rig, make_video(rig.tmp / "old.mkv", n=10, codec="mpeg4"))
    r = rig.get(f"/api/media/{old['id']}/source")
    assert r.status_code == 200 and r.content[4:8] == b"ftyp"            # source.mp4, made at ingest
    assert rig.get("/api/media/nope/source").status_code == 404


def test_show_while_a_new_crop_is_made(rig, monkeypatch):
    m = upload(rig, make_quad_video(rig.tmp / "q.mp4", n=10))
    gate = []
    real = library_mod.ingest.ingest_video

    def slow(*a, **k):
        while not gate:
            time.sleep(0.01)
        return real(*a, **k)

    monkeypatch.setattr(library_mod.ingest, "ingest_video", slow)
    rig.patch(f"/api/media/{m['id']}", json={"edit": {"rotate": 180}})
    assert rig.get(f"/api/media/{m['id']}").json()["status"] == "processing"
    # the previous result is still there to play
    assert rig.post(f"/api/media/{m['id']}/show", json={"target": "all"}).json() == {"ok": True}
    assert rig.get(f"/api/media/{m['id']}/thumb.jpg").status_code == 200
    gate.append(1)
    assert wait(rig, m["id"])["version"] == 2


def test_editor_socket_can_open_not_live(rig):
    with rig.websocket_connect("/ws/editor?live=0") as ws:
        while True:
            msg = ws.receive()
            if msg.get("text"):
                st = json.loads(msg["text"])
                if st.get("type") == "status":
                    break
        assert st["live"] is False
        assert rig.get("/api/state").json()["now"]["source"] != "editor"
    with rig.websocket_connect("/ws/editor") as ws:
        while True:
            msg = ws.receive()
            if msg.get("text"):
                st = json.loads(msg["text"])
                if st.get("type") == "status":
                    break
        assert st["live"] is True
