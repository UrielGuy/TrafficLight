# Architecture and contracts

This file is the contract between the modules. If code and this file disagree,
fix one of them in the same change.

## The sign

Six 128×64 HUB75 panels stacked vertically = a 128×384 canvas = three 128×128
**lights** (also "faces"): 0 top, 1 middle, 2 bottom. Each panel hangs on its
own port of a Colorlight 5A-75B receiver card running the **stock firmware**;
the PC or Raspberry Pi sends it raw Ethernet frames. Full 24-bit colour.

A frame everywhere in the code is a `numpy.ndarray`, `uint8`, shape
`(384, 128, 3)`, RGB, row 0 = top of the sign as the audience sees it.

## Pipeline

```
web UI (React)  --REST + WebSocket-->  server (FastAPI)
                                          |
        store (sets, settings: JSON files)  media library (uploads, proxies)
                                          |
   engine: render thread at Settings.fps
     schedule -> which set plays ----\
     editor session (draft set) ------+--> player.render(t) -> frame + audio cues
                                          |            |
                              output driver (Colorlight)   audio engine (sound card)
                              preview stream (WebSocket)
```

## Modules and owners

| Path | Role |
| --- | --- |
| `trafficlight/config.py` | Geometry constants, data paths. |
| `trafficlight/model.py` | Pydantic models: `Clip`, `Track`, `Set`, `Settings`, `MediaItem`. |
| `trafficlight/render/base.py` | `ClipRenderer`, `ParamSpec`, `RenderContext`, `AudioCue`, `RenderEnv`, registry. |
| `trafficlight/render/color.py` | Colour param values (`#rrggbb`, `face`, `random`), hue jump. |
| `trafficlight/render/basic/` | `light`, `text`, `figure` renderers; text layout; fonts; sprites. |
| `trafficlight/render/fx/` | Beat animations (`fx:*`) and the clock. |
| `trafficlight/render/media/` | `video`, `image`, `audio` clip types. |
| `trafficlight/media/library.py` | Uploads, proxies, thumbnails; implements `MediaLookup`. |
| `trafficlight/audio/` | `AudioEngine` contract (`base.py`) and the sounddevice mixer (`engine.py`). |
| `trafficlight/output/` | `OutputDriver` contract (`base.py`), Colorlight driver, panel mapping. |
| `trafficlight/store.py` | Sets and settings on disk. |
| `trafficlight/schedule.py` | Which set plays next (default set + random set). |
| `trafficlight/player.py` | Timeline evaluation and compositing for one set. |
| `trafficlight/engine.py` | Render thread, sign player, editor session, routing to driver / audio / preview. |
| `trafficlight/server/` | FastAPI app: REST, WebSockets, static web UI. |
| `web/` | React + TypeScript + Vite UI. `web/dist` is served by the server. |

## Data model

See `trafficlight/model.py` (authoritative). A **set** is a timeline:

```json
{"id": "a1b2c3d4e5f6", "name": "Morning", "bpm": 120, "beat_offset": 0, "duration": 30,
 "in_rotation": true,
 "tracks": [
   {"id": "...", "name": "Whole sign", "target": "all",   "muted": false, "clips": []},
   {"id": "...", "name": "Top",        "target": "face0", "muted": false, "clips": [
      {"id": "...", "type": "text", "start": 0, "duration": 4,
       "params": {"text": "STOP", "bg": "face", "ink": "#000000"},
       "mask": null, "opacity": 1, "fade_in": 0, "fade_out": 0}]},
   {"id": "...", "name": "Middle",     "target": "face1", "muted": false, "clips": []},
   {"id": "...", "name": "Bottom",     "target": "face2", "muted": false, "clips": []},
   {"id": "...", "name": "Audio",      "target": "audio", "muted": false, "clips": []}]}
```

* Times are seconds (floats). The beat grid (`bpm`, `beat_offset`) is for
  snapping in the editor and for the beat animations.
* Clips on one track never overlap. A set may have any number of tracks per
  target (the editor can add and remove tracks).
* `tracks[0]` is the top row of the editor and the top layer on the sign.

### Compositing (player)

For set time `t`:

1. Start with black. For each light, if `Settings.off_style == "ring"`, draw
   a dim outline ring (radius `FACE_RADIUS`, width `RING_WIDTH`, the light's
   `face_colors` entry at ~15 %) — this is what an unlit light looks like.
2. Walk the visual tracks from the last to the first (skipping muted ones).
   On each, find the clip covering `t` (`start <= t < start + duration`),
   render it into its box (128×128 at the light's rows for `faceN`, 128×384
   for `all`), then apply, in this order: the renderer's own alpha (if RGBA),
   the mask (`Clip.mask`; default True on a light = anti-aliased disc of
   radius `FACE_RADIUS`; on `all`, True = the three discs), `opacity`,
   `fade_in` / `fade_out` (linear), and alpha-blend over what is below.
3. Collect an `AudioCue` from every active clip whose renderer returns one
   (audio tracks and videos with sound), set `key = clip.id`, multiply `gain`
   by the clip's `opacity` and fades.

