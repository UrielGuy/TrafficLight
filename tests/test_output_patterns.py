"""Test patterns and the bring-up tool (against a fake card on localhost)."""
import numpy as np
import pytest

from fpga_fake import FakeCard, wait_for

from trafficlight import config
from trafficlight.model import OutputSettings, Settings
from trafficlight.output import patterns
from trafficlight.tools import testpattern

PH = config.PANEL_H


@pytest.mark.parametrize("name", patterns.NAMES)
def test_shapes(name):
    f = patterns.make(name)
    assert f.shape == (config.H, config.W, 3) and f.dtype == np.uint8 and f.flags.writeable


def test_unknown_and_off():
    assert patterns.make("nope") is None and patterns.make("off") is None


def test_result_is_a_copy():
    a = patterns.make("panels")
    a[:] = 0
    assert patterns.make("panels").any()


def test_solids():
    assert (patterns.make("red") == (255, 0, 0)).all()
    assert (patterns.make("white") == 255).all()


def test_gradient_ramps():
    g = patterns.make("gradient")
    assert g[0, 0].tolist() == [0, 0, 0] and g[0, -1].tolist() == [255, 0, 0]
    assert g[-1, -1].tolist() == [255, 255, 255]
    assert np.all(np.diff(g[0, :, 0].astype(int)) >= 0)


def test_panels_tell_the_panels_apart():
    f = patterns.make("panels").astype(int)
    for i in range(config.PANELS):
        p = f[i * PH:(i + 1) * PH]
        assert p[1, 1].tolist() == [255, 255, 255]                  # top-left marker
        assert p[-2, -2].tolist() != [255, 255, 255]                # nothing like it bottom-right
        assert p[0].min() == 255 and p[-1].min() == 255             # edge frame
        assert p[PH // 2, 3].tolist() == list(patterns.PANEL_COLORS[i])
        white = (p == 255).all(axis=2)
        assert white[8:-8, 10:80].sum() > 150                       # the big number
    colors = {tuple(c) for c in patterns.PANEL_COLORS}
    assert len(colors) == config.PANELS


def test_panels_upside_down_is_distinguishable():
    f = patterns.make("panels")
    p = f[:PH]
    assert not np.array_equal(p, p[::-1, ::-1])


def args(*a):
    return testpattern.parser().parse_args(list(a))


def test_build_settings_uses_saved_then_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAFFICLIGHT_DATA", str(tmp_path))
    saved = Settings(output=OutputSettings(color_order="bgr", panel_flip=[True] * 6, brightness=0.2,
                                           card_address="10.75.3.4", driver="none"))
    (tmp_path / "settings.json").write_text(saved.model_dump_json())
    s = testpattern.build_settings(args())
    assert s.color_order == "bgr" and s.panel_flip == [True] * 6 and s.card_address == "10.75.3.4"
    assert s.driver == "fpga"
    s = testpattern.build_settings(args("--order", "5,4,3,2,1,0", "--flip", "0,1,0,1,0,1", "--brightness", "0.3",
                                        "--card", "", "--port", "6000"))
    assert s.panel_order == [5, 4, 3, 2, 1, 0] and s.panel_flip == [False, True] * 3 and s.brightness == 0.3
    assert s.card_address == "" and s.card_port == 6000
    assert testpattern.build_settings(args("--raw")).color_order == "rgb"
    with pytest.raises(ValueError):
        testpattern.build_settings(args("--card", "10.75.0.2:80"))
    with pytest.raises(ValueError):
        testpattern.build_settings(args("--order", "0,0,1,2,3,4"))


def test_tool_takes_what_the_panels_showed(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAFFICLIGHT_DATA", str(tmp_path))
    assert testpattern.parse_seen("3, 8d,2u,1D,-,4") == [(3, False), (8, True), (2, False), (1, True), None, (4, False)]
    s = testpattern.build_settings(args("--seen", "3,8d,2,1d,5,4d", "--raw"))
    assert s.panel_order == [2, 7, 1, 0, 4, 3] and s.panel_flip == [False, True, False, True, False, True]
    assert not s.rotate180
    with pytest.raises(ValueError):
        testpattern.build_settings(args("--seen", "3,3,2,1,5,4"))
    with pytest.raises(ValueError):
        testpattern.build_settings(args("--seen", "3,8d,2,1d,5,4", "--rotate180"))
    with pytest.raises(ValueError):
        testpattern.parse_seen("3,x,2,1,5,4")


def test_tool_reports_an_address_that_does_not_resolve(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRAFFICLIGHT_DATA", str(tmp_path))
    assert testpattern.main(["--card", "no-such-host.invalid", "--pattern", "panels", "--seconds", "0.1"]) == 1
    assert "cannot be resolved" in capsys.readouterr().err


# Against a fake card on localhost: no real port (5775, 5776) is bound and nothing leaves 127.0.0.1.
LOCAL = ("--raw", "--card", "127.0.0.1", "--options", "announce_port=0,broadcast=,dither=0")


@pytest.fixture
def card():
    c = FakeCard()
    yield c
    c.close()


def test_tool_detects_the_card(tmp_path, monkeypatch, capsys, card):
    monkeypatch.setenv("TRAFFICLIGHT_DATA", str(tmp_path))
    assert testpattern.main([*LOCAL, "--port", str(card.port), "--detect"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("card 127.0.0.1:") and "firmware 0.1" in out


def test_tool_shows_a_pattern_and_leaves_the_sign_dark(tmp_path, monkeypatch, capsys, card):
    monkeypatch.setenv("TRAFFICLIGHT_DATA", str(tmp_path))
    assert testpattern.main([*LOCAL, "--port", str(card.port), "--pattern", "wiring", "--seconds", "1.5"]) == 0
    assert "done: " in capsys.readouterr().out
    assert any(b.strip(b"\0") for _, b in list(card.shown))
    wait_for(lambda: not card.shown[-1][1].strip(b"\0"), what="the sign dark")
