"""The streamer's light against the reference model: with a small schedule and real frame
packets, every OE pulse of a pass (time, length, row, lit pixels) matches model.Streamer, frames
swap only at the pass boundary, the pattern build lights its built-in picture, and a late SDRAM
is counted as underruns without changing any pulse length."""
import numpy as np
import pytest

from trafficlight.output.hub75 import protocol as P
from trafficlight.output.hub75.model import Streamer
from trafficlight.output.hub75.pack import unpack_bits
from trafficlight.output.hub75.schedule import CardLimits, Entry, Schedule, ScheduleParams

from tlcard import constants as C
from cardsim import CardSim, Light

LIMITS = CardLimits(min_visit_ticks=1)


def small_schedule(cols=4, rows=2, planes=2, visit=48, sid=0x5EED):
    p = ScheduleParams(planes=planes, split=planes, clk_div=2, fps=0, rows=rows, cols=cols,
                       blank_before=3, lat_ticks=2, blank_after=4, min_oe_ns=0)
    entries = (Entry(1, 10, visit), Entry(0, 5, visit), Entry(1, 0, visit), Entry(1, 7, visit + 5))
    return Schedule(p, LIMITS, entries, schedule_id=sid)


def random_frame(s: Schedule, seed: int) -> bytes:
    return np.random.default_rng(seed).integers(0, 256, s.frame_bytes, dtype=np.uint8).tobytes()


def expected_pulses(s: Schedule, frame: bytes):
    """(start, length, row, lit(x, lane) -> bit) per visit of one pass, from the model's timing."""
    st = Streamer(s)
    bits = unpack_bits(frame, s.planes, s.rows, s.cols)
    out = []
    for k, e in enumerate(s.entries):
        for a in range(s.rows):
            if e.oe_ticks:
                out.append((int(st.entry_start[k] + a * st.visit[k] + s.blank_ticks), e.oe_ticks, a, bits[e.plane, a]))
    return sorted(out, key=lambda p: p[0])


def pulses_between(light: Light, start: int, end: int):
    return [p for p in light.pulses if start <= p.start < end]


def check_pass(sim_pulses, exp, s: Schedule, t0: int):
    """sim pulses of one pass against the model's, with the pass's LAT(0,0) at model time blank_before."""
    assert len(sim_pulses) == len(exp)
    for p, (start, length, row, lit) in zip(sim_pulses, exp):
        assert p.start - t0 + s.params.blank_before == start
        assert p.length == length and p.row == row
        for x in range(s.cols):
            for lane in range(C.LANES):
                assert p.lit(x, lane) == lit[x, lane], (x, lane)


def test_light_matches_model_and_swaps_at_boundary():
    s = small_schedule()
    fa, fb = random_frame(s, 1), random_frame(s, 2)
    sim = CardSim()
    pass_ticks = s.frame_ticks

    def script():
        yield from sim.send(P.build_status_request(seq=1))
        yield from sim.send(P.build_schedule(s.to_bytes(), seq=2))
        for d in P.chunk_frame(fa, frame_id=10, schedule_id=s.schedule_id, chunk_bytes=32):
            yield from sim.send(d)
        yield from sim.wait(4 * pass_ticks)
        for d in P.chunk_frame(fb, frame_id=11, schedule_id=s.schedule_id, chunk_bytes=32):
            yield from sim.send(d)
        yield from sim.wait(4 * pass_ticks)
        yield from sim.send(P.build_status_request(seq=3))
        yield from sim.wait(200)

    sim.run(script())
    st = [r for r in sim.replies]
    assert [r.seq for r in st] == [1, 2, 3]
    assert st[0].flags & P.FLAG_BLANK and not st[0].flags & P.FLAG_SCHED_ACTIVE
    assert st[1].flags & P.FLAG_SCHED_ACTIVE and st[1].sched_active_id == s.schedule_id and st[1].sched_accepted == 1
    assert st[2].frames_shown == 2 and st[2].frames_committed == 2 and st[2].frame_id_shown == 11
    assert st[2].underruns == 0 and st[2].last_error == 0 and st[2].frame_ticks == pass_ticks
    assert st[2].tick_hz == C.DEFAULT_TICK_HZ and st[2].min_visit_ticks == C.MIN_VISIT_TICKS

    light = sim.light(s.cols)
    visits = len(s.entries) * s.rows
    # LAT events come one per visit; the pass starts are every `visits`-th one. Before a frame
    # was shown the pulses are absent (OE off), after it every pass is one of the two frames whole.
    starts = [c for i, (c, _row) in enumerate(light.lats) if i % visits == 0]
    assert all(b - a == pass_ticks for a, b in zip(starts, starts[1:]))
    ea, eb = expected_pulses(s, fa), expected_pulses(s, fb)
    seen = []
    for a, b in zip(starts, starts[1:]):
        pulses = pulses_between(light, a - s.params.blank_before, b - s.params.blank_before)
        if not pulses:
            seen.append("dark")
            continue
        try:
            check_pass(pulses, ea, s, a)
            seen.append("A")
        except AssertionError:
            check_pass(pulses, eb, s, a)
            seen.append("B")
    assert seen.index("A") > 0 and "B" in seen
    assert seen == ["dark"] * seen.index("A") + ["A"] * (seen.index("B") - seen.index("A")) + ["B"] * (len(seen) - seen.index("B"))


def test_pattern_build_lights_its_picture():
    table = dict(C.PATTERN_TABLE, oe_ticks=20, visit_ticks=300)
    sim = CardSim(pattern=True, pattern_table=table)
    sim.run(sim.wait(2 * 32 * 300 + 2000))
    light = sim.light(128)
    assert len(light.pulses) >= 32
    rows = {p.row for p in light.pulses}
    assert rows == set(range(32))
    for p in light.pulses[-32:]:
        assert p.length == 20
        a = p.row
        for c in range(8):
            for x in range(128):
                top = (p.lit(x, 6 * c), p.lit(x, 6 * c + 1), p.lit(x, 6 * c + 2))
                bottom = (p.lit(x, 6 * c + 3), p.lit(x, 6 * c + 4), p.lit(x, 6 * c + 5))
                if x == 0:
                    assert top == (0, 1, 0)
                elif a < 8:
                    assert top == (1, 0, 0)
                elif a >= 24:
                    assert top == (0, 0, 1)
                else:
                    assert top == ((1, 1, 1) if (x >> 4) <= c else (0, 0, 0))
                if x == a + 48:
                    assert bottom == (0, 0, 0)
                else:
                    assert bottom == [(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 1)][x >> 5]


def test_late_sdram_counts_underruns_but_keeps_pulse_lengths():
    s = small_schedule(visit=48)
    f = random_frame(s, 3)
    sim = CardSim(read_latency=60)      # a fetch now takes longer than a visit

    def script():
        yield from sim.send(P.build_schedule(s.to_bytes(), seq=1))
        for d in P.chunk_frame(f, frame_id=1, schedule_id=s.schedule_id, chunk_bytes=32):
            yield from sim.send(d)
        yield from sim.wait(6 * s.frame_ticks)
        yield from sim.send(P.build_status_request(seq=2))
        yield from sim.wait(200)

    sim.run(script())
    st = sim.replies[-1]
    assert st.underruns > 0 and st.frames_shown == 1
    light = sim.light(s.cols)
    lengths = {p.length for p in light.pulses}
    assert lengths <= {e.oe_ticks for e in s.entries if e.oe_ticks}
    assert st.frame_ticks > s.frame_ticks
