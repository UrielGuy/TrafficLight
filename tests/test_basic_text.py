"""render/basic: the text clip and its layout (same breaks as the old sign's engine)."""
import time

import numpy as np
import pytest

from trafficlight import config, model
from trafficlight.render import get_renderer
from trafficlight.render.base import RenderContext, RenderEnv
from trafficlight.render.basic import textlayout as tl

ENV = RenderEnv(settings=model.Settings())


def ctx(t=0.0, face=0, bpm=120.0):
    return RenderContext(t=t, clip_duration=4.0, set_time=t, bpm=bpm, beat=t * bpm / 60, clip_beat=t * bpm / 60,
                         width=config.FACE, height=config.FACE, face=face, face_color=(255, 0, 0), seed=1)


# Size and lines as tools/stoplight_render.py of the old sign lays them out.
OLD_SIGN = [
    ("STOP", 48, ["STOP"]),
    ("GO", 56, ["GO"]),
    ("Hello world", 30, ["Hello", "world"]),
    ("Welcome to the playa", 18, ["Welcome to", "the playa"]),
    ("The quick brown fox jumps over the lazy dog", 16, ["The quick", "brown", "fox jumps", "over the", "lazy dog"]),
    ("Dance like nobody is watching", 18, ["Dance like", "nobody is", "watching"]),
    ("DON'T WALK", 36, ["DON'T", "WALK"]),
    ("עצור", 48, ["עצור"]),
    ("שלום עולם", 36, ["שלום", "עולם"]),
    ("ברוכים הבאים לפלאיה", 22, ["ברוכים", "הבאים", "לפלאיה"]),
    ("אהבה היא התשובה לכל השאלות", 18, ["אהבה היא", "התשובה לכל", "השאלות"]),
    ("עברית (סוגריים) כאן", 26, ["עברית", "(סוגריים)", "כאן"]),
    ("שלום Burning Man", 26, ["שלום", "Burning", "Man"]),
    ("Burning Man 2026", 22, ["Burning", "Man 2026"]),
    ("Line one\nline two", 22, ["Line one", "line two"]),
    ("a\nb\nc", 30, ["a", "b", "c"]),
    ("שָׁלוֹם", 42, ["שלום"]),
]


@pytest.mark.parametrize("text,size,lines", OLD_SIGN)
def test_layout_matches_the_old_sign(text, size, lines):
    lay = tl.layout_text(text)
    assert lay.ok and (lay.size, lay.lines) == (size, lines)


def test_forced_breaks_and_cap():
    assert tl.layout_text("GO\nGO").lines == ["GO", "GO"]
    assert tl.layout_text("one\n\n\ntwo").lines == ["one", "two"]
    capped = tl.layout_text("STOP", max_size=22)
    assert capped.ok and capped.size == 22
    assert tl.layout_text("STOP", max_size=40).size == 36          # the largest size of the ladder under the cap
    assert tl.layout_text("STOP", max_size=3).size == tl.LADDER[-1]


def test_bidi_and_cleaning():
    vis = lambda s: "".join(chr(c) for c in tl.bidi_visual([ord(ch) for ch in s]))
    assert vis("abc (d)") == "abc (d)"
    assert vis("אב (ג)") == "(ג) בא"                                # reversed, brackets mirrored
    assert vis("שלום abc 12") == "abc 12 םולש"                      # digits are left-to-right, as on the old sign
    assert vis("אב 12 גד") == "דג 12 בא"                            # digits stay left to right
    assert tl.clean("שָׁלוֹם‏\tx") == [ord(c) for c in "שלום x"]
    assert tl.clean("שׁ") == [ord("ש")]                           # presentation form, point dropped


def test_hebrew_reads_right_to_left_on_screen():
    # lamed is the only letter with an ascender: written first, it must stand at the right
    a = tl.layout_text("לא").alpha > 127
    rows, cols = np.flatnonzero(a.any(axis=1)), np.flatnonzero(a.any(axis=0))
    mid = (cols[0] + cols[-1]) / 2
    ascender = np.flatnonzero(a[rows[0]:rows[0] + 3].any(axis=0))
    assert ascender.min() > mid


def test_text_is_centred_in_the_light():
    a = tl.layout_text("STOP").alpha > 127
    rows, cols = np.flatnonzero(a.any(axis=1)), np.flatnonzero(a.any(axis=0))
    c = config.FACE / 2
    assert abs((rows[0] + rows[-1] + 1) / 2 - c) <= 1 and abs((cols[0] + cols[-1] + 1) / 2 - c) <= 1
    yy, xx = np.mgrid[:config.FACE, :config.FACE] + 0.5
    inside = (xx - c) ** 2 + (yy - c) ** 2 <= config.FACE_RADIUS ** 2
    assert not (a & ~inside).any()


