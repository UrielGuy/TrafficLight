"""Text fitted inside a light: the old sign's TextLayout.cpp on TrueType outlines.

Same rules as the old engine (see TehilaStoplight tools/stoplight_render.py):
nikud and direction marks stripped, words split on spaces, "\\n" forces a
break after the word before it, each line put in visual order by a simple
bidi (Hebrew runs reversed, brackets mirrored inside them, digits and Latin
left to right), then for every em size from the largest down: the break
pattern with the fewest lines, then the smallest worst fill (ink width over
the circle's chord at the line's ink rows), then the first pattern; up to 14
words every pattern is tried, beyond that a greedy fill. The ink block is
centred vertically, each line horizontally on its ink.

Measurements are in pixels of the box (anti-aliased glyph coverage >= 50 %
counts as ink, like the old 1-bpp threshold); positions are snapped to whole
pixels so stems stay crisp.
"""
from __future__ import annotations

import math
import threading
import unicodedata
from collections import OrderedDict
from typing import Optional

import numpy as np

from ... import config
from .fonts import FontSet, Glyph, library

LADDER = (56, 48, 42, 36, 30, 26, 22, 18, 16, 14, 12, 11)   # em sizes for a FACE-px light, largest first
MAX_LINES = 8
MAX_ENUM_WORDS = 14

_MIRROR = {40: 41, 41: 40, 91: 93, 93: 91, 123: 125, 125: 123, 60: 62, 62: 60}


# ---------------------------------------------------------------- text -> words
def _is_hebrew(cp: int) -> bool:
    return 0x0590 <= cp <= 0x05FF


def _is_nikud(cp: int) -> bool:
    return 0x0591 <= cp <= 0x05C7


def _is_strong_l(cp: int) -> bool:
    return 65 <= cp <= 90 or 97 <= cp <= 122 or 48 <= cp <= 57 or 0x00C0 <= cp <= 0x024F


def clean(text: str) -> list[int]:
    """Code points to lay out: nikud, direction marks, CR and control characters dropped, tab / nbsp = space."""
    out = []
    for ch in text:
        cp = ord(ch)
        if 0xFB1D <= cp <= 0xFB4F:      # Hebrew presentation forms (letter + point in one code point)
            out.extend(c for c in (ord(x) for x in unicodedata.normalize("NFKD", ch)) if not _is_nikud(c))
            continue
        if _is_nikud(cp) or cp in (0x200E, 0x200F, 0xFEFF, 13):
            continue
        if cp in (9, 0xA0):
            cp = 32
        if cp < 0x20 and cp != 10:
            continue
        out.append(cp)
    return out


def split_words(cps: list[int]) -> tuple[list[tuple[int, ...]], list[bool]]:
    """Words and, per word, whether the user forced a line break after it (several newlines count as one)."""
    words: list[tuple[int, ...]] = []
    forced: list[bool] = []
    cur: list[int] = []
    for cp in cps:
        if cp in (32, 10):
            if cur:
                words.append(tuple(cur))
                forced.append(False)
                cur = []
            if cp == 10 and words:
                forced[-1] = True
        else:
            cur.append(cp)
    if cur:
        words.append(tuple(cur))
        forced.append(False)
    if forced:
        forced[-1] = False
    return words, forced


def bidi_visual(cps) -> list[int]:
    """One line in display order (left to right). Text with no Hebrew is left as it is."""
    n = len(cps)
    if n == 0 or not any(_is_hebrew(cp) for cp in cps):
        return list(cps)
    cls = ["R" if _is_hebrew(cp) else "L" if _is_strong_l(cp) else "N" for cp in cps]
    for i in range(n):
        if cls[i] != "N":
            continue
        prev = nxt = "R"
        for k in range(i - 1, -1, -1):
            if cls[k] != "N":
                prev = cls[k]
                break
        for k in range(i + 1, n):
            if cls[k] != "N":
                nxt = cls[k]
                break
        cls[i] = prev if prev == nxt else "R"
    runs, i = [], 0
    while i < n:
        j = i
        while j < n and cls[j] == cls[i]:
            j += 1
        runs.append((i, j))
        i = j
    out: list[int] = []
    for a, b in reversed(runs):
        if cls[a] == "R":
            out.extend(_MIRROR.get(cps[k], cps[k]) for k in range(b - 1, a - 1, -1))
        else:
            out.extend(cps[a:b])
    return out


