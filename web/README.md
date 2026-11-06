# TrafficLight web UI

React + TypeScript + Vite. `npm run build` type-checks and writes `web/dist`, which the Python server serves at `/`
(index.html is the fallback for client routes). From WSL: `cmd.exe /c "cd web && npm run build"` (working directory = repo root).
`npm run dev` serves on Vite's port and proxies `/api` and `/ws` to `localhost:8080`.
`npm test` runs the unit tests (vitest). `node e2e/editor.e2e.mjs` drives the editor end to end in Microsoft Edge
(playwright-core, `channel: 'msedge'`) against a running server (`TL_URL`, default `http://localhost:8091`; it needs a ready
video and audio item in the library) and writes screenshots to `e2e/shots`; it also crops a copy of the video for the whole sign (deleted at the end) and covers whole-sign blocks, the snap grid and video speed. `TL_CROP_VIDEO=<a video file> node e2e/crop.e2e.mjs`
does the same for video import and the crop tool: it uploads the file, imports it again through the link field (from the
server's own `/source`, no internet), drives the tool with mouse and touch, saves, and compares the tool's preview with
`/api/render.png` of a set using the result; then "Save as a copy", the editor's entry point and `/ws/editor?live=0`.
It deletes what it made (`TL_KEEP=1` keeps it). `node e2e/shot.mjs <url> <w> <h> <out.png>` takes one screenshot.

## Layout

| Path | What |
| --- | --- |
| `src/api/types.ts` | Types mirroring `model.py` and the API doc: `Set` (exported as `SetData` too), `Track`, `Clip`, `Settings`, `MediaItem`, `State`, `ClipType`, `ParamSpec`, `Problem`, editor WebSocket messages (`EditorClientMessage`, `EditorStatus`). |
| `src/api/client.ts` | `api.*`: one typed function per REST route. Errors throw `ApiError` (`message` = server `detail`, `status`, raw `detail`). URL helpers `mediaThumbUrl` (pass the item's `version` so a re-made thumbnail is fetched), `mediaFileUrl`, `mediaSourceUrl`, `renderUrl`. |
| `src/api/socket.ts` | `connectSocket(path, {onJson, onFrame, onOpen, onClose})` (`path` may be a function, asked at each reconnect): reconnecting WebSocket; text -> `onJson`, binary -> `onFrame(ArrayBuffer)`. Returns `{send, close, connected}`. Use it for `/ws/editor`. |
| `src/components/SignPreview.tsx` | `<SignPreview feed={...} />` (a `FrameFeed` with `subscribe(cb)`) or `<SignPreview frame={ArrayBuffer\|Blob\|url} />`. Canvas 128x384, nearest-neighbour, newest frame wins. For the editor: build a feed from the `onFrame` callback of your socket. |
| `src/components/ui.tsx` | `Toggle`, `Slider`, `Segmented`, `Field`, `Dialog`, `Warning`, `Empty` (+ the shell's `Toasts`, `ConfirmHost`, `ConnectionBanner`). |
| `src/state/store.ts` | `createStore` / `useStore`: a 20-line external store (no state library). |
| `src/state/app.ts` | `/ws/sign` connection (`useAppState()` = `State`, `useConnected()`, `signFeed`), resources refetched when `sets_rev` / `media_rev` move: `useCatalog()` (clip types), `useSets()`, `useMedia()`, `useSettings()`; `setsRes.refresh()` etc. to refetch at once; `patchSettings(partial)`. |
| `src/state/toast.ts` | `toast.error(e)`, `toast.info(text)`, `attempt(fn)` (runs, toasts failure). |
| `src/state/confirm.ts` | `confirmDialog(title, {text, action, danger})` -> `Promise<boolean>`. |
| `src/state/hooks.ts` | `useDebouncedValue(external, commit)`, `formatTime`, `formatSize`. |
| `src/pages/` | Dashboard, Sets, Media, Settings. |
| `src/media/` | The crop tool. `CropTool.tsx`: `openCropTool(item, onDone)` opens it from any page (`<CropHost/>` sits in the shell): the original from `/api/media/{id}/source` with a box (move / resize by pointer, arrow keys), shape (one light 1:1, whole sign 1:3, free), quarter turns, in / out on a scrub bar, and the result at the sign's resolution with "Show circles"; Save (PATCH `edit`, waits for the conversion), Save as a copy (duplicate, then PATCH). `editNote(item)` for cards. `cropmath.ts` (unit-tested): box geometry and the server's rules mirrored — proxy size, `cover` fit, the round masks (`FACE_RADIUS`) — so the preview is what the sign shows. |
| `src/editor/` | The timeline editor at `/editor/:id`. `Editor.tsx` page (load, recovery, session, keys, layout); `Timeline.tsx` ruler, track rows, clips (whole-sign clips as one block over the light rows), playhead and all pointer gestures; `ClipView.tsx`; `Palette.tsx` library; `Inspector.tsx` (controls generated from `ParamSpec`s, "Shows on" light / whole sign); `ClipPicture.tsx` (a video or picture clip's still as the lights show it); `Transport.tsx`. Plain logic, unit-tested: `model.ts` (rows, placement without overlaps including the whole-sign rule, snapping, trim, split, set edits), `clipinfo.ts` (what becomes a whole-sign clip, picture fitting), `history.ts` (undo), `beats.ts`. State: `doc.ts` (draft + undo + selection + local backup), `session.ts` (`/ws/editor`, playhead clock), `dnd.ts`, `view.ts`. Drawing: `draw.ts` (ruler and grid canvases). |
| `src/styles.css` | One stylesheet, CSS variables at the top (dark theme, amber accent). |
| `mock/` | Development mock of the API (`mock_server.py`, not the real server) and `shoot.sh` (Edge headless screenshots). |

The app shell (`App.tsx`) calls `useAppBootstrap()`, which opens `/ws/sign`, loads catalog and settings and keeps the lists fresh.
