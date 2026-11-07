"""ANNOUNCE (docs/FIRMWARE.md 6.7) from the whole core, with the flash model behind the id
reader: the periodic broadcast (first time, period, seq, destination), the answer to an
ANNOUNCE_REQ (unicast to its source, its seq), the bytes of both, the fixed-address build, and
STATUS replies sharing the one transmitter (requests during a broadcast are queued, not lost)."""
import struct

import pytest

from trafficlight.output.hub75 import protocol as P

from tlcard import constants as C
from tlcard.identity import ip_int, mac_for_ip
from cardsim import UID, CardSim
from simhelpers import fold_address

HOST_IP = ip_int("10.75.0.1")
BCAST = ip_int(C.ANNOUNCE_IP)
MS = 10                                     # cycles per millisecond at the test's tick rate
FAST_HZ = 1000 * MS


def announce_request(seq=0):
    return struct.pack("<4sBBHII", b"5A75", 1, C.TYPE_ANNOUNCE_REQ, 0, seq, 0)


def expected_fields(uid=UID, flags=C.AFLAG_UID_VALID, ip=None, mac=None):
    _fold, c, d, _valid = fold_address(uid)
    return dict(proto_version=1, fw_major=C.FW_VERSION[0], fw_minor=C.FW_VERSION[1], flags=flags,
                fw_build=0x20261106, uid=uid,
                mac=(mac if mac is not None else mac_for_ip(ip_int(f"10.75.{c}.{d}"))).to_bytes(6, "big"),
                udp_port=C.UDP_PORT,
                ip=(ip if ip is not None else ip_int(f"10.75.{c}.{d}")).to_bytes(4, "big"))


def check(sent, fields, ip, port, seq):
    assert (sent.ip, sent.port, sent.src_port) == (ip, port, C.UDP_PORT)
    got = dict(sent.msg)
    assert got.pop("seq") == seq
    got.pop("uptime_ms")
    assert got == fields


def test_periodic_broadcast_first_after_half_a_second_plus_fold_then_every_second():
    fold = fold_address(UID)[0]
    first_ms = C.ANNOUNCE_FIRST_MS + (fold & 0xFF)
    sim = CardSim(tick_hz=FAST_HZ)
    sim.run(sim.wait((first_ms + 2 * C.ANNOUNCE_PERIOD_MS + 5) * MS))
    assert len(sim.announces) == 3 and sim.replies == []
    for k, a in enumerate(sim.announces):
        check(a, expected_fields(), BCAST, C.ANNOUNCE_PORT, seq=k)              # broadcasts before this one
        ms = first_ms + k * C.ANNOUNCE_PERIOD_MS
        assert ms * MS <= a.cycle <= ms * MS + 20          # launched in the tick it falls due
        assert a.msg["uptime_ms"] in (ms, ms + 1)          # 12 words at 10 cycles per test millisecond


def test_answer_to_a_request_and_status_still_answered():
    sim = CardSim()

    def script():
        yield from sim.send(announce_request(seq=0xA0B0C0D0), src_port=C.ANNOUNCE_PORT, src_ip=HOST_IP)
        yield from sim.send(P.build_status_request(seq=77), src_port=40001, src_ip=HOST_IP)
        yield from sim.send(announce_request(seq=5), src_port=50000, src_ip=ip_int("10.75.0.2"))
        yield from sim.wait(300)
    sim.run(script())
    kinds = [type(s.msg).__name__ for s in sim.sent]
    assert kinds == ["dict", "Status", "dict"]
    check(sim.sent[0], expected_fields(), HOST_IP, C.ANNOUNCE_PORT, seq=0xA0B0C0D0)
    check(sim.sent[2], expected_fields(), ip_int("10.75.0.2"), 50000, seq=5)
    status = sim.sent[1]
    assert (status.ip, status.port, status.src_port, status.msg.seq) == (HOST_IP, 40001, C.UDP_PORT, 77)
    # The ANNOUNCE_REQ is a good packet: nothing counted.
    assert status.msg.bad_packets == 0 and status.msg.last_error == 0 and status.msg.rx_dropped == 0


