"""Setup checks (State.checks): one case per thing that can stand between the user and a working sign.

The failures come from the real code that detects them (the raw socket openers, the Colorlight driver
against a fake socket, the sound engine against a fake backend); only the platform conditions are faked.
"""
import sys

import numpy as np
import pytest

from test_audio_engine import WINDOWS, FakeBackend
from test_output_colorlight import FakeSocket, make_driver, reply, wait_for
from test_player_fakes import FakeAudio, FakeDriver
from trafficlight import config
from trafficlight.audio.base import NullAudio
from trafficlight.audio.engine import SoundDeviceEngine
from trafficlight.checks import LEVELS, setup_checks
from trafficlight.engine import Engine
from trafficlight.model import AudioSettings, OutputSettings, Settings
from trafficlight.output import rawsock_base, rawsock_npcap
from trafficlight.output.base import OutputError
from trafficlight.store import Store

AUDIO_OK = {"ok": True, "error": None, "device": None, "device_name": "Speakers", "volume": 0.8, "problems": []}
COLORLIGHT = Settings(output=OutputSettings(driver="colorlight", interface="eth1"))
WIRED = Settings(output=OutputSettings(driver="colorlight", interface="eth1", panel_order=[2, 7, 1, 0, 4, 3],
                                       panel_flip=[False, True] * 3))


def by_id(checks):
    assert len({c["id"] for c in checks}) == len(checks), "one entry per check"
    for c in checks:
        assert c["level"] in LEVELS and c["title"] and c["detail"]
        assert c["level"] == "ok" or c["fix"] or c["id"] in ("output.driver", "output.link", "output.receiver",
                                                              "audio.device")
    return {c["id"]: c for c in checks}


def failed_open(e: OutputError):
    """The engine's output status after the driver failed to open with `e`."""
    return {"driver": "colorlight", "ok": False, "error": str(e), "interface": "eth1", "problems": [e.as_problem()]}


def driver_checks(sock, settings=COLORLIGHT, ready=lambda st: True, **kw):
    drv = make_driver(sock, **kw)
    drv.open()
    try:
        assert wait_for(lambda: ready(drv.status()))
        return by_id(setup_checks(settings, drv.status(), AUDIO_OK))
    finally:
        drv.close()


# ------------------------------------------------------------------ output
def test_driver_off_is_a_neutral_note():
    c = setup_checks(Settings(), {"driver": "none", "ok": True, "error": None}, AUDIO_OK)
    assert [(x["id"], x["level"]) for x in c] == [("output.driver", "info"), ("audio.device", "ok")]
    assert "Colorlight" in c[0]["fix"]


def test_no_interface_chosen():
    c = by_id(setup_checks(Settings(output=OutputSettings(driver="colorlight")),
                           failed_open(rawsock_base.no_interface_error()), AUDIO_OK))
    assert c["output.interface"]["level"] == "error" and "Network interface" in c["output.interface"]["fix"]
    assert "output.link" not in c and "output.receiver" not in c       # nothing to say about those yet


def test_interface_missing():
    c = by_id(setup_checks(COLORLIGHT, failed_open(rawsock_base.missing_interface_error("no-such-if0")), AUDIO_OK))
    assert c["output.interface"]["title"] == "Network interface not found"
    assert "'no-such-if0'" in c["output.interface"]["detail"] and "Plug the adapter in" in c["output.interface"]["fix"]


def test_npcap_not_installed(monkeypatch):
    monkeypatch.setattr(rawsock_npcap, "_wpcap", None)
    monkeypatch.setattr(rawsock_npcap, "npcap_dir", lambda: config.REPO_ROOT / "no-npcap-here")

    def no_dll(name):
        raise OSError(f"cannot load {name}")
    monkeypatch.setattr(rawsock_npcap.ctypes, "CDLL", no_dll)
    with pytest.raises(OutputError) as e:
        rawsock_npcap.NpcapRawSocket("\\Device\\NPF_{X}")
    c = by_id(setup_checks(COLORLIGHT, failed_open(e.value), AUDIO_OK))["output.access"]
    assert c["level"] == "error" and c["title"] == "Npcap is not installed"
    assert "npcap.com" in c["fix"] and "Administrators only' unticked" in c["fix"]


def test_npcap_admin_only(monkeypatch):
    monkeypatch.setattr(rawsock_npcap, "load_wpcap", lambda: object())
    monkeypatch.setattr(rawsock_npcap, "find_adapter", lambda i: rawsock_npcap.Adapter(
        "{X}", "Ethernet 2", "Intel", "", True, 10 ** 9, 6))
    monkeypatch.setattr(rawsock_npcap, "npcap_admin_only", lambda: True)
    monkeypatch.setattr(rawsock_npcap, "is_elevated", lambda: False)
    with pytest.raises(OutputError) as e:
        rawsock_npcap.NpcapRawSocket("Ethernet 2")
    c = by_id(setup_checks(COLORLIGHT, failed_open(e.value), AUDIO_OK))["output.access"]
    assert c["title"] == "Npcap is restricted to administrators"
    assert "Reinstall Npcap" in c["fix"] and "unticked" in c["fix"]
    assert "Administrators only" in str(e.value)                     # the whole message still reads on its own


