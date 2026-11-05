"""SoundDeviceEngine against a fake stream backend: device handling, status, lifecycle."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_audio_mixer import RATE, frame_index, index_file, tone_file   # noqa: E402

from trafficlight.audio.base import AudioEngine, create_audio_engine    # noqa: E402
from trafficlight.audio.engine import RawDevice, SoundDeviceEngine, select_devices   # noqa: E402
from trafficlight.render.base import AudioCue    # noqa: E402


class FakeStream:
    latency = 0.05

    def __init__(self, backend, device, rate, channels, callback):
        self.backend, self.device, self.rate, self.channels, self.callback = backend, device, rate, channels, callback
        self.active = False
        self.closed = False

    def start(self):
        if self.device.name in self.backend.fail_start:
            raise OSError("Invalid device (PaErrorCode -9996)")
        self.active = True

    def stop(self):
        self.active = False

    def close(self):
        self.closed = True
        self.active = False

    def pull(self, frames=480):
        out = np.zeros((frames, self.channels), np.float32)
        self.callback(out, frames, None, None)
        return out


class FakeBackend:
    def __init__(self, devices):
        self.devices = list(devices)
        self.fail_start: set[str] = set()
        self.streams: list[FakeStream] = []
        self.refreshes = 0

    def output_devices(self):
        return list(self.devices)

    def refresh(self):
        self.refreshes += 1

    def open_stream(self, device, samplerate, channels, callback):
        s = FakeStream(self, device, samplerate, channels, callback)
        self.streams.append(s)
        return s

    @property
    def current(self) -> FakeStream:
        return self.streams[-1]


WINDOWS = [
    RawDevice(0, "Microsoft Sound Mapper - Output", "MME", 2, 44100.0, False),
    RawDevice(1, "Speakers (Realtek High Defini", "MME", 2, 44100.0, True),
    RawDevice(2, "Primary Sound Driver", "Windows DirectSound", 2, 44100.0, False),
    RawDevice(3, "Speakers (Realtek High Definition Audio)", "Windows DirectSound", 2, 44100.0, True),
    RawDevice(4, "Speakers (Realtek High Definition Audio)", "Windows WASAPI", 2, 48000.0, True),
    RawDevice(5, "USB Audio (Behringer UCA202)", "Windows WASAPI", 2, 44100.0, False),
]
PI = [
    RawDevice(0, "bcm2835 Headphones: - (hw:0,0)", "ALSA", 2, 48000.0),
    RawDevice(1, "vc4-hdmi-0: MAI PCM i2s-hifi-0 (hw:1,0)", "ALSA", 2, 48000.0),
    RawDevice(2, "surround51", "ALSA", 6, 48000.0),
    RawDevice(3, "pulse", "ALSA", 32, 44100.0),
    RawDevice(4, "default", "ALSA", 32, 44100.0, True),
]


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def make():
    engines = []

    def build(devices=WINDOWS, **kw):
        be = FakeBackend(devices)
        eng = SoundDeviceEngine(be, retry_seconds=0.05, **kw)
        engines.append(eng)
        return eng, be

    yield build
    for e in engines:
        e.close()


def test_one_host_api_without_duplicates_default_marked(make):
    eng, _ = make(WINDOWS)
    devs = eng.list_devices()
    assert [d.name for d in devs] == ["Speakers (Realtek High Definition Audio)", "USB Audio (Behringer UCA202)"]
    assert [d.default for d in devs] == [True, False]
    assert len({d.id for d in devs}) == 2
    assert all(isinstance(d.to_json(), dict) for d in devs)


def test_alsa_ids_survive_card_renumbering_and_noise_is_hidden():
    ids = [i for i, _ in select_devices(PI)]
    assert ids == ["bcm2835 Headphones: -", "vc4-hdmi-0: MAI PCM i2s-hifi-0", "pulse", "default"]
    renumbered = [RawDevice(7, "bcm2835 Headphones: - (hw:3,0)", "ALSA", 2, 48000.0)]
    assert select_devices(renumbered)[0][0] == ids[0]


def test_duplicate_names_get_distinct_ids():
    raw = [RawDevice(0, "Speakers", "ALSA", 2, 48000.0), RawDevice(1, "Speakers", "ALSA", 2, 48000.0)]
    assert [i for i, _ in select_devices(raw)] == ["Speakers", "Speakers #2"]


def test_default_device_opens_and_plays_a_cue(make, tmp_path):
    eng, be = make(WINDOWS)
    assert isinstance(eng, AudioEngine)
    assert eng.wait_settled()
    st = eng.status()
    assert st["ok"] and st["error"] is None
    assert st["device_name"] == "Speakers (Realtek High Definition Audio)"
    s = be.current
    assert (s.rate, s.channels, s.active) == (48000, 2, True)
    f = index_file(tmp_path / "i.wav", 5)
    eng.sync([AudioCue("a", str(f), 2.0)], True)
    assert eng.status()["voices"] == 1
    assert eng.mixer.wait_ready()
    out = np.concatenate([s.pull(480) for _ in range(20)])
    assert frame_index(out)[5000] == int(2.0 * RATE) + int(0.05 * RATE) + 5000     # latency 50 ms compensated
    eng.set_volume(0.5)
    assert eng.status()["volume"] == 0.5


def test_set_device_switches_while_running(make, tmp_path):
    eng, be = make(WINDOWS)
    eng.wait_settled()
    first = be.current
    uid = [d.id for d in eng.list_devices()][1]
    eng.set_device(uid)
    assert eng.wait_settled()
    assert first.closed
    assert be.current is not first and be.current.rate == 44100
    st = eng.status()
    assert st["ok"] and st["device"] == uid and st["device_name"].startswith("USB Audio")
    eng.set_device(None)
    eng.wait_settled()
    assert eng.status()["device_name"].startswith("Speakers")


def test_unknown_device_is_reported_then_recovers(make, tmp_path):
    eng, be = make(WINDOWS)
    eng.wait_settled()
    eng.set_device("No Such Card")
    assert eng.wait_settled()
    st = eng.status()
    assert not st["ok"] and "No Such Card" in st["error"] and st["device_name"] is None
    f = tone_file(tmp_path / "t.wav", 2)
    eng.sync([AudioCue("a", str(f), 0.0)], True)      # keeps running silently, never raises
    eng.set_device(None)
    assert eng.wait_settled()
    assert eng.status()["ok"] and eng.status()["error"] is None


def test_open_failure_is_reported_and_retried(make):
    eng, be = make(WINDOWS)
    eng.wait_settled()
    be.fail_start.add("USB Audio (Behringer UCA202)")
    eng.set_device("USB Audio (Behringer UCA202)")
    eng.wait_settled()
    st = eng.status()
    assert not st["ok"] and "Invalid device" in st["error"]
    be.fail_start.clear()                              # card works again
    assert wait_for(lambda: eng.status()["ok"])
    assert eng.status()["device_name"].startswith("USB Audio")


def test_unplugged_device_does_not_crash_and_is_reopened(make, tmp_path):
    eng, be = make(WINDOWS)
    eng.set_device("USB Audio (Behringer UCA202)")
    eng.wait_settled()
    assert eng.status()["ok"]
    usb = be.devices.pop()                              # unplug
    be.current.active = False                          # PortAudio stops the stream
    assert wait_for(lambda: not eng.status()["ok"])
    assert "USB" in eng.status()["error"] or "stopped" in eng.status()["error"] or "not found" in eng.status()["error"]
    f = tone_file(tmp_path / "t.wav", 2)
    eng.sync([AudioCue("a", str(f), 0.0)], True)
    be.devices.append(usb)                              # plug in again
    assert wait_for(lambda: eng.status()["ok"])
    assert be.refreshes >= 1
    assert be.current.active


def test_no_devices_at_all(make):
    eng, _ = make([])
    eng.wait_settled()
    st = eng.status()
    assert not st["ok"] and st["error"]
    eng.sync([], True)


def test_watchdog_through_the_engine(make, tmp_path):
    eng, be = make(WINDOWS, watchdog=0.15)
    eng.wait_settled()
    f = tone_file(tmp_path / "t.wav", 5)
    eng.sync([AudioCue("a", str(f), 0.0)], True)
    eng.mixer.wait_ready()
    s = be.current
    assert np.abs(np.concatenate([s.pull(480) for _ in range(10)])).max() > 0.1
    time.sleep(0.3)
    out = np.concatenate([s.pull(480) for _ in range(5)])
    assert np.abs(out[960:]).max() == 0.0


def test_close_is_clean_and_idempotent(make, tmp_path):
    eng, be = make(WINDOWS)
    eng.wait_settled()
    f = tone_file(tmp_path / "t.wav", 5)
    eng.sync([AudioCue("a", str(f), 0.0)], True)
    s = be.current
    eng.close()
    assert s.closed and not s.active
    assert eng.status()["voices"] == 0
    eng.sync([AudioCue("a", str(f), 0.0)], True)       # after close: ignored
    eng.close()


def test_factory_falls_back_to_silence_without_portaudio():
    try:
        import sounddevice  # noqa: F401
        pytest.skip("PortAudio present")
    except OSError:
        pass
    assert type(create_audio_engine()).__name__ == "NullAudio"
