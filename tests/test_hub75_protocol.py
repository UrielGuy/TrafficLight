"""Packets round-trip; the card model assembles frames, survives lost, reordered and duplicated
chunks, swaps only at a pass boundary and activates schedules atomically."""
import random

import numpy as np
import pytest

from trafficlight.output.hub75 import protocol as P
from trafficlight.output.hub75.model import BUFFERS, Card, newer
from trafficlight.output.hub75.schedule import CardLimits, ScheduleParams, build


@pytest.fixture(scope="module")
def sched():
    return build()


def frame_bytes(sched, seed):
    return np.random.default_rng(seed).integers(0, 256, sched.frame_bytes, dtype=np.uint8).tobytes()


def feed(card, packets):
    for p in packets:
        card.receive(p)


def status(card):
    return P.parse(card.receive(P.build_status_request(7)))


# -- packets -------------------------------------------------------------------------------

def test_common_header_and_status_round_trip():
    req = P.build_status_request(seq=42)
    assert len(req) == P.COMMON_BYTES and req[:4] == P.MAGIC and req[5] == P.STATUS_REQ
    assert P.parse(req) == P.StatusRequest(42)
    st = P.Status(fw_major=1, fw_minor=2, tick_hz=50_000_000, uptime_ms=12345, frames_shown=9, last_error=3, seq=42)
    data = P.build_status(st)
    assert len(data) == P.COMMON_BYTES + P.STATUS_BYTES and P.STATUS_BYTES == 100
    assert P.parse(data) == st
    offs = P.status_offsets()
    assert offs[0][2] == "proto_version" and offs[-1][0] + offs[-1][1] == P.STATUS_BYTES


def test_chunk_and_commit_round_trip():
    data = bytes(range(256)) * 5
    pkt = P.build_frame_chunk(0xDEADBEEF, 3, 4160, data, seq=5)
    assert len(pkt) == P.COMMON_BYTES + P.CHUNK_HEADER_BYTES + len(data) <= P.MTU_PAYLOAD
    assert P.parse(pkt) == P.FrameChunk(0xDEADBEEF, 3, 4160, data, 5)
    commit = P.build_frame_commit(0xDEADBEEF, 77, 294912, 205, seq=6)
    assert P.parse(commit) == P.FrameCommit(0xDEADBEEF, 77, 294912, 205, 6)


def test_schedule_packet_round_trip(sched):
    pkt = P.build_schedule(sched.to_bytes(), seq=1)
    parsed = P.parse(pkt)
    assert isinstance(parsed, P.SchedulePacket) and parsed.table == sched.to_bytes()
    assert parsed.schedule_id == sched.schedule_id and len(pkt) <= P.MTU_PAYLOAD


def test_chunking_a_frame(sched):
    frame = frame_bytes(sched, 0)
    packets = P.chunk_frame(frame, frame_id=1, schedule_id=sched.schedule_id)
    assert len(packets) == 206 and all(len(p) <= P.MTU_PAYLOAD for p in packets)
    chunks = [P.parse(p) for p in packets[:-1]]
    assert all(c.chunk_index == i and c.offset == i * P.MAX_CHUNK_DATA for i, c in enumerate(chunks))
    assert b"".join(c.data for c in chunks) == frame
    commit = P.parse(packets[-1])
    assert commit == P.FrameCommit(1, sched.schedule_id, sched.frame_bytes, 205, 205)


@pytest.mark.parametrize("bad", [
    b"",
    b"XXXX" + bytes(12),
    P.build_status_request()[:-1],
    P.build_status_request()[:4] + b"\x02" + P.build_status_request()[5:],       # version 2
    P.build_status_request()[:5] + b"\x7f" + P.build_status_request()[6:],       # unknown type
    P.build_frame_commit(1, 1, 1, 1)[:-1],
    P.build_frame_chunk(1, 0, 0, bytes(64))[:-1],
])
def test_bad_packets_are_rejected(bad):
    with pytest.raises(P.ProtocolError):
        P.parse(bad)


def test_chunk_builder_enforces_alignment():
    with pytest.raises(ValueError):
        P.build_frame_chunk(1, 0, 0, bytes(33))
    with pytest.raises(ValueError):
        P.build_frame_chunk(1, 0, 16, bytes(32))
    with pytest.raises(ValueError):
        P.build_frame_chunk(1, P.MAX_CHUNKS, 0, bytes(32))


def test_frame_id_comparison_wraps():
    assert newer(1, 0) and newer(0, 0xFFFFFFFF) and not newer(0xFFFFFFFF, 0) and not newer(5, 9) and not newer(5, 5)


# -- the card ------------------------------------------------------------------------------

