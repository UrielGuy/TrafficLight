"""Logical sign frame -> the image the receiver card expects.

The card's image (as configured in LEDVision) is PANEL_W wide and PANELS
bands of PANEL_H rows, one band per output port. Settings say how the sign
was wired and mounted:

* ``rotate180`` - the whole sign hangs upside down. Applied first, so
  ``panel_order`` / ``panel_flip`` below count panels from the top of the
  sign as it now hangs.
* ``panel_order[i] = b`` - canvas panel i (0 = top) goes to band b of the
  card's image.
* ``panel_flip[i]`` - canvas panel i is mounted rotated 180 degrees.
* ``color_order`` - the order of the colour bytes of each pixel as sent on
  the wire: "bgr" sends blue, green, red (the FPP convention).
* ``gamma`` - a lookup table applied in software (1.0 = none: the receiver
  card applies its own gamma).

All of it is folded into one precomputed byte-index map (plus an optional
256-entry LUT), so a frame costs one gather.
"""
from __future__ import annotations

import numpy as np

from .. import config
from ..model import OutputSettings

CHANNELS = "rgb"


def validate(s: OutputSettings) -> None:
    if sorted(s.panel_order) != list(range(config.PANELS)):
        raise ValueError(f"panel_order must be a permutation of 0..{config.PANELS - 1}, got {s.panel_order}")
    if len(s.panel_flip) != config.PANELS:
        raise ValueError(f"panel_flip needs {config.PANELS} entries, got {len(s.panel_flip)}")
    if sorted(s.color_order) != sorted(CHANNELS):
        raise ValueError(f"bad color_order {s.color_order!r}")
    if not s.gamma > 0:
        raise ValueError(f"gamma must be > 0, got {s.gamma}")


def source_coords(s: OutputSettings) -> tuple[np.ndarray, np.ndarray]:
    """For every pixel (y, x) of the card's image, the canvas pixel it shows: (sy, sx), each (H, W)."""
    H, W, PH = config.H, config.W, config.PANEL_H
    yy, xx = np.mgrid[0:PH, 0:W]
    sy = np.empty((H, W), np.intp)
    sx = np.empty((H, W), np.intp)
    for i, band in enumerate(s.panel_order):
        if s.panel_flip[i]:
            py, px = i * PH + (PH - 1 - yy), W - 1 - xx
        else:
            py, px = i * PH + yy, xx
        if s.rotate180:          # (py, px) are in the rotated canvas; the frame is the unrotated one
            py, px = H - 1 - py, W - 1 - px
        sy[band * PH:(band + 1) * PH] = py
        sx[band * PH:(band + 1) * PH] = px
    return sy, sx


def gamma_lut(gamma: float, scale: float = 1.0) -> np.ndarray | None:
    """uint8[256] for out = 255 * scale * (v/255)^gamma; None when it is the identity."""
    if gamma == 1.0 and scale >= 1.0:
        return None
    v = np.arange(256, dtype=np.float64) / 255.0
    return np.clip(np.rint(255.0 * max(scale, 0.0) * v ** gamma), 0, 255).astype(np.uint8)


class Mapper:
    """frame (H, W, 3) RGB -> card image (H, W, 3) in wire byte order."""

    def __init__(self, settings: OutputSettings, brightness_scale: float = 1.0):
        validate(settings)
        sy, sx = source_coords(settings)
        perm = np.array([CHANNELS.index(c) for c in settings.color_order], np.intp)
        identity_pixels = bool((sy == np.arange(config.H)[:, None]).all() and (sx == np.arange(config.W)).all())
        self.copy_only = identity_pixels and (perm == np.arange(3)).all()
        # Byte index into frame.ravel() for every output byte.
        self.index = ((sy * config.W + sx)[:, :, None] * 3 + perm).ravel()
        self.lut = gamma_lut(settings.gamma, brightness_scale)
        self._tmp = np.empty(config.H * config.W * 3, np.uint8)

    def apply(self, frame: np.ndarray, out: np.ndarray | None = None) -> np.ndarray:
        if frame.shape != (config.H, config.W, 3) or frame.dtype != np.uint8:
            raise ValueError(f"frame must be uint8 {(config.H, config.W, 3)}, got {frame.dtype} {frame.shape}")
        if out is None:
            out = np.empty((config.H, config.W, 3), np.uint8)
        if self.copy_only:
            src = frame
        else:
            np.take(np.ascontiguousarray(frame).ravel(), self.index, out=self._tmp)
            src = self._tmp.reshape(config.H, config.W, 3)
        if self.lut is None:
            out[...] = src
        else:
            np.take(self.lut, src, out=out)
        return out

    def inverse(self, image: np.ndarray) -> np.ndarray:
        """Card image -> logical frame (ignores the LUT). For tests and previews of what the card got."""
        out = np.empty(config.H * config.W * 3, np.uint8)
        out[self.index] = image.reshape(-1)
        return out.reshape(config.H, config.W, 3)