def test_every_font_choice_renders():
    for opt in get_renderer("text").describe()["params"][1]["options"]:
        lay = tl.layout_text("Hi שלום 12", font=opt["value"])
        assert lay.ok and not lay.missing, opt


def test_validate():
    T = get_renderer("text")
    assert T.validate({"text": "STOP"}, "face0", ENV) == []
    too_long = T.validate({"text": "Supercalifragilisticexpialidocious"}, "face1", ENV)
    assert any("does not fit" in m for m in too_long)
    assert any("does not fit" in m for m in T.validate({"text": " ".join(["word"] * 60)}, "face1", ENV))
    assert any("empty" in m for m in T.validate({"text": "  \n "}, "face0", ENV))
    assert any("whole sign" in m for m in T.validate({"text": "STOP"}, "all", ENV))
    missing = T.validate({"text": "Hi \u4e2d"}, "face0", ENV)
    assert len(missing) == 1 and "\u4e2d" in missing[0]


def test_colours():
    T = get_renderer("text")
    f = T({"text": "STOP"}, ENV).render(ctx())
    assert f.shape == (config.FACE, config.FACE, 3) and f.dtype == np.uint8
    assert tuple(f[0, 0]) == (255, 0, 0)                               # background = the light's colour
    assert ((f == 0).all(axis=2)).sum() > 500                          # black ink
    same = T({"text": "STOP", "ink": "face"}, ENV).render(ctx())
    assert ((same == 0).all(axis=2)).sum() > 500                       # ink = bg turns black
    night = T({"text": "STOP", "bg": "#000000", "ink": "#000000"}, ENV).render(ctx())
    assert ((night == 255).all(axis=2)).sum() > 500                    # ... or white on black
    hue = T({"text": "STOP", "bg": "#ff0000", "bg_hue": 120}, ENV)
    assert tuple(hue.render(ctx(t=0.5))[0, 0]) == (0, 255, 0)
    # a transparent background (the colour choice, or the switch of older sets): only the ink, with alpha
    for see_through in ({"bg": "transparent"}, {"transparent": True}, {"bg": "Transparent", "bg_hue": 90}):
        rgba = T({"text": "STOP", "ink": "#00ff00", **see_through}, ENV).render(ctx())
        assert rgba.shape == (config.FACE, config.FACE, 4)
        assert rgba[0, 0, 3] == 0 and rgba[..., 3].max() == 255 and (rgba[..., :3] == (0, 255, 0)).all()
    black = T({"text": "STOP", "bg": "transparent"}, ENV).render(ctx())     # the default black ink stays black
    assert black.shape[2] == 4 and not black[..., :3].any()
    bg = {p["key"]: p for p in T.describe()["params"]}["bg"]
    assert bg["transparent"] is True and "transparent" not in {p["key"] for p in T.describe()["params"]}
    assert "transparent" not in {p["key"]: p for p in T.describe()["params"]}["ink"]


def test_too_long_text_draws_only_the_background():
    f = get_renderer("text")({"text": "Supercalifragilisticexpialidocious"}, ENV).render(ctx())
    assert (f == (255, 0, 0)).all()


@pytest.mark.parametrize("params", [{"text": None}, {"text": 12345, "font": 7, "max_size": "big"},
                                    {"text": "x", "bg": "nonsense", "ink": [1, 2], "bg_hue": "fast", "transparent": "yes"},
                                    {"text": "\x00\x01‏"}, {"font": "no-such-font"}])
def test_bad_params_do_not_raise(params):
    T = get_renderer("text")
    f = T(params, ENV).render(ctx())
    assert f.shape[:2] == (config.FACE, config.FACE)
    assert isinstance(T.validate(params, "face0", ENV), list)


def test_layout_is_cached_and_frames_are_fast():
    T = get_renderer("text")
    r = T({"text": "שלום Burning Man 2026", "bg_hue": 30, "ink_hue": 60}, ENV)
    assert tl.get_layout("שלום Burning Man 2026", "auto", 0) is tl.get_layout("שלום Burning Man 2026", "auto", 0)
    r.render(ctx())
    n = 200
    t0 = time.perf_counter()
    for i in range(n):
        r.render(ctx(t=i / 60))
    assert (time.perf_counter() - t0) / n < 0.002
