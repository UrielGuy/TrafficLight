"""The fonts of the text clip: the TrueType / OpenType files in assets/fonts.

The old sign baked one composite bitmap font per size (its tools/gen_font.py):
the line box and the digits, punctuation and space of DejaVu Sans Bold, Latin
letters from High-School, Hebrew letters from Reisinger Michal, each scaled so
its letters stand as tall as DejaVu's capitals on the same baseline. A
`FontSet` rebuilds that at any pixel size from the outlines, anti-aliased.

Font choice "auto" is exactly that mix. Choosing a file puts it first: every
character it has comes from it (matched to the same height and baseline), the
rest falls back to the automatic mix.

Pillow is told to use its basic layout engine everywhere: glyphs are drawn
one at a time in visual order (textlayout.py does the bidi), and libraqm,
where it exists, must not reorder them a second time.
"""
from __future__ import annotations

import statistics
import threading
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ... import config

BASE_FILE = "DejaVuSans-Bold.ttf"            # line box, digits, punctuation, anything else
AUTO_LATIN_FILE = "High-School.ttf"          # A-Z, a-z (unicase: lower case draws capitals)
AUTO_HEBREW_FILE = "Reisinger-Michal-Regular.otf"

HEBREW_LETTERS = frozenset(list(range(0x05D0, 0x05EB)) + [0x05F3, 0x05F4])
LATIN_LETTERS = frozenset(list(range(0x41, 0x5B)) + list(range(0x61, 0x7B)))
# Letters that sit on the baseline at full letter height: used to match heights.
_REF_HEBREW = [0x05D1, 0x05D4, 0x05DE, 0x05E1, 0x05E9, 0x05EA, 0x05D8, 0x05E8]
_REF_LATIN = [ord(c) for c in "HEFILTNZ"]
_MEASURE_PX = 256
_INK_LEVEL = 128     # coverage that counts as ink for measuring (the old 1-bpp threshold)

_LABELS = {
    "auto": "Automatic (as the old sign)",
    "DejaVuSans-Bold": "DejaVu Sans Bold",
    "High-School": "High-School (Latin only)",
    "Reisinger-Michal-Regular": "Reisinger Michal",
    "heshbon-bold-alefalefalef": "Heshbon Bold (Hebrew)",
    "heshbon-regular-alefalefalef": "Heshbon Regular (Hebrew)",
}

# Faces whose letters the old sign respaced: each starts at the pen and is followed by an even gap
# (em fraction of the face's own size). High-School's own advances leave its letters cramped on the LEDs.
_EVEN_GAP = {"High-School": 64 / 1024}

# FreeType faces are not safe to use from two threads at once (render thread, validate in the server).
_FT_LOCK = threading.RLock()


class Face:
    """One font file: glyph coverage and its size / baseline match to the base font."""

    def __init__(self, path: Path):
        self.path = path
        self.id = path.stem
        self._fonts: dict[float, ImageFont.FreeTypeFont] = {}
        self._has: dict[int, bool] = {}
        self._notdef = None
        self.scale = 1.0     # pixel size of this face per em pixel of the base font
        self.dy = 0.0        # baseline correction, in em of the base font (+ = down)
        self.short = 0.0     # px its letters stand below the caps (see _match)
        self.cap_em = 0.0    # the base font's cap height per em

    def font(self, px: float) -> ImageFont.FreeTypeFont:
        px = round(px * 4) / 4
        f = self._fonts.get(px)
        if f is None:
            f = self._fonts[px] = ImageFont.truetype(str(self.path), max(1.0, px),
                                                    layout_engine=ImageFont.Layout.BASIC)
        return f

    def _sig(self, cp: int):
        f = self.font(48)
        m = f.getmask(chr(cp))
        return m.size, bytes(m), round(f.getlength(chr(cp)), 2)

    def has(self, cp: int) -> bool:
        """True if the font maps cp (its glyph differs from the .notdef a private-use code point gets)."""
        v = self._has.get(cp)
        if v is None:
            with _FT_LOCK:
                if self._notdef is None:
                    self._notdef = self._sig(0xE000)
                v = self._has[cp] = self._sig(cp) != self._notdef
        return v


def ink_box(face_font: ImageFont.FreeTypeFont, ch: str):
    """(alpha uint8 (h, w), x0, y0, ink) for one glyph drawn with its origin at (0, 0) on the
    baseline; ink = (left, top, right, bottom) of the coverage >= _INK_LEVEL, or None if blank."""
    mask, (ox, oy) = face_font.getmask2(ch, mode="L", anchor="ls")
    w, h = mask.size
    if w == 0 or h == 0:
        return np.zeros((0, 0), np.uint8), 0, 0, None
    a = np.asarray(mask, dtype=np.uint8).reshape(h, w)
    on = a >= _INK_LEVEL
    if not on.any():
        on = a > 0
    if not on.any():
        return a, ox, oy, None
    rows = np.flatnonzero(on.any(axis=1))
    cols = np.flatnonzero(on.any(axis=0))
    return a, ox, oy, (ox + int(cols[0]), oy + int(rows[0]), ox + int(cols[-1]) + 1, oy + int(rows[-1]) + 1)


