"""Bundles: sets and media moved from one TrafficLight to another through the HTTP API (two instances,
real media libraries, generated media)."""
from __future__ import annotations

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from test_media import make_audio, make_video
from test_player_fakes import FakeAudio, FakeDriver
from test_server_media import wait
from trafficlight.bundle import MANIFEST, media_used
from trafficlight.media import MediaLibrary
from trafficlight.media import library as library_mod
from trafficlight.model import Clip, Set, Track
from trafficlight.server import create_app


def instance(path):
    lib = MediaLibrary(path / "media")
    app = create_app(path, media=lib, audio=FakeAudio(), driver_factory=lambda s: FakeDriver(s),
                     web_dist=path / "dist")
    return TestClient(app), lib


@pytest.fixture
def two(tmp_path, monkeypatch):
    monkeypatch.setattr(library_mod, "_SWEEP_SECONDS", 0.1)
    a, lib_a = instance(tmp_path / "laptop")
    b, lib_b = instance(tmp_path / "sbc")
    with a, b:
        a.lib, b.lib = lib_a, lib_b
        yield a, b


def add_media(client, path, name) -> dict:
    with open(path, "rb") as f:
        r = client.post("/api/media", files={"file": (path.name, f, "application/octet-stream")}, data={"name": name})
    assert r.status_code == 200, r.text
    m = wait(client, r.json()["id"], 60)
    assert m["status"] == "ready", m
    return m


