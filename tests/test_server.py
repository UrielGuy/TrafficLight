import io
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from test_player_fakes import FakeAudio, FakeDriver, FakeMedia
from trafficlight import config
from trafficlight.model import MediaItem
from trafficlight.server import create_app

F = config.FACE


@pytest.fixture
def rig(tmp_path):
    media = FakeMedia(tmp_path / "data" / "media")
    audio = FakeAudio()
    app = create_app(tmp_path / "data", media=media, audio=audio, driver_factory=lambda s: FakeDriver(s),
                     web_dist=tmp_path / "dist")
    with TestClient(app) as client:
        client.media = media
        client.audio = audio
        client.app_ = app
        client.dist = tmp_path / "dist"
        yield client


def solid_clip(color="#ff0000", start=0, duration=5, **kw):
    return {"type": "test:solid", "start": start, "duration": duration, "params": {"color": color}, **kw}


def wait_for(fn, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_state_and_catalog(rig):
    st = rig.get("/api/state").json()
    assert set(st) >= {"now", "schedule", "output", "audio", "sets_rev", "media_rev", "version"}
    assert set(st["now"]) == {"source", "set_id", "set_name", "t", "duration", "paused"}
    assert set(st["schedule"]) == {"default_set", "random_minutes", "random_enabled", "random_in"}
    assert {"driver", "ok", "error", "fps"} <= set(st["output"])
    assert {"ok", "error", "device", "device_name", "volume", "voices"} <= set(st["audio"])
    types = {c["type"]: c for c in rig.get("/api/catalog").json()["clip_types"]}
    assert types["test:solid"]["params"][0]["kind"] == "color"


def test_sets_crud(rig):
    r = rig.post("/api/sets", json={"name": "Morning"})
    assert r.status_code == 200
    s = r.json()
    assert len(s["tracks"]) == 5 and s["id"]
    rev = rig.get("/api/state").json()["sets_rev"]
    s["tracks"][1]["clips"].append(solid_clip())
    s["name"] = "Morning 2"
    r = rig.put(f"/api/sets/{s['id']}", json=s)
    assert r.json()["name"] == "Morning 2"
    assert rig.get(f"/api/sets/{s['id']}").json()["tracks"][1]["clips"][0]["type"] == "test:solid"
    assert rig.get("/api/state").json()["sets_rev"] > rev
    d = rig.post(f"/api/sets/{s['id']}/duplicate").json()
    assert d["name"] == "Morning 2 copy"
    assert [x["id"] for x in rig.get("/api/sets").json()] == [s["id"], d["id"]]
    assert set(rig.get("/api/sets").json()[0]) == {"id", "name", "bpm", "duration", "in_rotation"}
    assert rig.post("/api/sets/order", json={"ids": [d["id"]]}).json() == {"ok": True}
    assert [x["id"] for x in rig.get("/api/sets").json()] == [d["id"], s["id"]]
    assert rig.delete(f"/api/sets/{d['id']}").json() == {"ok": True}
    assert rig.get(f"/api/sets/{d['id']}").status_code == 404
    assert rig.delete(f"/api/sets/{d['id']}").status_code == 404


def test_set_errors(rig):
    r = rig.post("/api/sets", json={"name": "x", "duration": "long"})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)
    assert rig.post("/api/sets", json={"id": "x y"}).status_code == 400
    rig.post("/api/sets", json={"id": "dup"})
    assert rig.post("/api/sets", json={"id": "dup"}).status_code == 409
    assert rig.put("/api/sets/bad%20id", json={}).status_code == 400
    assert rig.post("/api/sets/nope/show").status_code == 404
    assert rig.post("/api/sets/order", json={"ids": "x"}).status_code == 422
    r = rig.post("/api/sets", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 422 and isinstance(r.json()["detail"], str)


def test_validate(rig):
    s = {"name": "v", "duration": 10, "tracks": [
        {"target": "face0", "clips": [solid_clip(), {"type": "no:such", "start": 1, "duration": 1}]}]}
    probs = rig.post("/api/validate", json=s).json()["problems"]
    assert any("Unknown clip type" in p["message"] for p in probs)
    assert any("Overlaps" in p["message"] for p in probs)


def test_show_transport_and_state(rig):
    s = rig.post("/api/sets", json={"name": "A", "duration": 20,
                                    "tracks": [{"target": "face0", "clips": [solid_clip(duration=20)]}]}).json()
    assert rig.post(f"/api/sets/{s['id']}/show").json() == {"ok": True}
    wait_for(lambda: rig.get("/api/state").json()["now"]["source"] == "show")
    assert rig.post("/api/transport/pause").json() == {"ok": True}
    assert wait_for(lambda: rig.get("/api/state").json()["now"]["paused"])
    t1 = rig.get("/api/state").json()["now"]["t"]
    time.sleep(0.1)
    assert rig.get("/api/state").json()["now"]["t"] == t1
    rig.post("/api/transport/play")
    rig.post("/api/transport/next")
    wait_for(lambda: rig.get("/api/state").json()["now"]["source"] == "schedule")


def test_settings(rig):
    st = rig.get("/api/settings").json()
    assert st["fps"] == 60
    out = rig.patch("/api/settings", json={"audio": {"volume": 0.25}}).json()
    assert out["audio"]["volume"] == 0.25 and out["audio"]["device"] is None
    wait_for(lambda: rig.audio.volume == 0.25)
    r = rig.patch("/api/settings", json={"off_style": "plaid"})
    assert r.status_code == 422 and "off_style" in r.json()["detail"]
    assert rig.patch("/api/settings", json={"schedule": {"default_set": "nope"}}).status_code == 400
    s = rig.post("/api/sets", json={"name": "A"}).json()
    assert rig.patch("/api/settings", json={"schedule": {"default_set": s["id"]}}).json()["schedule"]["default_set"] == s["id"]
    rig.delete(f"/api/sets/{s['id']}")
    assert rig.get("/api/settings").json()["schedule"]["default_set"] is None


def test_devices_and_test_pattern(rig):
    assert rig.get("/api/audio/devices").json() == [{"id": "dev1", "name": "Speakers", "default": True, "channels": 2}]
    assert rig.get("/api/output/interfaces").status_code == 404
    assert rig.post("/api/output/test", json={"pattern": "red"}).json() == {"ok": True}
    wait_for(lambda: rig.get("/api/state").json()["now"]["source"] == "test")
    assert rig.post("/api/output/test", json={"pattern": "plaid"}).status_code == 400
    rig.post("/api/output/test", json={"pattern": "off"})
    wait_for(lambda: rig.get("/api/state").json()["now"]["source"] == "idle")
    assert rig.post("/api/output/test", json={"pattern": "wiring"}).json() == {"ok": True}
    wait_for(lambda: rig.get("/api/state").json()["now"]["set_name"] == "wiring")
    rig.post("/api/output/test", json={"pattern": "off"})
    wait_for(lambda: rig.get("/api/state").json()["now"]["source"] == "idle")


def test_settings_refuse_impossible_wiring(rig):
    ok = rig.patch("/api/settings", json={"output": {"panel_order": [7, 2, 0, 5, 1, 4], "panel_flip": [True] * 6}})
    assert ok.status_code == 200 and ok.json()["output"]["panel_order"] == [7, 2, 0, 5, 1, 4]
    for body, words in [({"panel_order": [0, 2, 1, 3, 2, 5]}, "panels 2 and 5 are on the same connector J3"),
                        ({"panel_order": [0, 1, 2, 3, 4, config.PORTS]}, f"connectors J1..J{config.PORTS}"),
                        ({"panel_order": [0, 1, 2]}, "needs 6 entries"),
                        ({"panel_flip": [True]}, "needs 6 entries")]:
        r = rig.patch("/api/settings", json={"output": body})
        assert r.status_code == 422, body
        detail = r.json()["detail"]
        assert words in detail and detail.startswith("output.panel_") and "Value error" not in detail
    assert rig.get("/api/settings").json()["output"]["panel_order"] == [7, 2, 0, 5, 1, 4]


def test_media_routes(rig):
    r = rig.post("/api/media", files={"file": ("clip.mp4", b"video bytes", "video/mp4")}, data={"name": "My clip"})
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["name"] == "My clip" and item["kind"] == "video"
    assert rig.post("/api/media", files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 415
    assert not list((rig.app_.state.store.root / "tmp").iterdir())     # temp uploads cleaned up
    assert [m["id"] for m in rig.get("/api/media").json()] == [item["id"]]
    mid = item["id"]
    assert rig.get(f"/api/media/{mid}").json()["name"] == "My clip"
    assert rig.patch(f"/api/media/{mid}", json={"name": "Renamed"}).json()["name"] == "Renamed"
    assert rig.patch("/api/media/nope", json={"name": "x"}).status_code == 404
    assert rig.get(f"/api/media/{mid}/thumb.jpg").headers["content-type"] == "image/jpeg"
    r = rig.get(f"/api/media/{mid}/file", headers={"Range": "bytes=0-4"})
    assert r.status_code == 206 and r.content == b"video"
    assert rig.get(f"/api/media/{mid}/peaks").json()["rate"] == 10.0
    assert rig.get("/api/media/nope").status_code == 404

    # make-set: one clip per light for "faces", duration = the item's
    s = rig.post(f"/api/media/{mid}/make-set", json={"target": "faces", "loop": True}).json()
    clips = [(t["target"], c) for t in s["tracks"] for c in t["clips"]]
    assert sorted(t for t, _ in clips) == ["face0", "face1", "face2"]
    assert all(c["type"] == "video" and c["params"] == {"media": mid, "loop": True} and c["duration"] == 5.0
               for _, c in clips)
    assert s["duration"] == 5.0 and s["name"] == "Renamed"
    assert rig.get(f"/api/sets/{s['id']}").status_code == 200
    assert rig.post(f"/api/media/{mid}/make-set", json={"target": "nowhere"}).status_code == 422

    # in use -> 409 with the set names, unless forced
    r = rig.delete(f"/api/media/{mid}")
    assert r.status_code == 409 and r.json()["sets"] == ["Renamed"] and "Renamed" in r.json()["detail"]
    assert rig.delete(f"/api/media/{mid}?force=1").json() == {"ok": True}
    assert rig.get(f"/api/media/{mid}").status_code == 404


def test_media_show_still_and_processing(rig):
    img = rig.media.put(MediaItem(id="img000000001", name="Pic", kind="image", status="ready"))
    assert rig.post(f"/api/media/{img.id}/show", json={"target": "face1"}).json() == {"ok": True}
    st = wait_for(lambda: (s := rig.get("/api/state").json()["now"])["source"] == "show" and s)
    assert st["set_id"] is None and st["set_name"] == "Pic" and st["duration"] == 10.0
    s = rig.post(f"/api/media/{img.id}/make-set").json()          # no body: defaults
    assert [t["target"] for t in s["tracks"] if t["clips"]] == ["all"]
    rig.media.put(MediaItem(id="busy00000001", name="Busy", kind="video", status="processing"))
    assert rig.post("/api/media/busy00000001/show", json={}).status_code == 409


def test_render_png(rig):
    s = rig.post("/api/sets", json={"name": "A", "tracks": [{"target": "face2", "clips": [solid_clip("#00ff00")]}]}).json()
    r = rig.get(f"/api/render.png?set={s['id']}&t=1")
    assert r.headers["content-type"] == "image/png"
    a = np.asarray(Image.open(io.BytesIO(r.content)))
    assert a.shape == (config.H, config.W, 3) and tuple(a[2 * F + 64, 64]) == (0, 255, 0)
    assert rig.get("/api/render.png?set=nope").status_code == 404


def test_ws_sign(rig):
    with rig.websocket_connect("/ws/sign?fps=30") as ws:
        got_state = got_frame = False
        for _ in range(20):
            msg = ws.receive()
            if msg.get("text"):
                import json
                d = json.loads(msg["text"])
                assert d["type"] == "state" and "now" in d
                got_state = True
            elif msg.get("bytes"):
                im = Image.open(io.BytesIO(msg["bytes"]))
                assert im.size == (config.W, config.H)
                got_frame = True
            if got_state and got_frame:
                break
        assert got_state and got_frame


def _next_json(ws, type_, n=50):
    import json
    for _ in range(n):
        msg = ws.receive()
        if msg.get("text"):
            d = json.loads(msg["text"])
            if d["type"] == type_:
                return d
    raise AssertionError(f"no {type_} message")


def _next_frame(ws, n=50):
    for _ in range(n):
        msg = ws.receive()
        if msg.get("bytes"):
            return np.asarray(Image.open(io.BytesIO(msg["bytes"])).convert("RGB"))
    raise AssertionError("no frame")


def test_ws_editor(rig):
    draft = {"name": "Draft", "duration": 8, "tracks": [
        {"id": "t0", "target": "face0", "clips": [solid_clip("#ff0000", 0, 8, id="c1")]},
        {"id": "t1", "target": "face1", "clips": [{"id": "c2", "type": "test:solid", "start": 0, "duration": 8,
                                                   "params": {"tag": "bad"}}]}]}
    with rig.websocket_connect("/ws/editor?fps=30") as ws:
        ws.send_json({"type": "set", "set": draft})
        st = _next_json(ws, "status")
        while st["duration"] != 8:
            st = _next_json(ws, "status")
        assert st["problems"] == [{"clip_id": "c2", "track_id": "t1", "message": "tag says bad"}]
        assert st["live"] is True and st["playing"] is False and st["t"] == 0
        wait_for(lambda: rig.get("/api/state").json()["now"]["source"] == "editor")
        ws.send_json({"type": "seek", "t": 2.5})
        assert _next_json(ws, "status")["t"] == 2.5
        ws.send_json({"type": "play"})
        assert _next_json(ws, "status")["playing"] is True
        ws.send_json({"type": "loop", "on": True, "from": 1, "to": 3})
        assert _next_json(ws, "status")["loop"] == {"on": True, "from": 1.0, "to": 3.0}
        ws.send_json({"type": "live", "on": False})
        assert _next_json(ws, "status")["live"] is False
        wait_for(lambda: rig.get("/api/state").json()["now"]["source"] == "idle")
        frame = _next_frame(ws)
        assert tuple(frame[64, 64]) == (255, 0, 0)
        ws.send_json({"type": "warp"})
        assert "Unknown" in _next_json(ws, "error")["message"]
        ws.send_json({"type": "set", "set": {"duration": "x"}})
        assert "Invalid set" in _next_json(ws, "error")["message"]
        # a second editor closes this one
        with rig.websocket_connect("/ws/editor") as ws2:
            closed = _next_json(ws, "closed")
            assert closed["reason"]
            assert _next_json(ws2, "status")["duration"] == 0
    wait_for(lambda: rig.app_.state.engine.editor is None)


def test_web_ui_fallbacks(rig):
    r = rig.get("/")
    assert r.status_code == 200 and "npm run build" in r.text
    rig.dist.mkdir()
    (rig.dist / "index.html").write_text("<html>app</html>")
    (rig.dist / "assets").mkdir()
    (rig.dist / "assets" / "x.js").write_text("console.log(1)")
    assert rig.get("/").text == "<html>app</html>"
    assert rig.get("/sets/abc").text == "<html>app</html>"
    assert rig.get("/assets/x.js").text == "console.log(1)"
    assert rig.get("/../pyproject.toml").text == "<html>app</html>"
    r = rig.get("/api/nothing")
    assert r.status_code == 404 and r.json() == {"detail": "Not found"}


def test_runs_without_media_library(tmp_path):
    app = create_app(tmp_path, media=None, audio=FakeAudio(), driver_factory=lambda s: FakeDriver(s),
                     web_dist=tmp_path / "dist")
    with TestClient(app) as c:
        assert c.get("/api/media").json() == []
        assert c.post("/api/media", files={"file": ("a.mp4", b"x")}).status_code == 503
        assert c.get("/api/state").json()["media_rev"] == 0
