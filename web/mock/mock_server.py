"""DEVELOPMENT MOCK of the TrafficLight API (docs/ARCHITECTURE.md), NOT the real server.

Serves web/dist plus in-memory plausible data and a generated frame stream so
the UI can be exercised without the Python server:

    ~/.venvs/trafficlight/bin/python web/mock/mock_server.py --port 8091
"""
from __future__ import annotations

import argparse, asyncio, io, math, time, uuid
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI, File, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response
from PIL import Image

DIST = Path(__file__).resolve().parent.parent / "dist"
app = FastAPI()
T0 = time.time()
rev = {"sets": 1, "media": 1}


def sset(name, bpm=120, dur=30, rot=True):
    return {"id": uuid.uuid4().hex[:12], "name": name, "bpm": bpm, "beat_offset": 0, "duration": dur, "in_rotation": rot, "tracks": []}


sets = [sset("Morning"), sset("Disco", 128, 60), sset("Quiet hours", 90, 120, False)]
settings = {
    "output": {"driver": "colorlight", "interface": "eth0", "brightness": 0.5, "gamma": 1.0, "rotate180": False,
               "panel_order": [0, 1, 2, 3, 4, 5], "panel_flip": [False, False, True, False, False, False], "color_order": "rgb"},
    "audio": {"device": None, "volume": 0.8},
    "schedule": {"default_set": sets[0]["id"], "random_minutes": 6, "random_enabled": True},
    "face_colors": ["#ff0000", "#ffb000", "#00ff40"], "off_style": "ring", "fps": 60,
}
media = [
    {"id": "m1", "name": "fire.mp4", "kind": "video", "status": "ready", "error": None, "duration": 12.5, "width": 640, "height": 360, "fps": 30, "has_audio": True, "size": 4_200_000, "added": 0},
    {"id": "m2", "name": "logo.png", "kind": "image", "status": "ready", "error": None, "duration": 0, "width": 512, "height": 512, "fps": 0, "has_audio": False, "size": 80_000, "added": 0},
    {"id": "m3", "name": "track.mp3", "kind": "audio", "status": "ready", "error": None, "duration": 190, "width": 0, "height": 0, "fps": 0, "has_audio": True, "size": 5_000_000, "added": 0},
    {"id": "m4", "name": "broken.avi", "kind": "video", "status": "error", "error": "Cannot decode", "duration": 0, "width": 0, "height": 0, "fps": 0, "has_audio": False, "size": 1000, "added": 0},
]
playing = {"paused": False, "set": 0, "t0": time.time()}
audio_error = {"on": True}


def frame(t: float, set_idx: int = 0) -> bytes:
    y, x = np.mgrid[0:384, 0:128]
    img = np.zeros((384, 128, 3), np.uint8)
    cols = [(255, 30, 30), (255, 176, 0), (30, 255, 64)]
    for i, c in enumerate(cols):
        d = np.hypot(x - 63.5, y - (i * 128 + 63.5))
        on = int(t * 1.5 + set_idx) % 3 == i
        img[d < 58] = np.array(c) * (1 if on else 0.12)
        img[(d >= 58) & (d < 61)] = (60, 60, 60)
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, "PNG")
    return buf.getvalue()


def state():
    s = sets[playing["set"] % len(sets)] if sets else None
    t = (time.time() - playing["t0"]) % (s["duration"] if s else 1)
    return {"now": {"source": "schedule" if s else "idle", "set_id": s and s["id"], "set_name": s and s["name"], "t": t,
                    "duration": s["duration"] if s else 0, "paused": playing["paused"]},
            "schedule": {**settings["schedule"], "random_in": max(0, 360 - (time.time() - T0) % 360)},
            "output": {"driver": "colorlight", "ok": False, "error": "Npcap not installed (mock)", "interface": "eth0", "fps": 59.9},
            "audio": {"ok": not audio_error["on"], "error": "No audio device (mock)" if audio_error["on"] else None, "device": None, "device_name": "Speakers",
                      "volume": settings["audio"]["volume"], "voices": 0},
            "sets_rev": rev["sets"], "media_rev": rev["media"], "version": "0.1.0-mock"}


def err(code, msg):
    return JSONResponse({"detail": msg}, status_code=code)


def merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            merge(a[k], v)
        else:
            a[k] = v


@app.get("/api/state")
def get_state(): return state()

@app.get("/api/catalog")
def catalog(): return {"clip_types": []}

@app.get("/api/sets")
def list_sets(): return [{k: s[k] for k in ("id", "name", "bpm", "duration", "in_rotation")} for s in sets]

@app.post("/api/sets")
async def create_set(r: Request):
    b = await r.json()
    s = sset(b.get("name", "Untitled"), b.get("bpm", 120), b.get("duration", 30))
    sets.append(s); rev["sets"] += 1
    return s

