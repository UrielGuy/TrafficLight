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
    """Sends frames (config.H x config.W RGB) to the sign. One instance at a time; all calls
    come from the engine's render thread except status() (any thread)."""

    def open(self) -> None:
        """Acquire the device. Raise OutputError with a message for the UI on failure."""

    def send(self, frame: np.ndarray) -> None:
        """frame: uint8 (384, 128, 3) RGB, row 0 = top of the sign, as the audience sees it.
        Panel order, flips, rotation, colour order, gamma: the driver applies
        OutputSettings itself. Must not block longer than a frame; drop rather than queue."""

    def send_card(self, image: np.ndarray) -> None:
        """image: uint8 (CARD_H, W, 3) RGB, already in the receiver card's layout (band b = connector
        J(b+1)), for test pictures about the wiring itself. Panel order, flips and rotation are not
        applied; colour order, gamma and brightness are. Same timing rules as send()."""

    def set_brightness(self, value: float) -> None:
        """0..1."""

    def close(self) -> None: ...

    def status(self) -> dict[str, Any]:
        """{"driver": str, "ok": bool, "error": str|None, "interface": str|None, ...}"""
        return {"driver": "none", "ok": True, "error": None, "interface": None}


def problem(check: str, title: str, detail: str, fix: str | None = None, level: str = "error") -> dict[str, Any]:
    """One entry of a driver's status()["problems"]: what the setup checks (trafficlight/checks.py) show.
    `check` is the stable id of the check it fails (e.g. "output.link"); level "error" or "warning"."""
    return {"id": check, "level": level, "title": title, "detail": detail, "fix": fix}


class OutputError(RuntimeError):
    """A failure with a message for the UI. str() is the whole message (detail, then the fix); the parts
    feed the setup checks. `check` says which one failed: output.interface (none chosen, not found),
    output.access (Npcap, permissions, opening the port) or output.driver (anything else)."""

    def __init__(self, detail: str, fix: str | None = None, *, check: str = "output.driver", title: str | None = None):
        super().__init__(f"{detail} {fix}" if fix else detail)
        self.detail, self.fix, self.check = detail, fix, check
        self.title = title or "The LED output cannot start"

    def as_problem(self) -> dict[str, Any]:
        return problem(self.check, self.title, self.detail, self.fix)


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
