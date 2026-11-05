"""SoundDeviceEngine: the AudioEngine that plays through PortAudio (the `sounddevice` package).

Layers: `mixer.py` (voices, gain, soft limit; testable without a sound card) <- this module
(device choice and the stream lifecycle) <- a `StreamBackend` (PortAudio, or a fake in tests).

Host API choice: one API only, so a device is listed once instead of once per API.
Windows: WASAPI (full device names where MME truncates them to 31 characters, lowest latency
without exclusive access, follows the system default), then DirectSound, then MME.
Linux (Raspberry Pi): ALSA, which also reaches PulseAudio/PipeWire through the "pulse" and
"default" pseudo devices. macOS: Core Audio.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from ..render.base import AudioCue
from .base import AudioDevice, AudioEngine
from .mixer import Mixer

log = logging.getLogger(__name__)

HOST_API_PREFERENCE = ["Windows WASAPI", "Core Audio", "ALSA", "Windows DirectSound", "MME", "JACK Audio Connection Kit"]
# ALSA lists every plugin PCM of a card; these are never what a person wants to pick.
_ALSA_NOISE = re.compile(r"^(surround\d*|front|rear|side|center_lfe|iec958|spdif|dmix|dsnoop|null|lavrate|samplerate|speexrate|upmix|vdownmix)\b", re.I)
_HW_SUFFIX = re.compile(r"\s*\(hw:\d+,\d+\)\s*$")


@dataclass
class RawDevice:
    index: int              # backend-specific handle, valid only until the device list is refreshed
    name: str
    hostapi: str
    channels: int           # max output channels
    samplerate: float       # the device's default rate
    default: bool = False   # default output of its host API


class StreamBackend(Protocol):
    def output_devices(self) -> list[RawDevice]: ...
    def refresh(self) -> None:
        """Re-scan hardware (only called while no stream is open)."""
    def open_stream(self, device: Optional[RawDevice], samplerate: int, channels: int,
                    callback: Callable[..., None]) -> Any:
        """A started-able stream with start(), stop(), close(), latency (s) and active."""


class SoundDeviceBackend:
    def __init__(self) -> None:
        import sounddevice as sd            # raises OSError when PortAudio is missing
        self.sd = sd

    def refresh(self) -> None:
        # Re-initialising PortAudio is the only way to see a hot-plugged card.
        self.sd._terminate()
        self.sd._initialize()

    def output_devices(self) -> list[RawDevice]:
        sd = self.sd
        apis = sd.query_hostapis()
        out = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] < 1:
                continue
            api = apis[d["hostapi"]]
            out.append(RawDevice(index=i, name=d["name"], hostapi=api["name"], channels=int(d["max_output_channels"]),
                                 samplerate=float(d["default_samplerate"]),
                                 default=api.get("default_output_device", -1) == i))
        return out

    def open_stream(self, device, samplerate, channels, callback):
        sd = self.sd
        kwargs: dict[str, Any] = dict(device=None if device is None else device.index, samplerate=samplerate,
                                      channels=channels, dtype="float32", latency="high", callback=callback)
        if device is not None and "WASAPI" in device.hostapi:
            try:    # shared mode normally needs the mixer's exact rate; let PortAudio convert
                return sd.OutputStream(extra_settings=sd.WasapiSettings(auto_convert=True), **kwargs)
            except Exception:
                log.debug("WASAPI auto-convert unavailable", exc_info=True)
        return sd.OutputStream(**kwargs)


def select_devices(raw: list[RawDevice]) -> list[tuple[str, RawDevice]]:
    """The devices of the one host API we use, as (stable id, device). The id is the device name
    without ALSA's volatile "(hw:card,dev)" suffix, so it survives reboots and replugging that
    renumbers cards; clashes get " #2", " #3"."""
    apis = {d.hostapi for d in raw}
    chosen = next((a for a in HOST_API_PREFERENCE if a in apis), None) or (sorted(apis)[0] if apis else None)
    out: list[tuple[str, RawDevice]] = []
    seen: dict[str, int] = {}
    for d in raw:
        if d.hostapi != chosen or (chosen == "ALSA" and _ALSA_NOISE.match(d.name) and not d.default):
            continue
        base = _HW_SUFFIX.sub("", d.name).strip() or d.name
        seen[base] = seen.get(base, 0) + 1
        out.append((base if seen[base] == 1 else f"{base} #{seen[base]}", d))
    return out


class SoundDeviceEngine(AudioEngine):
    def __init__(self, backend: Optional[StreamBackend] = None, retry_seconds: float = 2.0,
                 watchdog: float = 0.5, clock: Callable[[], float] = time.monotonic):
        self._backend = backend or SoundDeviceBackend()
        self._retry = retry_seconds
        self._mixer = Mixer(clock=clock, watchdog=watchdog)
        self._lock = threading.Lock()           # guards the fields below; never held across backend calls
        self._wanted: Optional[str] = None
        self._stream: Any = None
        self._device: Optional[str] = None
        self._device_name: Optional[str] = None
        self._error: Optional[str] = None
        self._dirty = True
        self._closed = False
        self._cb_calls = 0
        self._cb_max = 0.0
        self._cb_late = 0
        self._cb_flags = 0
        self._wake = threading.Event()
        self._settled = threading.Event()
        self._thread = threading.Thread(target=self._manage, name="audio-device", daemon=True)
        self._thread.start()

    # ---------------------------------------------------------------- AudioEngine
    def list_devices(self) -> list[AudioDevice]:
        try:
            raw = self._backend.output_devices()
        except Exception:
            log.exception("listing sound devices failed")
            return []
        return [AudioDevice(id=i, name=d.name, default=d.default, channels=d.channels) for i, d in select_devices(raw)]

    def set_device(self, device_id: Optional[str]) -> None:
        with self._lock:
            self._wanted = device_id or None
            self._dirty = True
            self._settled.clear()
        self._wake.set()

    def set_volume(self, volume: float) -> None:
        self._mixer.set_volume(volume)

    def sync(self, cues: list[AudioCue], playing: bool = True) -> None:
        if not self._closed:
            self._mixer.sync(cues, playing)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"ok": self._error is None, "error": self._error, "device": self._device,
                    "device_name": self._device_name, "volume": self._mixer.volume, "voices": self._mixer.voice_count()}

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._wake.set()
        self._thread.join(timeout=5)
        stream, self._stream = self._stream, None
        if stream is not None:
            self._fade_and_stop(stream)
        self._mixer.close()

    # ---------------------------------------------------------------- extras for tools and tests
    def wait_settled(self, timeout: float = 5.0) -> bool:
        """Block until a pending set_device() has been tried (successfully or not)."""
        return self._settled.wait(timeout)

    def stats(self) -> dict[str, Any]:
        s = self._stream
        return {"callbacks": self._cb_calls, "max_callback_ms": self._cb_max * 1000, "late_callbacks": self._cb_late,
                "device_flags": self._cb_flags, "latency": getattr(s, "latency", None),
                "rate": self._mixer.rate, "channels": self._mixer.channels}

    @property
    def mixer(self) -> Mixer:
        return self._mixer

    # ---------------------------------------------------------------- device thread
    def _callback(self, outdata, frames, time_info=None, status=None) -> None:
        t0 = time.perf_counter()
        try:
            outdata[:] = self._mixer.render(frames)
        except Exception:
            outdata.fill(0)
        if status:
            self._cb_flags += 1
        dt = time.perf_counter() - t0
        self._cb_calls += 1
        self._cb_max = max(self._cb_max, dt)
        if dt > frames / self._mixer.rate:
            self._cb_late += 1

    def _manage(self) -> None:
        next_try = 0.0
        while not self._closed:
            self._wake.wait(0.5)
            self._wake.clear()
            if self._closed:
                break
            with self._lock:
                dirty = self._dirty
                stream = self._stream
            lost = stream is not None and not getattr(stream, "active", True)
            if lost:
                self._set_error("The sound device stopped (unplugged?)", keep_device=True)
                self._drop_stream()
            elif stream is None and not dirty and self._error is not None and time.monotonic() >= next_try:
                dirty = True
            if dirty or lost:
                with self._lock:
                    self._dirty = False
                ok = self._open(refresh=lost or self._error is not None)
                next_try = time.monotonic() + self._retry
                with self._lock:
                    if not self._dirty:
                        self._settled.set()
                if not ok and self._error is None:
                    self._error = "Could not open the sound device"

    def _set_error(self, msg: str, keep_device: bool = False) -> None:
        with self._lock:
            self._error = msg
            if not keep_device:
                self._device_name = None

    def _drop_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            for fn in (stream.stop, stream.close):
                try:
                    fn()
                except Exception:
                    pass

    def _fade_and_stop(self, stream) -> None:
        self._mixer.sync([], False)
        if getattr(stream, "active", False):
            time.sleep(0.03)                    # let the callback fade out what is playing
        for fn in (stream.stop, stream.close):
            try:
                fn()
            except Exception:
                log.debug("closing stream", exc_info=True)

    def _open(self, refresh: bool) -> bool:
        old, self._stream = self._stream, None
        if old is not None:
            self._fade_and_stop(old)
        wanted = self._wanted
        try:
            if refresh:
                self._backend.refresh()
            devices = select_devices(self._backend.output_devices())
            if wanted is None:
                pick = next(((i, d) for i, d in devices if d.default), devices[0] if devices else None)
                if pick is None:
                    raise LookupError("There is no sound output device on this computer")
            else:
                pick = next(((i, d) for i, d in devices if i == wanted), None)
                if pick is None:
                    raise LookupError(f"Sound device '{wanted}' was not found (unplugged?)")
            dev_id, dev = pick
            rate = int(dev.samplerate) or 48000
            channels = 2 if dev.channels >= 2 else 1
            self._mixer.configure(rate, channels)
            stream = self._backend.open_stream(dev, rate, channels, self._callback)
            self._mixer.latency = float(getattr(stream, "latency", 0.0) or 0.0)
            try:
                stream.start()
            except Exception:
                try:
                    stream.close()
                finally:
                    raise
        except Exception as exc:
            msg = str(exc) or type(exc).__name__
            if not isinstance(exc, LookupError):
                msg = f"Could not open the sound device: {msg}"
            log.warning("audio: %s", msg)
            with self._lock:
                self._error, self._device_name = msg, None
                self._device = wanted
            return False
        with self._lock:
            self._stream = stream
            self._error = None
            self._device, self._device_name = dev_id, dev.name
        return True