@app.post("/api/sets/order")
async def order(r: Request):
    ids = (await r.json())["ids"]
    sets.sort(key=lambda s: ids.index(s["id"])); rev["sets"] += 1
    return {"ok": True}

def find(i): return next((s for s in sets if s["id"] == i), None)

@app.get("/api/sets/{i}")
def get_set(i: str): return find(i) or err(404, "No such set")

@app.put("/api/sets/{i}")
async def put_set(i: str, r: Request):
    s = find(i); s.update(await r.json()); rev["sets"] += 1
    return s

@app.delete("/api/sets/{i}")
def del_set(i: str):
    sets.remove(find(i)); rev["sets"] += 1
    return {"ok": True}

@app.post("/api/sets/{i}/duplicate")
def dup(i: str):
    s = {**find(i), "id": uuid.uuid4().hex[:12], "name": find(i)["name"] + " copy"}
    sets.append(s); rev["sets"] += 1
    return s

@app.post("/api/sets/{i}/show")
def show(i: str):
    playing.update(set=sets.index(find(i)), t0=time.time())
    return {"ok": True}

@app.post("/api/transport/{what}")
def transport(what: str):
    if what == "pause": playing["paused"] = True
    elif what == "play": playing["paused"] = False
    else: playing.update(set=playing["set"] + 1, t0=time.time())
    return {"ok": True}

@app.get("/api/settings")
def get_settings(): return settings

@app.patch("/api/settings")
async def patch_settings(r: Request):
    merge(settings, await r.json())
    return settings

@app.get("/api/audio/devices")
def devices(): return [{"id": "d1", "name": "Speakers (Realtek)", "default": True, "channels": 2}, {"id": "d2", "name": "USB DAC", "default": False, "channels": 2}]

@app.get("/api/output/interfaces")
def ifaces(): return [{"id": "eth0", "name": "Ethernet", "description": "Intel I219", "mac": "aa:bb", "up": True}, {"id": "wlan0", "name": "Wi-Fi", "description": "", "mac": "cc:dd", "up": False}]

@app.post("/api/output/test")
async def test(r: Request): return {"ok": True}

@app.get("/api/media")
def list_media(): return media

@app.post("/api/media")
async def upload(file: UploadFile = File(...)):
    await file.read()
    await asyncio.sleep(1)
    m = {**media[1], "id": uuid.uuid4().hex[:6], "name": file.filename, "status": "processing"}
    media.insert(0, m); rev["media"] += 1
    async def done():
        await asyncio.sleep(4); m["status"] = "ready"; rev["media"] += 1
    asyncio.create_task(done())
    return m

def findm(i): return next((m for m in media if m["id"] == i), None)

@app.get("/api/media/{i}")
def get_media(i: str): return findm(i) or err(404, "No such item")

@app.patch("/api/media/{i}")
async def ren(i: str, r: Request):
    m = findm(i); m["name"] = (await r.json())["name"]; rev["media"] += 1
    return m

@app.delete("/api/media/{i}")
def del_media(i: str, force: int = 0):
    if i == "m1" and not force: return err(409, "Used by sets: Morning, Disco")
    media.remove(findm(i)); rev["media"] += 1
    return {"ok": True}

@app.get("/api/media/{i}/thumb.jpg")
def thumb(i: str):
    buf = io.BytesIO()
    Image.new("RGB", (160, 100), (60 + hash(i) % 150, 80, 120)).save(buf, "JPEG")
    return Response(buf.getvalue(), media_type="image/jpeg")

@app.post("/api/media/{i}/show")
async def mshow(i: str): return {"ok": True}

@app.post("/api/media/{i}/make-set")
async def mmake(i: str):
    s = sset("From " + findm(i)["name"]); sets.append(s); rev["sets"] += 1
    return s

@app.get("/api/render.png")
def render(set: str = "", t: float = 0):
    return Response(frame(0, sets.index(find(set)) if find(set) else 0), media_type="image/png")


@app.websocket("/ws/sign")
async def ws_sign(ws: WebSocket):
    await ws.accept()
    n = 0
    try:
        while True:
            if n % 5 == 0:
                await ws.send_json({"type": "state", **state()})
            await ws.send_bytes(frame(time.time() - T0, playing["set"]))
            n += 1
            await asyncio.sleep(0.05)
    except Exception:
        pass


@app.get("/__frame")
def frame_page(w: int = 390, h: int = 1700, src: str = "/"):
    """Headless Edge cannot go below ~500 px wide: wrap the app in an iframe of the phone width."""
    return Response(f'<body style="margin:0;background:#333"><iframe src="{src}" style="width:{w}px;height:{h}px;border:0;background:#000"></iframe>', media_type="text/html")


@app.get("/{path:path}")
def spa(path: str):
    f = DIST / path
    return FileResponse(f if path and f.is_file() else DIST / "index.html")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=8091)
    uvicorn.run(app, host="127.0.0.1", port=ap.parse_args().port, log_level="warning")
