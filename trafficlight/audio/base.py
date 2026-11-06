"""Audio engine contract: plays the sounds the player asks for on a chosen output device."""
from __future__ import annotations

import sys
from dataclasses import asdict, dataclass
from typing import Any, Optional

from ..render.base import AudioCue


@dataclass
class AudioDevice:
    id: str            # what Settings.audio.device stores; stable across restarts as far as possible
    name: str          # for the UI
    default: bool = False
    channels: int = 2

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class AudioEngine:
    """All methods are safe to call from any thread and never block on the sound card.

    sync() is declarative: the engine's render loop calls it every frame with the
    complete list of sounds that should be heard now. The audio engine
      * starts a voice for a cue key it has not seen (at cue.position),
      * seeks a voice whose position drifted more than ~80 ms from its cue
        (a seek or loop on the timeline),
      * follows cue.gain smoothly (no clicks),
      * fades out (~10 ms) and drops voices whose key is no longer listed.
    playing=False (paused) silences everything but keeps the voices.
    """

    def list_devices(self) -> list[AudioDevice]:
        return []

    def set_device(self, device_id: Optional[str]) -> None:
        """None = the system default output. Errors are reported through status()."""

    def set_volume(self, volume: float) -> None:
        """Master volume 0..1."""

    def sync(self, cues: list[AudioCue], playing: bool = True) -> None: ...

    def close(self) -> None: ...

    def status(self) -> dict[str, Any]:
        """{"ok": bool, "error": str|None, "device": str|None, "device_name": str|None, "volume": float, "voices": int}"""
        return {"ok": True, "error": None, "device": None, "device_name": None, "volume": 1.0, "voices": 0}


CHOOSE_DEVICE = "Settings → Audio → Output device"


def device_problem(code: str, error: str) -> dict[str, Any]:
    """The setup check (trafficlight/checks.py) for an engine error. code: "device_missing" (the chosen device
    is not there), "no_device" (the computer has none), "lost" (it stopped while playing), "open_failed"."""
    title, fix = {
        "device_missing": ("The chosen sound device is not there",
                           f"Plug it in (the server keeps trying), or choose another one in {CHOOSE_DEVICE}."),
        "no_device": ("No sound output device",
                      "Connect speakers or a USB sound card, or enable the computer's audio output."),
        "lost": ("The sound device stopped", "Plug it back in: the server reopens it by itself."),
    }.get(code, ("The sound device cannot be opened",
                 f"Close other programs that may hold it exclusively, or choose another device in {CHOOSE_DEVICE}."))
    return {"id": "audio.device", "level": "error", "title": title, "detail": error, "fix": fix}


def backend_problem(reason: Optional[str]) -> dict[str, Any]:
    """The check for running without the sound library (NullAudio)."""
    if sys.platform.startswith("linux"):
        fix = ("Install PortAudio with `sudo apt install libportaudio2` (scripts/install_pi.sh does it), "
               "then restart the server.")
    else:
        fix = ("Reinstall the Python packages (`pip install -r requirements.txt` in the venv): on Windows the "
               "sounddevice package brings PortAudio. Then restart the server.")
    detail = "The sound library (sounddevice / PortAudio) could not be loaded, so the sign runs silent"
    return {"id": "audio.backend", "level": "error", "title": "No sound output on this machine",
            "detail": f"{detail}: {reason}." if reason else f"{detail}.", "fix": fix}


class NullAudio(AudioEngine):
    """No sound (tests, or a machine without a sound card). `reason`: why the real engine did not load."""

    def __init__(self, reason: Optional[str] = None):
        self.reason = reason


def create_audio_engine() -> AudioEngine:
    """The real engine if the sound libraries load, else NullAudio."""
    try:
        from .engine import SoundDeviceEngine
        return SoundDeviceEngine()
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception("audio unavailable, running silent")
        return NullAudio(str(e) or type(e).__name__)
