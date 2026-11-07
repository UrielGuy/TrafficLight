# Architecture and contracts

This file is the contract between the modules. If code and this file disagree,
fix one of them in the same change.

## The sign

Six 128×64 HUB75 panels stacked vertically = a 128×384 canvas = three 128×128
**lights** (also "faces"): 0 top, 1 middle, 2 bottom. Each panel hangs on its
own port of a Colorlight 5A-75B card running our **custom firmware**
(`docs/FIRMWARE.md`, `Settings.output.driver = "fpga"`); the PC or Raspberry Pi
sends it the picture as UDP packets, packed into bit planes by the host. Full
24-bit colour. (The card's stock firmware is no longer supported.)

The card has eight ports (J1..J8) and its image is 128×512
(`config.PORTS` bands of 64 rows, `config.CARD_H`): band *b* feeds J*b+1*.
Only the output driver sees that image; `Settings.output.panel_order` says
which band each panel of the sign is on, the two bands without a panel are
sent black.

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
                              output driver (LED card)     audio engine (sound card)
                              preview stream (WebSocket)
```

## Modules and owners

| Path | Role |
| --- | --- |
| `trafficlight/config.py` | Geometry constants, data paths. |
| `trafficlight/model.py` | Pydantic models: `Clip`, `Track`, `Set`, `Settings`, `MediaItem`. |
| `trafficlight/render/base.py` | `ClipRenderer`, `ParamSpec`, `RenderContext`, `AudioCue`, `RenderEnv`, registry. |
| `trafficlight/render/color.py` | Colour param values (`#rrggbb`, `face`, `random`), hue jump. |
| `trafficlight/render/basic/` | `light`, `off`, `text`, `figure` renderers; text layout; fonts; sprites. |
| `trafficlight/render/fx/` | Beat animations (`fx:*`) and the clock. |
| `trafficlight/render/media/` | `video`, `image`, `audio` clip types. |
| `trafficlight/media/library.py` | Uploads, link imports, edits, proxies, thumbnails; implements `MediaLookup`. |
| `trafficlight/media/ingest.py` | Original -> prepared files (PyAV, Pillow): rotate, crop, trim, proxy, sound, thumbnail. |
| `trafficlight/media/download.py` | Fetching a link with yt-dlp (as a library) and merging picture and sound with PyAV. |
| `trafficlight/audio/` | `AudioEngine` contract (`base.py`) and the sounddevice mixer (`engine.py`). |
| `trafficlight/output/` | `OutputDriver` contract (`base.py`), the driver (`fpga.py`: custom firmware over UDP, options in `TRAFFICLIGHT_FPGA`; `Settings.output.card_address` / `card_port`: the address `""` (the default) is automatic, the card is found by its announcements (docs/FIRMWARE.md 6.1, 6.7); an address pins it; port 5775; gamma and brightness are applied by its packer, so `output.gamma` is the real gamma, default 2.2), panel mapping (`mapping.py`: panel order, flips, rotation, colour order), test patterns, the wiring rule (`wiring.py`: what the panels show → `panel_order` / `panel_flip`). `driver = "none"` sends nothing (the preview still works). Settings files that still say `"colorlight"` (the stock-firmware driver, removed) load as `"fpga"`, a gamma of 1.0 there as 2.2. |
| `trafficlight/output/hub75/` | Host side of the custom card firmware (`docs/FIRMWARE.md` is its contract): visit schedule, bit-plane packer, UDP packets, behavioural model of the card. |
| `trafficlight/checks.py` | The setup checks (`State.checks`), built from the output and audio statuses. |
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
* `tracks[0]` is the top layer on the sign (and the top row of the editor,
  among the tracks that have a row; see below), except that whole-sign
  (`all`) tracks are always beneath the lights' tracks: a whole-sign clip is
  the backdrop and a clip on one light overrides it there.

**How the editor shows whole-sign clips.** A track with `target: "all"` has
no row in the timeline. Its clips are drawn as one tall block over all light
rows (every `faceN` track, extra ones included); the light rows are kept
together above the audio rows (reordering and adding tracks enforce it; a
stored order that mixes them is shown that way and rewritten only when the
track order is edited). New whole-sign clips go on the first `all` track,
which the editor creates when a set has none. A whole-sign clip is the
backdrop of the three lights: a clip on a light's row may sit over it in
time and replaces it in that light (drawn on top of the block in the
timeline, as the player composites it); the `off` clip (black) switches one
light off that way. Whole-sign clips may not overlap each other, and clips
on one row may not overlap each other. A library video or picture is
added as a whole-sign clip when its crop shape is `sign`, or `free` and taller
than √3:1. The editor clears `muted` on `all` tracks with its first edit (they
have no mute button).

### Compositing (player)

For set time `t`:

1. Start with black. For each light, if `Settings.off_style == "ring"`, draw
   a dim outline ring (radius `FACE_RADIUS`, width `RING_WIDTH`, the light's
   `face_colors` entry at ~15 %) — this is what an unlit light looks like.
2. Walk the visual tracks from the bottom layer to the top, skipping muted
   ones: first the `all` tracks from the last to the first, then the lights'
   tracks from the last to the first (`player.paint_order`), so a clip on a
   light always shows over a whole-sign clip. On each, find the clip covering
   `t` (`start <= t < start + duration`), render it into its box (128×128 at
   the light's rows for `faceN`, 128×384 for `all`) and alpha-blend it over
   what is below with the renderer's own alpha (if RGBA), `opacity` and
   `fade_in` / `fade_out` (linear).
   The mask (`Clip.mask`; default True, because only the round lights are
   seen on the sign) is not applied clip by clip. Two pictures are kept: what
   is seen through the lights (every clip) and what is seen outside them
   (only clips with the mask off, and types with `masked = False` such as
   `off`, which always fill their whole box). They are joined at the end
   through the anti-aliased discs of radius `FACE_RADIUS` (half the box: a
   disc fills its light's 128 px). So a clip on a light replaces the clip
   below it right up to the rim, with nothing of the lower one left in the
   soft edge.
3. Collect an `AudioCue` from every active clip whose renderer returns one
   (audio tracks and videos with sound), set `key = clip.id`, multiply `gain`
   by the clip's `opacity` and fades.

`RenderContext.seed` is stable while a clip plays once and changes the next
time the set comes round, so "random" colours re-roll every pass.

**Transparent colours.** A colour param whose `ParamSpec` has
`"transparent": true` (backgrounds: text, figures, the beat animations'
circle or background) may hold the value `"transparent"` besides
`"#rrggbb"`, `"face"` and `"random"`. Nothing is drawn in that colour: the
renderer returns RGBA and what is below the clip shows through, which is how
text or an animation on one light sits over a whole-sign video
(`render/color.is_transparent`).

## Clip types

Every clip type is a `ClipRenderer` subclass registered with `@register`.
`GET /api/catalog` publishes them and the UI builds its palette and inspector
from that, so adding a renderer needs no UI change.

| type | where | what |
| --- | --- | --- |
| `light` | face, all | A plain disc in a colour (a lit traffic light). |
| `off` | face, all | Black, over the whole box (never masked). On a light over a whole-sign clip it switches that light off. |
| `text` | face | Text fitted in the light: automatic size and line breaks, Hebrew and English. |
| `figure` | face | Animated pictogram (walking, dancing, DJ…). |
| `fx:quarters`, `fx:pulse`, `fx:wave`, `fx:clock`, `fx:ecg`, `fx:ecgsweep`, `fx:heart`, `fx:ripples`, `fx:eq`, `fx:chase`, `fx:ball`, `fx:sparkle`, `fx:fillv`, `fx:fillx` | face (some also all) | Beat animations ported from the old sign. |
| `clock` | face | Wall clock showing the real time. |
| `video` | face, all | A video from the library, fitted to the box; optional sound. |
| `image` | face, all | A still image or GIF. |
| `audio` | audio | Music or sound from the library. |

## Importing and cropping video

Uploads and link imports are stored as they are (`original.*`, full
quality); nothing is masked or reduced. What the sign plays is a proxy made
from the original according to the item's `MediaEdit`: rotate, then crop,
then trim, then scale to the smallest size that still covers 128×384 (never
up). The edit is not destructive and can be changed at any time; the proxy
is simply made again. `MediaItem.duration / width / height` describe the
prepared result; `source_*` the original.

The crop tool (Media page) shows the whole original with a crop box that can
be locked to one light (1:1) or the whole sign (1:3), in/out points, rotation
in quarter turns, and a preview of the result as the sign will show it —
optionally through the round masks (one disc, or three for the whole sign),
because that is what a light really shows. A crop made for one light can
still be placed on the whole sign and the other way round: the clip's `fit`
decides how it fills the box.

Coordinates: the crop is `[x, y, w, h]` in fractions of the picture after the
rotation; trim times are seconds from the original's first picture. Width
and height of the result are in pixels of the original (a 1:1 crop of a
1080p video is about 1080×1080); the proxy is then made just big enough to
cover the sign.

**Changing the edit while the item plays.** The prepared files of each
version live in their own folder (`v<N>/`, `MediaItem.version`); a new edit
is prepared into `v<N+1>/` while `v<N>/` stays untouched and in use (the item
reports `processing`, with `duration` etc. still describing `v<N>`). When the
new files are complete the version moves in one step; a playing clip sees
the new path on its next frame, decodes the new proxy alongside the old one
and switches as soon as the new one has the frame for the current time (no
restart of the clip, the set or the server); its sound switches at the same
moment. The old folder is then deleted, retried every few seconds until no
reader holds a file open (Windows cannot delete an open file). Nothing is
ever renamed or replaced in place. A newer edit cancels one still being
prepared. If preparing fails while an earlier result exists, the item stays
`ready` with its previous `edit` and `error` says what went wrong.

**Link imports.** `yt-dlp` (used as a library; there is no ffmpeg executable)
only finds the streams. The best picture up to `max_height` (H.264 first) and
the best sound (Opus first: YouTube's AAC decodes ~35 ms late, Opus within
2 ms, measured on flash-and-beep test clips) are fetched as two files over
HTTP range requests (or yt-dlp's own downloader for HLS / DASH fragments),
then copied packet for packet into one MP4 (or WebM / MKV) with PyAV: that
is the original, nothing re-encoded, picture and sound in step as the site
served them. `start` / `duration` fetch only about that much of the file,
keep it from the keyframe before `start`, and become the item's initial
trim (which can be widened later only within what was fetched). A link to
a plain media file (a picture, a GIF, a video) is fetched as it is.

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

`Settings.sign_on = false` (the dashboard's Turn off) makes the sign dark and
silent until it is turned back on: the output driver keeps getting frames, all
black, the audio no cues, and the preview shows the black sign. The show runs
on unseen (schedule, sets, editor live mode keep their clocks), so turning it
on picks up where the show is by then. Test patterns still show: they are
deliberate setup pictures. The setting survives a restart; bundles do not
carry it.

## HTTP API

JSON everywhere. Errors: HTTP 4xx with `{"detail": "message"}`.

| Call | Body → Result |
| --- | --- |
| `GET /api/state` | → `State` (below) |
| `GET /api/catalog` | → `{"clip_types": [ClipType]}`; `ClipType` = `ClipRenderer.describe()`: `{"type","label","category","targets","default_duration","description","params":[ParamSpec],"masked"}`. Fonts and figures are `select` params with their options listed |
| `GET /api/sets` | → `[SetSummary]` in play order |
| `POST /api/sets` | `Set` (id optional; `tracks` optional → default five tracks) → `Set` |
| `GET /api/sets/{id}` | → `Set` |
| `PUT /api/sets/{id}` | `Set` → `Set` |
| `DELETE /api/sets/{id}` | → `{"ok": true}` |
| `POST /api/sets/{id}/duplicate` | → `Set` (new id, name + " copy") |
| `POST /api/sets/order` | `{"ids": [...]}` → `{"ok": true}` |
| `POST /api/sets/{id}/show` | → `{"ok": true}` — show now |
| `POST /api/validate` | `Set` → `{"problems": [{"clip_id", "track_id", "message"}]}` |
| `GET /api/bundle?set={id}[&set={id}…]` | → a zip (attachment) of those sets and the media they use, to move them to another TrafficLight. `?kind=all`: every set and the media they use; `?kind=everything`: every set, every media item and the show settings (`schedule`, `face_colors`, `off_style`; never output, wiring or sound, which belong to the computer). Media come with their original and the prepared files of their current version, so they play at once at the other end. Layout in `trafficlight/bundle.py`. Made while it downloads (no length). 404 for an unknown set, 422 for no set / a bad `kind` |
| `POST /api/bundle` | multipart `file` (a bundle zip) + `replace` (default `true`) → `{"sets_added", "sets_replaced", "sets_copied", "sets_unchanged", "media_added", "media_updated", "media_unchanged", "media_failed": [names], "settings": bool}`. Ids are kept, so sets find their media. Something already here under the same id: with `replace` a set is overwritten and a media item takes the bundle's name and edit (a changed edit is re-made here); without, media stay as they are and a differing set is added next to the one here with new ids, named "… (imported)". Identical sets count as unchanged. `kind` all / everything also bring the play order (sets only here go after), everything the show settings (the default set follows its set). 415 for a file that is not a bundle or comes from a newer version, 507 when the disk lacks room for it |
| `POST /api/transport/pause`, `/play`, `/next` | → `{"ok": true}` — the sign's own playback |
| `GET /api/settings` | → `Settings` |
| `PATCH /api/settings` | partial `Settings` (deep-merged) → `Settings`; takes effect at once. 422 with a readable `detail` for an impossible wiring: `output.panel_order` must be 6 different bands 0..7 (connector − 1), `output.panel_flip` 6 entries |
| `GET /api/audio/devices` | → `[{"id","name","default","channels"}]` |
| `POST /api/output/test` | `{"pattern": "off" \| "panels" \| "white" \| "red" \| "green" \| "blue" \| "gradient" \| "wiring"}` → `{"ok": true}`; a test picture replaces the show until `"off"` (panels = each panel in its own colour with its number, to check the mapping; wiring = drawn in the card's image and sent past the mapping: each port's band shows its connector number 1..8, underlined, and an arrow to the top of the band, so each panel shows where it is plugged in and which way up it hangs. The preview meanwhile shows the sign as the current mapping would show that picture) |
| `GET /api/media` | → `[MediaItem]` |
| `POST /api/media` | multipart `file` (+ optional `name`) → `MediaItem` with `status: "processing"`; poll `GET /api/media/{id}` until `ready` or `error` |
| `POST /api/media/probe` | `{"url": "https://…"}` → `{"title": str \| null, "duration": seconds \| null, "warn_over": 240}` — what can be found out about a link's video without downloading it (a second or two: it asks the site); unknowns are `null` (direct file links, unknown sites, offline). The import page asks "are you sure?" before downloading more than `warn_over` seconds (`config.IMPORT_WARN_SECONDS`). 422 without a `url` |
| `POST /api/media/import` | `{"url": "https://…", "max_height": 480, "start": 0, "duration": null, "name": null}` (all but `url` optional) → `MediaItem` with `status: "processing"`, `stage: "queued"` — fetch a video from a link (YouTube and whatever else yt-dlp handles, or a direct file URL) into the library, picture and sound. `max_height` is the "480" of 480p (the shorter side; 144..4320; default `config.IMPORT_HEIGHT` = 480, which covers even a whole-sign crop of the full picture height); links are fetched one at a time in the order they were added: the others wait as `stage: "queued"` and turn `"downloading"` when their turn comes; `start` / `duration` (seconds) fetch only that part and set the item's trim; `name` defaults to the video's title. Failures (offline, unsupported site, removed video) end as `status: "error"` with a readable `error`; a `PATCH` with an `edit` (even the same one) tries the download again with the same settings. 422 for anything but an http(s) link |
| `GET /api/media/{id}` | → `MediaItem` (`progress` 0..1 and `stage` while processing: `"queued"`, `"downloading"`, `"waiting"`, `"converting"`, `"preparing the crop view"`) |
| `PATCH /api/media/{id}` | `{"name"?, "edit"?: MediaEdit}` (at least one) → `MediaItem`; a changed `edit` re-makes the prepared files from the original (`status` goes back to `processing`; the old proxy keeps playing until the new one is ready, then every set using the item shows the new crop; see "Importing and cropping video"). 422 for an invalid edit (crop outside the picture, `end <= start`, an in point after the end, a crop or rotation of a sound); a still image ignores the trim. On an item that ended in error, an `edit` (changed or not) tries again |
| `POST /api/media/{id}/duplicate` | `{"name"?}` (default: name + " copy") → `MediaItem` — a second item from the same original, with the same edit and ready at once (for another crop of the same video; then `PATCH` its `edit`). The original is hard-linked where the file system allows it, else copied. 409 while the original is still downloading |
| `GET /api/media/{id}/source` | the whole original picture, un-cropped and un-rotated (but upright, as players show it), in a form a browser can play: the original when it can (pictures; MP4 / MOV / WebM with H.264, VP8/9 or AV1), else `source.mp4` made at ingest (H.264, longest side ≤ 1280, no sound; also for GIFs, which a browser cannot seek). Supports Range. 404 for sounds and before there is one |
| `DELETE /api/media/{id}` | → `{"ok": true}`; if sets use it, 409 `{"detail": "Used by A, B", "sets": ["A", "B"]}` unless `?force=1` |
| `GET /api/media/{id}/thumb.jpg` | a small still (video: a frame; audio: its waveform; image: itself) |
| `GET /api/media/{id}/file` | the file for the browser to play in the library page (supports Range) |
| `GET /api/media/{id}/peaks` | audio / video with sound: `{"rate": peaks per second, "peaks": [0..1,...]}` for drawing a waveform on the timeline |
| `POST /api/media/{id}/show` | `{"target": "face0"\|"face1"\|"face2"\|"all"\|"faces", "loop": true}` → `{"ok": true}` — play just this item now, once, as an unsaved set (`faces` = the same on all three lights; length = the item's duration, 10 s for a still; `loop` is the clip's loop param; 409 if the item is not ready; `now.set_id` is null and `set_name` the item's name) |
| `POST /api/media/{id}/make-set` | same body → `Set` — a new set containing just this item |
| `GET /api/render.png?set={id}&t={seconds}` | one frame of a stored set as PNG (thumbnails) |

```json
State = {
  "now": {"source": "schedule" | "show" | "editor" | "test" | "idle",
          "set_id": "…" | null, "set_name": "…" | null,
          "t": 12.3, "duration": 30.0, "paused": false},
  "schedule": {"default_set": "…" | null, "random_minutes": 6, "random_enabled": true,
               "random_in": 123.0 | null},          // seconds until a random set is due; null = n/a
  "output": {"driver": "fpga", "ok": true, "error": null, "fps": 59.9, "render_ms": 1.2,
             // driver "fpga" adds, among its counters:
             // "address": "10.75.38.201:5775" | null (no card yet), "answering": true,
             // "found": {"ip", "port", "mac", "uid" (hex), "uid_valid", "fixed_address", "fw", "auto"} | null,
             // "others": [{"ip", "port", "mac", "uid", "uid_valid", "fixed_address", "fw"}, …]   (cards heard, not driven)
            },
  "audio": {"ok": true, "error": null, "device": "…", "device_name": "…", "volume": 0.8, "voices": 1},
  "checks": [{"id": "output.receiver", "level": "error" | "warning" | "info" | "ok",
              "title": "The LED card does not answer", "detail": "…", "fix": "…" | null}, …],
  "sets_rev": 12, "media_rev": 3,                  // bump when sets / media change: refetch
  "version": "0.1.0"
}
```

`output` and `audio` also carry `problems`: the failures in the same shape
as a check, from the code that detects them. **`checks`** (built in
`trafficlight/checks.py`) is the setup list for the UI, most basic first,
leaving out what cannot be known yet (no answer: nothing about the card's
firmware). Levels: `error` = the sign or the sound cannot work, `warning` =
works but something is off, `info` = a neutral note or hint (driver set to
none, panel wiring still the default), `ok`. Ids, in this order:
`output.driver` (off / on / cannot start), `output.network` (this computer's
port on the sign's network 10.75.0.0/16, from `output/network.py` via psutil:
none, netmask narrower than /16, no link, a host address in the cards' range,
two ports on the network, the route to the cards leaving through another
adapter, a link too slow for the frame rate; the driver status carries it as
`network`, `need_mbps` and `frame_rate`), `output.settings` (a card address
that does not resolve, wiring values that do not fit), `output.access` (the UDP
socket cannot be opened, or the announce port 5776 is taken by another
server), `output.send` (sending fails: usually the port has no address in the
card's network), `output.receiver` (card found and answering, nothing found,
announces but does not answer, a pinned address that does not answer),
`output.cards` (other cards heard and not driven), `output.firmware`
(version, fits this sign), `output.schedule` (refresh schedule built and
accepted, fewer planes than planned, rejected), `output.packets` (card drops
packets or loses frames), `output.timing` (card refresh stretching),
`output.wiring`, `audio.backend` (PortAudio missing), `audio.device`
(missing, none, cannot open, stopped), `server.web_ui` (not built).

## WebSockets

Text messages are JSON; binary messages are one complete image file (PNG or
JPEG, 128×384) of the newest frame — the browser draws it with
`createImageBitmap(new Blob([data]))`, scaled up with nearest-neighbour.

**`/ws/sign?fps=20`** — the dashboard. Server sends `{"type": "state", …State}`
a few times a second and binary sign frames at up to `fps`. No client
messages.

**`/ws/editor?fps=30&live=1`** — one editor session; opening a second one closes the
first (the first gets `{"type": "closed", "reason": "…"}`). `live=0` opens the
session not live (the sign carries on with its schedule), so a client whose
Live switch is off never takes the LEDs over, not even until its first
message. Client → server:

| Message | Meaning |
| --- | --- |
| `{"type": "set", "set": Set}` | the draft (send after every edit, debounced); playback position is kept |
| `{"type": "play"}` / `{"type": "pause"}` | transport |
| `{"type": "seek", "t": 12.3}` | move the playhead (also while paused: the frame at `t` is shown) |
| `{"type": "loop", "on": true, "from": 0, "to": 30}` | loop the whole draft or a range (default: on, whole set) |
| `{"type": "live", "on": true}` | show the draft on the LEDs and speakers (default on) |

Server → client: `{"type": "status", "t": 12.3, "playing": true, "live": true,
"duration": 30, "loop": {"on": true, "from": 0, "to": 30},
"problems": [{"clip_id", "track_id", "message"}]}` about ten times a second
(and at once after every client message), `{"type": "error", "message"}` for
a message it could not use, and binary editor frames. A session opens paused
at t = 0, and live unless the URL says `live=0`. `problems` holds each renderer's `validate()` results plus
structural ones (unknown type, wrong target for the track, overlapping clips,
zero length, a clip starting after the set ends). While a live session is
open the sign's own clock is frozen; when the socket closes the session ends
and the sign resumes its schedule where it was.

Other rules the server follows: set ids are `[A-Za-z0-9_-]{1,64}`; `PUT
/api/sets/{id}` creates the set if it does not exist; validation errors are
422 with `detail` as a string; "show now" restarts the random interval when
the shown set starts and un-pauses the sign; a test pattern wins over
everything and silences the sound.

## Internal Python interfaces

Contracts in code: `render/base.py` (renderers; `RenderContext.still` marks a
one-off picture such as `/api/render.png`, for which a renderer may wait for a
decoder instead of drawing a stand-in), `output/base.py` (drivers),
`audio/base.py` (audio engine). Two more that cross package borders:

```python
# trafficlight/media/library.py — implements render.base.MediaLookup
class MediaLibrary:
    def __init__(self, root: Path, fetcher=None): ...   # root = data_dir() / "media"; loads what is there.
                                                # fetcher: stands in for download.fetch (tests: no network)
    rev: int                                    # grows on every change (add, status change, rename, delete;
                                                # progress at most twice a second)
    def list(self) -> list[MediaItem]: ...      # newest first
    def get(self, media_id: str) -> MediaItem | None: ...
    def add(self, src: Path, name: str, edit: MediaEdit | None = None) -> MediaItem: ...
        # `src` is a temp file holding the upload, with the original extension. It is moved into
        # the library; returns at once with status "processing"; ingest runs on a background thread
        # and ends with status "ready" or "error". ValueError for a file type it cannot take.
    def import_url(self, url, *, max_height=config.IMPORT_HEIGHT, start=0.0, duration=None, name=None) -> MediaItem: ...
        # POST /api/media/import: returns at once ("processing", stage "queued"); one download thread
        # fetches links one at a time in order ("downloading"), then the ingest thread prepares. ValueError for something that is not an http(s) link.
    def update(self, media_id, *, name=None, edit: MediaEdit | None = None) -> MediaItem: ...
        # PATCH: rename and / or a new edit (re-made in the background). KeyError, ValueError (see the route)
    def rename(self, media_id: str, name: str) -> MediaItem: ...   # update(name=...)
    def duplicate(self, media_id, name=None) -> MediaItem: ...     # KeyError; ValueError while downloading
    def delete(self, media_id: str) -> None: ...                    # KeyError if unknown
    def video_path(self, media_id) -> str | None: ...   # MediaLookup: the proxy the sign decodes (of the
    def image_path(self, media_id) -> str | None: ...   #   current version; renderers ask every frame and
    def audio_path(self, media_id) -> str | None: ...   #   follow a change)
    def thumb_path(self, media_id) -> Path | None: ...  # thumb.jpg of the result
    def file_path(self, media_id) -> Path | None: ...   # what GET /api/media/{id}/file serves: playable in a browser
    def source_path(self, media_id) -> Path | None: ... # what GET /api/media/{id}/source serves
    def peaks(self, media_id) -> dict | None: ...       # {"rate": float, "peaks": [0..1, ...]}
    def wait_ready(self, media_id, timeout: float = 30) -> MediaItem: ...   # blocks until not "processing"
    def close(self) -> None: ...

# trafficlight/output/base.py
class OutputDriver:
    def send(self, frame): ...          # (384,128,3) RGB, mapped by the driver (order, flips, rotation, colours)
    def send_card(self, image): ...     # (CARD_H,128,3) RGB already in the card's layout: colour order, gamma,
                                        # brightness only (the wiring picture); NullDriver ignores it
    def set_frame_rate(self, fps): ...  # Settings.fps, from the engine; the driver builds the card's refresh schedule for it
    def status(self) -> dict: ...       # {"driver", "ok", "error", ..., "problems": [check-shaped dicts]}
class OutputError(RuntimeError):        # OutputError(detail, fix, check="output.access", title=...): str() is the
                                        # whole message; as_problem() the check entry

# trafficlight/output/patterns.py
def make(name: str) -> np.ndarray | None: ...
    # a (384,128,3) test frame for "panels" | "white" | "red" | "green" | "blue" | "gradient"; None for unknown
def make_card(name: str) -> np.ndarray | None: ...      # (CARD_H,128,3) for "wiring"
def wiring_preview(order, flip, rotate180) -> np.ndarray: ...   # what the sign shows of it under that mapping

# trafficlight/output/wiring.py (same rule in web/src/pages/wiring.ts)
def from_seen(seen) -> tuple[list[int], list[bool]]: ...
    # seen[i] = (connector 1..8, arrow points down) or None (dark) for the panel at position i (0 = top)
    # -> panel_order[i] = connector - 1, panel_flip[i] = arrow down, for rotate180 off. A dark panel gets the
    # lowest unused band. ValueError for duplicates / out of range.
def to_seen(order, flip, rotate180=False) -> list[tuple[int, bool]]: ...   # the inverse (pre-fills the wizard)
```

The server owns every HTTP route, including `/api/media*` (it calls
`MediaLibrary`).

## Files on disk (`data/`, override with `TRAFFICLIGHT_DATA`)

```
data/settings.json
data/sets/index.json          play order: {"order": [ids]}
data/sets/<id>.json           one Set
data/media/<id>/meta.json     MediaItem (+ "import": what is left to fetch, until a link import has its original)
data/media/<id>/original.*    the upload or download, untouched (a duplicate's is a hard link where possible)
data/media/<id>/source.mp4    only if a browser cannot play the original: the whole picture for the crop tool
data/media/<id>/v<N>/         the prepared files of version N (MediaItem.version); older versions are deleted
                              once nothing reads them, unfinished ones at start-up:
    proxy.mp4                 video: turned, cropped, trimmed, small, short-GOP, no sound (what the sign decodes)
    proxy.png                 image: turned and cropped
    audio.wav                 sound track of a video (cut to match the proxy) / of an audio item
    thumb.jpg, peaks.json     thumbnail of the result; waveform
data/media/<id>/download/     a link import while it downloads
```
