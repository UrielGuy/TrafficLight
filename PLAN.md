# Plan

## Decisions (2026-11-04)

* **Controller:** Colorlight 5A-75B with its stock firmware; frames go out as
  raw Ethernet (Npcap on Windows, AF_PACKET on the Pi). One-time panel setup
  in LEDVision. Hardware exists but is not wired yet → build against a
  preview / packet tests first, bring-up with the user later.
* **Audio:** output only. Music and video sound on the timeline play on an
  output device chosen in the UI. Tempo is set per set (no live beat detection).
* **Stack:** Python backend (FastAPI, numpy, Pillow, PyAV, sounddevice);
  React + TypeScript + Vite UI, built to `web/dist` and served by the backend.
* **Model:** a set is a timeline (tracks for the three lights, the whole
  sign, audio) of clips; every clip type is a pluggable renderer published
  through `/api/catalog`. The old sign's keyframes, loop sections and 2-bit
  colours are not carried over; its content types are (text, figures, beat
  animations, clock).
* **Assumed:** Raspberry Pi 4 or 5, 64-bit OS. Old sets and `.anm` files are
  not imported (the source videos can be uploaded as they are).

## Work packages

Phase 0 — contracts (done by the lead): repo, environments, `model.py`,
`render/base.py`, `render/color.py`, `output/base.py`, `audio/base.py`,
`docs/ARCHITECTURE.md`.

Wave 1 — in parallel, each owns its files only:

| # | Package | Owns | Size |
| --- | --- | --- | --- |
| 1 | Core: store, schedule, player, engine, HTTP/WebSocket server | `store.py`, `schedule.py`, `player.py`, `engine.py`, `server/`, `__main__.py` | large |
| 2 | Colorlight output driver, panel mapping, test patterns, bring-up guide | `output/` (not `base.py`), `tools/testpattern.py`, `docs/HARDWARE.md` | large |
| 3 | Light, text (Hebrew/English fitting), figures | `render/basic/`, `assets/fonts/` | large |
| 4 | Beat animations and clock | `render/fx/` | medium |
| 5 | Media library, video / image / audio clip types | `media/`, `render/media/` | large |
| 6 | Audio engine (device choice, mixer, sync) | `audio/engine.py` | medium |
| 7 | Web UI shell: dashboard, sets, media library, settings | `web/` (not `web/src/editor/`) | medium |

Wave 2 — after wave 1 is integrated:

| # | Package | Owns | Size |
| --- | --- | --- | --- |
| 8 | Timeline editor | `web/src/editor/` | large |
| 9 | Run / install scripts for Windows and the Pi, README | `scripts/`, `README.md` | small |
| 10 | Integration: real server + UI in a browser, a demo set, fix what the seams reveal | everything | lead |

Later, with the user: hardware bring-up (LEDVision setup, wiring, mapping,
brightness, refresh), performance check on the Pi.