def add_set(client, name, media_ids=(), **extra) -> dict:
    tracks = [{"name": "Whole sign", "target": "all", "clips": [
        {"type": "video", "start": i * 2.0, "duration": 2.0, "params": {"media": mid}} for i, mid in enumerate(media_ids)]}]
    r = client.post("/api/sets", json={"name": name, "duration": 10, "tracks": tracks, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def download(client, **query) -> bytes:
    r = client.get("/api/bundle", params=query)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"]
    return r.content


def upload(client, data: bytes, replace: bool = True) -> dict:
    r = client.post("/api/bundle", files={"file": ("bundle.zip", data, "application/zip")},
                    data={"replace": "true" if replace else "false"})
    assert r.status_code == 200, r.text
    return r.json()


def names(z: bytes) -> set[str]:
    return set(zipfile.ZipFile(io.BytesIO(z)).namelist())


def test_media_used_finds_ids_anywhere_in_params():
    s = Set(tracks=[Track(target="all", clips=[Clip(type="video", params={"media": "m1", "x": ["m2", {"y": "m1"}]}),
                                               Clip(type="text", params={"text": "m3"})])])
    assert media_used([s], {"m1", "m2", "m9"}) == ["m1", "m2"]


def test_one_set_moves_with_its_media_ready_at_once(two, tmp_path):
    a, b = two
    used = add_media(a, make_video(tmp_path / "v.mp4"), "Used")
    unused = add_media(a, make_audio(tmp_path / "s.wav"), "Unused")
    s = add_set(a, "Sunrise ✨", [used["id"]])
    add_set(a, "Other")

    r = a.get("/api/bundle", params={"set": s["id"]})
    assert 'filename="trafficlight-Sunrise-' in r.headers["content-disposition"]
    z = download(a, set=s["id"])
    n = names(z)
    assert MANIFEST in n and f"sets/{s['id']}.json" in n and len([x for x in n if x.startswith("sets/")]) == 1
    assert f"media/{used['id']}/meta.json" in n and not any(unused["id"] in x for x in n)
    assert f"media/{used['id']}/v{used['version']}/proxy.mp4" in n

    out = upload(b, z)
    assert out["sets_added"] == ["Sunrise ✨"] and out["media_added"] == ["Used"] and not out["media_failed"]
    m = b.get(f"/api/media/{used['id']}").json()
    assert m["status"] == "ready" and m["version"] == used["version"]      # prepared files came along
    assert b.get(f"/api/sets/{s['id']}").json() == a.get(f"/api/sets/{s['id']}").json()
    assert b.get(f"/api/render.png?set={s['id']}&t=0.5").status_code == 200
    assert b.get(f"/api/media/{used['id']}/thumb.jpg").status_code == 200

    # The same bundle again: nothing changes.
    again = upload(b, z)
    assert again["sets_unchanged"] == ["Sunrise ✨"] and again["media_unchanged"] == ["Used"]
    assert len(b.get("/api/sets").json()) == 1 and len(b.get("/api/media").json()) == 1


def test_everything_carries_unused_media_order_and_show_settings(two, tmp_path):
    a, b = two
    v = add_media(a, make_video(tmp_path / "v.mp4"), "Clip")
    sound = add_media(a, make_audio(tmp_path / "s.wav"), "Spare sound")
    s1 = add_set(a, "First", [v["id"]])
    s2 = add_set(a, "Second")
    a.post("/api/sets/order", json={"ids": [s2["id"], s1["id"]]})
    a.patch("/api/settings", json={"schedule": {"default_set": s1["id"], "random_minutes": 3},
                                    "face_colors": ["#110000", "#001100", "#000011"],
                                    "output": {"brightness": 0.9}})
    local = add_set(b, "Only on the sign")

    sets_only = names(download(a, kind="all"))
    assert not any(sound["id"] in x for x in sets_only)
    z = download(a, kind="everything")
    assert any(sound["id"] in x for x in names(z))

    out = upload(b, z)
    assert sorted(out["sets_added"]) == ["First", "Second"] and sorted(out["media_added"]) == ["Clip", "Spare sound"]
    assert out["settings"] is True
    assert [s["id"] for s in b.get("/api/sets").json()] == [s2["id"], s1["id"], local["id"]]
    st = b.get("/api/settings").json()
    assert st["schedule"]["default_set"] == s1["id"] and st["schedule"]["random_minutes"] == 3
    assert st["face_colors"] == ["#110000", "#001100", "#000011"]
    assert st["output"]["brightness"] == 0.5          # this computer's own settings stay


def test_replace_or_keep_both(two, tmp_path):
    a, b = two
    v = add_media(a, make_video(tmp_path / "v.mp4"), "Clip")
    s = add_set(a, "Show", [v["id"]])
    upload(b, download(a, set=s["id"]))

    full = a.get(f"/api/sets/{s['id']}").json()
    a.put(f"/api/sets/{s['id']}", json={**full, "bpm": 99})
    a.patch(f"/api/media/{v['id']}", json={"name": "Clip, cropped", "edit": {"crop": [0, 0, 0.5, 1]}})
    wait(a, v["id"], 60)
    z = download(a, set=s["id"])

    kept = upload(b, z, replace=False)
    assert kept["sets_copied"] == ["Show (imported)"] and kept["media_unchanged"] == ["Clip"]
    sets = b.get("/api/sets").json()
    assert [x["bpm"] for x in sets] == [120, 99] and sets[1]["id"] != s["id"]

    out = upload(b, z)
    assert out["sets_replaced"] == ["Show"] and out["media_updated"] == ["Clip, cropped"]
    assert b.get(f"/api/sets/{s['id']}").json()["bpm"] == 99
    m = wait(b, v["id"], 60)
    assert m["status"] == "ready" and m["edit"]["crop"] == [0, 0, 0.5, 1] and m["name"] == "Clip, cropped"


def test_bad_uploads_and_requests(two, tmp_path):
    a, b = two
    assert a.get("/api/bundle").status_code == 422                      # kind=sets needs a set
    assert a.get("/api/bundle", params={"kind": "nope"}).status_code == 422
    assert a.get("/api/bundle", params={"set": "missing"}).status_code == 404
    r = b.post("/api/bundle", files={"file": ("x.zip", b"not a zip", "application/zip")})
    assert r.status_code == 415
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "hi")
    assert b.post("/api/bundle", files={"file": ("x.zip", buf.getvalue(), "application/zip")}).status_code == 415
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(MANIFEST, json.dumps({"format": 99}))
    r = b.post("/api/bundle", files={"file": ("x.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 415 and "newer" in r.json()["detail"]


def test_files_outside_an_item_are_refused(two, tmp_path):
    a, b = two
    v = add_media(a, make_video(tmp_path / "v.mp4"), "Clip")
    src = zipfile.ZipFile(io.BytesIO(download(a, set=add_set(a, "S", [v["id"]])["id"])))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for info in src.infolist():
            z.writestr(info.filename, src.read(info))
        z.writestr(f"media/{v['id']}/../../../escaped.txt", "x")
    out = upload(b, buf.getvalue())
    assert out["media_failed"] and not out["media_added"]
    assert not list((tmp_path / "sbc").rglob("escaped.txt")) and not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path / "sbc" / "media" / v["id"]).exists()
    assert out["sets_added"] == ["S"]                     # the set comes anyway; its clip shows nothing