def _match(face: Face, base: Face) -> None:
    """Scale `face` so its letters stand as tall as the base font's capitals and sit on its baseline."""
    if face is base:
        return
    refs = [cp for cp in _REF_HEBREW if face.has(cp)]
    # The old generator matched Hebrew by trying sizes upwards and keeping the first whose letters
    # reached the cap height in whole pixels: they stood about half a pixel short at every size.
    face.short = 0.5 if len(refs) >= 3 else 0.0
    if len(refs) < 3:
        refs = [cp for cp in _REF_LATIN if face.has(cp)]
    if len(refs) < 3:
        return
    with _FT_LOCK:
        cap = ink_box(base.font(_MEASURE_PX), "H")[3]
        boxes = [ink_box(face.font(_MEASURE_PX), chr(cp))[3] for cp in refs]
    boxes = [b for b in boxes if b is not None]
    if cap is None or not boxes:
        return
    h = statistics.median(b[3] - b[1] for b in boxes)
    if h > 0:
        face.scale = (cap[3] - cap[1]) / h
        face.cap_em = (cap[3] - cap[1]) / _MEASURE_PX
    face.dy = -statistics.median(b[3] for b in boxes) * face.scale / _MEASURE_PX


# High-School's K and X differ from H, R from A and V from U only by small corner cuts that
# vanish on the LEDs ("FOX" reads "FOH", "OVER" "OUER"). The old sign redrew them in its style:
# the H's height, width and stems with real diagonals (R: the P's bowl and a diagonal leg).
_REDRAW_FACES = frozenset(["High-School"])
_REDRAWN = frozenset(ord(c) for c in "KRVXkrvx")
_SS = 8


def _bits(font: ImageFont.FreeTypeFont, ch: str):
    """(bool ink mask cropped to its box, box top relative to the baseline) or None."""
    a, ox, oy, ink = ink_box(font, ch)
    if ink is None:
        return None
    l, t, r, b = ink
    return a[t - oy:b - oy, l - ox:r - ox] >= _INK_LEVEL, t


def _redrawn(face: Face, cp: int, px: float):
    """ink_box() of K, R, V or X drawn _SS times larger from the H (and P) and filtered down."""
    with _FT_LOCK:
        big = face.font(px * _SS)
        hb, pb = _bits(big, "H"), _bits(big, "P")
    if hb is None or pb is None:
        return None
    hm, top = hb
    h, w = hm.shape
    gaps = np.flatnonzero(~hm[0])
    s = int(gaps[0]) if gaps.size else max(1, w // 5)               # stem width
    bars = np.flatnonzero(hm.all(axis=1))                           # the crossbar's rows
    waist = (bars[0] + bars[-1] + 1) / 2 if bars.size else h / 2
    cw, ch_ = -(-w // _SS) * _SS, -(-h // _SS) * _SS
    img = Image.new("L", (cw, ch_), 0)
    d = ImageDraw.Draw(img)

    def line(x0, y0, x1, y1):
        d.line([(x0, y0), (x1, y1)], fill=255, width=s)

    c = chr(cp).upper()
    if c == "K":
        d.rectangle([0, 0, s - 1, h - 1], fill=255)
        line(s * 0.6, waist, w - s * 0.55, s * 0.3)
        line(s * 1.2, waist - s * 0.2, w - s * 0.55, h - s * 0.3)
    elif c == "X":
        line(s * 0.55, s * 0.3, w - s * 0.55, h - s * 0.3)
        line(w - s * 0.55, s * 0.3, s * 0.55, h - s * 0.3)
    elif c == "V":
        line(s * 0.55, s * 0.3, w / 2, h - s * 0.45)
        line(w - s * 0.55, s * 0.3, w / 2, h - s * 0.45)
    else:   # R
        pm = pb[0]
        full = np.flatnonzero(pm.sum(axis=1) >= pm.shape[1] - _SS)
        full = full[full > 0]
        bowl_end = int(full[-1]) + 1 if full.size else round(waist)
        bowl = pm[:min(bowl_end, h), :min(pm.shape[1], w)]
        canvas = np.asarray(img, dtype=np.uint8).copy()
        canvas[:bowl.shape[0], :bowl.shape[1]][bowl] = 255
        img = Image.fromarray(canvas)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, s - 1, h - 1], fill=255)
        line(w * 0.45, bowl_end - s * 0.5, w - s * 0.55, h - s * 0.3)
    a = np.asarray(img.reduce(_SS), dtype=np.uint8)
    oy = round(top / _SS)
    on = a >= _INK_LEVEL
    if not on.any():
        return None
    rows, cols = np.flatnonzero(on.any(axis=1)), np.flatnonzero(on.any(axis=0))
    return a, 0, oy, (int(cols[0]), oy + int(rows[0]), int(cols[-1]) + 1, oy + int(rows[-1]) + 1)