`RenderContext.seed` is stable while a clip plays once and changes the next
time the set comes round, so "random" colours re-roll every pass.

## Clip types

Every clip type is a `ClipRenderer` subclass registered with `@register`.
`GET /api/catalog` publishes them and the UI builds its palette and inspector
from that, so adding a renderer needs no UI change.

| type | where | what |
| --- | --- | --- |
| `light` | face, all | A plain disc in a colour (a lit traffic light). |
| `text` | face | Text fitted in the light: automatic size and line breaks, Hebrew and English. |
| `figure` | face | Animated pictogram (walking, dancing, DJ…). |
| `fx:quarters`, `fx:pulse`, `fx:wave`, `fx:clock`, `fx:ecg`, `fx:ecgsweep`, `fx:heart`, `fx:ripples`, `fx:eq`, `fx:chase`, `fx:ball`, `fx:sparkle`, `fx:fillv`, `fx:fillx` | face (some also all) | Beat animations ported from the old sign. |
| `clock` | face | Wall clock showing the real time. |
| `video` | face, all | A video from the library, fitted to the box; optional sound. |
| `image` | face, all | A still image or GIF. |
| `audio` | audio | Music or sound from the library. |

## Schedule

`Settings.schedule.default_set` is the set the sign loops. When it ends and
`random_minutes` have passed since the last random set **started** (or since
start-up, or since a set was shown by hand), one other set with
`in_rotation` picked at random (never the previous pick when there are two
or more candidates) plays once, then the default set again. Nothing is cut
short. "Show now" plays a set once, then returns to the default set and
restarts the interval. Without a default set, all sets play in order, each
once, forever. No sets at all: the sign shows unlit lights.

## Engine

One render thread, `Settings.fps` times a second:

* the **sign player** plays what the schedule says; its frames go to the
  output driver, its cues to the audio engine;
* an **editor session** (at most one; it exists while an editor WebSocket is
  open) plays a draft set under the editor's transport. With `live: true`
  (default) the editor session **replaces** the sign player on the LEDs and
  the speakers and the schedule is suspended; with `live: false` the sign
  carries on with the schedule and the editor only gets preview frames (no
  sound).
* the latest sign frame and editor frame are kept for the preview sockets.

Pause stops the clock of the sign player (picture stands still, sound stops).

## HTTP API

JSON everywhere. Errors: HTTP 4xx with `{"detail": "message"}`.

| Call | Body → Result |
| --- | --- |
| `GET /api/state` | → `State` (below) |
| `GET /api/catalog` | → `{"clip_types": [ClipType]}`; `ClipType` = `ClipRenderer.describe()`: `{"type","label","category","targets","default_duration","description","params":[ParamSpec]}`. Fonts and figures are `select` params with their options listed |
| `GET /api/sets` | → `[SetSummary]` in play order |
| `POST /api/sets` | `Set` (id optional; `tracks` optional → default five tracks) → `Set` |
| `GET /api/sets/{id}` | → `Set` |
| `PUT /api/sets/{id}` | `Set` → `Set` |
| `DELETE /api/sets/{id}` | → `{"ok": true}` |
| `POST /api/sets/{id}/duplicate` | → `Set` (new id, name + " copy") |
| `POST /api/sets/order` | `{"ids": [...]}` → `{"ok": true}` |
| `POST /api/sets/{id}/show` | → `{"ok": true}` — show now |
| `POST /api/validate` | `Set` → `{"problems": [{"clip_id", "track_id", "message"}]}` |
| `POST /api/transport/pause`, `/play`, `/next` | → `{"ok": true}` — the sign's own playback |
| `GET /api/settings` | → `Settings` |
| `PATCH /api/settings` | partial `Settings` (deep-merged) → `Settings`; takes effect at once |
| `GET /api/audio/devices` | → `[{"id","name","default","channels"}]` |
| `GET /api/output/interfaces` | → `[{"id","name","description","mac","up"}]` |
| `POST /api/output/test` | `{"pattern": "off" \| "panels" \| "white" \| "red" \| "green" \| "blue" \| "gradient"}` → `{"ok": true}`; a test picture replaces the show until `"off"` (panels = each panel in its own colour with its number, to check the mapping) |
| `GET /api/media` | → `[MediaItem]` |
| `POST /api/media` | multipart `file` (+ optional `name`) → `MediaItem` with `status: "processing"`; poll `GET /api/media/{id}` until `ready` or `error` |
| `GET /api/media/{id}` | → `MediaItem` |
| `PATCH /api/media/{id}` | `{"name"}` → `MediaItem` |
| `DELETE /api/media/{id}` | → `{"ok": true}` (409 with the names of the sets that use it unless `?force=1`) |
| `GET /api/media/{id}/thumb.jpg` | a small still (video: a frame; audio: its waveform; image: itself) |
| `GET /api/media/{id}/file` | the file for the browser to play in the library page (supports Range) |
| `GET /api/media/{id}/peaks` | audio / video with sound: `{"rate": peaks per second, "peaks": [0..1,...]}` for drawing a waveform on the timeline |
| `POST /api/media/{id}/show` | `{"target": "face0"\|"face1"\|"face2"\|"all"\|"faces", "loop": true}` → `{"ok": true}` — play just this video / image now (`faces` = the same on all three lights) |
| `POST /api/media/{id}/make-set` | same body → `Set` — a new set containing just this item |
| `GET /api/render.png?set={id}&t={seconds}` | one frame of a stored set as PNG (thumbnails) |

