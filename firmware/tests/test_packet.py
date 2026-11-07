"""The packet parser on datagrams built with the host's protocol module: good packets become
messages with their payload in the FIFO; short, malformed, unknown and errored ones are counted
and leave nothing behind; packets that do not fit are dropped whole."""
import os
import struct

import pytest
from migen import *
from migen.sim import passive, run_simulation

from trafficlight.output.hub75 import protocol as P
from trafficlight.output.hub75.schedule import BUFFER_BYTES, MAX_COLS, MAX_ENTRIES, MAX_PLANES, MAX_ROWS, MIN_VISIT_TICKS, build

from tlcard import constants as C
from tlcard.packet import KIND_ANNOUNCE, KIND_CHUNK, KIND_COMMIT, KIND_SCHEDULE, KIND_STATUS, PacketParser
from simhelpers import send_datagram


def test_constants_match_host_package():
    assert C.MAGIC_WORD == int.from_bytes(P.MAGIC, "little")
    assert (C.BUFFER_BYTES, C.MAX_CHUNKS, C.MAX_CHUNK_DATA, C.STATUS_BYTES) == \
        (BUFFER_BYTES, P.MAX_CHUNKS, P.MAX_CHUNK_DATA, P.STATUS_BYTES)
    assert C.UDP_PORT == P.PORT
    assert (C.ERR_STALE, C.ERR_BAD_PACKET, C.FLAG_BLANK) == (P.ERR_STALE, P.ERR_BAD_PACKET, P.FLAG_BLANK)
    assert (C.MIN_VISIT_TICKS, C.MAX_ENTRIES, C.MAX_ROWS, C.MAX_COLS, C.MAX_PLANES) == \
        (MIN_VISIT_TICKS, MAX_ENTRIES, MAX_ROWS, MAX_COLS, MAX_PLANES)


def test_announce_constants_match_host_package():
    if not hasattr(P, "ANNOUNCE_PORT"):
        pytest.skip("the host package has no ANNOUNCE numbers yet")
    assert (C.ANNOUNCE_PORT, C.TYPE_ANNOUNCE_REQ, C.TYPE_ANNOUNCE, C.ANNOUNCE_BYTES) == \
        (P.ANNOUNCE_PORT, P.TYPE_ANNOUNCE_REQ, P.TYPE_ANNOUNCE, P.ANNOUNCE_BODY_BYTES)


def announce_request(seq=0):
    return struct.pack("<4sBBHII", b"5A75", 1, C.TYPE_ANNOUNCE_REQ, 0, seq, 0)


def announce(body=bytes(C.ANNOUNCE_BYTES)):
    return struct.pack("<4sBBHII", b"5A75", 1, C.TYPE_ANNOUNCE, len(body), 0, 0) + body


class Harness:
    """Runs datagrams through a parser and drains messages and FIFO words into Python lists."""

    def __init__(self, fifo_words=256, msg_depth=4, drain=True):
        self.dut = PacketParser(fifo_words=fifo_words, msg_depth=msg_depth)
        self.msgs = []
        self.words = []
        self.bad = 0
        self.dropped = 0
        self.drain = drain

    @passive
    def monitor(self):
        dut = self.dut
        # Written values take effect at the next edge: keep the pops asserted and record what is
        # at the outputs in a cycle where valid/readable say it is being consumed.
        yield dut.source.ready.eq(1)
        yield dut.fifo.re.eq(int(self.drain))
        while True:
            if (yield dut.bad_packet):
                self.bad += 1
            if (yield dut.dropped):
                self.dropped += 1
            if (yield dut.source.valid):
                m = {}
                for name, _ in dut.source.description.payload_layout:
                    m[name] = yield getattr(dut.source, name)
                self.msgs.append(m)
            if self.drain and (yield dut.fifo.readable):
                self.words.append((yield dut.fifo.dout))
            yield

    def run(self, datagrams, **kw):
        def stim():
            for d in datagrams:
                yield from send_datagram(self.dut.sink, d, **kw)
            # The FIFO drains one word per cycle after the last commit.
            for _ in range(400):
                yield
        run_simulation(self.dut, [stim(), self.monitor()], vcd_name=os.environ.get("TLCARD_VCD"))
        return self


def words_of(data: bytes) -> list[int]:
    return [int.from_bytes(data[i:i + 4], "little") for i in range(0, len(data), 4)]


def test_status_request_and_commit():
    commit = P.build_frame_commit(frame_id=0xDEADBEEF, schedule_id=0x1234, frame_bytes=294912, chunk_count=205, seq=9)
    h = Harness().run([P.build_status_request(seq=42), commit], src_port=5000, src_ip=0x0A4B0001)
    assert h.bad == 0 and h.dropped == 0 and h.words == []
    assert [m["kind"] for m in h.msgs] == [KIND_STATUS, KIND_COMMIT]
    assert (h.msgs[0]["seq"], h.msgs[0]["src_port"], h.msgs[0]["src_ip"]) == (42, 5000, 0x0A4B0001)
    c = h.msgs[1]
    assert (c["frame_id"], c["sched_id"], c["frame_bytes"], c["chunk_count"]) == (0xDEADBEEF, 0x1234, 294912, 205)


