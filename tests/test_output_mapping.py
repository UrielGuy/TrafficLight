"""Panel mapping: each option alone, combinations, round trips, unused bands, speed."""
import itertools
import time

import numpy as np
import pytest

from trafficlight import config
from trafficlight.model import OutputSettings
from trafficlight.output.mapping import Mapper, gamma_lut

PH = config.PANEL_H
CARD = (config.CARD_H, config.W, 3)


@pytest.fixture
def frame():
    return np.random.default_rng(0).integers(0, 256, (config.H, config.W, 3), dtype=np.uint8)


def panel(img, i):
    return img[i * PH:(i + 1) * PH]


def test_card_image_has_a_band_per_connector():
    assert config.CARD_H == config.PORTS * PH and config.PORTS > config.PANELS


def test_identity_is_a_copy_with_spare_bands_black(frame):
    m = Mapper(OutputSettings())
    assert m.copy_only and m.lut is None
    out = m.apply(frame)
    assert out.shape == CARD and np.array_equal(out[:config.H], frame)
    assert not out[config.H:].any()


def test_identity_with_lut_blanks_spare_bands(frame):
    out = np.full(CARD, 77, np.uint8)            # a reused buffer must not keep old bytes in the spare bands
    Mapper(OutputSettings(gamma=2.2)).apply(frame, out)
    assert np.array_equal(out[:config.H], gamma_lut(2.2)[frame]) and not out[config.H:].any()


def test_color_orders(frame):
    for order in ("rgb", "rbg", "grb", "gbr", "brg", "bgr"):
        out = Mapper(OutputSettings(color_order=order)).apply(frame)
        for k, ch in enumerate(order):
            assert np.array_equal(out[:config.H, :, k], frame[..., "rgb".index(ch)])


def test_panel_order(frame):
    order = [2, 0, 1, 5, 3, 4]
    out = Mapper(OutputSettings(panel_order=order)).apply(frame)
    for i, band in enumerate(order):
        assert np.array_equal(panel(out, band), panel(frame, i))


def test_panel_order_on_any_connector(frame):
    order = [7, 2, 0, 5, 1, 4]                   # J8 used, J4 and J7 free
    out = np.full(CARD, 9, np.uint8)
    Mapper(OutputSettings(panel_order=order, color_order="bgr")).apply(frame, out)
    for i, band in enumerate(order):
        assert np.array_equal(panel(out, band), panel(frame, i)[..., ::-1])
    for band in set(range(config.PORTS)) - set(order):
        assert not panel(out, band).any()


def test_panel_flip(frame):
    flip = [True, False, False, True, False, False]
    out = Mapper(OutputSettings(panel_flip=flip)).apply(frame)
    for i in range(config.PANELS):
        want = panel(frame, i)[::-1, ::-1] if flip[i] else panel(frame, i)
        assert np.array_equal(panel(out, i), want)


def test_rotate180_alone(frame):
    out = Mapper(OutputSettings(rotate180=True)).apply(frame)
    assert np.array_equal(out[:config.H], frame[::-1, ::-1])


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
    for _ in range(25):
        s = OutputSettings(panel_order=[int(b) for b in rng.choice(config.PORTS, config.PANELS, replace=False)],
                           panel_flip=[bool(x) for x in rng.integers(0, 2, config.PANELS)],
                           rotate180=bool(rng.integers(2)),
                           color_order=["rgb", "rbg", "grb", "gbr", "brg", "bgr"][rng.integers(6)])
        m = Mapper(s)
        out = m.apply(frame)
        assert np.array_equal(m.inverse(out), frame)
        used = np.concatenate([panel(out, b) for b in s.panel_order])
        assert np.array_equal(np.sort(used.ravel()), np.sort(frame.ravel()))   # a permutation of the bytes


def test_every_permutation_of_six_bands_still_works(frame):
    for order in itertools.islice(itertools.permutations(range(config.PANELS)), 0, None, 37):
        m = Mapper(OutputSettings(panel_order=list(order)))
        assert np.array_equal(m.inverse(m.apply(frame)), frame)


def test_apply_card_skips_the_pixel_map():
    img = np.random.default_rng(3).integers(0, 256, CARD, dtype=np.uint8)
    s = OutputSettings(panel_order=[7, 6, 5, 4, 3, 2], panel_flip=[True] * 6, rotate180=True,
                       color_order="brg", gamma=2.2)
    out = Mapper(s).apply_card(img)
    assert np.array_equal(out, gamma_lut(2.2)[img[..., [2, 0, 1]]])
    assert np.array_equal(Mapper(OutputSettings()).apply_card(img), img)
    with pytest.raises(ValueError):
        Mapper(OutputSettings()).apply_card(img[:config.H])


def test_gamma_lut(frame):
    lut = gamma_lut(2.2)
    assert lut[0] == 0 and lut[255] == 255 and lut[128] == round(255 * (128 / 255) ** 2.2)
    assert np.all(np.diff(lut.astype(int)) >= 0)
    out = Mapper(OutputSettings(gamma=2.2, color_order="bgr")).apply(frame)
    assert np.array_equal(out[:config.H], lut[frame[..., ::-1]])
    assert gamma_lut(1.0) is None
    assert gamma_lut(1.0, 0.5)[255] == 128


@pytest.mark.parametrize("bad", [dict(panel_order=[0, 0, 1, 2, 3, 4]), dict(panel_order=[0, 1, 2]),
                                 dict(panel_order=[0, 1, 2, 3, 4, config.PORTS]), dict(panel_order=[-1, 1, 2, 3, 4, 5]),
                                 dict(panel_flip=[True])])
def test_invalid_wiring_refused_by_the_model(bad):
    with pytest.raises(ValueError):
        OutputSettings(**bad)


@pytest.mark.parametrize("bad", [dict(panel_order=[0, 0, 1, 2, 3, 4]), dict(panel_order=[0, 1, 2]),
                                 dict(panel_flip=[True]), dict(gamma=0.0)])
def test_invalid_settings(bad):
    s = OutputSettings()
    for k, v in bad.items():                     # by assignment, as the bring-up tool does: the model does not check
        setattr(s, k, v)
    with pytest.raises(ValueError):
        Mapper(s)


def test_rejects_wrong_frame(frame):
    with pytest.raises(ValueError):
        Mapper(OutputSettings()).apply(frame[:10])


def test_fast(frame):
    m = Mapper(OutputSettings(panel_order=[7, 4, 3, 2, 1, 0], panel_flip=[True, False] * 3,
                              color_order="bgr", gamma=2.2))
    out = np.empty(CARD, np.uint8)
    m.apply(frame, out)
    t = time.perf_counter()
    for _ in range(20):
        m.apply(frame, out)
    assert (time.perf_counter() - t) / 20 < 0.010