def test_card_is_dark_until_a_schedule_and_a_frame_arrive(sched):
    card = Card()
    st = status(card)
    assert st.flag(P.FLAG_BLANK) and not st.flag(P.FLAG_SCHED_ACTIVE) and st.tick_hz == CardLimits().tick_hz
    assert st.ports == 8 and st.buffers == BUFFERS and st.seq == 7 and st.firmware == "0.1"
    frame = frame_bytes(sched, 1)
    feed(card, P.chunk_frame(frame, 1, sched.schedule_id))
    assert status(card).frames_rejected == 1                 # no schedule: the frame cannot be interpreted
    reply = P.parse(card.receive(P.build_schedule(sched.to_bytes(), seq=3)))
    assert reply.sched_active_id == sched.schedule_id and reply.seq == 3   # activated at once: nothing was running
    feed(card, P.chunk_frame(frame, 2, sched.schedule_id))
    st = status(card)
    assert st.flag(P.FLAG_FRAME_PENDING) and st.frame_id_shown == 0 and card.shown_frame is None
    card.boundary()
    st = status(card)
    assert card.shown_frame == frame and st.frame_id_shown == 2 and st.frames_shown == 1
    assert st.flag(P.FLAG_HAVE_FRAME) and not st.flag(P.FLAG_BLANK) and st.frame_ticks == sched.frame_ticks


def ready_card(sched):
    card = Card()
    card.receive(P.build_schedule(sched.to_bytes()))
    return card


def test_lost_chunk_means_the_frame_is_never_shown(sched):
    card = ready_card(sched)
    good = frame_bytes(sched, 1)
    feed(card, P.chunk_frame(good, 1, sched.schedule_id))
    card.boundary()
    packets = P.chunk_frame(frame_bytes(sched, 2), 2, sched.schedule_id)
    del packets[100]
    feed(card, packets)
    st = status(card)
    assert not st.flag(P.FLAG_FRAME_PENDING) and st.frames_committed == 1   # still waiting for chunk 100
    card.boundary()
    assert card.shown_frame == good                            # the last good frame stays
    # The next frame shows normally, and only then is the lossy one known to be lost.
    feed(card, P.chunk_frame(frame_bytes(sched, 3), 3, sched.schedule_id))
    assert status(card).frames_incomplete == 1
    card.boundary()
    assert card.shown_frame == frame_bytes(sched, 3)


def test_reordered_and_duplicated_chunks_still_complete(sched):
    card = ready_card(sched)
    frame = frame_bytes(sched, 5)
    packets = P.chunk_frame(frame, 9, sched.schedule_id)
    rng = random.Random(0)
    rng.shuffle(packets)                                      # the commit may even come first
    packets += rng.sample(packets, 20)                         # duplicates
    feed(card, packets)
    st = status(card)
    assert st.flag(P.FLAG_FRAME_PENDING) and st.frames_committed == 1 and st.frames_incomplete == 0
    card.boundary()
    assert card.shown_frame == frame


def test_stale_chunks_are_dropped(sched):
    card = ready_card(sched)
    feed(card, P.chunk_frame(frame_bytes(sched, 1), 10, sched.schedule_id))
    late = P.chunk_frame(frame_bytes(sched, 0), 9, sched.schedule_id)
    card.receive(late[0])
    card.receive(late[-1])                                     # a stale commit too
    st = status(card)
    assert st.chunks_dropped == 2 and st.last_error == P.ERR_STALE and st.frames_committed == 1


def test_newer_frame_abandons_an_incomplete_one(sched):
    card = ready_card(sched)
    half = P.chunk_frame(frame_bytes(sched, 1), 1, sched.schedule_id)[:100]
    feed(card, half)
    frame2 = frame_bytes(sched, 2)
    feed(card, P.chunk_frame(frame2, 2, sched.schedule_id))
    st = status(card)
    assert st.frames_incomplete == 1 and st.frames_committed == 1
    card.boundary()
    assert card.shown_frame == frame2


def test_swap_only_at_the_boundary_and_newest_wins(sched):
    card = ready_card(sched)
    f1, f2, f3 = (frame_bytes(sched, i) for i in (1, 2, 3))
    feed(card, P.chunk_frame(f1, 1, sched.schedule_id))
    card.boundary()
    assert card.shown_frame == f1
    feed(card, P.chunk_frame(f2, 2, sched.schedule_id))
    assert card.shown_frame == f1                              # not before the boundary
    feed(card, P.chunk_frame(f3, 3, sched.schedule_id))        # replaces the pending f2
    st = status(card)
    assert st.frames_replaced == 1 and card.shown_frame == f1
    card.boundary()
    assert card.shown_frame == f3 and status(card).frames_shown == 2
    card.boundary()
    assert card.shown_frame == f3                              # nothing new: keep showing


