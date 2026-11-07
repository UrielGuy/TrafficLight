"""UDP datagram words -> validated messages (docs/FIRMWARE.md section 6).

The parser never stalls LiteEth: it takes one 32-bit word per cycle from the UDP user port,
checks the common header and the type-specific body on the fly, and pushes chunk data and
schedule tables into a receive FIFO with rollback (a packet that turns out bad or that does
not fit is undone whole). A small message FIFO carries one entry per accepted packet, in
arrival order, to the receiver (receiver.py), which processes them in that order so that a
commit is handled only after its chunks are in the SDRAM.
"""
from migen import *

from litex.gen import *
from litex.soc.interconnect import stream
from liteeth.common import eth_udp_user_description

from . import constants as C

KIND_CHUNK, KIND_COMMIT, KIND_SCHEDULE, KIND_STATUS = 0, 1, 2, 3

# Chunk: frame_id, index, words, offset_words, range_bad. Commit: frame_id, sched_id, frame_bytes,
# chunk_count. Schedule: words (table words in the data FIFO), seq, src_port, src_ip. Status: seq,
# src_port, src_ip.
msg_layout = [
    ("kind", 2),
    ("frame_id", 32),
    ("index", 9),
    ("words", 10),
    ("offset_words", 17),
    ("range_bad", 1),
    ("sched_id", 32),
    ("frame_bytes", 32),
    ("chunk_count", 16),
    ("seq", 32),
    ("src_port", 16),
    ("src_ip", 32),
]


class RollbackFIFO(Module):
    """A block-RAM FIFO whose writes become visible only on ``commit``; ``rollback`` discards the
    words written since the last commit. ``free`` counts against the uncommitted write pointer,
    so a packet that fits at its first word still fits at its last."""

    def __init__(self, width, depth):
        self.din = Signal(width)
        self.we = Signal()
        self.commit = Signal()
        self.rollback = Signal()
        self.dout = Signal(width)
        self.re = Signal()
        self.readable = Signal()
        self.free = Signal(max=depth + 1)
        self.level = Signal(max=depth + 1)

        # # #

        abits = log2_int(depth)
        mem = Memory(width, depth)
        wrport = mem.get_port(write_capable=True)
        rdport = mem.get_port()
        self.specials += mem, wrport, rdport

        wr = Signal(abits)
        wr_commit = Signal(abits)
        rd = Signal(abits)
        rd_next = Signal(abits)
        commit_d = Signal()
        self.comb += [
            rd_next.eq(rd + (self.re & self.readable)),
            rdport.adr.eq(rd_next),
            self.dout.eq(rdport.dat_r),
            wrport.adr.eq(wr),
            wrport.dat_w.eq(self.din),
            wrport.we.eq(self.we),
            self.level.eq(wr_commit - rd),
            self.readable.eq(self.level != 0),
            self.free.eq(depth - (wr - rd)),
        ]
        # The commit lands one cycle late so a word written in the commit cycle is never read
        # back in the next one (the RAM returns old data on a same-address read-during-write).
        self.sync += [
            rd.eq(rd_next),
            commit_d.eq(self.commit),
            If(self.rollback, wr.eq(wr_commit)).Elif(self.we, wr.eq(wr + 1)),
            If(commit_d, wr_commit.eq(wr)),
        ]


