"""Card image -> frame-buffer bytes in the exact order the streamer reads them.

Layout (docs/FIRMWARE.md section 5): record (plane p, row address a) = ``cols`` columns of 6
bytes; column x is a 48-bit little-endian word whose bit 6c + s is connector J(c+1), signal s
(0 R1, 1 G1, 2 B1 for panel row a; 3 R2, 4 G2, 5 B2 for panel row a + SCAN_ROWS). Records are
stored plane-major: offset = (p * rows + a) * cols * 6. Plane 0 is the LSB.

The gamma table maps 8-bit input to the PWM depth with 4 extra fraction bits; with dithering
on, the fraction left over each frame is carried to the next, so a still picture averages to
the 16-bit value over up to 16 frames. Everything is whole-array numpy: a frame packs in a
few milliseconds.
"""
from __future__ import annotations

import numpy as np

from ... import config
from .schedule import BYTES_PER_COLUMN, PORTS, Schedule

FRACTION_BITS = 4
_LANES = PORTS * BYTES_PER_COLUMN       # 48 bits per column


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


class Packer:
    """Holds the LUT and the dithering state. ``pack`` is the hot path."""

    def __init__(self, schedule: Schedule, gamma: float = 2.2, brightness: float = 1.0, dither: bool = True):
        _check_geometry(schedule.rows, schedule.cols)
        self.planes, self.rows, self.cols = schedule.planes, schedule.rows, schedule.cols
        self.frame_bytes = schedule.frame_bytes
        self.dither = dither
        self.gamma, self.brightness = gamma, brightness
        self.lut = gamma_lut(gamma, brightness, self.planes)
        self._err = np.zeros((config.CARD_H, config.W, 3), np.uint16)
        self._acc = np.empty((config.CARD_H, config.W, 3), np.uint16)
        self._lanes = np.empty((self.rows, self.cols, _LANES), np.uint16)

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
        acc = self._acc
        np.take(self.lut, image, out=acc)
        if self.dither:
            # The LUT tops out at (2**planes - 1) * 16 and the carry is at most 15: no uint16 overflow.
            acc += self._err
            np.bitwise_and(acc, (1 << FRACTION_BITS) - 1, out=self._err)
            acc >>= FRACTION_BITS
        else:
            acc += 1 << (FRACTION_BITS - 1)
            acc >>= FRACTION_BITS
        # (connector, group, row, col, colour) -> (row, col, connector, group, colour) = the 48 lanes of a column.
        v = acc.reshape(PORTS, 2, self.rows, self.cols, 3).transpose(2, 3, 0, 1, 4)
        lanes = self._lanes
        lanes[...] = v.reshape(self.rows, self.cols, _LANES)
        # Each lane's value is two little-endian bytes; unpacking them bit by bit gives, per lane, bit 0..15.
        bits = np.unpackbits(lanes.view(np.uint8), axis=-1, bitorder="little")      # (rows, cols, 48 * 16)
        bits = bits.reshape(self.rows, self.cols, _LANES, 16)[..., :self.planes]    # (rows, cols, 48, planes)
        planes_first = np.ascontiguousarray(bits.transpose(3, 0, 1, 2))            # (planes, rows, cols, 48)
        out.reshape(self.planes, self.rows, self.cols, BYTES_PER_COLUMN)[...] = \
            np.packbits(planes_first, axis=-1, bitorder="little")
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
