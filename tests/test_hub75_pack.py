"""Packing proved through the streamer model: what the panels would light, per pixel and colour."""
import time

import numpy as np
import pytest

from trafficlight import config
from trafficlight.output.hub75.model import Streamer
from trafficlight.output.hub75.pack import FRACTION_BITS, Packer, gamma_lut, unpack
from trafficlight.output.hub75.schedule import ScheduleParams, build

CARD = (config.CARD_H, config.W, 3)


@pytest.fixture(scope="module")
def sched():
    return build()


@pytest.fixture(scope="module")
def streamer(sched):
    return Streamer(sched)


def linear_packer(sched, **kw):
    return Packer(sched, gamma=1.0, dither=False, **kw)


def test_frame_bytes_and_layout_are_plane_major(sched):
    pk = linear_packer(sched)
    img = np.zeros(CARD, np.uint8)
    img[0, 0, 0] = 255                     # J1, row address 0, column 0, R1: bit 0 of every plane's first word
    out = pk.pack(img)
    assert out.shape == (sched.frame_bytes,)
    rec = sched.record_bytes
    for p in range(sched.planes):
        assert out[p * sched.rows * rec] == 1
    assert np.count_nonzero(out) == sched.planes


def test_single_pixels_land_on_their_lane(sched, streamer):
    pk = linear_packer(sched)
    rng = np.random.default_rng(1)
    for c in range(config.PORTS):                             # every connector
        for r in (0, 1, config.SCAN_ROWS - 1, config.SCAN_ROWS, config.PANEL_H - 1):   # both halves
            x = int(rng.integers(0, config.W))
            for ch in range(3):
                img = np.zeros(CARD, np.uint8)
                y = c * config.PANEL_H + r
                img[y, x, ch] = 255
                on = streamer.on_ticks(pk.pack(img))
                assert on[y, x, ch] == sum(sched.plane_weight_ticks())
                on[y, x, ch] = 0
                assert not on.any(), (c, r, x, ch)


def test_gamma_ramp_is_linear_in_light(sched, streamer):
    pk = linear_packer(sched)
    img = np.zeros(CARD, np.uint8)
    img[:, :, 0] = np.arange(config.W, dtype=np.uint8) * 2
    img[:, :, 1] = 255 - img[:, :, 0]
    on = streamer.on_ticks(pk.pack(img)).astype(np.float64)
    lsb = sched.plane_weight_ticks()[0]
    expect = (gamma_lut(1.0, 1.0, sched.planes)[img].astype(np.int64) + (1 << (FRACTION_BITS - 1))) >> FRACTION_BITS
    assert np.array_equal(on, expect * lsb)
    # 4096 distinct levels over the whole range, proportional to the input.
    assert np.allclose(on[0, :, 0] / on[0, -1, 0], (np.arange(config.W) * 2) / 254, atol=1e-3)


def test_gamma_and_brightness_go_through_the_lut(sched, streamer):
    pk = Packer(sched, gamma=2.2, brightness=0.5, dither=False)
    img = np.full(CARD, 128, np.uint8)
    on = streamer.on_ticks(pk.pack(img))
    frac = on[0, 0, 0] / sum(sched.plane_weight_ticks())
    assert abs(frac - 0.5 * (128 / 255) ** 2.2) < 2e-3
    assert (on == on[0, 0, 0]).all()


def test_unpack_is_the_inverse(sched):
    pk = Packer(sched, gamma=2.2, dither=False)
    img = np.random.default_rng(2).integers(0, 256, CARD, dtype=np.uint8)
    values = unpack(pk.pack(img), sched.planes, sched.rows, sched.cols)
    assert np.array_equal(values, (pk.lut[img].astype(np.int64) + 8) >> 4)


def test_all_connectors_full_white(sched, streamer):
    pk = linear_packer(sched)
    on = streamer.on_ticks(pk.pack(np.full(CARD, 255, np.uint8)))
    assert (on == sum(sched.plane_weight_ticks())).all()


def test_pulses_of_a_bright_pixel_are_spread_over_the_frame(sched, streamer):
    pk = linear_packer(sched)
    pk.lut = (np.arange(256, dtype=np.uint16) << 8)       # value = input * 16: 128 -> 2048, the MSB alone
    img = np.zeros(CARD, np.uint8)
    y, x = 3 * config.PANEL_H + 40, 77
    img[y, x, 2] = 128
    pulses = streamer.pulses(pk.pack(img), y, x, 2)
    assert len(pulses) == len(sched.subframes) == 32
    gaps = np.diff(pulses[:, 0])
    tick = 1 / sched.limits.tick_hz
    assert gaps.max() * tick < 1 / 1400 and gaps.min() * tick > 1 / 2200    # about 1920 Hz, never a long dark gap
    assert pulses[:, 1].sum() == sched.plane_weight_ticks()[11]
    pk.lut = gamma_lut(1.0, 1.0, sched.planes)
    img[y, x, 2] = 255
    pulses = streamer.pulses(pk.pack(img), y, x, 2)
    assert len(pulses) == len(sched.entries) and pulses[:, 1].sum() == sum(sched.plane_weight_ticks())