class Glyph:
    __slots__ = ("alpha", "x0", "y0", "adv", "ink")

    def __init__(self, alpha, x0, y0, adv, ink):
        self.alpha, self.x0, self.y0, self.adv, self.ink = alpha, x0, y0, adv, ink


class FontSet:
    """The composite font for one choice at one em size (pixels of the base font)."""

    def __init__(self, lib: "FontLibrary", choice: str, size: float):
        self.lib, self.choice, self.size = lib, choice, size
        base = lib.base
        if base is not None:
            asc, desc = base.font(size).getmetrics()
        else:
            asc, desc = round(size * 0.93), round(size * 0.24)
        self.ascent, self.descent = asc, desc
        self.line_height = asc + desc
        self._glyphs: dict[int, Optional[Glyph]] = {}

    def glyph(self, cp: int) -> Optional[Glyph]:
        """None if no font has cp."""
        if cp in self._glyphs:
            return self._glyphs[cp]
        face = self.lib.route(self.choice, cp)
        g = None
        if face is not None:
            with _FT_LOCK:
                px = self.size * face.scale
                if face.short and face.cap_em:
                    px *= max(0.5, 1 - face.short / (face.cap_em * self.size))
                f = face.font(px)
                drawn = _redrawn(face, cp, px) if face.id in _REDRAW_FACES and cp in _REDRAWN else None
                a, x0, y0, ink = drawn or ink_box(f, chr(cp))
                adv = f.getlength(chr(cp))
            dy = round(face.dy * self.size)
            gap = _EVEN_GAP.get(face.id)
            if gap is not None and ink is not None:
                x0 -= ink[0]
                adv = ink[2] - ink[0] + max(1, round(gap * f.size))
                ink = (0, ink[1], ink[2] - ink[0], ink[3])
            if ink is not None:
                ink = (ink[0], ink[1] + dy, ink[2], ink[3] + dy)
            g = Glyph(a, x0, y0 + dy, adv, ink)
        self._glyphs[cp] = g
        return g

    def space(self) -> float:
        g = self.glyph(32)
        return g.adv if g is not None else self.size / 3


class FontLibrary:
    """Every font file in a directory, matched to the base font."""

    def __init__(self, directory: Path):
        self.faces: dict[str, Face] = {}
        for p in font_files(directory):
            try:
                face = Face(p)
                face.font(16)
            except OSError:
                continue
            self.faces[face.id] = face
        self.base = self.faces.get(Path(BASE_FILE).stem) or next(iter(self.faces.values()), None)
        self.latin = self.faces.get(Path(AUTO_LATIN_FILE).stem)
        self.hebrew = self.faces.get(Path(AUTO_HEBREW_FILE).stem)
        if self.base is not None:
            for face in self.faces.values():
                _match(face, self.base)
        self._sets: dict[tuple[str, float], FontSet] = {}
        self._lock = threading.Lock()

    def normalise_choice(self, choice) -> str:
        return choice if isinstance(choice, str) and choice in self.faces else "auto"

    def route(self, choice: str, cp: int) -> Optional[Face]:
        chosen = self.faces.get(choice)
        if chosen is not None and chosen.has(cp):
            return chosen
        if cp in HEBREW_LETTERS and self.hebrew is not None and self.hebrew.has(cp):
            return self.hebrew
        if cp in LATIN_LETTERS and self.latin is not None and self.latin.has(cp):
            return self.latin
        if self.base is not None and self.base.has(cp):
            return self.base
        return None

    def fontset(self, choice: str, size: float) -> FontSet:
        key = (choice, size)
        with self._lock:
            fs = self._sets.get(key)
            if fs is None:
                fs = self._sets[key] = FontSet(self, choice, size)
            return fs


_LIB: Optional[FontLibrary] = None
_LIB_LOCK = threading.Lock()


def library() -> FontLibrary:
    global _LIB
    with _LIB_LOCK:
        if _LIB is None:
            _LIB = FontLibrary(config.FONTS_DIR)
        return _LIB


def font_files(directory: Path) -> list[Path]:
    """The font files in a directory, in the same order on every platform."""
    try:
        paths = [p for p in directory.iterdir() if p.suffix.lower() in (".ttf", ".otf")]
    except OSError:
        return []
    return sorted(paths, key=lambda p: p.name.lower())


def font_options() -> list[dict[str, str]]:
    """The font select's options without loading the fonts (used at import time): automatic, then each file."""
    out = [{"value": "auto", "label": _LABELS["auto"]}]
    for p in font_files(config.FONTS_DIR):
        out.append({"value": p.stem, "label": _LABELS.get(p.stem, p.stem.replace("-", " ").replace("_", " "))})
    return out