def test_chunk_data_lands_in_fifo_in_order():
    data = bytes(range(256)) * 5 + bytes(160)      # 1440 bytes
    pkt = P.build_frame_chunk(frame_id=7, chunk_index=3, offset=4320, data=data, seq=1)
    small = P.build_frame_chunk(frame_id=7, chunk_index=4, offset=393216 - 32, data=bytes(range(32)), seq=2)
    h = Harness(fifo_words=1024).run([pkt, small])
    assert h.bad == 0 and h.dropped == 0
    assert [m["kind"] for m in h.msgs] == [KIND_CHUNK, KIND_CHUNK]
    m = h.msgs[0]
    assert (m["frame_id"], m["index"], m["words"], m["offset_words"], m["range_bad"]) == (7, 3, 360, 4320 // 4, 0)
    assert h.msgs[1]["range_bad"] == 0 and h.msgs[1]["words"] == 8
    assert h.words == words_of(data) + words_of(bytes(range(32)))


def test_chunk_out_of_range_is_flagged_not_dropped_by_parser():
    pkt = P.build_frame_chunk(frame_id=1, chunk_index=0, offset=393216, data=bytes(32))
    h = Harness().run([pkt])
    assert h.msgs[0]["range_bad"] == 1 and h.bad == 0


def test_schedule_table_words():
    s = build()
    table = s.to_bytes()
    h = Harness(fifo_words=1024).run([P.build_schedule(table, seq=5)])
    assert h.bad == 0 and [m["kind"] for m in h.msgs] == [KIND_SCHEDULE]
    assert h.msgs[0]["words"] == len(table) // 4 and h.msgs[0]["seq"] == 5
    assert h.words == words_of(table)


@pytest.mark.parametrize("name, datagram", [
    ("short", b"5A75"),
    ("header only 15", P.build_status_request()[:15]),
    ("bad magic", b"XX75" + P.build_status_request()[4:]),
    ("bad version", P.build_status_request()[:4] + b"\x02" + P.build_status_request()[5:]),
    ("unknown type", P.build_status_request()[:5] + b"\x7f" + P.build_status_request()[6:]),
    ("body length mismatch", P.build_status_request() + b"\0\0\0\0"),
    ("chunk length wrong", P.build_frame_chunk(1, 0, 0, bytes(64))[:-32]),
    ("chunk header short", P.build_frame_chunk(1, 0, 0, bytes(32))[:20] + b""),
    ("commit wrong size", P.build_frame_commit(1, 2, 3, 4) + bytes(8)),
    ("schedule not a multiple of 8", P.build_schedule(build().to_bytes()) + bytes(4)),
    ("status echo wrong size", P.build_status(P.Status())[:-4]),
    ("announce request with a body", announce_request() + bytes(4)),
    ("announce echo wrong size", announce(bytes(28))),
])
def test_bad_packets_are_counted_and_leave_nothing(name, datagram):
    # Fix up the body-length field where the test wants a consistent header with a bad body.
    if name in ("chunk length wrong", "commit wrong size", "schedule not a multiple of 8", "status echo wrong size",
                "chunk header short", "announce request with a body"):
        body = len(datagram) - 16
        datagram = datagram[:6] + body.to_bytes(2, "little") + datagram[8:]
    h = Harness(fifo_words=1024).run([datagram, P.build_status_request(seq=1)])
    assert h.bad == 1, name
    assert h.dropped == 0 and h.words == []
    assert [m["kind"] for m in h.msgs] == [KIND_STATUS] and h.msgs[0]["seq"] == 1


def test_announce_request_is_a_message_and_an_announce_is_ignored():
    h = Harness().run([announce_request(seq=0x55), announce(), P.build_status_request(seq=3)],
                      src_port=5776, src_ip=0x0A4B0001)
    assert h.bad == 0 and h.dropped == 0 and h.words == []
    assert [m["kind"] for m in h.msgs] == [KIND_ANNOUNCE, KIND_STATUS]
    assert (h.msgs[0]["seq"], h.msgs[0]["src_port"], h.msgs[0]["src_ip"]) == (0x55, 5776, 0x0A4B0001)


def test_status_echo_is_ignored_silently():
    h = Harness(fifo_words=1024).run([P.build_status(P.Status()), P.build_status_request(seq=3)])
    assert h.bad == 0 and h.dropped == 0 and [m["seq"] for m in h.msgs] == [3]


def test_mac_error_drops_packet_and_rolls_back():
    pkt = P.build_frame_chunk(frame_id=1, chunk_index=0, offset=0, data=bytes(range(64)))
    h = Harness(fifo_words=1024).run([pkt], error_last=True)
    assert h.dropped == 1 and h.bad == 0 and h.msgs == [] and h.words == []
    h = Harness(fifo_words=1024).run([pkt])
    assert h.dropped == 0 and len(h.words) == 16


def test_full_fifo_drops_whole_packets_and_keeps_later_ones():
    big = P.build_frame_chunk(frame_id=1, chunk_index=0, offset=0, data=bytes(range(128)) * 2)   # 256 B = 64 words
    h = Harness(fifo_words=128, drain=False).run([big, big, big, P.build_status_request(seq=4)])
    # 128 words of FIFO: the first packet (64 data words, 72 words on the wire) fits, the second
    # is judged against the whole datagram and does not, the third neither; the status request fits.
    assert h.dropped == 2 and h.bad == 0
    assert [m["kind"] for m in h.msgs] == [KIND_CHUNK, KIND_STATUS]
