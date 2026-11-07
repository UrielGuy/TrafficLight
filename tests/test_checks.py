"""Setup checks (State.checks): one case per thing that can stand between the user and a working sign.

The output failures come from status dicts shaped like the custom-firmware driver's (the driver itself, against
a fake card, is in test_output_fpga.py); the sound failures from the sound engine against a fake backend.
"""
import sys

import numpy as np
import pytest

from test_audio_engine import WINDOWS, FakeBackend
from test_player_fakes import FakeAudio, FakeDriver
from trafficlight.audio.base import NullAudio
from trafficlight.audio.engine import SoundDeviceEngine
from trafficlight.checks import LEVELS, setup_checks
from trafficlight.engine import Engine
from trafficlight.model import AudioSettings, OutputSettings, Settings
from trafficlight.output.base import OutputError, problem
from trafficlight.store import Store

AUDIO_OK = {"ok": True, "error": None, "device": None, "device_name": "Speakers", "volume": 0.8, "problems": []}
FPGA = Settings(output=OutputSettings(driver="fpga"))


def by_id(checks):
    assert len({c["id"] for c in checks}) == len(checks), "one entry per check"
    for c in checks:
        assert c["level"] in LEVELS and c["title"] and c["detail"]
        assert c["level"] == "ok" or c["fix"] or c["id"] in ("output.driver", "output.receiver", "output.schedule",
                                                              "audio.device")
    return {c["id"]: c for c in checks}


def failed_open(e: OutputError):
    """The engine's output status after the driver failed to open with `e`."""
    return {"driver": "fpga", "ok": False, "error": str(e), "problems": [e.as_problem()]}


def running(**kw):
    """An open driver's status, no card heard yet."""
    return {"driver": "fpga", "ok": True, "error": None, "open": True, "problems": [], "address": None,
            "answering": False, "found": None, "others": [], "card": None, "schedule": None, "sent_fps": 0.0,
            "holding": False, **kw}


# ------------------------------------------------------------------ output
def test_driver_off_is_a_neutral_note():
    c = setup_checks(Settings(output=OutputSettings(driver="none")), {"driver": "none", "ok": True, "error": None},
                     AUDIO_OK)
    assert [(x["id"], x["level"]) for x in c] == [("output.driver", "info"), ("audio.device", "ok")]
    assert "Custom firmware" in c[0]["fix"]


def test_the_socket_cannot_be_opened():
    e = OutputError("Cannot open a UDP socket: denied.", "Check that no firewall blocks this program.",
                    check="output.access", title="The network socket cannot be opened")
    c = by_id(setup_checks(FPGA, failed_open(e), AUDIO_OK))
    assert c["output.access"]["level"] == "error" and "firewall" in c["output.access"]["fix"]
    assert "output.receiver" not in c and "output.wiring" not in c        # nothing to say about those yet


def test_unexpected_driver_failure(tmp_path):
    e = Engine(Store(tmp_path), audio=FakeAudio(), driver_factory=lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    e.step(1.0)
    c = by_id(e.state()["checks"])["output.driver"]
    assert c["level"] == "error" and c["detail"] == "boom"


def test_still_looking_for_the_card():
    c = by_id(setup_checks(FPGA, running(), AUDIO_OK))
    assert c["output.receiver"]["level"] == "info" and "announcements" in c["output.receiver"]["detail"]
    assert c["output.driver"]["detail"] == "Open, waiting for frames."


def test_a_held_still_picture_is_explained():
    c = by_id(setup_checks(FPGA, running(holding=True), AUDIO_OK))["output.driver"]
    assert c["level"] == "ok" and "still picture" in c["detail"]


def test_sending_failure_is_listed():
    st = running(ok=False, error="x", problems=[problem("output.send", "Sending to the card failed",
                                                        "Sending failed: x", "retry")])
    assert by_id(setup_checks(FPGA, st, AUDIO_OK))["output.send"]["level"] == "error"


# ------------------------------------------------------------------ audio, web UI
def test_audio_backend_unavailable(tmp_path):
    e = Engine(Store(tmp_path), audio=NullAudio("PortAudio library not found"), driver_factory=lambda s: FakeDriver(s))
    e.step(1.0)
    st = e.state()
    c = by_id(st["checks"])["audio.backend"]
    assert c["level"] == "error" and "PortAudio library not found" in c["detail"]
    assert ("libportaudio2" in c["fix"]) if sys.platform.startswith("linux") else ("requirements.txt" in c["fix"])
    assert st["audio"]["error"] == "No sound output on this machine"


@pytest.fixture
def sound():
    engines = []

    def build(devices=WINDOWS):
        eng = SoundDeviceEngine(FakeBackend(devices), retry_seconds=0.05)
        engines.append(eng)
        return eng

    yield build
    for e in engines:
        e.close()


def test_audio_device_missing(sound):
    eng = sound()
    eng.set_device("USB Audio (Behringer UCA202) gone")
    eng.wait_settled()
    c = by_id(setup_checks(Settings(), {"driver": "none"}, eng.status()))["audio.device"]
    assert c["level"] == "error" and c["title"] == "The chosen sound device is not there"
    assert "Settings → Audio → Output device" in c["fix"]


def test_audio_device_fails_to_open(sound):
    eng = sound()
    eng.wait_settled()
    eng._backend.fail_start.add("USB Audio (Behringer UCA202)")
    eng.set_device("USB Audio (Behringer UCA202)")
    eng.wait_settled()
    c = by_id(setup_checks(Settings(), {"driver": "none"}, eng.status()))["audio.device"]
    assert c["title"] == "The sound device cannot be opened" and "Invalid device" in c["detail"]


def test_no_sound_device_at_all(sound):
    eng = sound([])
    eng.wait_settled()
    c = by_id(setup_checks(Settings(), {"driver": "none"}, eng.status()))["audio.device"]
    assert c["title"] == "No sound output device"


def test_audio_fine(sound):
    eng = sound()
    eng.wait_settled()
    c = by_id(setup_checks(Settings(audio=AudioSettings(volume=0.5)), {"driver": "none"}, eng.status()))["audio.device"]
    assert c["level"] == "ok" and c["title"].startswith("Sound: ")


def test_web_ui_missing():
    c = by_id(setup_checks(Settings(), {"driver": "none"}, AUDIO_OK, web_ui=False))
    assert c["server.web_ui"]["level"] == "warning" and "npm run build" in c["server.web_ui"]["fix"]
    assert "server.web_ui" not in by_id(setup_checks(Settings(), {"driver": "none"}, AUDIO_OK, web_ui=True))


def test_state_carries_the_checks(tmp_path):
    e = Engine(Store(tmp_path), audio=FakeAudio(), driver_factory=lambda s: FakeDriver(s))
    e.step(1.0)
    checks = e.state()["checks"]
    assert checks[0]["id"] == "output.driver" and checks[0]["level"] == "info"
    assert np.all([set(c) == {"id", "level", "title", "detail", "fix"} for c in checks])