def test_linux_permission():
    rawsock_linux = pytest.importorskip("trafficlight.output.rawsock_linux")    # needs fcntl: not on Windows
    c = by_id(setup_checks(COLORLIGHT, failed_open(rawsock_linux.permission_error()), AUDIO_OK))["output.access"]
    assert c["title"] == "No permission for raw Ethernet"
    assert "AmbientCapabilities=CAP_NET_RAW" in c["fix"] and "sudo setcap cap_net_raw,cap_net_admin+eip" in c["fix"]


def test_cannot_open_for_another_reason():
    c = by_id(setup_checks(COLORLIGHT, failed_open(rawsock_base.open_failed_error("eth1", "Network is down")),
                           AUDIO_OK))["output.access"]
    assert c["title"] == "Cannot open the network interface" and "Network is down" in c["detail"]


def test_unexpected_driver_failure(tmp_path):
    e = Engine(Store(tmp_path), audio=FakeAudio(), driver_factory=lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    e.store.update_settings({"output": {"driver": "colorlight", "interface": "eth1"}})
    e.step(1.0)
    c = by_id(e.state()["checks"])["output.driver"]
    assert c["level"] == "error" and c["detail"] == "boom"


def test_link_down():
    c = driver_checks(FakeSocket(answer=reply(), link=(False, None)))
    assert c["output.link"]["level"] == "error" and c["output.link"]["title"] == "No link on the network port"
    assert c["output.interface"]["level"] == "ok" and c["output.access"]["level"] == "ok"
    assert "output.receiver" not in c


def test_link_not_gigabit():
    c = driver_checks(FakeSocket(answer=reply(), link=(True, 100)), ready=lambda st: st["receiver_found"])
    assert c["output.link"]["level"] == "error" and "100 Mb/s" in c["output.link"]["detail"]
    assert c["output.receiver"]["level"] == "ok"


def test_receiver_not_answering():
    c = driver_checks(FakeSocket(answer=None), ready=lambda st: not st["ok"])
    assert c["output.link"]["level"] == "ok" and c["output.link"]["title"] == "Link up, 1000 Mb/s"
    assert c["output.receiver"]["level"] == "error" and "cable" in c["output.receiver"]["fix"]
    assert "output.card_size" not in c and "output.wiring" not in c


def test_still_looking_for_the_receiver():
    st = {"driver": "colorlight", "ok": True, "error": None, "open": True, "link": True, "link_mbps": 1000,
          "receiver": None, "problems": [], "detecting": True, "sent_fps": 60.0, "interface_name": "eth1"}
    assert by_id(setup_checks(COLORLIGHT, st, AUDIO_OK))["output.receiver"]["level"] == "info"


def test_receiver_configured_for_another_size():
    # 1376x688 is what the user's card reported before its LEDVision setup.
    c = driver_checks(FakeSocket(answer=reply(size=(1376, 688))), ready=lambda st: st["receiver_found"])
    assert c["output.receiver"]["level"] == "ok"
    size = c["output.card_size"]
    assert size["level"] == "error" and "1376x688" in size["detail"] and f"{config.W}x{config.CARD_H}" in size["detail"]
    assert "LEDVision" in size["fix"] and "section 4" in size["fix"]
    assert "output.wiring" not in c


def test_all_fine_but_wiring_never_set_is_a_hint():
    c = driver_checks(FakeSocket(answer=reply()), ready=lambda st: st["receiver_found"])
    assert [k for k, v in c.items() if v["level"] != "ok"] == ["output.wiring"]
    assert c["output.wiring"]["level"] == "info" and "Panel wiring" in c["output.wiring"]["fix"]
    assert c["output.card_size"]["title"] == f"Receiver card set up for {config.W}x{config.CARD_H}"
    assert list(c) == ["output.driver", "output.interface", "output.access", "output.link", "output.receiver",
                       "output.card_size", "output.wiring", "audio.device"]


def test_all_fine():
    c = driver_checks(FakeSocket(answer=reply()), settings=WIRED, ready=lambda st: st["receiver_found"],
                      panel_order=WIRED.output.panel_order, panel_flip=WIRED.output.panel_flip)
    assert all(v["level"] == "ok" for v in c.values())
    assert c["output.wiring"]["detail"] == "From the top: J3, J8 upside down, J2, J1 upside down, J5, J4 upside down."


def test_sending_failure_is_listed():
    st = {"driver": "colorlight", "ok": False, "error": "x", "open": True, "link": True, "link_mbps": 1000,
          "receiver": None, "detecting": True, "problems": [
              {"id": "output.send", "level": "error", "title": "Sending to the card failed", "detail": "Sending failed: x",
               "fix": "retry"}]}
    assert by_id(setup_checks(COLORLIGHT, st, AUDIO_OK))["output.send"]["level"] == "error"


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
