# TrafficLight — guide for agents

Show controller for a Burning Man art piece: a traffic light whose three
lights are six 128×64 HUB75 panels (128×384, 24-bit colour) on a Colorlight
5A-75B receiver card (stock firmware, raw Ethernet). Runs on Windows and on a
Raspberry Pi. Web UI with a timeline editor.

**Read `docs/ARCHITECTURE.md` first**: it is the contract (data model, module
interfaces, HTTP and WebSocket API). `PLAN.md` has the work breakdown.

The predecessor is `../TehilaStoplight` (ESP32 firmware, 2-bit colour). Its
features are being carried over; its docs (`CLAUDE.md`, `docs/ARCHITECTURE.md`)
and `tools/stoplight_render.py` (a Python port of its text engine, face
renderer and beat effects) are the reference for behaviour. Read-only: never
change anything there.

## Environments

The repo lives on the Windows drive and is worked on from WSL.

* **Linux (WSL), stands in for the Pi:** `~/.venvs/trafficlight/bin/python`
  (Python 3.12, all of `requirements-dev.txt`). No PortAudio in WSL: the
  `sounddevice` import fails there, so audio code must degrade (NullAudio) and
  be tested with a fake backend.
* **Windows:** `.venv\Scripts\python.exe` (Python 3.13, same packages; sound
  devices and Npcap work here). From WSL:
  `cmd.exe /c ".venv\Scripts\python.exe -m pytest tests\test_x.py -q"`
  (the working directory must be the repo).
* **Node** exists only on Windows (v25): from WSL run
  `cmd.exe /c "cd web && npm run build"`.
* New Python dependency: install it in both environments and add it to
  `requirements.txt`.

## Commands

```
~/.venvs/trafficlight/bin/python -m pytest -q             # all tests (Linux)
~/.venvs/trafficlight/bin/python -m trafficlight           # server on http://localhost:8080
cmd.exe /c "cd web && npm run build"                        # UI -> web/dist (served by the server)
```

## Conventions

* A frame is `uint8 (384, 128, 3)` RGB. Geometry constants come from
  `trafficlight/config.py`; never write 128 / 384 / 61 as literals elsewhere.
* Colours are 24-bit. Anti-alias shapes and text.
* Renderers are pure functions of `RenderContext` and must be fast (numpy on
  whole arrays, no per-pixel Python loops, cache per size/params).
* The same code runs on Windows and Linux: `pathlib`, no shell-outs, no
  platform checks outside `output/` and `audio/`.
* Runtime data goes under `config.data_dir()`; tests set `TRAFFICLIGHT_DATA`
  to a temp directory and never touch `data/`.
* Tests live in `tests/test_<area>*.py` and run without hardware, sound card
  or network.
* Comments say why, not what. No dead code, no TODO placeholders that pretend
  to be features.
