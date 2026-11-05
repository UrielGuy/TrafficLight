"""Fixed geometry and data paths. Everything else is in Settings (model.py)."""
from __future__ import annotations

import os
from pathlib import Path

# The sign: six 128x64 panels stacked = 128 wide, 384 tall = three 128x128 lights.
W = 128
H = 384
FACES = 3
FACE = 128            # one light's box is FACE x FACE; light i covers rows i*FACE .. (i+1)*FACE-1 (0 = top)
PANEL_W = 128
PANEL_H = 64
PANELS = 6

FACE_RADIUS = 61.0    # radius of the lit disc inside its 128 px box (3 px margin, as the old sign)
RING_WIDTH = 2.0      # outline ring of an unlit light

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = REPO_ROOT / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
WEB_DIST = REPO_ROOT / "web" / "dist"


def data_dir() -> Path:
    """Runtime data (sets, media, settings). Override with TRAFFICLIGHT_DATA."""
    p = Path(os.environ.get("TRAFFICLIGHT_DATA") or (REPO_ROOT / "data"))
    p.mkdir(parents=True, exist_ok=True)
    return p
