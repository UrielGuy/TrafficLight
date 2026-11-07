"""The receiver state machine against model.Card: the same datagrams go to both and every status
field that the model defines must agree (counters, flags, ids, last_error), for in-order,
out-of-order, duplicated and lost chunks, commit-first, stale ids, wrong totals, frames for the
wrong schedule, schedule replacement and rejection."""
import random

import numpy as np
import pytest

from trafficlight.output.hub75 import protocol as P
from trafficlight.output.hub75.model import Card
from trafficlight.output.hub75.schedule import BUFFER_BYTES, CardLimits, Entry, Schedule, ScheduleParams

from cardsim import CardSim

LIMITS = CardLimits(min_visit_ticks=1)
COMPARED = ("flags", "last_error", "sched_active_id", "sched_pending_id", "frame_id_shown", "frame_id_rx",
            "frames_shown", "frames_committed", "frames_incomplete", "frames_rejected", "frames_replaced",
            "chunks_received", "chunks_dropped", "rx_dropped", "bad_packets", "sched_accepted", "sched_rejected")


def schedule(cols=8, rows=1, planes=2, visit=20000, sid=0x1001, lat=2):
    # Long visits: a pass is tens of thousands of cycles, so a test's packets between two
    # `boundary()` calls never straddle one of the card's own boundaries.
    p = ScheduleParams(planes=planes, split=planes, clk_div=2, fps=0, rows=rows, cols=cols,
                       blank_before=2, lat_ticks=lat, blank_after=2, min_oe_ns=0)
    entries = tuple(Entry(pl, 8, visit) for pl in range(planes))      # OE >= 100 ns: the model insists
    return Schedule(p, LIMITS, entries, schedule_id=sid)


def frame(s: Schedule, seed: int) -> bytes:
    return np.random.default_rng(seed).integers(0, 256, s.frame_bytes, dtype=np.uint8).tobytes()


def chunks(s, f, frame_id, sid=None):
    return P.chunk_frame(f, frame_id=frame_id, schedule_id=sid or s.schedule_id, chunk_bytes=32)


class Both:
    """Feeds the gateware and the model the same datagrams; `status()` compares the replies."""

    def __init__(self, pass_ticks):
        self.sim = CardSim()
        self.model = Card(LIMITS)
        self.pass_ticks = pass_ticks
        self.seq = 0
        self.script = []
        self.model_status = []

    def send(self, *datagrams):
        for d in datagrams:
            self.script.append(("send", d))
            reply = self.model.receive(d)
            if reply is not None:
                self.model_status.append(P.parse(reply))

    def boundary(self):
        """One pass boundary on both: the model's call, and on the card the next time the
        fetcher's boundary request is served (at once while no table is active)."""
        self.script.append(("boundary", None))
        self.model.boundary()

    def status(self):
        self.seq += 1
        self.send(P.build_status_request(seq=self.seq))

    def run(self):
        def gen():
            for kind, arg in self.script:
                if kind == "send":
                    yield from self.sim.send(arg)
                    # The gateware handles a message in a few cycles plus the chunk's SDRAM write.
                    yield from self.sim.wait(60)
                elif kind == "wait":
                    yield from self.sim.wait(arg)
                else:
                    while not (yield self.sim.dut.rx.boundary_ack):
                        yield
                    yield from self.sim.wait(20)
            yield from self.sim.wait(200)
        self.sim.run(gen())
        got = self.sim.replies
        assert len(got) == len(self.model_status), (len(got), len(self.model_status))
        for g, m in zip(got, self.model_status):
            for name in COMPARED:
                assert getattr(g, name) == getattr(m, name), (name, g, m)
        return got


def test_in_order_frames_and_status_fields():
    s = schedule()
    b = Both(s.frame_ticks)
    b.status()
    b.send(P.build_schedule(s.to_bytes(), seq=7))
    b.boundary()
    b.send(*chunks(s, frame(s, 1), 100))
    b.status()
    b.boundary()
    b.send(*chunks(s, frame(s, 2), 101))
    b.send(*chunks(s, frame(s, 3), 102))           # replaces the pending one
    b.status()
    b.boundary()
    b.status()
    got = b.run()
    assert got[-1].frames_shown == 2 and got[-1].frames_replaced == 1 and got[-1].frame_id_shown == 102


def test_out_of_order_duplicate_and_commit_first():
    s = schedule(cols=16)                 # 192 bytes: six chunks to shuffle
    f = frame(s, 5)
    b = Both(s.frame_ticks)
    b.send(P.build_schedule(s.to_bytes()))
    b.boundary()
    pk = chunks(s, f, 1)
    data, commit = pk[:-1], pk[-1]
    rng = random.Random(3)
    shuffled = data[:]
    rng.shuffle(shuffled)
    b.send(commit, *shuffled[:3], shuffled[1], *shuffled[3:], shuffled[0])     # commit first, duplicates
    b.status()
    b.boundary()
    b.status()
    got = b.run()
    # The duplicate inside the frame counts as received; the one after the last distinct chunk
    # finds the frame finished and is stale.
    assert got[-1].frames_committed == 1 and got[-1].frames_shown == 1
    assert got[-1].chunks_received == len(data) + 1 and got[-1].chunks_dropped == 1
    slots = [bytes(b.sim.sdram.mem[i * BUFFER_BYTES:i * BUFFER_BYTES + s.frame_bytes]) for i in range(3)]
    assert b.model.shown_frame == f and f in slots


