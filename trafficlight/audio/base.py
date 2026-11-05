"""Audio engine contract: plays the sounds the player asks for on a chosen output device."""
from __future__ import annotations

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


class NullAudio(AudioEngine):
    """No sound (tests, or a machine without a sound card)."""


def create_audio_engine() -> AudioEngine:
    """The real engine if the sound libraries load, else NullAudio."""
    try:
        from .engine import SoundDeviceEngine
        return SoundDeviceEngine()
    except Exception:
        import logging
        logging.getLogger(__name__).exception("audio unavailable, running silent")
        return NullAudio()
