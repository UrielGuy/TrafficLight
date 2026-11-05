# TrafficLight web UI

React + TypeScript + Vite. `npm run build` type-checks and writes `web/dist`, which the Python server serves at `/`
(index.html is the fallback for client routes). From WSL: `cmd.exe /c "cd web && npm run build"` (working directory = repo root).
`npm run dev` serves on Vite's port and proxies `/api` and `/ws` to `localhost:8080`.
`npm test` runs the unit tests (vitest). `node e2e/editor.e2e.mjs` drives the editor end to end in Microsoft Edge
(playwright-core, `channel: 'msedge'`) against a running server (`TL_URL`, default `http://localhost:8091`; it needs a ready
video and audio item in the library) and writes screenshots to `e2e/shots`. `node e2e/shot.mjs <url> <w> <h> <out.png>` takes one.

## Layout

| Path | What |
| --- | --- |
| `src/api/types.ts` | Types mirroring `model.py` and the API doc: `Set` (exported as `SetData` too), `Track`, `Clip`, `Settings`, `MediaItem`, `State`, `ClipType`, `ParamSpec`, `Problem`, editor WebSocket messages (`EditorClientMessage`, `EditorStatus`). |
| `src/api/client.ts` | `api.*`: one typed function per REST route. Errors throw `ApiError` (`message` = server `detail`, `status`, raw `detail`). URL helpers `mediaThumbUrl`, `mediaFileUrl`, `renderUrl`. |
| `src/api/socket.ts` | `connectSocket(path, {onJson, onFrame, onOpen, onClose})`: reconnecting WebSocket; text -> `onJson`, binary -> `onFrame(ArrayBuffer)`. Returns `{send, close, connected}`. Use it for `/ws/editor`. |
| `src/components/SignPreview.tsx` | `<SignPreview feed={...} />` (a `FrameFeed` with `subscribe(cb)`) or `<SignPreview frame={ArrayBuffer\|Blob\|url} />`. Canvas 128x384, nearest-neighbour, newest frame wins. For the editor: build a feed from the `onFrame` callback of your socket. |
| `src/components/ui.tsx` | `Toggle`, `Slider`, `Segmented`, `Field`, `Dialog`, `Warning`, `Empty` (+ the shell's `Toasts`, `ConfirmHost`, `ConnectionBanner`). |
| `src/state/store.ts` | `createStore` / `useStore`: a 20-line external store (no state library). |
| `src/state/app.ts` | `/ws/sign` connection (`useAppState()` = `State`, `useConnected()`, `signFeed`), resources refetched when `sets_rev` / `media_rev` move: `useCatalog()` (clip types), `useSets()`, `useMedia()`, `useSettings()`; `setsRes.refresh()` etc. to refetch at once; `patchSettings(partial)`. |
| `src/state/toast.ts` | `toast.error(e)`, `toast.info(text)`, `attempt(fn)` (runs, toasts failure). |
| `src/state/confirm.ts` | `confirmDialog(title, {text, action, danger})` -> `Promise<boolean>`. |
| `src/state/hooks.ts` | `useDebouncedValue(external, commit)`, `formatTime`, `formatSize`. |
| `src/pages/` | Dashboard, Sets, Media, Settings. |
| `src/editor/` | The timeline editor at `/editor/:id`. `Editor.tsx` page (load, recovery, session, keys, layout); `Timeline.tsx` ruler, tracks, clips, playhead and all pointer gestures; `ClipView.tsx`; `Palette.tsx` library; `Inspector.tsx` (controls generated from `ParamSpec`s); `Transport.tsx`. Plain logic, unit-tested: `model.ts` (placement without overlaps, snapping, trim, split, set edits), `history.ts` (undo), `beats.ts`. State: `doc.ts` (draft + undo + selection + local backup), `session.ts` (`/ws/editor`, playhead clock), `dnd.ts`, `view.ts`. Drawing: `draw.ts` (ruler and grid canvases). |
| `src/styles.css` | One stylesheet, CSS variables at the top (dark theme, amber accent). |
| `mock/` | Development mock of the API (`mock_server.py`, not the real server) and `shoot.sh` (Edge headless screenshots). |

The app shell (`App.tsx`) calls `useAppBootstrap()`, which opens `/ws/sign`, loads catalog and settings and keeps the lists fresh.