class PacketParser(LiteXModule):
    """One datagram in, one message out (or a bad_packet / dropped pulse)."""

    def __init__(self, fifo_words=C.RX_FIFO_WORDS, msg_depth=C.MSG_FIFO_DEPTH):
        self.sink = sink = stream.Endpoint(eth_udp_user_description(32))
        self.fifo = fifo = RollbackFIFO(32, fifo_words)
        self.msgs = msgs = stream.SyncFIFO(msg_layout, msg_depth)
        self.source = msgs.source
        self.bad_packet = Signal()      # header, length or type wrong (error 1)
        self.dropped = Signal()         # did not fit, or a MAC error

        # # #

        widx = Signal(10)               # word index within the datagram
        pushed = Signal(10)             # data words written to the FIFO for this datagram
        drop = Signal()                 # no room: the datagram is skipped whole
        err_seen = Signal()
        length = Signal(16)
        src_port = Signal(16)
        src_ip = Signal(32)
        ptype = Signal(8)
        version = Signal(8)
        body_len = Signal(16)
        seq = Signal(32)
        f0 = Signal(32)                 # body words 0..3: their meaning depends on the type
        f1 = Signal(32)
        f2 = Signal(32)
        f3 = Signal(32)
        magic_ok = Signal()

        word = sink.valid & sink.ready
        self.comb += sink.ready.eq(1)
        # Which words carry payload for the FIFO: schedule tables from body word 0, chunk data
        # from body word 4.
        push = Signal()
        self.comb += push.eq(word & ~drop & ~err_seen & (
            ((ptype == C.TYPE_SCHEDULE) & (widx >= 4)) |
            ((ptype == C.TYPE_FRAME_DATA) & (widx >= 8))))
        self.comb += [fifo.din.eq(sink.data), fifo.we.eq(push)]

        # Type-specific validity, evaluated at the last word (every field has arrived by then).
        chunk_len = f1[16:32]
        chunk_index = f1[0:16]
        chunk_offset = f2
        words_total = Signal(10)
        self.comb += words_total.eq(pushed + push)
        chunk_ok = ((body_len >= 16) & (chunk_len == body_len - 16) & (chunk_len != 0) &
                    (chunk_len <= C.MAX_CHUNK_DATA) & (chunk_len[0:5] == 0) & (chunk_offset[0:5] == 0) &
                    (chunk_index < C.MAX_CHUNKS) & (words_total == chunk_len[2:12]))
        commit_ok = body_len == 16
        sched_ok = (body_len >= C.SCHEDULE_HEADER_BYTES) & ((body_len - C.SCHEDULE_HEADER_BYTES)[0:3] == 0)
        status_echo_ok = body_len == C.STATUS_BYTES      # a STATUS sent to the card: well-formed, ignored
        type_ok = Signal()
        emit = Signal()
        self.comb += Case(ptype, {
            C.TYPE_FRAME_DATA: [type_ok.eq(chunk_ok), emit.eq(1)],
            C.TYPE_FRAME_COMMIT: [type_ok.eq(commit_ok), emit.eq(1)],
            C.TYPE_SCHEDULE: [type_ok.eq(sched_ok), emit.eq(1)],
            C.TYPE_STATUS_REQ: [type_ok.eq(1), emit.eq(1)],
            C.TYPE_STATUS: [type_ok.eq(status_echo_ok), emit.eq(0)],
            "default": [type_ok.eq(0), emit.eq(0)],
        })
        header_ok = magic_ok & (version == C.PROTO_VERSION) & (widx >= 3) & (length >= C.COMMON_BYTES) & \
            (body_len == length - C.COMMON_BYTES)
        good = header_ok & type_ok

        # Range check for chunks (error 5 in the receiver): offset + length > BUFFER_BYTES, in
        # 33 bits so a huge offset cannot wrap.
        range_sum = Signal(33)
        self.comb += range_sum.eq(chunk_offset + chunk_len)

        self.comb += [
            msgs.sink.kind.eq(Mux(ptype == C.TYPE_FRAME_DATA, KIND_CHUNK,
                                  Mux(ptype == C.TYPE_FRAME_COMMIT, KIND_COMMIT,
                                      Mux(ptype == C.TYPE_SCHEDULE, KIND_SCHEDULE, KIND_STATUS)))),
            msgs.sink.frame_id.eq(f0),
            msgs.sink.index.eq(chunk_index[0:9]),
            msgs.sink.words.eq(words_total),
            msgs.sink.offset_words.eq(chunk_offset[2:19]),
            msgs.sink.range_bad.eq(range_sum > C.BUFFER_BYTES),
            msgs.sink.sched_id.eq(f1),
            msgs.sink.frame_bytes.eq(f2),
            msgs.sink.chunk_count.eq(f3[0:16]),
            msgs.sink.seq.eq(seq),
            msgs.sink.src_port.eq(src_port),
            msgs.sink.src_ip.eq(src_ip),
        ]

        last = word & sink.last
        self.sync += [
            self.bad_packet.eq(0),
            self.dropped.eq(0),
            msgs.sink.valid.eq(0),
            fifo.commit.eq(0),
            fifo.rollback.eq(0),
            If(word,
                widx.eq(widx + 1),
                If(push, pushed.eq(pushed + 1)),
                If(sink.error != 0, err_seen.eq(1)),
                Case(widx, {
                    0: [magic_ok.eq(sink.data == C.MAGIC_WORD), length.eq(sink.length),
                        src_port.eq(sink.src_port), src_ip.eq(sink.ip_address),
                        err_seen.eq(sink.error != 0), pushed.eq(0),
                        # Everything the datagram can push must fit now: the FIFO only drains meanwhile.
                        drop.eq((fifo.free < ((sink.length + 3)[2:12])) | ~msgs.sink.ready)],
                    1: [version.eq(sink.data[0:8]), ptype.eq(sink.data[8:16]), body_len.eq(sink.data[16:32])],
                    2: [seq.eq(sink.data)],
                    4: [f0.eq(sink.data)],
                    5: [f1.eq(sink.data)],
                    6: [f2.eq(sink.data)],
                    7: [f3.eq(sink.data)],
                }),
            ),
            If(last,
                widx.eq(0),
                If(drop | err_seen | (sink.error != 0),
                    self.dropped.eq(1),
                    fifo.rollback.eq(1),
                ).Elif(~good,
                    self.bad_packet.eq(1),
                    fifo.rollback.eq(1),
                ).Else(
                    fifo.commit.eq(1),
                    msgs.sink.valid.eq(emit),
                ),
            ),
        ]