```json
State = {
  "now": {"source": "schedule" | "show" | "editor" | "test" | "idle",
          "set_id": "…" | null, "set_name": "…" | null,
          "t": 12.3, "duration": 30.0, "paused": false},
  "schedule": {"default_set": "…" | null, "random_minutes": 6, "random_enabled": true,
               "random_in": 123.0 | null},          // seconds until a random set is due; null = n/a
  "output": {"driver": "colorlight", "ok": true, "error": null, "interface": "…", "fps": 59.9},
  "audio": {"ok": true, "error": null, "device": "…", "device_name": "…", "volume": 0.8, "voices": 1},
  "sets_rev": 12, "media_rev": 3,                  // bump when sets / media change: refetch
  "version": "0.1.0"
}
```

## WebSockets

Text messages are JSON; binary messages are one complete image file (PNG or
JPEG, 128×384) of the newest frame — the browser draws it with
`createImageBitmap(new Blob([data]))`, scaled up with nearest-neighbour.

**`/ws/sign?fps=20`** — the dashboard. Server sends `{"type": "state", …State}`
a few times a second and binary sign frames at up to `fps`. No client
messages.

**`/ws/editor?fps=30`** — one editor session; opening a second one closes the
first (the first gets `{"type": "closed", "reason": "…"}`). Client → server:

| Message | Meaning |
| --- | --- |
| `{"type": "set", "set": Set}` | the draft (send after every edit, debounced); playback position is kept |
| `{"type": "play"}` / `{"type": "pause"}` | transport |
| `{"type": "seek", "t": 12.3}` | move the playhead (also while paused: the frame at `t` is shown) |
| `{"type": "loop", "on": true, "from": 0, "to": 30}` | loop the whole draft or a range (default: on, whole set) |
| `{"type": "live", "on": true}` | show the draft on the LEDs and speakers (default on) |

Server → client: `{"type": "status", "t": 12.3, "playing": true, "live": true,
"duration": 30, "problems": [{"clip_id", "track_id", "message"}]}` about ten
times a second (and at once after every client message), and binary editor
frames. The session ends when the socket closes; the sign returns to its
schedule.

## Internal Python interfaces

Contracts in code: `render/base.py` (renderers), `output/base.py` (drivers),
`audio/base.py` (audio engine). Two more that cross package borders:

```python
# trafficlight/media/library.py — implements render.base.MediaLookup
class MediaLibrary:
    def __init__(self, root: Path): ...        # root = data_dir() / "media"; loads what is there
    rev: int                                    # grows on every change (add, status change, rename, delete)
    def list(self) -> list[MediaItem]: ...      # newest first
    def get(self, media_id: str) -> MediaItem | None: ...
    def add(self, src: Path, name: str) -> MediaItem: ...
        # `src` is a temp file holding the upload, with the original extension. It is moved into
        # the library; returns at once with status "processing"; ingest runs on a background thread
        # and ends with status "ready" or "error". ValueError for a file type it cannot take.
    def rename(self, media_id: str, name: str) -> MediaItem: ...   # KeyError if unknown
    def delete(self, media_id: str) -> None: ...                    # KeyError if unknown
    def video_path(self, media_id) -> str | None: ...   # MediaLookup: the proxy the sign decodes
    def image_path(self, media_id) -> str | None: ...
    def audio_path(self, media_id) -> str | None: ...
    def thumb_path(self, media_id) -> Path | None: ...  # thumb.jpg
    def file_path(self, media_id) -> Path | None: ...   # what GET /api/media/{id}/file serves: playable in a browser
    def peaks(self, media_id) -> dict | None: ...       # {"rate": float, "peaks": [0..1, ...]}
    def wait_ready(self, media_id, timeout: float = 30) -> MediaItem: ...   # blocks until not "processing"
    def close(self) -> None: ...

# trafficlight/output/patterns.py
def make(name: str) -> np.ndarray | None: ...
    # a (384,128,3) test frame for "panels" | "white" | "red" | "green" | "blue" | "gradient"; None for unknown
```

The server owns every HTTP route, including `/api/media*` (it calls
`MediaLibrary`).

## Files on disk (`data/`, override with `TRAFFICLIGHT_DATA`)

```
data/settings.json
data/sets/index.json          play order: {"order": [ids]}
data/sets/<id>.json           one Set
data/media/<id>/meta.json     MediaItem
data/media/<id>/original.*    the upload
data/media/<id>/proxy.mp4     video: small, short-GOP, no sound (what the sign decodes)
data/media/<id>/audio.*       sound track of a video / normalised audio item
data/media/<id>/thumb.jpg, peaks.json
```