def test_announce_sent_to_the_card_is_ignored_and_a_wrong_one_is_bad():
    sim = CardSim()
    echo = struct.pack("<4sBBHII", b"5A75", 1, C.TYPE_ANNOUNCE, C.ANNOUNCE_BYTES, 1, 0) + bytes(C.ANNOUNCE_BYTES)

    def script():
        # Gaps: a STATUS reads its counters while it goes out.
        yield from sim.send(echo, gap=100)
        yield from sim.send(P.build_status_request(seq=1), gap=100)
        yield from sim.send(echo[:6] + struct.pack("<H", 28) + echo[8:-4], gap=100)     # consistent header, short body
        yield from sim.send(P.build_status_request(seq=2))
        yield from sim.wait(300)
    sim.run(script())
    assert [s.seq for s in sim.replies] == [1, 2] and sim.announces == []
    assert sim.replies[0].bad_packets == 0 and sim.replies[1].bad_packets == 1


@pytest.mark.parametrize("pattern", [False, True])
def test_fixed_address_and_flags(pattern):
    ip = ip_int("10.75.3.4")
    mac = 0x02AABBCCDDEE
    sim = CardSim(address=(ip, mac), pattern=pattern)

    def script():
        yield from sim.send(announce_request(seq=9), src_port=C.ANNOUNCE_PORT, src_ip=HOST_IP)
        yield from sim.wait(200)
    sim.run(script())
    # The uid is still read and reported; the pattern build has a frame from the start.
    flags = C.AFLAG_UID_VALID | C.AFLAG_FIXED_ADDRESS | (C.AFLAG_HAVE_FRAME if pattern else 0)
    check(sim.announces[0], expected_fields(flags=flags, ip=ip, mac=mac), HOST_IP, C.ANNOUNCE_PORT, seq=9)


def test_flash_not_answering_gives_an_address_and_uid_not_valid():
    sim = CardSim(uid=bytes(8))

    def script():
        yield from sim.send(announce_request(seq=1), src_port=C.ANNOUNCE_PORT, src_ip=HOST_IP)
        yield from sim.wait(200)
    sim.run(script())
    check(sim.announces[0], expected_fields(uid=bytes(8), flags=0), HOST_IP, C.ANNOUNCE_PORT, seq=1)
    assert sim.announces[0].msg["ip"] == bytes([10, 75, 1, 1])


def test_requests_around_a_broadcast_are_queued_and_cannot_hold_it_back():
    fold = fold_address(UID)[0]
    due = (C.ANNOUNCE_FIRST_MS + (fold & 0xFF)) * MS
    sim = CardSim(tick_hz=FAST_HZ)
    # Twelve requests back to back: their replies keep the transmitter busy for ~250 cycles,
    # across the moment the broadcast falls due.
    requests = [P.build_status_request(seq=10 + k) if k % 2 else announce_request(seq=10 + k) for k in range(12)]

    def script():
        while sim.cycle < due - 100:
            yield
        for r in requests:
            yield from sim.send(r, src_port=6000, src_ip=HOST_IP)
        yield from sim.wait(600)
    sim.run(script())
    answers = [s.msg["seq"] if isinstance(s.msg, dict) else s.msg.seq for s in sim.sent if s.ip == HOST_IP]
    assert answers == list(range(10, 22))                          # every request answered, in order
    bcast = [k for k, s in enumerate(sim.sent) if s.ip == BCAST]
    assert len(bcast) == 1
    b = sim.sent[bcast[0]]
    assert b.port == C.ANNOUNCE_PORT and b.msg["seq"] == 0
    # Out after at most the reply in flight when it fell due (a STATUS: 29 words), not after the queue.
    assert 0 < bcast[0] < len(sim.sent) - 1 and b.cycle <= due + 30 + 12 + 10
    assert sim.replies[-1].rx_dropped == 0
