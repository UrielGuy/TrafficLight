"""FastAPI app: the HTTP API and WebSockets of docs/ARCHITECTURE.md, and the web UI.

Request handlers that touch the store or the engine are plain `def`s (they run
in the thread pool); the engine never makes them wait for a frame. The
WebSocket loops send the newest frame at the socket's own rate: a slow client
only skips frames, a stuck one is dropped after SEND_TIMEOUT.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import urllib.parse
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketDisconnect, WebSocketState

from .. import __version__, bundle, config
from ..audio.base import AudioEngine, create_audio_engine
from ..engine import Engine, EditorSession, encode_png
from ..model import Clip, MediaEdit, MediaItem, Set, default_tracks
from ..output.base import create_driver, list_interfaces
from ..player import render_still, validate_set
from ..store import SetExists, Store, valid_id

log = logging.getLogger(__name__)

STILL_SECONDS = 10.0       # length of a set made from a still image
SEND_TIMEOUT = 10.0        # a socket that takes longer than this to accept a message is dead
STATE_EVERY = 0.25         # /ws/sign state messages
STATUS_EVERY = 0.1         # /ws/editor status messages
VALIDATE_CACHE_MAX = 4000
BUNDLE_SPARE_BYTES = 500_000_000   # an import never fills the disk to the last byte
_AUTO = object()

NO_UI_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>TrafficLight</title>
<style>body{font-family:system-ui,sans-serif;max-width:40em;margin:3em auto;line-height:1.5}
code{background:#eee;padding:.1em .3em}</style></head><body>
<h1>TrafficLight is running</h1>
<p>The web UI has not been built yet, so there is nothing to show here. Build it with
<code>cd web &amp;&amp; npm install &amp;&amp; npm run build</code> (creates <code>web/dist</code>)
and reload this page.</p>
<p>The API works already: <a href="/api/state">/api/state</a>,
<a href="/api/sets">/api/sets</a>, <a href="/api/catalog">/api/catalog</a>,
<a href="/docs">/docs</a>.</p></body></html>"""


def open_media_library(root: Path) -> Any:
    """The media library, or None if its module is missing or broken (the sign still runs)."""
    try:
        from ..media.library import MediaLibrary
    except Exception:
        log.exception("media library unavailable: running without media")
        return None
    try:
        return MediaLibrary(root)
    except Exception:
        log.exception("media library failed to open: running without media")
        return None


def _problem(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()) if x != "body")
        msg = str(err.get("msg"))
        if err.get("type") == "value_error":      # pydantic prefixes our own messages with "Value error, "
            msg = msg.removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "Invalid data"


def media_set(item: MediaItem, target: str, loop: bool) -> Set:
    """A set holding just this media item (POST /api/media/{id}/show and /make-set)."""
    duration = item.duration if item.duration > 0 else STILL_SECONDS
    tracks = default_tracks()
    by_target = {t.target: t for t in tracks}

    def clip(kind: str) -> Clip:
        return Clip(type=kind, start=0.0, duration=duration, params={"media": item.id, "loop": loop})

    if item.kind == "audio":
        by_target["audio"].clips.append(clip("audio"))
    elif target == "faces":
        for f in ("face0", "face1", "face2"):
            by_target[f].clips.append(clip(item.kind))
    else:
        by_target[target].clips.append(clip(item.kind))
    return Set(name=item.name, duration=duration, tracks=tracks)