def test_temporal_dithering_averages_to_the_fine_value(sched):
    pk = Packer(sched, gamma=2.2, dither=True)
    img = np.random.default_rng(3).integers(0, 256, CARD, dtype=np.uint8)
    n = 1 << FRACTION_BITS
    total = np.zeros(CARD, np.int64)
    for _ in range(n):
        total += unpack(pk.pack(img), sched.planes, sched.rows, sched.cols)
    assert np.array_equal(total, pk.lut[img].astype(np.int64))         # exact over 16 frames
    pk.reset_dither()
    first = unpack(pk.pack(img), sched.planes, sched.rows, sched.cols)
    assert np.array_equal(first, pk.lut[img] >> FRACTION_BITS)


def test_dithering_never_overflows_at_full_white(sched):
    pk = Packer(sched, gamma=1.0, dither=True)
    img = np.full(CARD, 255, np.uint8)
    for _ in range(20):
        v = unpack(pk.pack(img), sched.planes, sched.rows, sched.cols)
        assert (v == 4095).all()


def test_other_depths_pack(streamer):
    s = build(ScheduleParams(planes=8, split=8))
    pk = linear_packer(s)
    img = np.full(CARD, 255, np.uint8)
    on = Streamer(s).on_ticks(pk.pack(img))
    assert (on == sum(s.plane_weight_ticks())).all() and s.frame_bytes == 8 * 32 * 128 * 6


def test_rejects_wrong_shapes(sched):
    pk = Packer(sched)
    with pytest.raises(ValueError):
        pk.pack(np.zeros((config.H, config.W, 3), np.uint8))
    with pytest.raises(ValueError):
        pk.pack(np.zeros(CARD, np.uint8), out=np.zeros(10, np.uint8))


def test_packing_is_fast(sched):
    pk = Packer(sched, gamma=2.2, dither=True)
    img = np.random.default_rng(4).integers(0, 256, CARD, dtype=np.uint8)
    out = np.empty(sched.frame_bytes, np.uint8)
    for _ in range(3):
        pk.pack(img, out)
    t0 = time.perf_counter()
    for _ in range(10):
        pk.pack(img, out)
    ms = (time.perf_counter() - t0) / 10 * 1e3
    assert ms < 40, f"pack took {ms:.1f} ms"          # ~1.5 ms on a PC; lenient for CI and the Pi


def _reference_pack(pk: Packer, image: np.ndarray, err: np.ndarray) -> np.ndarray:
    """The packer as first written: one byte per bit, reordered with numpy. Slow, obviously right."""
    from trafficlight.output.hub75.schedule import BYTES_PER_COLUMN, PORTS
    acc = pk.lut[image].astype(np.uint16)
    if pk.dither:
        acc += err
        err[...] = acc & ((1 << FRACTION_BITS) - 1)
        acc >>= FRACTION_BITS
    else:
        acc = (acc + (1 << (FRACTION_BITS - 1))) >> FRACTION_BITS
    lanes = np.ascontiguousarray(acc.reshape(PORTS, 2, pk.rows, pk.cols, 3).transpose(2, 3, 0, 1, 4)
                                 ).reshape(pk.rows, pk.cols, PORTS * 6)
    bits = np.unpackbits(lanes.view(np.uint8), axis=-1, bitorder="little").reshape(pk.rows, pk.cols, -1, 16)
    planes_first = np.ascontiguousarray(bits[..., :pk.planes].transpose(3, 0, 1, 2))
    return np.packbits(planes_first, axis=-1, bitorder="little").reshape(-1)


@pytest.mark.parametrize("planes", [8, 10, 12])
@pytest.mark.parametrize("dither", [True, False])
def test_packer_matches_the_reference_bit_for_bit(planes, dither):
    s = build(ScheduleParams(planes=planes, split=min(planes, 6)))
    pk = Packer(s, gamma=2.2, brightness=0.8, dither=dither)
    err = np.zeros(CARD, np.uint16)
    rng = np.random.default_rng(planes)
    still = rng.integers(0, 256, CARD, np.uint8)
    for i in range(6):          # a still picture, then changing ones: the dithering carry must match too
        img = still if i < 3 else rng.integers(0, 256, CARD, np.uint8)
        assert np.array_equal(pk.pack(img), _reference_pack(pk, img, err)), (planes, dither, i)
