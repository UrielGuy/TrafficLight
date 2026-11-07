"""Visit schedule: the budget for the defaults, the spread of the planes, validation, wire format."""
import pytest

from trafficlight import config
from trafficlight.output.hub75 import schedule as S
from trafficlight.output.hub75.schedule import CardLimits, Entry, Schedule, ScheduleError, ScheduleParams, build


def test_defaults_give_12_bits_at_60_fps_and_1920_hz():
    s = build()
    b = s.budget()
    assert s.planes == 12 and len(s.entries) == 69 == S.entry_count(12, 6) and len(s.subframes) == 32
    assert abs(b["frame_hz"] - 60.0) < 0.01                      # padded to exactly one pass per host frame
    assert 1900 < b["subframe_hz_mean"] < 1940 and b["subframe_hz_min"] > 1400
    assert b["lsb_ns"] >= 100.0 and b["levels"] == 4096
    assert b["shift_mhz"] == 25.0 and s.frame_bytes == 12 * config.SCAN_ROWS * config.W * 6 == 294912
    assert b["read_mbps_peak"] < 160 and b["duty"] > 0.7


def test_plane_weights_are_binary():
    s = build()
    w = s.plane_weight_ticks()
    assert all(w[p] == w[0] << p for p in range(s.planes))


def test_segments_of_each_plane_are_spread_evenly():
    s = build()
    for plane in range(s.params.split, s.planes):
        where = [j for j, sf in enumerate(s.subframes) if any(s.entries[i].plane == plane for i in sf)]
        n = 2 ** (plane - s.params.split)
        step = len(s.subframes) // n
        assert where == [i * step + step // 2 for i in range(n)]
    # Every sub-frame starts with the MSB and lists planes brightest first.
    for sf in s.subframes:
        planes = [s.entries[i].plane for i in sf]
        assert planes[0] == s.planes - 1 and planes == sorted(planes, reverse=True)


def test_every_visit_satisfies_the_timing_rules():
    s = build()
    for e in s.entries:
        assert e.visit_ticks >= s.blank_ticks + e.oe_ticks
        assert e.visit_ticks >= s.shift_ticks + s.params.lat_ticks + s.params.shift_margin
        assert e.visit_ticks >= s.limits.min_visit_ticks


@pytest.mark.parametrize("clk_div,split,expect_hz", [(2, 6, 1920), (4, 7, 960)])
def test_other_clocks(clk_div, split, expect_hz):
    s = build(ScheduleParams(clk_div=clk_div, split=split))
    b = s.budget()
    assert s.planes == 12 and abs(b["subframe_hz_mean"] - expect_hz) < 5 and b["lsb_ns"] >= 100


def test_too_deep_for_the_frame_says_what_to_change():
    with pytest.raises(ScheduleError, match="fewer planes"):
        build(ScheduleParams(planes=12, split=5))
    with pytest.raises(ScheduleError, match="fewer planes"):
        build(ScheduleParams(clk_div=4, split=6))


def test_lower_fps_buys_a_longer_lsb():
    assert build(ScheduleParams(fps=30)).budget()["lsb_ns"] > build().budget()["lsb_ns"] * 2


def test_no_fps_means_no_padding():
    s = build(ScheduleParams(fps=0, lsb_ticks=5))
    assert all(e.visit_ticks == s.min_visit_ticks(e.oe_ticks) for e in s.entries)
    assert s.budget()["frame_hz"] > 60


def test_no_split_is_plain_bcm():
    s = build(ScheduleParams(planes=8, split=8))
    assert len(s.entries) == 8 and len(s.subframes) == 1
    assert [e.plane for e in s.entries] == list(range(7, -1, -1))


def test_card_limits_change_the_table():
    slow = CardLimits(min_visit_ticks=300)
    s = build(lim=slow)
    assert min(e.visit_ticks for e in s.entries) >= 300
    with pytest.raises(ScheduleError, match="fewer planes"):
        build(lim=CardLimits(min_visit_ticks=400))     # even the segment visits would take 400 ticks
    with pytest.raises(ScheduleError, match="entries"):
        build(ScheduleParams(planes=13, split=6), CardLimits(max_entries=100))


def test_wire_round_trip():
    s = build()
    data = s.to_bytes()
    assert len(data) == S.HEADER_BYTES + len(s.entries) * S.ENTRY_BYTES
    back = Schedule.from_bytes(data)
    assert back.entries == s.entries and back.schedule_id == s.schedule_id
    assert back.params.clk_div == 2 and back.params.blank_before == s.params.blank_before
    assert back.frame_bytes == s.frame_bytes and back.to_bytes() == data


def test_schedule_id_follows_the_table():
    a, b = build(), build(ScheduleParams(blank_after=17))
    assert a.schedule_id == build().schedule_id and a.schedule_id != b.schedule_id


def test_wrong_tick_rate_is_rejected():
    data = build().to_bytes()
    with pytest.raises(ScheduleError, match="ticks") as e:
        Schedule.from_bytes(data, CardLimits(tick_hz=100_000_000))
    assert e.value.kind == "tick"


def test_malformed_tables_are_rejected():
    data = build().to_bytes()
    with pytest.raises(ScheduleError):
        Schedule.from_bytes(data[:-8])                     # entry count does not match
    with pytest.raises(ScheduleError):
        Schedule.from_bytes(data[:10] + b"\x40" + data[11:])  # rows = 64


def test_validate_catches_a_short_visit():
    s = build()
    bad = Entry(11, 320, 100)
    with pytest.raises(ScheduleError, match="shorter") as e:
        Schedule(s.params, s.limits, (bad,) + s.entries[1:]).validate()
    assert e.value.kind == "entry"
    with pytest.raises(ScheduleError, match="plane"):
        Schedule(s.params, s.limits, (Entry(12, 320, 400),)).validate()
