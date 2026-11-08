"""Card image -> frame-buffer bytes in the exact order the streamer reads them.

Layout (docs/FIRMWARE.md section 5): record (plane p, row address a) = ``cols`` columns of 6
bytes; column x is a 48-bit little-endian word whose bit 6c + s is connector J(c+1), signal s
(0 R1, 1 G1, 2 B1 for panel row a; 3 R2, 4 G2, 5 B2 for panel row a + SCAN_ROWS). Records are
stored plane-major: offset = (p * rows + a) * cols * 6. Plane 0 is the LSB.

The gamma table maps 8-bit input to the PWM depth with 4 extra fraction bits; with dithering
on, the fraction left over each frame is carried to the next, so a still picture averages to the
16-bit value over up to 16 frames.

How the bits get into place without touching them one by one: the card image is first gathered into
lane order, (row address, column, lane), so a column's 48 lanes sit side by side. After gamma and
dithering each lane holds a value of `planes` bits, split into its low and high byte. Eight lanes' low
bytes are an 8 x 8 bit matrix (byte i = lane 8q + i, bit m = plane m) in one 64-bit word; transposed,
byte m holds bit m of the eight lanes, which is byte q of plane m's 48-bit column word. The transpose
is three rounds of shifts and masks over all words at once (Hacker's Delight, "transpose8"), and a
final byte reorder puts the planes first. Nothing ever takes a byte per bit, which on a Raspberry Pi
is what costs: it is memory-bound.
"""
from __future__ import annotations

import sys

import numpy as np

from ... import config
from .schedule import BYTES_PER_COLUMN, PORTS, Schedule

FRACTION_BITS = 4
_LANES = PORTS * BYTES_PER_COLUMN       # 48 bits per column
_GROUPS = _LANES // 8                   # 64-bit words per column and byte of the values
assert _LANES == BYTES_PER_COLUMN * 8
# The words are byte arrays viewed as 64-bit integers: byte i must be bits 8i..8i+7.
assert sys.byteorder == "little", "the packer assumes a little-endian CPU"
# transpose8: (shift, mask) of the three rounds that swap the off-diagonal 1x1, 2x2 and 4x4 blocks.
_ROUNDS = tuple((np.uint64(s), np.uint64(m)) for s, m in
                ((7, 0x00AA00AA00AA00AA), (14, 0x0000CCCC0000CCCC), (28, 0x00000000F0F0F0F0)))


def gamma_lut(gamma: float, scale: float, planes: int) -> np.ndarray:
    """uint16[256]: 2**planes - 1 at full scale, times 16 (12.4 fixed point for 12 planes)."""
    if not gamma > 0:
        raise ValueError(f"gamma must be > 0, got {gamma}")
    top = (2 ** planes - 1) << FRACTION_BITS
    v = np.arange(256, dtype=np.float64) / 255.0
    return np.clip(np.rint(top * min(max(scale, 0.0), 1.0) * v ** gamma), 0, top).astype(np.uint16)


def _check_geometry(rows: int, cols: int) -> None:
    # The packer is for this sign: a connector's band is two groups of `rows` panel rows, `cols` wide.
    if rows * 2 != config.PANEL_H or cols != config.W:
        raise ValueError(f"schedule geometry {rows} rows x {cols} cols does not match the card image "
                         f"({config.PANEL_H} rows per band, {config.W} wide)")


def lane_index(rows: int, cols: int) -> np.ndarray:
    """For (row address a, column x, lane l = 6c + 3g + k) in that order, the index into the card image's
    bytes (CARD_H, W, 3) of connector c, group g (upper/lower half), colour k."""
    a, x, c, g, k = np.ix_(range(rows), range(cols), range(PORTS), range(2), range(3))
    return ((((c * 2 + g) * rows + a) * cols + x) * 3 + k).reshape(-1).astype(np.intp)


