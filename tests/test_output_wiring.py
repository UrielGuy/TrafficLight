"""The wiring picture and the rule from "what each panel shows" to panel_order / panel_flip.

The test that matters is the round trip on a simulated, miswired sign: panels on random connectors,
some upside down; read what each panel shows of the wiring picture, turn that into settings with the
rule, then check that a frame sent through the real Mapper comes out in order and upright.
"""
import numpy as np
import pytest

from trafficlight import config
from trafficlight.model import OutputSettings
from trafficlight.output import patterns, wiring
from trafficlight.output.mapping import Mapper

PH, W = config.PANEL_H, config.W


class SimSign:
    """Panel at position i (0 = top) is plugged into band bands[i] and mounted rotated 180 degrees if
    rotated[i]; positions in `dead` stay dark."""

    def __init__(self, bands, rotated, dead=()):
        self.bands, self.rotated, self.dead = list(bands), list(rotated), set(dead)

    def show(self, card: np.ndarray) -> np.ndarray:
        """What the audience sees, (H, W, 3), when the card's image is `card`."""
        assert card.shape == (config.CARD_H, W, 3)
        out = np.zeros((config.H, W, 3), np.uint8)
        for i, (b, rot) in enumerate(zip(self.bands, self.rotated)):
            if i in self.dead:
                continue
            p = card[b * PH:(b + 1) * PH]
            out[i * PH:(i + 1) * PH] = p[::-1, ::-1] if rot else p
        return out


def random_sign(rng, dead=()):
    bands = [int(b) for b in rng.choice(config.PORTS, config.PANELS, replace=False)]
    return SimSign(bands, [bool(r) for r in rng.integers(0, 2, config.PANELS)], dead)


def band_picture(b):
    return patterns.make_card("wiring")[b * PH:(b + 1) * PH]


def read(view_panel: np.ndarray) -> wiring.Seen:
    """What a person reads off one panel of the wiring picture: (number, arrow points down) or None."""
    if not view_panel.any():
        return None
    hits = [(b + 1, down) for b in range(config.PORTS) for down in (False, True)
            if np.array_equal(view_panel, band_picture(b)[::-1, ::-1] if down else band_picture(b))]
    assert len(hits) == 1, hits
    return hits[0]


def seen_on(sign: SimSign) -> list[wiring.Seen]:
    view = sign.show(patterns.make_card("wiring"))
    return [read(view[i * PH:(i + 1) * PH]) for i in range(config.PANELS)]


@pytest.fixture
def frame():
    return np.random.default_rng(1).integers(0, 256, (config.H, W, 3), dtype=np.uint8)


# ------------------------------------------------------------------ the picture
def test_wiring_picture_is_in_card_space():
    img = patterns.make_card("wiring")
    assert img.shape == (config.CARD_H, W, 3) and img.dtype == np.uint8 and img.flags.writeable
    assert (img[..., 0] == img[..., 1]).all() and (img[..., 1] == img[..., 2]).all()   # grey: any colour order
    assert patterns.make_card("panels") is None and patterns.make_card("nope") is None


def test_every_band_differs_from_every_other_both_ways_up():
    pics = [band_picture(b) for b in range(config.PORTS)]
    variants = [p for p in pics] + [p[::-1, ::-1] for p in pics]
    for i, a in enumerate(variants):
        for b in variants[i + 1:]:
            assert np.abs(a.astype(int) - b.astype(int)).sum() > 255 * 200


