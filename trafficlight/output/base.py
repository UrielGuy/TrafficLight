"""Output driver contract: where finished frames go."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from ..model import OutputSettings


@dataclass
class NetInterface:
    id: str            # what Settings.output.interface stores (stable across restarts)
    name: str          # short name ("eth0", "Ethernet 3")
    description: str   # adapter description for the UI
    mac: str = ""
    up: bool = True

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class OutputDriver:
    """Sends 128x384 RGB frames to the sign. One instance at a time; all calls come
    from the engine's render thread except status() (any thread)."""

    def open(self) -> None:
        """Acquire the device. Raise OutputError with a message for the UI on failure."""

    def send(self, frame: np.ndarray) -> None:
        """frame: uint8 (384, 128, 3) RGB, row 0 = top of the sign, as the audience sees it.
        Panel order, flips, rotation, colour order, gamma: the driver applies
        OutputSettings itself. Must not block longer than a frame; drop rather than queue."""

    def set_brightness(self, value: float) -> None:
        """0..1."""

    def close(self) -> None: ...

    def status(self) -> dict[str, Any]:
        """{"driver": str, "ok": bool, "error": str|None, "interface": str|None, ...}"""
        return {"driver": "none", "ok": True, "error": None, "interface": None}


class OutputError(RuntimeError):
    pass


class NullDriver(OutputDriver):
    """No hardware: frames go nowhere (the web preview still shows them)."""


def create_driver(settings: OutputSettings) -> OutputDriver:
    """The driver for these settings (not yet opened)."""
    if settings.driver == "colorlight":
        from .colorlight import ColorlightDriver
        return ColorlightDriver(settings)
    return NullDriver()


def list_interfaces() -> list[NetInterface]:
    """Network interfaces the Colorlight driver could send on ([] if it cannot tell)."""
    try:
        from .colorlight import list_interfaces as _li
        return _li()
    except Exception:
        return []