class Packer:
    """Holds the LUT and the dithering state. ``pack`` is the hot path."""

    def __init__(self, schedule: Schedule, gamma: float = 2.2, brightness: float = 1.0, dither: bool = True):
        _check_geometry(schedule.rows, schedule.cols)
        if not 0 < schedule.planes <= 16 - FRACTION_BITS:
            raise ValueError(f"{schedule.planes} planes do not fit the 16-bit gamma table")
        self.planes, self.rows, self.cols = schedule.planes, schedule.rows, schedule.cols
        self.frame_bytes = schedule.frame_bytes
        self.dither = dither
        self.gamma, self.brightness = gamma, brightness
        self.lut = gamma_lut(gamma, brightness, self.planes)
        shape = (self.rows, self.cols, _LANES)
        self._index = lane_index(self.rows, self.cols)
        self._in = np.empty(shape, np.uint8)
        self._acc = np.empty(shape, np.uint16)
        self._err = np.zeros(shape, np.uint16)          # dithering carry, in lane order
        self._nbytes = (self.planes + 7) // 8            # bytes of a value that hold planes
        self._words = np.zeros((self.rows, self.cols, self._nbytes, _LANES), np.uint8)
        self._tmp = np.empty((self.rows, self.cols, self._nbytes, _GROUPS), np.uint64)

    def set_gamma(self, gamma: float, brightness: float) -> None:
        self.gamma, self.brightness = gamma, brightness
        self.lut = gamma_lut(gamma, brightness, self.planes)

    def reset_dither(self) -> None:
        self._err[...] = 0

    def pack(self, image: np.ndarray, out: np.ndarray | None = None) -> np.ndarray:
        """image: uint8 (CARD_H, W, 3), channel k = colour line k (R, G, B after the mapper's colour
        order). Returns uint8[frame_bytes] (in ``out`` if given)."""
        if image.shape != (config.CARD_H, config.W, 3) or image.dtype != np.uint8:
            raise ValueError(f"card image must be uint8 {(config.CARD_H, config.W, 3)}, got {image.dtype} {image.shape}")
        if out is None:
            out = np.empty(self.frame_bytes, np.uint8)
        elif out.shape != (self.frame_bytes,) or out.dtype != np.uint8:
            raise ValueError(f"out must be uint8[{self.frame_bytes}]")
        np.take(np.ascontiguousarray(image).reshape(-1), self._index, out=self._in.reshape(-1))
        acc = self._acc
        np.take(self.lut, self._in, out=acc)
        if self.dither:
            # The LUT tops out at (2**planes - 1) * 16 and the carry is at most 15: no uint16 overflow.
            acc += self._err
            np.bitwise_and(acc, (1 << FRACTION_BITS) - 1, out=self._err)
            acc >>= FRACTION_BITS
        else:
            acc += 1 << (FRACTION_BITS - 1)
            acc >>= FRACTION_BITS
        words = self._words
        np.copyto(words[:, :, 0, :], acc, casting="unsafe")                 # the low byte
        if self._nbytes > 1:
            np.right_shift(acc, 8, out=words[:, :, 1, :], casting="unsafe")
        x, t = words.view(np.uint64), self._tmp          # (rows, cols, nbytes, groups)
        for shift, mask in _ROUNDS:
            np.right_shift(x, shift, out=t)
            np.bitwise_xor(t, x, out=t)
            np.bitwise_and(t, mask, out=t)
            np.bitwise_xor(x, t, out=x)
            np.left_shift(t, shift, out=t)
            np.bitwise_xor(x, t, out=x)
        # Byte m of word (byte j, group q) is byte q of plane 8j + m's column word: planes to the front.
        planes = words.reshape(self.rows, self.cols, self._nbytes, _GROUPS, 8).transpose(2, 4, 0, 1, 3)
        out.reshape(self.planes, self.rows, self.cols, BYTES_PER_COLUMN)[...] = \
            planes.reshape(self._nbytes * 8, self.rows, self.cols, BYTES_PER_COLUMN)[:self.planes]
        return out


def unpack_bits(frame: np.ndarray | bytes, planes: int, rows: int, cols: int) -> np.ndarray:
    """Frame bytes -> uint8 (planes, rows, cols, 48): the bit of every lane in every record."""
    buf = np.frombuffer(bytes(frame) if not isinstance(frame, np.ndarray) else frame, np.uint8)
    need = planes * rows * cols * BYTES_PER_COLUMN
    if buf.size != need:
        raise ValueError(f"frame of {buf.size} bytes, expected {need}")
    return np.unpackbits(buf.reshape(planes, rows, cols, BYTES_PER_COLUMN), axis=-1, bitorder="little")


def unpack(frame: np.ndarray | bytes, planes: int, rows: int, cols: int) -> np.ndarray:
    """Frame bytes -> the PWM values, uint16 (CARD_H, W, 3): the packer's inverse (before gamma)."""
    _check_geometry(rows, cols)
    bits = unpack_bits(frame, planes, rows, cols).astype(np.uint16)
    weights = (np.uint16(1) << np.arange(planes, dtype=np.uint16))
    values = np.tensordot(weights, bits, axes=(0, 0))                 # (rows, cols, 48)
    v = values.reshape(rows, cols, PORTS, 2, 3).transpose(2, 3, 0, 1, 4)
    return np.ascontiguousarray(v.reshape(config.CARD_H, config.W, 3)).astype(np.uint16)