def test_arrow_points_to_the_top_of_its_band():
    for b in range(config.PORTS):
        lit = band_picture(b)[..., 0] > 128
        right = lit[:, W * 9 // 16:]
        rows = np.flatnonzero(right.any(axis=1))
        widths = right.sum(axis=1)
        top, bottom = rows[0], rows[-1]
        assert top <= 4 and bottom >= PH - 6                    # spans the band
        assert widths[top] < 10                                 # the tip is at the top
        assert widths[top:].max() > 2 * widths[bottom]          # the head (wide) is above the shaft (narrow)
        assert np.argmax(widths) < PH // 2
        left = lit[:, :W // 2]
        bar = np.flatnonzero(left.any(axis=1))[-1]
        assert bar > PH * 3 // 4 and left[bar].sum() > 20       # the underline sits under the number


# ------------------------------------------------------------------ the rule
def test_worked_example():
    # The web UI's unit tests use the same case (web/src/pages/wiring.test.ts).
    seen = [(3, False), (8, True), (2, False), (1, True), (5, False), (4, True)]
    assert wiring.from_seen(seen) == ([2, 7, 1, 0, 4, 3], [False, True, False, True, False, True])
    assert wiring.to_seen([2, 7, 1, 0, 4, 3], [False, True, False, True, False, True]) == seen


def test_round_trip_on_a_miswired_sign(frame):
    rng = np.random.default_rng(42)
    for _ in range(200):
        sign = random_sign(rng)
        order, flip = wiring.from_seen(seen_on(sign))
        s = OutputSettings(panel_order=order, panel_flip=flip, rotate180=False)
        assert np.array_equal(sign.show(Mapper(s).apply(frame)), frame)
        # and the web preview of the wiring picture is what that sign shows of it
        assert np.array_equal(patterns.wiring_preview(tuple(order), tuple(flip), False),
                              sign.show(patterns.make_card("wiring")))


def test_round_trip_with_colour_order_and_gamma(frame):
    sign = SimSign([6, 0, 3, 7, 1, 2], [True, False, True, True, False, False])
    order, flip = wiring.from_seen(seen_on(sign))
    out = Mapper(OutputSettings(panel_order=order, panel_flip=flip, color_order="bgr")).apply(frame)
    assert np.array_equal(sign.show(out)[..., ::-1], frame)


def test_dead_panels_do_not_block_the_others(frame):
    rng = np.random.default_rng(7)
    for dead in ([2], [0, 5], [1, 3, 4]):
        for _ in range(30):
            sign = random_sign(rng, dead=dead)
            seen = seen_on(sign)
            assert [i for i, s in enumerate(seen) if s is None] == dead
            order, flip = wiring.from_seen(seen)
            assert len(set(order)) == config.PANELS
            shown = sign.show(Mapper(OutputSettings(panel_order=order, panel_flip=flip)).apply(frame))
            for i in set(range(config.PANELS)) - set(dead):
                assert np.array_equal(shown[i * PH:(i + 1) * PH], frame[i * PH:(i + 1) * PH])


def test_dark_panel_gets_the_lowest_free_connector():
    seen = [(1, False), None, (3, False), (4, False), (5, False), (6, False)]
    assert wiring.from_seen(seen) == ([0, 1, 2, 3, 4, 5], [False] * 6)
    seen = [(2, True), (1, False), None, (3, False), None, (8, False)]
    assert wiring.from_seen(seen) == ([1, 0, 3, 2, 4, 7], [True, False, False, False, False, False])


def test_to_seen_folds_rotate180_in(frame):
    rng = np.random.default_rng(3)
    for _ in range(50):
        s = OutputSettings(panel_order=[int(b) for b in rng.choice(config.PORTS, config.PANELS, replace=False)],
                           panel_flip=[bool(x) for x in rng.integers(0, 2, config.PANELS)],
                           rotate180=bool(rng.integers(2)))
        order, flip = wiring.from_seen(wiring.to_seen(s.panel_order, s.panel_flip, s.rotate180))
        same = OutputSettings(panel_order=order, panel_flip=flip, rotate180=False)
        assert np.array_equal(Mapper(same).apply(frame), Mapper(s).apply(frame))


@pytest.mark.parametrize("seen, words", [
    ([(3, False), (3, True), (1, False), (2, False), (4, False), (5, False)], "panels 1 and 2 show the same number 3"),
    ([(config.PORTS + 1, False), (3, True), (1, False), (2, False), (4, False), (5, False)],
     f"connector {config.PORTS + 1} does not exist"),
    ([(0, False), (3, True), (1, False), (2, False), (4, False), (5, False)], "connector 0 does not exist"),
    ([(1, False)] * 2, f"need what {config.PANELS} panels show"),
])
def test_from_seen_refuses(seen, words):
    with pytest.raises(ValueError, match=words):
        wiring.from_seen(seen)


def test_order_problems_read_well():
    assert wiring.order_problem([0, 1, 2, 3, 4, 5]) is None
    assert wiring.order_problem([7, 6, 0, 1, 2, 3]) is None
    assert "panels 2 and 5 are on the same connector J3" in wiring.order_problem([0, 2, 1, 3, 2, 5])
    assert "panels 1, 2 and 3" in wiring.order_problem([4, 4, 4, 0, 1, 2])
    assert f"J1..J{config.PORTS}" in wiring.order_problem([0, 1, 2, 3, 4, config.PORTS])
    assert "needs 6 entries" in wiring.order_problem([0, 1])
    assert "needs 6 entries" in wiring.flip_problem([True])
