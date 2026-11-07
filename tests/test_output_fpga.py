"""The custom-firmware driver against a fake card (the behavioural model behind a localhost UDP socket)."""
import socket
import time

import numpy as np
import pytest

from fpga_fake import FakeCard, wait_for
from trafficlight import config
from trafficlight.model import OutputSettings
from trafficlight.output import fpga
from trafficlight.output.base import OutputError, create_driver
from trafficlight.output.fpga import FpgaDriver, FpgaOptions
from trafficlight.output.hub75 import protocol as P
from trafficlight.output.hub75.pack import Packer
from trafficlight.output.mapping import Mapper


@pytest.fixture(autouse=True)
def quick_polling(monkeypatch):
    monkeypatch.setattr(fpga, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(fpga, "ANSWER_TIMEOUT", 0.5)
    monkeypatch.setattr(fpga, "LOST_AFTER", 0.15)
    monkeypatch.setattr(fpga, "RESEND_EVERY", 0.2)
    monkeypatch.setattr(fpga, "HEALTH_WINDOW", 5.0)


@pytest.fixture
def card():
    c = FakeCard()
    yield c
    c.close()


@pytest.fixture
def make(card):
    drivers = []

    def make(fps=60, brightness=1.0, gamma=2.2, address="127.0.0.1", port=None, options=None, factory=None, **kw):
        s = OutputSettings(driver="fpga", card_address=address, card_port=port or card.port,
                           brightness=brightness, gamma=gamma, **kw)
        d = FpgaDriver(s, options or FpgaOptions(dither=False, broadcast=""), factory)
        d.set_frame_rate(fps)
        d.open()
        d.set_brightness(brightness)
        drivers.append(d)
        return d

    yield make
    for d in drivers:
        d.close()


def picture(seed=1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    base = np.linspace(0, 255, config.H * config.W * 3).reshape(config.H, config.W, 3)
    return ((base + rng.integers(0, 40, base.shape)) % 256).astype(np.uint8)


def expected(frame, schedule, gamma=2.2, brightness=1.0, dither=False, card_image=False):
    """What the card should hold for a picture: the same mapping and packing, done independently."""
    m = Mapper(OutputSettings(gamma=1.0))
    img = m.apply_card(frame) if card_image else m.apply(frame)
    return Packer(schedule, gamma, brightness, dither).pack(img).tobytes()


def ready(card, d):
    wait_for(lambda: card.active is not None and d.status()["answering"], what="schedule active")


# ------------------------------------------------------------------ discovery and schedule
def test_schedule_is_built_from_the_status_and_activated(card, make):
    d = make()
    s = wait_for(lambda: card.active, what="schedule active")
    assert s.limits.tick_hz == 50_000_000 and s.planes == 12 and abs(s.frame_ticks - 50_000_000 / 60) < s.rows
    st = wait_for(lambda: d.status() if d.status()["card"] else None, what="status")
    assert st["ok"] and st["answering"] and st["card"]["firmware"] == "0.1"
    assert wait_for(lambda: d.status()["card"]["schedule_id"] == s.schedule_id)
    assert st["schedule"]["planes"] == 12 and st["problems"] == []


def test_a_card_with_other_limits_gets_a_schedule_for_them(make):
    from trafficlight.output.hub75.schedule import CardLimits
    c = FakeCard(CardLimits(tick_hz=100_000_000, min_visit_ticks=300))
    try:
        d = make(port=c.port)
        s = wait_for(lambda: c.active, what="schedule")
        assert s.limits.tick_hz == 100_000_000 and all(e.visit_ticks >= 300 for e in s.entries)
        assert abs(s.frame_ticks - 100_000_000 / 60) < s.rows
    finally:
        c.close()


def test_card_found_by_broadcast_when_its_address_is_wrong(card, make):
    d = make(address="127.0.0.2", options=FpgaOptions(dither=False, broadcast="127.0.0.1"))
    wait_for(lambda: card.active is not None, what="schedule via broadcast")
    p = wait_for(lambda: [x for x in d.status()["problems"] if x["id"] == "output.receiver"], what="warning")
    assert p[0]["level"] == "warning" and "127.0.0.1" in p[0]["fix"] and d.status()["ok"]


def test_no_answer_is_a_problem_with_a_fix(make):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    try:
        d = make(port=s.getsockname()[1])
        p = wait_for(lambda: next((x for x in d.status()["problems"] if x["id"] == "output.receiver"), None),
                     what="no-answer problem")
    finally:
        s.close()
    assert p["level"] == "error" and "does not answer" in p["title"]
    assert "10.75.0.1" in p["fix"] and "ping 10.75.0.2" in p["fix"]
    st = d.status()
    assert not st["ok"] and st["answering"] is False and st["error"].startswith("No answer from 127.0.0.1")


def test_unresolvable_address_fails_to_open(card):
    d = FpgaDriver(OutputSettings(driver="fpga", card_address="no-such-host.invalid"), FpgaOptions())
    with pytest.raises(OutputError) as e:
        d.open()
    assert e.value.check == "output.settings" and "no-such-host.invalid" in e.value.detail


# ------------------------------------------------------------------ frames
def test_a_frame_arrives_whole_and_equals_the_packed_bytes(card, make):
    d = make()
    ready(card, d)
    f = picture()
    d.send(f)
    want = expected(f, card.active)
    got = wait_for(lambda: card.shown_frame == want, what="frame shown")
    assert got and card.counter("frames_incomplete") == 0 and card.counter("frames_rejected") == 0
    st = d.status()
    assert st["frames"] >= 1 and st["pack_ms"] > 0


def test_a_card_image_skips_the_panel_mapping_but_not_the_gamma(card, make):
    d = make(panel_order=[5, 4, 3, 2, 1, 0], color_order="bgr")
    ready(card, d)
    img = picture(2)[:config.CARD_H] if config.CARD_H <= config.H else np.pad(picture(2), ((0, config.CARD_H - config.H), (0, 0), (0, 0)))
    d.send_card(img)
    m = Mapper(OutputSettings(gamma=1.0, color_order="bgr"))
    want = Packer(card.active, 2.2, 1.0, False).pack(m.apply_card(img)).tobytes()
    wait_for(lambda: card.shown_frame == want, what="card image shown")


def test_panel_order_and_colour_order_follow_the_mapper(card, make):
    d = make(panel_order=[1, 0, 2, 3, 4, 5], color_order="brg")
    ready(card, d)
    f = picture(3)
    d.send(f)
    m = Mapper(OutputSettings(gamma=1.0, panel_order=[1, 0, 2, 3, 4, 5], color_order="brg"))
    want = Packer(card.active, 2.2, 1.0, False).pack(m.apply(f)).tobytes()
    wait_for(lambda: card.shown_frame == want, what="mapped frame shown")


def test_the_newest_frame_wins(card, make):
    d = make()
    ready(card, d)
    frames = [picture(i) for i in range(12)]
    for f in frames:
        d.send(f)
    want = expected(frames[-1], card.active)
    wait_for(lambda: card.shown_frame == want, what="the last frame shown")
    assert d.status()["frames"] + d.status()["dropped"] >= 1


def test_identical_frames_are_not_sent_again(card, make):
    d = make()
    ready(card, d)
    f = picture()
    d.send(f)
    wait_for(lambda: card.shown_frame is not None)
    n = d.status()["frames"]
    for _ in range(5):
        d.send(f)
        time.sleep(0.02)
    st = wait_for(lambda: d.status() if d.status()["held"] >= 5 else None, what="held frames")
    assert st["frames"] == n and st["holding"]


def test_dithering_keeps_a_still_picture_changing(card, make):
    d = make(options=FpgaOptions(dither=True, broadcast=""), brightness=1.0, gamma=1.0)
    ready(card, d)
    f = np.full((config.H, config.W, 3), 100, np.uint8)
    # 100/255 of 4095 = 1605.88: the carried fraction makes the packed bytes alternate between 1605 and 1606;
    # a frame equal to the last one sent is not sent, the rest are.
    for _ in range(40):
        d.send(f)
        time.sleep(0.01)
    st = wait_for(lambda: d.status() if d.status()["frames"] >= 3 else None, what="several dithered frames")
    levels = {int(Packer_unpack(s)[0, 0, 0]) for _, s in card.shown}
    assert levels <= {1605, 1606} and len(levels) == 2 and st["held"] > 0


def Packer_unpack(frame):
    from trafficlight.output.hub75.pack import unpack
    return unpack(frame, 12, 32, config.W)


def test_frames_wait_for_the_schedule_instead_of_being_rejected(card, make):
    card.silent = True
    d = make()
    f = picture()
    d.send(f)
    time.sleep(0.1)
    assert card.shown_frame is None and not card.received
    card.silent = False
    want_after = lambda: card.active is not None and card.shown_frame == expected(f, card.active)
    wait_for(want_after, what="held frame sent once the schedule was in")
    assert card.counter("frames_rejected") == 0


# ------------------------------------------------------------------ loss
def test_a_frame_that_lost_a_chunk_is_never_shown_and_the_next_is(card, make):
    d = make()
    ready(card, d)
    a, b = picture(5), picture(6)
    lost = {}
    card.drop = lambda p: isinstance(p, P.FrameChunk) and p.chunk_index == 7 and lost.setdefault("id", p.frame_id) == p.frame_id
    d.send(a)
    wait_for(lambda: d.status()["frames"] >= 1)
    wait_for(lambda: card.received and any(isinstance(p, P.FrameCommit) for p in card.received), what="commit of A")
    d.send(b)
    want = expected(b, card.active)
    wait_for(lambda: card.shown_frame == want, what="B shown")
    assert card.counter("frames_incomplete") == 1
    assert expected(a, card.active) not in [s for _, s in card.shown]


def test_a_lost_last_frame_of_a_still_picture_is_sent_again(card, make):
    d = make()
    ready(card, d)
    f = picture(7)
    first = {}
    card.drop = lambda p: isinstance(p, P.FrameCommit) and first.setdefault("id", p.frame_id) == p.frame_id
    d.send(f)
    want = expected(f, card.active)
    wait_for(lambda: card.shown_frame == want, what="resent frame shown")
    assert d.status()["resent"] >= 1


def test_card_loss_counters_become_warnings(card, make):
    d = make()
    ready(card, d)
    time.sleep(0.2)
    assert d.status()["problems"] == []
    card.bump("rx_dropped", 9)
    card.bump("underruns", 2)
    card.bump("frames_incomplete", 4)
    ids = lambda: {p["id"]: p for p in d.status()["problems"]}
    wait_for(lambda: {"output.packets", "output.timing"} <= set(ids()), what="warnings")
    p = ids()
    assert p["output.packets"]["level"] == "warning" and "9 packets" in p["output.packets"]["detail"]
    assert "gigabit" in p["output.packets"]["fix"]
    assert p["output.timing"]["level"] == "warning" and "2 rows" in p["output.timing"]["detail"]
    st = d.status()
    assert st["ok"] and len(st["warnings"]) == 2
    assert st["card"]["rx_dropped"] == 9 and st["card"]["underruns"] == 2 and st["card"]["frames_incomplete"] == 4


def test_old_counts_from_before_the_driver_started_are_not_news(card, make):
    card.bump("rx_dropped", 1000)
    card.bump("frames_incomplete", 50)
    d = make()
    ready(card, d)
    time.sleep(0.3)
    assert d.status()["problems"] == []
    assert d.status()["card"]["rx_dropped"] == 1000


# ------------------------------------------------------------------ changes
def test_card_restart_uploads_the_schedule_and_the_picture_again(card, make):
    d = make()
    ready(card, d)
    f = picture()
    d.send(f)
    sched = card.active
    want = expected(f, sched)
    wait_for(lambda: card.shown_frame == want)
    wait_for(lambda: card.card.uptime_ms > 100, what="uptime")
    card.restart()
    assert card.active is None and card.shown_frame is None
    wait_for(lambda: card.active is not None and card.active.schedule_id == sched.schedule_id, what="schedule again")
    wait_for(lambda: card.shown_frame == want, what="picture again, nothing new was sent")


def test_brightness_change_repacks_the_picture_through_the_lut(card, make):
    d = make(brightness=1.0)
    ready(card, d)
    f = picture()
    d.send(f)
    wait_for(lambda: card.shown_frame == expected(f, card.active))
    d.set_brightness(0.25)
    sched = card.active
    wait_for(lambda: card.shown_frame == expected(f, sched, brightness=0.25), what="dimmed picture")
    assert card.active.schedule_id == sched.schedule_id          # a new LUT, the same schedule
    assert d.status()["brightness"] == 0.25


def test_frame_rate_change_builds_and_uploads_a_new_schedule(card, make):
    d = make(fps=60)
    ready(card, d)
    f = picture()
    d.send(f)
    old = card.active
    wait_for(lambda: card.shown_frame == expected(f, old))
    d.set_frame_rate(30)
    new = wait_for(lambda: card.active if card.active.schedule_id != old.schedule_id else None, what="new schedule")
    assert abs(new.frame_ticks - 50_000_000 / 30) < new.rows
    wait_for(lambda: card.shown_frame == expected(f, new), what="picture under the new schedule")
    wait_for(lambda: d.status()["schedule"]["frame_hz"] == pytest.approx(30, abs=0.1), what="status shows 30 Hz")


def test_a_rate_the_depth_does_not_fit_gives_fewer_planes_and_says_so(card, make):
    d = make(fps=120)
    s = wait_for(lambda: card.active, what="schedule")
    assert s.planes < 12
    p = wait_for(lambda: next((x for x in d.status()["problems"] if x["id"] == "output.schedule"), None), what="warning")
    assert p["level"] == "warning" and f"running with {s.planes}" in p["detail"] and d.status()["ok"]


def test_a_schedule_that_cannot_fit_is_an_error(card, make):
    d = make(fps=1000, options=FpgaOptions(planes=12, dither=False, broadcast=""))
    p = wait_for(lambda: next((x for x in d.status()["problems"] if x["id"] == "output.schedule"), None), what="error")
    assert p["level"] == "error" and not d.status()["ok"] and card.active is None


def test_a_new_program_instance_starts_with_newer_frame_ids(card, make):
    d1 = make()
    ready(card, d1)
    for i in range(30):
        d1.send(picture(i))
        time.sleep(0.005)
    d1.close()
    d2 = make()
    f = picture(99)
    d2.send(f)
    wait_for(lambda: card.shown_frame == expected(f, card.active), what="frame of the second instance")
    assert card.counter("chunks_dropped") == 0


def test_close_leaves_the_card_dark(card, make):
    d = make()
    ready(card, d)
    f = picture()
    d.send(f)
    wait_for(lambda: card.shown_frame == expected(f, card.active))
    d.close()
    wait_for(lambda: card.shown_frame is not None and not any(card.shown_frame), what="black frame")


# ------------------------------------------------------------------ pacing
class Recorder(socket.socket):
    """A real UDP socket that stamps every frame datagram it sends."""
    stamps: list

    def sendto(self, data, addr):
        if len(data) > 16 and data[5] in (P.FRAME_DATA, P.FRAME_COMMIT):
            self.stamps.append((time.perf_counter(), data[5]))
        return super().sendto(data, addr)


def recorder_factory(stamps):
    def factory():
        s = Recorder(socket.AF_INET, socket.SOCK_DGRAM)
        s.stamps = stamps
        return s
    return factory


def test_packets_go_out_in_bursts_with_the_gaps_the_spec_asks_for(card, make):
    stamps = []
    d = make(factory=recorder_factory(stamps))
    ready(card, d)
    d.send(picture())
    wait_for(lambda: len(stamps) >= 206, what="one frame of packets")
    t = [s for s, _ in stamps[:206]]
    assert stamps[205][1] == P.FRAME_COMMIT and stamps[204][1] == P.FRAME_DATA
    gap = 250e-6
    for i in range(8, 206, 8):
        assert t[i] - t[i - 1] >= gap, f"group starting at packet {i} came {1e6 * (t[i] - t[i - 1]):.0f} us after the last"
    assert d.status()["min_gap_us"] >= 250
    wait_for(lambda: card.shown_frame is not None and card.counter("frames_incomplete") == 0)


def test_pacing_can_be_changed_and_switched_off(card, make):
    stamps = []
    d = make(factory=recorder_factory(stamps), options=FpgaOptions(dither=False, broadcast="", burst=4, gap_us=500))
    ready(card, d)
    d.send(picture())
    wait_for(lambda: len(stamps) >= 206)
    t = [s for s, _ in stamps[:206]]
    assert all(t[i] - t[i - 1] >= 500e-6 for i in range(4, 206, 4))
    assert t[205] - t[0] >= 51 * 500e-6 * 0.99          # 51 gaps of 500 us

    stamps2 = []
    d2 = make(factory=recorder_factory(stamps2), options=FpgaOptions(dither=False, broadcast="", pace=False))
    wait_for(lambda: d2.status()["answering"])
    d2.send(picture(4))
    wait_for(lambda: len(stamps2) >= 206)
    assert stamps2[205][0] - stamps2[0][0] < t[205] - t[0]


def test_pacing_costs_what_the_spec_says_per_frame(card, make):
    """206 packets in groups of 8 with 250 us between them: 25 gaps = 6.25 ms plus the sends themselves."""
    stamps = []
    d = make(factory=recorder_factory(stamps))
    ready(card, d)
    for i in range(10):
        d.send(picture(i))
        wait_for(lambda: d.status()["frames"] >= i + 1)
    ms = d.status()["send_ms"]
    assert 6.0 <= ms < 16.0, ms             # the spec: 6.5 ms, inside a 60 fps period


# ------------------------------------------------------------------ options, settings, factory
def test_options_parse():
    o = FpgaOptions.parse("planes=11, dither=off,burst=4,gap_us=300,broadcast=10.0.0.255/255.255.255.255")
    assert (o.planes, o.dither, o.burst, o.gap_us) == (11, False, 4, 300.0)
    assert o.broadcasts == ["10.0.0.255", "255.255.255.255"]
    assert FpgaOptions.parse(None) == FpgaOptions() and FpgaOptions().planes == 12 and FpgaOptions().burst == 8
    with pytest.raises(ValueError):
        FpgaOptions.parse("speed=3")
    with pytest.raises(ValueError):
        FpgaOptions.parse("chunk_bytes=1000")


def test_settings_defaults_and_old_files():
    s = OutputSettings.model_validate({"driver": "colorlight", "interface": "eth0", "brightness": 0.3})
    assert s.card_address == "10.75.0.2" and s.card_port == 5775
    assert OutputSettings(driver="fpga", card_address=" 192.168.1.5 ").card_address == "192.168.1.5"
    for bad in ("", "10.0.0.1:80", "a b"):
        with pytest.raises(ValueError):
            OutputSettings(card_address=bad)


def test_the_factory_makes_the_driver():
    assert isinstance(create_driver(OutputSettings(driver="fpga")), FpgaDriver)


# ------------------------------------------------------------------ setup checks
AUDIO_OK = {"ok": True, "error": None, "device": None, "device_name": "Speakers", "volume": 0.8, "problems": []}


def checks_of(d, **settings):
    from trafficlight.checks import LEVELS, setup_checks
    from trafficlight.model import Settings
    out = setup_checks(Settings(output=OutputSettings(driver="fpga", **settings)), d.status(), AUDIO_OK)
    assert len({c["id"] for c in out}) == len(out)
    for c in out:
        assert c["level"] in LEVELS and c["title"] and c["detail"]
        assert c["level"] in ("ok", "info") or c["fix"], c
    return {c["id"]: c for c in out}


def test_checks_when_everything_works(card, make):
    d = make()
    ready(card, d)
    d.send(picture())
    wait_for(lambda: d.status()["schedule"] and d.status()["card"]["schedule_id"] == card.active.schedule_id)
    c = checks_of(d, panel_order=[1, 0, 2, 3, 4, 5])
    assert list(c) == ["output.driver", "output.receiver", "output.firmware", "output.schedule", "output.packets",
                       "output.timing", "output.wiring", "audio.device"]
    assert all(v["level"] == "ok" for v in c.values())
    assert c["output.receiver"]["title"] == "LED card found" and "0.1" in c["output.receiver"]["detail"]
    assert "12 bits at 60 Hz" in c["output.schedule"]["title"]


def test_checks_when_the_card_does_not_answer(make):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    try:
        d = make(port=s.getsockname()[1])
        wait_for(lambda: not d.status()["ok"], what="no answer")
        c = checks_of(d)
    finally:
        s.close()
    assert c["output.driver"]["level"] == "ok" and c["output.receiver"]["level"] == "error"
    assert "output.firmware" not in c and "output.schedule" not in c and "output.wiring" not in c


def test_checks_show_the_cards_complaints(card, make):
    d = make()
    ready(card, d)
    time.sleep(0.2)
    card.bump("rx_dropped", 3)
    card.bump("underruns", 1)
    wait_for(lambda: len(d.status()["problems"]) == 2, what="warnings")
    c = checks_of(d)
    assert c["output.packets"]["level"] == "warning" and c["output.timing"]["level"] == "warning"
    assert c["output.receiver"]["level"] == "ok"


def test_checks_for_a_card_that_is_not_ours(card, make):
    card.limits = card.card.limits = type(card.limits)(max_cols=64)      # too narrow for 128 columns
    d = make()
    wait_for(lambda: d.status()["card"], what="status")
    c = checks_of(d)
    assert c["output.firmware"]["level"] == "error" and "64 columns" in c["output.firmware"]["detail"]


def test_checks_for_a_bad_address_and_for_looking(card):
    from trafficlight.checks import setup_checks
    from trafficlight.model import Settings
    d = FpgaDriver(OutputSettings(driver="fpga", card_address="no-such-host.invalid"), FpgaOptions())
    with pytest.raises(OutputError) as e:
        d.open()
    st = {"driver": "fpga", "ok": False, "error": str(e.value), "problems": [e.value.as_problem()]}
    c = {x["id"]: x for x in setup_checks(Settings(output=OutputSettings(driver="fpga")), st, AUDIO_OK)}
    assert c["output.settings"]["level"] == "error" and "no-such-host.invalid" in c["output.settings"]["detail"]
    looking = {"driver": "fpga", "ok": True, "error": None, "open": True, "problems": [], "card": None,
               "address": "10.75.0.2:5775"}
    c = {x["id"]: x for x in setup_checks(Settings(output=OutputSettings(driver="fpga")), looking, AUDIO_OK)}
    assert c["output.receiver"]["level"] == "info" and "10.75.0.2:5775" in c["output.receiver"]["detail"]