def test_lost_chunk_then_newer_frame_and_stale_packets():
    s = schedule()
    b = Both(s.frame_ticks)
    b.send(P.build_schedule(s.to_bytes()))
    b.boundary()
    pk = chunks(s, frame(s, 1), 10)
    b.send(*pk[1:])                                   # chunk 0 lost: never complete
    b.status()
    b.send(*chunks(s, frame(s, 2), 11))               # abandons frame 10 (frames_incomplete)
    b.send(pk[0])                                     # late chunk of 10: stale
    b.send(P.build_frame_commit(9, s.schedule_id, s.frame_bytes, 3))   # older id: stale
    b.status()
    b.boundary()
    b.status()
    got = b.run()
    assert got[-1].frames_incomplete == 1 and got[-1].chunks_dropped == 2 and got[-1].last_error == P.ERR_STALE
    assert got[-1].frame_id_shown == 11


def test_frame_id_wraps():
    s = schedule()
    b = Both(s.frame_ticks)
    b.send(P.build_schedule(s.to_bytes()))
    b.boundary()
    b.send(*chunks(s, frame(s, 1), 0xFFFFFFFF))
    b.send(*chunks(s, frame(s, 2), 0))               # 0 is newer than 0xFFFFFFFF
    b.send(*chunks(s, frame(s, 3), 0xFFFFFFFE))      # older: stale
    b.status()
    got = b.run()
    assert got[-1].frames_committed == 2 and got[-1].frame_id_rx == 0 and got[-1].chunks_dropped == 4


def test_wrong_totals_and_out_of_range_and_wrong_schedule():
    s = schedule()
    b = Both(s.frame_ticks)
    b.send(P.build_schedule(s.to_bytes()))
    b.boundary()
    pk = chunks(s, frame(s, 1), 1)
    b.send(*pk[:-1], P.build_frame_commit(1, s.schedule_id, s.frame_bytes + 32, len(pk) - 1))   # bytes wrong
    b.status()
    pk = chunks(s, frame(s, 2), 2)
    b.send(*pk[:-1], P.build_frame_commit(2, s.schedule_id, s.frame_bytes, len(pk) - 2))        # count too small
    b.status()
    b.send(P.build_frame_chunk(3, 0, BUFFER_BYTES, bytes(32)))
    b.status()
    b.send(*chunks(s, frame(s, 4), 4, sid=0x2222))    # for another schedule
    b.status()
    b.boundary()
    b.status()
    got = b.run()
    assert got[-1].frames_incomplete == 2 and got[-1].frames_rejected == 1 and got[-1].frames_shown == 0
    assert got[3].last_error == P.ERR_CHUNK_RANGE          # got[0] is the schedule's reply


def test_schedule_switch_drops_pending_frame_and_takes_effect_at_boundary():
    s1, s2 = schedule(sid=0x1001), schedule(planes=1, cols=16, sid=0x2002)     # both 96-byte frames
    b = Both(s1.frame_ticks)
    b.send(P.build_schedule(s1.to_bytes()))
    b.boundary()
    b.send(*chunks(s1, frame(s1, 1), 1))
    b.boundary()
    b.send(*chunks(s1, frame(s1, 2), 2))              # pending under s1
    b.send(P.build_schedule(s2.to_bytes()))           # pending table: the pending frame is dropped
    b.status()
    b.send(*chunks(s1, frame(s1, 3), 3))              # packed for s1: rejected now
    b.send(*chunks(s2, frame(s2, 4), 4))              # for s2: pending
    b.status()
    b.boundary()
    b.status()
    b.send(P.build_schedule(s2.to_bytes()))           # the running table again: harmless
    b.boundary()
    b.status()
    got = b.run()
    # Replies: s1, s2, status, status, status, s2 again, status.
    assert got[2].sched_pending_id == 0x2002 and got[2].frames_replaced == 1
    assert got[4].sched_active_id == 0x2002 and got[4].frame_id_shown == 4 and got[4].frames_rejected == 1
    assert got[6].sched_active_id == 0x2002 and got[6].sched_accepted == 3 and got[6].frames_replaced == 1


@pytest.mark.parametrize("mutate, err", [
    (lambda t: t[:4] + (49_000_000).to_bytes(4, "little") + t[8:], P.ERR_SCHED_TICK),
    (lambda t: t[:8] + (0).to_bytes(2, "little") + t[10:], P.ERR_SCHED_LIMITS),             # entries 0
    (lambda t: t[:10] + bytes([40]) + t[11:], P.ERR_SCHED_LIMITS),                            # rows 40
    (lambda t: t[:14] + bytes([1]) + t[15:], P.ERR_SCHED_LIMITS),                             # clk_div 1
    (lambda t: t[:24] + (12345).to_bytes(4, "little") + t[28:], P.ERR_SCHED_LIMITS),          # frame_bytes
    (lambda t: t[:32] + bytes([9]) + t[33:], P.ERR_SCHED_ENTRY),                              # plane 9
    (lambda t: t[:18] + (0).to_bytes(2, "little") + t[20:], P.ERR_SCHED_LIMITS),              # lat 0
])
def test_rejected_schedules(mutate, err):
    s = schedule()
    b = Both(s.frame_ticks)
    b.send(P.build_schedule(mutate(s.to_bytes()), seq=3))
    b.send(P.build_schedule(s.to_bytes(), seq=4))
    got = b.run()
    assert got[0].last_error == err and got[0].sched_rejected == 1 and not got[0].flags & P.FLAG_SCHED_ACTIVE
    assert got[1].sched_accepted == 1 and got[1].flags & P.FLAG_SCHED_ACTIVE


def test_bad_packets_counted_like_the_model():
    s = schedule()
    b = Both(s.frame_ticks)
    b.send(b"junk", P.build_status_request()[:10], P.build_status_request()[:5] + b"\x66" + P.build_status_request()[6:])
    b.status()
    got = b.run()
    assert got[0].bad_packets == 3 and got[0].last_error == P.ERR_BAD_PACKET