def test_receiving_never_touches_the_shown_or_pending_frame(sched):
    card = ready_card(sched)
    f1, f2 = frame_bytes(sched, 1), frame_bytes(sched, 2)
    feed(card, P.chunk_frame(f1, 1, sched.schedule_id))
    card.boundary()
    feed(card, P.chunk_frame(f2, 2, sched.schedule_id))        # pending
    # A third frame streams in while f2 waits: f1 stays on the panels, f2 stays intact.
    feed(card, P.chunk_frame(frame_bytes(sched, 3), 3, sched.schedule_id)[:50])
    assert card.shown_frame == f1 and bytes(card.buffers[card.spare][:sched.frame_bytes]) == f2
    card.boundary()
    assert card.shown_frame == f2


def test_frame_rate_faster_than_the_card_never_starves(sched):
    card = ready_card(sched)
    shown = []
    for i in range(1, 21):
        feed(card, P.chunk_frame(frame_bytes(sched, i), i, sched.schedule_id))
        if i % 2 == 0:                                        # the card runs at half the host's rate
            card.boundary()
            shown.append(status(card).frame_id_shown)
    assert shown == list(range(2, 21, 2))


def test_frame_for_another_schedule_is_rejected(sched):
    card = ready_card(sched)
    feed(card, P.chunk_frame(frame_bytes(sched, 1), 1, sched.schedule_id + 1))
    st = status(card)
    assert st.frames_rejected == 1 and st.last_error == P.ERR_FRAME_SCHEDULE and not st.flag(P.FLAG_FRAME_PENDING)


def test_commit_with_wrong_totals_is_incomplete(sched):
    card = ready_card(sched)
    packets = P.chunk_frame(frame_bytes(sched, 1), 1, sched.schedule_id)
    packets[-1] = P.build_frame_commit(1, sched.schedule_id, sched.frame_bytes - 32, 205)
    feed(card, packets)
    assert status(card).frames_incomplete == 1


def test_schedule_switch_is_atomic_at_the_boundary(sched):
    card = ready_card(sched)
    f1 = frame_bytes(sched, 1)
    feed(card, P.chunk_frame(f1, 1, sched.schedule_id))
    card.boundary()
    other = build(ScheduleParams(planes=8, split=8))
    assert other.schedule_id != sched.schedule_id and other.frame_bytes != sched.frame_bytes
    feed(card, P.chunk_frame(frame_bytes(sched, 2), 2, sched.schedule_id))     # pending under the old table
    reply = P.parse(card.receive(P.build_schedule(other.to_bytes())))
    assert reply.sched_active_id == sched.schedule_id and reply.sched_pending_id == other.schedule_id
    assert not reply.flag(P.FLAG_FRAME_PENDING) and reply.frames_replaced == 1   # the old-layout frame is dropped
    # Frames must now be packed for the pending table; an old-layout one is rejected.
    feed(card, P.chunk_frame(frame_bytes(sched, 3), 3, sched.schedule_id))
    assert status(card).frames_rejected == 1
    new_frame = np.random.default_rng(9).integers(0, 256, other.frame_bytes, dtype=np.uint8).tobytes()
    feed(card, P.chunk_frame(new_frame, 4, other.schedule_id))
    assert card.shown_frame == f1 and card.active.schedule_id == sched.schedule_id
    card.boundary()
    st = status(card)
    assert st.sched_active_id == other.schedule_id and st.sched_pending_id == 0
    assert card.shown_frame == new_frame and st.frame_ticks == other.frame_ticks


def test_bad_schedules_are_rejected_with_a_code(sched):
    card = Card()
    wrong_tick = build(lim=CardLimits(tick_hz=100_000_000)).to_bytes()
    reply = P.parse(card.receive(P.build_schedule(wrong_tick)))
    assert reply.last_error == P.ERR_SCHED_TICK and reply.sched_rejected == 1 and reply.sched_active_id == 0
    table = bytearray(sched.to_bytes())
    table[32 + 4:32 + 6] = (10).to_bytes(2, "little")          # first visit far too short
    reply = P.parse(card.receive(P.build_schedule(bytes(table))))
    assert reply.last_error == P.ERR_SCHED_ENTRY and reply.sched_rejected == 2


def test_garbage_is_counted(sched):
    card = Card()
    assert card.receive(b"hello") is None
    card.receive(P.build_frame_chunk(1, 0, 0, bytes(32))[:-5])
    assert status(card).bad_packets == 2 and status(card).last_error == P.ERR_BAD_PACKET


def test_chunk_beyond_the_buffer_is_dropped(sched):
    card = ready_card(sched)
    card.receive(P.build_frame_chunk(1, 0, 400_000, bytes(64)))
    assert status(card).chunks_dropped == 1 and status(card).last_error == P.ERR_CHUNK_RANGE


def test_the_card_takes_a_pass_of_any_length():
    # The frame rate is not on the wire: a 30 fps table is as good as a 60 fps one.
    card = Card()
    slow = build(ScheduleParams(fps=30))
    assert status_of_upload(card, slow).sched_active_id == slow.schedule_id


def status_of_upload(card, sched):
    return P.parse(card.receive(P.build_schedule(sched.to_bytes(), 3)))
