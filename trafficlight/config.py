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
# 1/32 scan: a row address lights panel row a (the R1/G1/B1 group) and row a + SCAN_ROWS (R2/G2/B2) together.
SCAN_ROWS = PANEL_H // 2

# The receiver card's image: one 64-row band per HUB75 connector, band b = connector J(b+1), from the top.
# Bands without a panel are sent black; Settings.output.panel_order says which band each panel is on.
PORTS = 8
CARD_H = PORTS * PANEL_H

FACE_RADIUS = FACE / 2    # the lit disc fills its 128 px box edge to edge
RING_WIDTH = 2.0      # outline ring of an unlit light
TEXT_RADIUS = 61.0    # text is fitted inside this: a small margin to the light's edge, and the old sign's layouts

# Link imports fetch up to this ("480" of 480p) unless asked otherwise. The sign is 128x384, so 480 lines
# cover even a whole-sign crop of the full picture height without enlarging it; more only helps a crop that
# zooms into a small part of the picture, and costs download time and disk.
IMPORT_HEIGHT = 480
# The import page asks "are you sure?" before downloading more than this many seconds of a video: a show
# clip is rarely longer, and a whole concert costs minutes of download and gigabytes of disk.
IMPORT_WARN_SECONDS = 240.0

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = REPO_ROOT / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
WEB_DIST = REPO_ROOT / "web" / "dist"


def data_dir() -> Path:
    """Runtime data (sets, media, settings). Override with TRAFFICLIGHT_DATA."""
    p = Path(os.environ.get("TRAFFICLIGHT_DATA") or (REPO_ROOT / "data"))
    p.mkdir(parents=True, exist_ok=True)
    return p
