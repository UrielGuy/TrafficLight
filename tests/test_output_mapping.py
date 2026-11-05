"""Panel mapping: each option alone, combinations, round trips, speed."""
import itertools
import time

import numpy as np
import pytest

from trafficlight import config
from trafficlight.model import OutputSettings
from trafficlight.output.mapping import Mapper, gamma_lut

PH = config.PANEL_H


@pytest.fixture
def frame():
    return np.random.default_rng(0).integers(0, 256, (config.H, config.W, 3), dtype=np.uint8)


def panel(img, i):
    return img[i * PH:(i + 1) * PH]


def test_identity_is_a_copy(frame):
    m = Mapper(OutputSettings())
    assert m.copy_only and m.lut is None
    out = m.apply(frame)
    assert np.array_equal(out, frame) and out is not frame


def test_color_orders(frame):
    for order in ("rgb", "rbg", "grb", "gbr", "brg", "bgr"):
        out = Mapper(OutputSettings(color_order=order)).apply(frame)
        for k, ch in enumerate(order):
            assert np.array_equal(out[..., k], frame[..., "rgb".index(ch)])


def test_panel_order(frame):
    order = [2, 0, 1, 5, 3, 4]
    out = Mapper(OutputSettings(panel_order=order)).apply(frame)
    for i, band in enumerate(order):
        assert np.array_equal(panel(out, band), panel(frame, i))


def test_panel_flip(frame):
    flip = [True, False, False, True, False, False]
    out = Mapper(OutputSettings(panel_flip=flip)).apply(frame)
    for i in range(config.PANELS):
        want = panel(frame, i)[::-1, ::-1] if flip[i] else panel(frame, i)
        assert np.array_equal(panel(out, i), want)


def test_rotate180_alone(frame):
    out = Mapper(OutputSettings(rotate180=True)).apply(frame)
    assert np.array_equal(out, frame[::-1, ::-1])


def test_rotate180_equals_reversed_order_with_all_flipped(frame):
    a = Mapper(OutputSettings(rotate180=True)).apply(frame)
    b = Mapper(OutputSettings(panel_order=[5, 4, 3, 2, 1, 0], panel_flip=[True] * 6)).apply(frame)
    assert np.array_equal(a, b)


def test_rotate180_then_order_counts_panels_as_hung(frame):
    # Upside-down sign whose top panel (canvas panel 0 after rotation = bottom of the frame) is on band 3.
    order = [3, 0, 1, 2, 4, 5]
    out = Mapper(OutputSettings(rotate180=True, panel_order=order)).apply(frame)
    rotated = frame[::-1, ::-1]
    for i, band in enumerate(order):
        assert np.array_equal(panel(out, band), panel(rotated, i))


def test_round_trips(frame):
    rng = np.random.default_rng(5)
    orders = list(itertools.permutations(range(config.PANELS)))
    for _ in range(25):
        s = OutputSettings(panel_order=list(orders[rng.integers(len(orders))]),
                           panel_flip=[bool(x) for x in rng.integers(0, 2, config.PANELS)],
                           rotate180=bool(rng.integers(2)),
                           color_order=["rgb", "rbg", "grb", "gbr", "brg", "bgr"][rng.integers(6)])
        m = Mapper(s)
        out = m.apply(frame)
        assert np.array_equal(m.inverse(out), frame)
        assert np.array_equal(np.sort(out.ravel()), np.sort(frame.ravel()))   # a permutation of the bytes


def test_gamma_lut(frame):
    lut = gamma_lut(2.2)
    assert lut[0] == 0 and lut[255] == 255 and lut[128] == round(255 * (128 / 255) ** 2.2)
    assert np.all(np.diff(lut.astype(int)) >= 0)
    out = Mapper(OutputSettings(gamma=2.2, color_order="bgr")).apply(frame)
    assert np.array_equal(out, lut[frame[..., ::-1]])
    assert gamma_lut(1.0) is None
    assert gamma_lut(1.0, 0.5)[255] == 128


@pytest.mark.parametrize("bad", [dict(panel_order=[0, 0, 1, 2, 3, 4]), dict(panel_order=[0, 1, 2]),
                                 dict(panel_flip=[True]), dict(gamma=0.0)])
def test_invalid_settings(bad):
    with pytest.raises(ValueError):
        Mapper(OutputSettings(**bad))


def test_rejects_wrong_frame(frame):
    with pytest.raises(ValueError):
        Mapper(OutputSettings()).apply(frame[:10])


def test_fast(frame):
    m = Mapper(OutputSettings(panel_order=[5, 4, 3, 2, 1, 0], panel_flip=[True, False] * 3,
                              color_order="bgr", gamma=2.2))
    out = np.empty_like(frame)
    m.apply(frame, out)
    t = time.perf_counter()
    for _ in range(20):
        m.apply(frame, out)
    assert (time.perf_counter() - t) / 20 < 0.010