# ---------------------------------------------------------------- measuring
class _Line:
    """A line's glyphs in visual order with their pen x, and its ink box (y relative to the baseline)."""
    __slots__ = ("items", "l", "r", "t", "b", "empty")

    def __init__(self, fs: FontSet, logical):
        self.items: list[tuple[Glyph, int]] = []
        self.empty = True
        self.l = self.r = self.t = self.b = 0
        pen = 0.0
        for cp in bidi_visual(logical):
            g = fs.glyph(cp)
            if g is None:
                continue
            x = round(pen)
            if g.ink is not None:
                l, t, r, b = x + g.ink[0], g.ink[1], x + g.ink[2], g.ink[3]
                if self.empty:
                    self.l, self.t, self.r, self.b, self.empty = l, t, r, b, False
                else:
                    self.l, self.t, self.r, self.b = min(self.l, l), min(self.t, t), max(self.r, r), max(self.b, b)
            self.items.append((g, x))
            pen += g.adv


def _chord_half(r: float, y0: float, y1: float) -> int:
    """Half the chord at the row farther from the centre, in whole pixels (ink is whole pixels wide)."""
    dy = max(abs(y0), abs(y1))
    return 0 if dy >= r else math.floor(math.sqrt(r * r - dy * dy))


class _Search:
    """The best break pattern for one font size (bestForFont)."""

    def __init__(self, fs: FontSet, words, forced, cx: float, cy: float, r: float):
        self.fs, self.words, self.forced, self.cx, self.cy, self.r = fs, words, forced, cx, cy, r
        self.k = len(words)
        self._mets: dict[tuple[int, int], _Line] = {}

    def met(self, a: int, b: int) -> _Line:
        m = self._mets.get((a, b))
        if m is None:
            line: list[int] = []
            for i in range(a, b):
                if i > a:
                    line.append(32)
                line.extend(self.words[i])
            m = self._mets[(a, b)] = _Line(self.fs, line)
        return m

    def place(self, lines, build: bool = False):
        """Worst fill of these lines (None if one does not fit); with build, (fill, [(glyph, x, y)])."""
        fs, cy, r = self.fs, self.cy, self.r
        LH, asc = fs.line_height, fs.ascent
        mets = [self.met(a, b) for a, b in lines]
        tops = [i * LH + asc + m.t for i, m in enumerate(mets) if not m.empty]
        if not tops:
            return None
        bots = [i * LH + asc + m.b for i, m in enumerate(mets) if not m.empty]
        top = round(cy - (min(tops) + max(bots)) / 2)
        worst = 0.0
        glyphs = [] if build else None
        for i, m in enumerate(mets):
            if m.empty:
                continue
            base = top + i * LH + asc
            half = _chord_half(r, base + m.t - cy, base + m.b - cy)
            ink_w = m.r - m.l
            if half <= 0 or ink_w > 2 * half:
                return None
            worst = max(worst, ink_w / (2 * half))
            if build:
                origin = round(self.cx - ink_w / 2 - m.l)
                glyphs.extend((g, origin + x + g.x0, base + g.y0) for g, x in m.items)
        return (worst, glyphs) if build else worst

    def best(self, max_n: int):
        min_lines = 1 + sum(1 for i in range(self.k - 1) if self.forced[i])
        if min_lines > max_n:
            return None
        if self.k <= MAX_ENUM_WORDS:
            return self._enumerate(max_n, min_lines)
        return self._greedy(max_n)

    def _enumerate(self, max_n: int, min_lines: int):
        # The fewest lines, then the smallest worst fill, then the lowest break pattern
        # (bit i = a break after word i): the minimum of (lines, fill, pattern).
        k, forced, fs = self.k, self.forced, self.fs
        adv = []
        for wd in self.words:
            a = 0.0
            for cp in wd:
                g = fs.glyph(cp)
                if g is not None:
                    a += g.adv
            adv.append(a)
        space = fs.space()
        limit = 2 * self.r + 4
        two_r = 2 * self.r
        ok: dict[tuple[int, int], bool] = {}

        def line_ok(a, b):
            v = ok.get((a, b))
            if v is None:
                m = self.met(a, b)
                v = ok[(a, b)] = m.empty or (m.r - m.l) <= two_r
            return v

        for n in range(min_lines, max_n + 1):
            best = None
            stack: list[tuple[int, int]] = []

            def rec(a, left, pat):
                nonlocal best
                if left == 1:
                    for i in range(a, k - 1):
                        if forced[i]:
                            return
                    if sum(adv[a:k]) + space * (k - 1 - a) > limit or not line_ok(a, k):
                        return
                    stack.append((a, k))
                    fill = self.place(stack)
                    stack.pop()
                    if fill is not None and (best is None or (fill, pat) < best[:2]):
                        best = (fill, pat, list(stack) + [(a, k)])
                    return
                lw = -space
                for b in range(a + 1, k - left + 2):
                    if b > a + 1 and forced[b - 2]:
                        break
                    lw += adv[b - 1] + space
                    if lw > limit:
                        break
                    if not line_ok(a, b):
                        continue
                    stack.append((a, b))
                    rec(b, left - 1, pat | (1 << (b - 1)))
                    stack.pop()

            rec(0, n, 0)
            if best is not None:
                return best[2]
        return None

    def _greedy(self, max_n: int):
        k, forced, r, cy = self.k, self.forced, self.r, self.cy
        LH = self.fs.line_height
        found, best_lines, best_fill, out = False, 0, 0.0, None
        for n in range(1, max_n + 1):
            block_top = cy - (n * LH) // 2
            lines, w, bad = [], 0, False
            while w < k:
                if len(lines) >= n:
                    bad = True
                    break
                li = len(lines)
                limit = 2 * _chord_half(r, block_top + li * LH + LH // 4 - cy, block_top + (li + 1) * LH - LH // 4 - cy)
                end = w + 1
                while end < k and not forced[end - 1]:
                    m = self.met(w, end + 1)
                    if (0 if m.empty else m.r - m.l) > limit:
                        break
                    end += 1
                lines.append((w, end))
                w = end
            if bad:
                continue
            fill = self.place(lines)
            if fill is None:
                continue
            nl = len(lines)
            if not found or nl < best_lines or (nl == best_lines and fill < best_fill):
                found, best_lines, best_fill, out = True, nl, fill, lines
        return out


# ---------------------------------------------------------------- result
class Layout:
    """A fitted text: `alpha` is the ink coverage (uint8, the box's shape) or None when nothing fits.
    `size` is the em size chosen (in px of a FACE-px light), `lines` the logical text of each line."""

    def __init__(self, error: str = "", missing: str = ""):
        self.error = error
        self.missing = missing          # characters no font has (left out)
        self.size = 0
        self.lines: list[str] = []
        self.alpha: Optional[np.ndarray] = None

    @property
    def ok(self) -> bool:
        return self.alpha is not None


def size_ladder(max_size: int) -> list[int]:
    """The em sizes tried, largest first, under the cap (0 = no cap; a cap below the smallest size = the smallest)."""
    if max_size and max_size > 0:
        cap = max(max_size, LADDER[-1])
        return [s for s in LADDER if s <= cap]
    return list(LADDER)


def layout_text(text: str, font: str = "auto", max_size: int = 0,
                width: int = config.FACE, height: int = config.FACE) -> Layout:
    """Fit `text` in the disc of a width x height box (uncached; see get_layout)."""
    lib = library()
    choice = lib.normalise_choice(font)
    cps = clean(text if isinstance(text, str) else str(text or ""))
    missing = "".join(dict.fromkeys(chr(cp) for cp in cps if cp not in (10, 32) and lib.route(choice, cp) is None))
    if missing:
        cps = [cp for cp in cps if chr(cp) not in missing]
    words, forced = split_words(cps)
    if not words:
        return Layout("empty", missing)
    scale = min(width, height) / config.FACE
    r = config.TEXT_RADIUS * scale
    cx, cy = width / 2, height / 2
    for size in size_ladder(max_size):
        fs = lib.fontset(choice, size * scale)
        max_n = min(int(2 * r // fs.line_height), MAX_LINES)
        s = _Search(fs, words, forced, cx, cy, r)
        lines = s.best(max_n)
        if lines is None:
            continue
        _, glyphs = s.place(lines, build=True)
        out = Layout("", missing)
        out.size = size
        out.lines = [" ".join("".join(chr(c) for c in w) for w in words[a:b]) for a, b in lines]
        out.alpha = _raster(glyphs, width, height)
        return out
    return Layout("too long", missing)


def _raster(glyphs, width: int, height: int) -> np.ndarray:
    a = np.zeros((height, width), np.uint8)
    for g, x, y in glyphs:
        h, w = g.alpha.shape
        xa, xb, ya, yb = max(x, 0), min(x + w, width), max(y, 0), min(y + h, height)
        if xa < xb and ya < yb:
            np.maximum(a[ya:yb, xa:xb], g.alpha[ya - y:yb - y, xa - x:xb - x], out=a[ya:yb, xa:xb])
    return a


_CACHE: "OrderedDict[tuple, Layout]" = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 256


def get_layout(text: str, font: str = "auto", max_size: int = 0,
               width: int = config.FACE, height: int = config.FACE) -> Layout:
    """layout_text, cached per (text, font, cap, box). The result is shared: do not write to it."""
    key = (text, font, max_size, width, height)
    with _CACHE_LOCK:
        lay = _CACHE.get(key)
        if lay is not None:
            _CACHE.move_to_end(key)
            return lay
    lay = layout_text(text, font, max_size, width, height)
    with _CACHE_LOCK:
        _CACHE[key] = lay
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return lay