def create_app(data_dir: Optional[Path] = None, *, store: Optional[Store] = None, media: Any = _AUTO,
               audio: Optional[AudioEngine] = None, driver_factory: Any = None,
               web_dist: Optional[Path] = None, run_engine: bool = True) -> FastAPI:
    """The app. Everything not given is made from `data_dir` (default config.data_dir()):
    tests pass fakes for media, audio and the output driver."""
    data_dir = Path(data_dir) if data_dir is not None else config.data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    store = store or Store(data_dir)
    if media is _AUTO:
        media = open_media_library(data_dir / "media")
    engine = Engine(store, media, audio or create_audio_engine(), driver_factory or create_driver)
    dist = Path(web_dist) if web_dist is not None else config.WEB_DIST

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_engine:
            engine.start()
        try:
            yield
        finally:
            await run_in_threadpool(engine.stop)
            if media is not None:
                try:
                    media.close()
                except Exception:
                    log.exception("closing the media library")

    app = FastAPI(title="TrafficLight", version=__version__, lifespan=lifespan)
    app.state.engine = engine
    app.state.store = store
    app.state.media = media

    @app.exception_handler(RequestValidationError)
    async def _bad_request(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=422, content={"detail": _problem(exc)})

    def need_media() -> Any:
        if media is None:
            raise HTTPException(503, "The media library is not available")
        return media

    def media_item(media_id: str) -> MediaItem:
        item = need_media().get(media_id)
        if item is None:
            raise HTTPException(404, "No such media item")
        return item

    def get_set_or_404(set_id: str) -> Set:
        s = store.get_set(set_id)
        if s is None:
            raise HTTPException(404, "No such set")
        return s

    # ------------------------------------------------------------ state, catalog
    @app.get("/api/state")
    def get_state() -> dict:
        return engine.state()

    @app.get("/api/catalog")
    def get_catalog() -> dict:
        from .. import render
        return {"clip_types": render.catalog()}

    # ------------------------------------------------------------ sets
    @app.get("/api/sets")
    def list_sets() -> list[dict]:
        return [s.model_dump() for s in store.summaries()]

    @app.post("/api/sets")
    def create_set(body: dict = Body(...)) -> dict:
        try:
            return store.create_set(body).model_dump()
        except ValidationError as e:
            raise HTTPException(422, _problem(e))
        except SetExists as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/sets/order")
    def order_sets(body: dict = Body(...)) -> dict:
        ids = body.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise HTTPException(422, "ids: a list of set ids")
        store.set_order(ids)
        return {"ok": True}

    @app.get("/api/sets/{set_id}")
    def get_set(set_id: str) -> dict:
        return get_set_or_404(set_id).model_dump()

    @app.put("/api/sets/{set_id}")
    def put_set(set_id: str, body: dict = Body(...)) -> dict:
        if not valid_id(set_id):
            raise HTTPException(400, "Invalid set id")
        try:
            s = Set.model_validate({**body, "id": set_id})
        except ValidationError as e:
            raise HTTPException(422, _problem(e))
        return store.save_set(s).model_dump()

    @app.delete("/api/sets/{set_id}")
    def delete_set(set_id: str) -> dict:
        try:
            store.delete_set(set_id)
        except KeyError:
            raise HTTPException(404, "No such set")
        return {"ok": True}

    @app.post("/api/sets/{set_id}/duplicate")
    def duplicate_set(set_id: str) -> dict:
        try:
            return store.duplicate_set(set_id).model_dump()
        except KeyError:
            raise HTTPException(404, "No such set")

    @app.post("/api/sets/{set_id}/show")
    def show_set(set_id: str) -> dict:
        if not engine.show_set(set_id):
            raise HTTPException(404, "No such set")
        return {"ok": True}

    @app.post("/api/validate")
    def validate(body: dict = Body(...)) -> dict:
        try:
            s = Set.model_validate(body)
        except ValidationError as e:
            raise HTTPException(422, _problem(e))
        return {"problems": validate_set(s, engine.env)}

    # ------------------------------------------------------------ bundles (trafficlight/bundle.py)
    @app.get("/api/bundle")
    def download_bundle(kind: str = Query("sets"), sets: list[str] = Query([], alias="set")):
        if kind not in ("sets", "all", "everything"):
            raise HTTPException(422, "kind: sets, all or everything")
        if kind == "sets" and not sets:
            raise HTTPException(422, "Name at least one set (?set=<id>)")
        try:
            plan = bundle.plan_export(store, media, kind, sets)
        except KeyError:
            raise HTTPException(404, "No such set")
        quoted = urllib.parse.quote(plan.filename)
        return StreamingResponse(bundle.stream(plan), media_type="application/zip", headers={
            "Content-Disposition": f"attachment; filename=\"{plan.filename}\"; filename*=UTF-8''{quoted}"})

    @app.post("/api/bundle")
    def upload_bundle(file: UploadFile = File(...), replace: bool = Form(True)) -> dict:
        tmp_dir = data_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tmp_dir / f"{uuid.uuid4().hex}.zip"
        try:
            with open(tmp, "wb") as f:
                shutil.copyfileobj(file.file, f, 1 << 20)
            try:
                need = bundle.needed_bytes(tmp)
            except (zipfile.BadZipFile, OSError):
                raise HTTPException(415, "This is not a zip file")
            free = shutil.disk_usage(data_dir).free
            if need > free - BUNDLE_SPARE_BYTES:
                raise HTTPException(507, f"Not enough disk space: the bundle needs {need / 1e9:.1f} GB, "
                                         f"{max(0, free - BUNDLE_SPARE_BYTES) / 1e9:.1f} GB are free")
            try:
                return bundle.import_bundle(store, media, tmp, replace).to_json()
            except bundle.BundleError as e:
                raise HTTPException(415, str(e))
        finally:
            tmp.unlink(missing_ok=True)

    # ------------------------------------------------------------ transport
    @app.post("/api/transport/pause")
    def pause() -> dict:
        engine.pause()
        return {"ok": True}

    @app.post("/api/transport/play")
    def play() -> dict:
        engine.play()
        return {"ok": True}

    @app.post("/api/transport/next")
    def next_set() -> dict:
        engine.next()
        return {"ok": True}

    # ------------------------------------------------------------ settings, devices, output
    @app.get("/api/settings")
    def get_settings() -> dict:
        return store.settings().model_dump()

    @app.patch("/api/settings")
    def patch_settings(body: dict = Body(...)) -> dict:
        sch = body.get("schedule")
        if isinstance(sch, dict) and sch.get("default_set") is not None and not store.has_set(sch["default_set"]):
            raise HTTPException(400, "default_set: no such set")
        try:
            return store.update_settings(body).model_dump()
        except ValidationError as e:
            raise HTTPException(422, _problem(e))

    @app.get("/api/audio/devices")
    def audio_devices() -> list[dict]:
        try:
            return [d.to_json() for d in engine.audio.list_devices()]
        except Exception:
            log.exception("listing audio devices")
            return []

    @app.get("/api/output/interfaces")
    def output_interfaces() -> list[dict]:
        return [i.to_json() for i in list_interfaces()]

    @app.post("/api/output/test")
    def output_test(body: dict = Body(...)) -> dict:
        try:
            engine.test_pattern(str(body.get("pattern", "")))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    # ------------------------------------------------------------ media
    @app.get("/api/media")
    def list_media() -> list[dict]:
        return [m.model_dump() for m in media.list()] if media is not None else []

    @app.post("/api/media")
    def upload_media(file: UploadFile = File(...), name: Optional[str] = Form(None)) -> dict:
        lib = need_media()
        filename = Path(file.filename or "upload")
        tmp_dir = data_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tmp_dir / f"{uuid.uuid4().hex}{filename.suffix.lower()[:12]}"
        try:
            with open(tmp, "wb") as f:
                shutil.copyfileobj(file.file, f, 1 << 20)
            item = lib.add(tmp, (name or "").strip() or filename.stem or "Untitled")
        except ValueError as e:
            raise HTTPException(415, str(e))
        finally:
            tmp.unlink(missing_ok=True)      # add() moved it unless it failed
        return item.model_dump()

    @app.get("/api/media/{media_id}")
    def get_media(media_id: str) -> dict:
        return media_item(media_id).model_dump()

    @app.post("/api/media/probe")
    def probe_media(body: dict = Body(...)) -> dict:
        # A plain def: FastAPI runs it on a worker thread, and asking the site takes seconds.
        lib = need_media()
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            raise HTTPException(422, "url: a web link")
        p = lib.probe(url.strip())
        return {"title": p.title, "duration": p.duration, "warn_over": config.IMPORT_WARN_SECONDS}

    @app.post("/api/media/import")
    def import_media(body: dict = Body(...)) -> dict:
        lib = need_media()
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            raise HTTPException(422, "url: a web link")
        try:
            max_height = int(body.get("max_height") or config.IMPORT_HEIGHT)
            start = float(body.get("start") or 0.0)
            duration = body.get("duration")
            duration = None if duration in (None, "") else float(duration)
        except (TypeError, ValueError):
            raise HTTPException(422, "max_height, start and duration are numbers")
        if not 144 <= max_height <= 4320 or start < 0 or (duration is not None and duration <= 0):
            raise HTTPException(422, "max_height: 144..4320, start >= 0, duration > 0")
        name = body.get("name") if isinstance(body.get("name"), str) else None
        try:
            return lib.import_url(url, max_height=max_height, start=start, duration=duration, name=name).model_dump()
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.patch("/api/media/{media_id}")
    def patch_media(media_id: str, body: dict = Body(...)) -> dict:
        name = body.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            raise HTTPException(422, "name: a non-empty string")
        edit = None
        if body.get("edit") is not None:
            try:
                edit = MediaEdit.model_validate(body["edit"])
            except ValidationError as e:
                raise HTTPException(422, "edit: " + _problem(e))
        if name is None and edit is None:
            raise HTTPException(422, "Nothing to change: give name and / or edit")
        try:
            return need_media().update(media_id, name=name, edit=edit).model_dump()
        except KeyError:
            raise HTTPException(404, "No such media item")
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.post("/api/media/{media_id}/duplicate")
    def duplicate_media(media_id: str, body: Optional[dict] = Body(None)) -> dict:
        name = (body or {}).get("name")
        try:
            return need_media().duplicate(media_id, name if isinstance(name, str) else None).model_dump()
        except KeyError:
            raise HTTPException(404, "No such media item")
        except ValueError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/media/{media_id}/source")
    def media_source(media_id: str):
        media_item(media_id)
        p = need_media().source_path(media_id)
        if p is None or not Path(p).is_file():
            raise HTTPException(404, "No picture to crop (yet)")
        return FileResponse(p, headers={"Cache-Control": "no-cache"})

    @app.delete("/api/media/{media_id}")
    def delete_media(media_id: str, force: bool = Query(False)):
        lib = need_media()
        media_item(media_id)
        users = [s.name for s in store.sets_using_media(media_id)]
        if users and not force:
            return JSONResponse(status_code=409, content={
                "detail": "Used by " + ", ".join(users), "sets": users})
        try:
            lib.delete(media_id)
        except KeyError:
            raise HTTPException(404, "No such media item")
        return {"ok": True}

    @app.get("/api/media/{media_id}/thumb.jpg")
    def media_thumb(media_id: str):
        p = need_media().thumb_path(media_id)
        if p is None or not Path(p).is_file():
            raise HTTPException(404, "No thumbnail")
        return FileResponse(p, media_type="image/jpeg")

    @app.get("/api/media/{media_id}/file")
    def media_file(media_id: str):
        p = need_media().file_path(media_id)
        if p is None or not Path(p).is_file():
            raise HTTPException(404, "No file")
        return FileResponse(p)

    @app.get("/api/media/{media_id}/peaks")
    def media_peaks(media_id: str) -> dict:
        peaks = need_media().peaks(media_id)
        if peaks is None:
            raise HTTPException(404, "No sound")
        return peaks

    def _media_body_set(media_id: str, body: Optional[dict]) -> Set:
        item = media_item(media_id)
        if item.status != "ready" and not (item.status == "processing" and item.version > 0):
            raise HTTPException(409, "The item is still processing" if item.status == "processing"
                                else f"The item could not be processed: {item.error or 'error'}")
        body = body or {}
        target = body.get("target", "all")
        if target not in ("face0", "face1", "face2", "all", "faces"):
            raise HTTPException(422, "target: face0, face1, face2, all or faces")
        return media_set(item, target, bool(body.get("loop", True)))

    @app.post("/api/media/{media_id}/show")
    def media_show(media_id: str, body: Optional[dict] = Body(None)) -> dict:
        engine.show_temp(_media_body_set(media_id, body))
        return {"ok": True}

    @app.post("/api/media/{media_id}/make-set")
    def media_make_set(media_id: str, body: Optional[dict] = Body(None)) -> dict:
        s = _media_body_set(media_id, body)
        return store.save_set(s).model_dump()

    # ------------------------------------------------------------ stills
    @app.get("/api/render.png")
    def render_png(set: str = Query(...), t: float = Query(0.0)):
        s = get_set_or_404(set)
        return Response(encode_png(render_still(s, t, engine.env)), media_type="image/png",
                        headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------ websockets
    @app.websocket("/ws/sign")
    async def ws_sign(ws: WebSocket):
        fps = _socket_fps(ws, 20)
        await ws.accept()
        send = _Sender(ws)

        async def produce():
            last_no, next_state = 0, 0.0
            pace = _Pace(fps)
            while True:
                t0 = time.perf_counter()
                if t0 >= next_state:
                    next_state = t0 + STATE_EVERY
                    await send.json({"type": "state", **(await run_in_threadpool(engine.state))})
                if engine.sign_frame.no != last_no:
                    no, data = await run_in_threadpool(engine.sign_frame.encoded)
                    if data is not None:
                        await send.bytes(data)
                        last_no = no
                await pace.wait()

        await _serve(ws, produce(), _drain(ws))

    @app.websocket("/ws/editor")
    async def ws_editor(ws: WebSocket):
        fps = _socket_fps(ws, 30)
        live = ws.query_params.get("live", "1").strip().lower() not in ("0", "false", "no", "off")
        await ws.accept()
        session = engine.open_editor(live=live)
        send = _Sender(ws)

        async def produce():
            last_no, next_status = 0, 0.0
            pace = _Pace(fps)
            while True:
                t0 = time.perf_counter()
                if session.closed_reason:
                    await send.json({"type": "closed", "reason": session.closed_reason})
                    return
                if t0 >= next_status:
                    next_status = t0 + STATUS_EVERY
                    await send.json(session.status())
                if session.frame.no != last_no:
                    no, data = await run_in_threadpool(session.frame.encoded)
                    if data is not None:
                        await send.bytes(data)
                        last_no = no
                await pace.wait()

        async def consume():
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                text = msg.get("text")
                if text is None:
                    continue
                error = await _editor_message(session, text)
                if error:
                    await send.json({"type": "error", "message": error})
                await send.json(session.status())

        try:
            await _serve(ws, produce(), consume())
        finally:
            engine.close_editor(session)

    async def _editor_message(session: EditorSession, text: str) -> Optional[str]:
        try:
            msg = json.loads(text)
            kind = msg.get("type")
            if kind == "set":
                s = Set.model_validate(msg.get("set"))
                if len(session.validate_cache) > VALIDATE_CACHE_MAX:
                    session.validate_cache.clear()
                problems = await run_in_threadpool(validate_set, s, engine.env, session.validate_cache)
                session.set_draft(s, problems)
            elif kind == "play":
                session.play()
            elif kind == "pause":
                session.pause()
            elif kind == "seek":
                session.seek(float(msg["t"]))
            elif kind == "loop":
                session.loop(bool(msg.get("on", True)), msg.get("from"), msg.get("to"))
            elif kind == "live":
                session.set_live(bool(msg.get("on", True)))
            else:
                return f"Unknown message type {kind!r}"
        except ValidationError as e:
            return "Invalid set: " + _problem(e)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            return f"Bad message: {e}"
        return None

    # ------------------------------------------------------------ web UI
    @app.get("/{path:path}", include_in_schema=False)
    def web_ui(path: str):
        if path == "api" or path.startswith(("api/", "ws/")):
            raise HTTPException(404, "Not found")
        index = dist / "index.html"
        if not index.is_file():
            return HTMLResponse(NO_UI_PAGE)
        if path:
            f = (dist / path).resolve()
            if f.is_file() and dist.resolve() in f.parents:
                return FileResponse(f)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


# ---------------------------------------------------------------- socket helpers
def _socket_fps(ws: WebSocket, default: float) -> float:
    try:
        fps = float(ws.query_params.get("fps", default))
    except ValueError:
        fps = default
    return max(1.0, min(60.0, fps))


class _Pace:
    """Deadline pacing for a socket's frame rate. asyncio timers may fire up to a clock
    tick early (15.6 ms on Windows); sleeping to fixed deadlines keeps the average exact."""

    def __init__(self, fps: float):
        self.period = 1.0 / fps
        self.next = time.perf_counter()

    async def wait(self) -> None:
        now = time.perf_counter()
        self.next = max(self.next + self.period, now - self.period)   # no burst after a stall
        await asyncio.sleep(max(0.0, self.next - now))


class _Sender:
    """Serialises sends of the socket's tasks and gives up on a client that stopped reading."""

    def __init__(self, ws: WebSocket):
        self._ws = ws
        self._lock = asyncio.Lock()

    async def json(self, data: dict) -> None:
        await self._send(self._ws.send_text(json.dumps(data)))

    async def bytes(self, data: bytes) -> None:
        await self._send(self._ws.send_bytes(data))

    async def _send(self, coro) -> None:
        async with self._lock:
            await asyncio.wait_for(coro, SEND_TIMEOUT)


async def _drain(ws: WebSocket) -> None:
    """Read (and ignore) client messages until the socket closes."""
    while True:
        msg = await ws.receive()
        if msg["type"] == "websocket.disconnect":
            return


async def _serve(ws: WebSocket, *coros) -> None:
    """Run the socket's tasks until one ends (client gone, stuck, or done), then close."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    for t in done:
        exc = t.exception() if not t.cancelled() else None
        if exc is not None and not isinstance(exc, (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError, OSError)):
            log.warning("websocket task failed: %r", exc)
    if ws.application_state != WebSocketState.DISCONNECTED and ws.client_state != WebSocketState.DISCONNECTED:
        try:
            await ws.close()
        except Exception:
            pass
