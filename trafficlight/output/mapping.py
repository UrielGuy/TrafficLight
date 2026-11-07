"""Logical sign frame -> the card's image.

The card's image is PANEL_W wide and PORTS bands of PANEL_H rows (CARD_H
rows), band b fed to connector J(b+1). The sign has PANELS panels, so some
bands carry no panel: they are sent black. Settings say how the sign was
wired and mounted:

* ``rotate180`` - the whole sign hangs upside down. Applied first, so
  ``panel_order`` / ``panel_flip`` below count panels from the top of the
  sign as it now hangs.
* ``panel_order[i] = b`` - canvas panel i (0 = top) goes to band b of the
  card's image, i.e. the panel is plugged into connector J(b+1).
* ``panel_flip[i]`` - canvas panel i is mounted rotated 180 degrees.
* ``color_order`` - which colour goes to the panels' R, G, B inputs: "bgr"
  puts blue on R, green on G, red on B.

All of it is folded into one precomputed byte-index map, so a frame costs one
gather. A picture drawn in the card's image already (the "wiring" test
picture) skips the pixel map: only the colour order applies (``apply_card``).
Gamma and brightness are not applied here: the driver's packer applies them
at more than 8 bits (hub75/pack.py).
"""
from __future__ import annotations

import numpy as np

from .. import config
from ..model import OutputSettings
from .wiring import flip_problem, order_problem

CHANNELS = "rgb"


def validate(s: OutputSettings) -> None:
    # The model checks these on construction; settings changed by assignment (the bring-up tool) are not.
    problem = order_problem(s.panel_order)
    if problem:
        raise ValueError(f"panel_order: {problem}")
    problem = flip_problem(s.panel_flip)
    if problem:
        raise ValueError(f"panel_flip: {problem}")
    if sorted(s.color_order) != sorted(CHANNELS):
        raise ValueError(f"bad color_order {s.color_order!r}")
    if not s.gamma > 0:
        raise ValueError(f"gamma must be > 0, got {s.gamma}")


def source_coords(s: OutputSettings) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For every pixel (y, x) of the card's image, the canvas pixel it shows: (sy, sx), each (CARD_H, W),
    and `used`, (CARD_H,) bool: the rows of bands that carry a panel (sy, sx are 0 elsewhere)."""
    H, W, PH = config.H, config.W, config.PANEL_H
    yy, xx = np.mgrid[0:PH, 0:W]
    sy = np.zeros((config.CARD_H, W), np.intp)
    sx = np.zeros((config.CARD_H, W), np.intp)
    used = np.zeros(config.CARD_H, bool)
    for i, band in enumerate(s.panel_order):
        if s.panel_flip[i]:
            py, px = i * PH + (PH - 1 - yy), W - 1 - xx
        else:
            py, px = i * PH + yy, xx
        if s.rotate180:          # (py, px) are in the rotated canvas; the frame is the unrotated one
            py, px = H - 1 - py, W - 1 - px
        sy[band * PH:(band + 1) * PH] = py
        sx[band * PH:(band + 1) * PH] = px
        used[band * PH:(band + 1) * PH] = True
    return sy, sx, used


class Mapper:
    """frame (H, W, 3) RGB -> card image (CARD_H, W, 3) in the panels' colour order."""

    def __init__(self, settings: OutputSettings):
        validate(settings)
        sy, sx, used = source_coords(settings)
        self.perm = np.array([CHANNELS.index(c) for c in settings.color_order], np.intp)
        self.rgb = bool((self.perm == np.arange(3)).all())
        H = config.H
        identity_pixels = bool(used[:H].all() and not used[H:].any()
                               and (sy[:H] == np.arange(H)[:, None]).all() and (sx[:H] == np.arange(config.W)).all())
        self.copy_only = identity_pixels and self.rgb
        # Byte index into frame.ravel() for every output byte.
        self.index = ((sy * config.W + sx)[:, :, None] * 3 + self.perm).ravel()
        self._used_bytes = np.repeat(used, config.W * 3)
        PH = config.PANEL_H
        self._blank = [slice(b * PH, (b + 1) * PH) for b in range(config.PORTS) if not used[b * PH]]
        self._tmp = np.empty(config.CARD_H * config.W * 3, np.uint8)

    def apply(self, frame: np.ndarray, out: np.ndarray | None = None) -> np.ndarray:
        if frame.shape != (config.H, config.W, 3) or frame.dtype != np.uint8:
            raise ValueError(f"frame must be uint8 {(config.H, config.W, 3)}, got {frame.dtype} {frame.shape}")
        if out is None:
            out = np.empty((config.CARD_H, config.W, 3), np.uint8)
        if self.copy_only:
            # Panels on bands 0..PANELS-1 in order, upright: the frame is the top of the card's image.
            out[:config.H] = frame
            out[config.H:] = 0
            return out
        np.take(np.ascontiguousarray(frame).ravel(), self.index, out=self._tmp)
        src = self._tmp.reshape(config.CARD_H, config.W, 3)
        for rows in self._blank:
            src[rows] = 0
        out[...] = src
        return out

    def apply_card(self, image: np.ndarray, out: np.ndarray | None = None) -> np.ndarray:
        """An RGB picture drawn in the card's image (CARD_H, W, 3) -> the panels' colour order, no pixel map."""
        if image.shape != (config.CARD_H, config.W, 3) or image.dtype != np.uint8:
            raise ValueError(f"card image must be uint8 {(config.CARD_H, config.W, 3)}, got {image.dtype} {image.shape}")
        if out is None:
            out = np.empty((config.CARD_H, config.W, 3), np.uint8)
        out[...] = image if self.rgb else image[..., self.perm]
        return out

    def inverse(self, image: np.ndarray) -> np.ndarray:
        """Card image -> logical frame: what the sign shows if the settings match its wiring. For tests and
        previews."""
        out = np.zeros(config.H * config.W * 3, np.uint8)
        out[self.index[self._used_bytes]] = image.reshape(-1)[self._used_bytes]
        return out.reshape(config.H, config.W, 3)
