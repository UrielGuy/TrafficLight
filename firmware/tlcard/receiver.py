"""The receiver: frame slots, chunk bookkeeping, commits, schedule tables and the pass boundary
(docs/FIRMWARE.md sections 6.5, 6.6, 7 and 8), plus the status reply builder.

Messages arrive from the packet parser in datagram order and are handled one at a time, so a
commit is only acted on after its chunks have gone through the SDRAM writer. The boundary
(the fetcher starting entry 0, row 0) is served between messages, never in a cycle where a
message changes the same state, so the two never interleave.
"""
from migen import *

from litex.gen import *
from litex.soc.interconnect import stream
from liteeth.common import eth_udp_user_description

from . import constants as C
from .packet import KIND_CHUNK, KIND_COMMIT, KIND_SCHEDULE, msg_layout

# A table header as the card keeps it (section 6.5), in block-RAM-free registers.
table_layout = [
    ("id", 32),
    ("entries", 8),
    ("rows", 6),
    ("planes", 5),
    ("cols", 9),
    ("clk_div", 8),
    ("blank_before", 16),
    ("lat_ticks", 16),
    ("blank_after", 16),
    ("frame_bytes", 20),
]

# One table-RAM entry: plane, OE ticks, visit ticks.
ENTRY_WIDTH = 4 + 16 + 16
TABLES = 3                     # active, pending and the one being uploaded
TABLE_SLOT = 256               # entries per table in the RAM (176 used)

COUNTERS = ("frames_shown", "frames_committed", "frames_incomplete", "frames_rejected", "frames_replaced",
            "chunks_received", "chunks_dropped", "rx_dropped", "underruns", "bad_packets",
            "sched_accepted", "sched_rejected")


def Constant8(v):
    return Constant(v, 8)


def pack_entry(plane, oe, visit):
    return (plane & 0xF) | ((oe & 0xFFFF) << 4) | ((visit & 0xFFFF) << 20)


class Counters(Module):
    """One 32-bit wrapping counter per name; each has a one-cycle increment pulse."""

    def __init__(self, names=COUNTERS):
        self.inc = {n: Signal(name="inc_" + n) for n in names}
        self.value = {n: Signal(32, name="cnt_" + n) for n in names}
        for n in names:
            self.sync += If(self.inc[n], self.value[n].eq(self.value[n] + 1))


class Receiver(LiteXModule):
    """Message sink -> SDRAM write jobs, table RAM, frame/schedule state and status replies."""

    def __init__(self, tick_hz, pattern=False, pattern_table=C.PATTERN_TABLE):
        self.sink = sink = stream.Endpoint(msg_layout)
        # Receive FIFO read side (the parser's RollbackFIFO).
        self.fifo_dout = Signal(32)
        self.fifo_readable = Signal()
        self.fifo_re = Signal()
        # SDRAM writer job: `count` words from the FIFO to word address `addr`; the writer pops the
        # FIFO itself through wr_pop while the job runs.
        self.wr_start = Signal()
        self.wr_addr = Signal(21)
        self.wr_count = Signal(10)
        self.wr_pop = Signal()
        self.wr_done = Signal()
        # Pass boundary from the fetcher (section 7.2).
        self.boundary_req = Signal()
        self.boundary_ack = Signal()
        # What the streamer reads: the active table and the shown slot.
        self.active = Record(table_layout)
        self.active_valid = Signal(reset=int(pattern))
        self.active_idx = Signal(max=TABLES)
        self.shown = Signal(max=C.BUFFERS)
        self.have_frame = Signal(reset=int(pattern))
        self.pattern_mode = Signal(reset=int(pattern))   # built-in pattern until a schedule is accepted
        # Status.
        self.counters = Counters()
        self.last_error = Signal(8)
        self.pending = Record(table_layout)
        self.pending_valid = Signal()
        self.frame_pending = Signal()
        self.frame_id_shown = Signal(32)
        self.frame_id_rx = Signal(32)
        self.inc_underruns = Signal()
        self.bad_packet = Signal()          # from the parser: counted and the last error
        self.frame_ticks = Signal(32)
        self.uptime_ms = Signal(32)
        # Reply requests (status request or schedule result).
        self.reply_start = Signal()
        self.reply_seq = Signal(32)
        self.reply_port = Signal(16)
        self.reply_ip = Signal(32)
        self.reply_busy = Signal()

        # Table RAM: three tables of TABLE_SLOT entries. The pattern build starts with its
        # built-in table in table 0.
        init = [0] * (TABLES * TABLE_SLOT)
        if pattern:
            init[0] = pack_entry(0, pattern_table["oe_ticks"], pattern_table["visit_ticks"])
        self.table_mem = Memory(ENTRY_WIDTH, TABLES * TABLE_SLOT, init=init)
        self.table_wr = self.table_mem.get_port(write_capable=True)
        self.specials += self.table_mem, self.table_wr

        # # #

        inc = self.counters.inc
        self.comb += inc["underruns"].eq(self.inc_underruns), inc["bad_packets"].eq(self.bad_packet)
        self.sync += If(self.bad_packet, self.last_error.eq(C.ERR_BAD_PACKET))

        # Uptime.
        ms_div = Signal(max=tick_hz // 1000)
        self.sync += If(ms_div == tick_hz // 1000 - 1, ms_div.eq(0), self.uptime_ms.eq(self.uptime_ms + 1)
                        ).Else(ms_div.eq(ms_div + 1))

        if pattern:
            p = pattern_table
            for name, value in (("entries", 1), ("rows", p["rows"]), ("planes", p["planes"]), ("cols", p["cols"]),
                                ("clk_div", p["clk_div"]), ("blank_before", p["blank_before"]),
                                ("lat_ticks", p["lat_ticks"]), ("blank_after", p["blank_after"]),
                                ("frame_bytes", p["planes"] * p["rows"] * p["cols"] * 6)):
                getattr(self.active, name).reset = value

        # Receiving state (7.1).
        rx_active = Signal()
        rx_seen = Signal()
        bitmap = Signal(C.MAX_CHUNKS)
        count = Signal(10)
        rx_bytes = Signal(21)
        max_index = Signal(9)
        commit_seen = Signal()
        c_sched_id = Signal(32)
        c_frame_bytes = Signal(32)
        c_chunk_count = Signal(16)
        spare = Signal(max=C.BUFFERS, reset=1)
        rxbuf = Signal(max=C.BUFFERS, reset=2)
        spare_frame_id = Signal(32)
        pending_idx = Signal(max=TABLES)
        staging_idx = Signal(max=TABLES, reset=1)

        m = Record(msg_layout)              # the message being handled
        self.sync += If(sink.valid & sink.ready, m.raw_bits().eq(sink.payload.raw_bits()))

        # Frame id a is newer than b with 32-bit wrap: 0 < (a - b) mod 2^32 < 2^31 (section 7.1).
        diff = Signal(32)
        self.comb += diff.eq(m.frame_id - self.frame_id_rx)
        is_newer = (diff != 0) & ~diff[31]
        belongs = rx_active & (m.frame_id == self.frame_id_rx)
        stale = ~belongs & rx_seen & ~is_newer
        bit_set = (bitmap >> m.index)[0]

        # The schedule being uploaded (6.5): header words then entries.
        st = Record(table_layout)
        st_tick = Signal(32)
        st_flags_len_ok = Signal()
        st_words = Signal(10)
        hdr_i = Signal(3)
        ent_i = Signal(8)
        ent_plane = Signal(8)
        ent_oe = Signal(16)
        ent_err = Signal()
        prod_pr = Signal(11)                # planes * rows
        prod_prc = Signal(20)               # planes * rows * cols
        fb_expect = Signal(23)
        sched_err = Signal(8)
        discard_left = Signal(10)

        hdr_w = self.fifo_dout
        # Raw header fields are wider than the registers; a non-zero overflow fails the limits,
        # which is also what the reference reports for them.
        st_entries_hi = Signal(8)
        st_rows_hi = Signal(2)
        st_planes_hi = Signal(3)
        st_cols_hi = Signal(7)
        st_fb_hi = Signal(12)
        hi_bad = (st_entries_hi != 0) | (st_rows_hi != 0) | (st_planes_hi != 0) | (st_cols_hi != 0) | (st_fb_hi != 0)
        # The checks in the order the reference raises them (the first failing one names the error):
        # table size and frame_bytes product, then the tick rate, then the limits.
        size_bad = (st_words != (8 + (st.entries << 1))) | (fb_expect != st.frame_bytes) | hi_bad
        tick_bad = st_tick != tick_hz
        limits_bad = ((st.entries == 0) | (st.entries > C.MAX_ENTRIES) | (st.rows == 0) | (st.rows > C.MAX_ROWS) |
                      (st.cols == 0) | (st.cols > C.MAX_COLS) | (st.planes == 0) | (st.planes > C.MAX_PLANES) |
                      (st.clk_div < 2) | (st.lat_ticks == 0) | (st.frame_bytes > C.BUFFER_BYTES))
        pending_target_id = Mux(self.pending_valid, self.pending.id, self.active.id)
        pending_target_fb = Mux(self.pending_valid, self.pending.frame_bytes, self.active.frame_bytes)
        target_ok = (self.pending_valid | self.active_valid) & (c_sched_id == pending_target_id) & \
            (c_frame_bytes == pending_target_fb)
        complete = (count == c_chunk_count) & (rx_bytes == c_frame_bytes) & (max_index < c_chunk_count)
        finished = commit_seen & (count >= c_chunk_count)

        settle = Signal(3)

        def set_error(code):
            return NextValue(self.last_error, code)

        fsm = FSM(reset_state="GET")
        self.submodules.fsm = fsm
        fsm.act("GET",
            sink.ready.eq(1),
            If(sink.valid, NextState("DECODE")),
        )
        fsm.act("DECODE",
            Case(m.kind, {
                KIND_CHUNK: [
                    If(m.range_bad,
                        set_error(C.ERR_CHUNK_RANGE), inc["chunks_dropped"].eq(1),
                        NextValue(discard_left, m.words), NextState("DISCARD"),
                    ).Else(NextState("ACCEPT"))],
                KIND_COMMIT: NextState("ACCEPT"),
                KIND_SCHEDULE: [NextValue(hdr_i, 0), NextValue(discard_left, m.words), NextState("SCHED_HDR")],
                "default": NextState("REPLY"),
            }),
        )
        fsm.act("ACCEPT",
            If(stale,
                set_error(C.ERR_STALE), inc["chunks_dropped"].eq(1),
                If(m.kind == KIND_CHUNK, NextValue(discard_left, m.words), NextState("DISCARD")).Else(NextState("GET")),
            ).Else(
                If(~belongs,
                    If(rx_active, inc["frames_incomplete"].eq(1)),
                    NextValue(bitmap, 0), NextValue(count, 0), NextValue(rx_bytes, 0), NextValue(max_index, 0),
                    NextValue(commit_seen, 0),
                    NextValue(self.frame_id_rx, m.frame_id), NextValue(rx_active, 1), NextValue(rx_seen, 1),
                ),
                If(m.kind == KIND_CHUNK,
                    self.wr_start.eq(1),
                    NextState("WRITE"),
                ).Else(
                    NextValue(c_sched_id, m.sched_id), NextValue(c_frame_bytes, m.frame_bytes),
                    NextValue(c_chunk_count, m.chunk_count), NextValue(commit_seen, 1),
                    NextState("PROMOTE"),
                ),
            ),
        )
        self.comb += [
            self.wr_addr.eq(rxbuf * C.BUFFER_WORDS + m.offset_words),
            self.wr_count.eq(m.words),
        ]
        fsm.act("WRITE",
            If(self.wr_done,
                inc["chunks_received"].eq(1),
                If(~bit_set,
                    NextValue(bitmap, bitmap | (1 << m.index)),
                    NextValue(count, count + 1),
                    NextValue(rx_bytes, rx_bytes + (m.words << 2)),
                    If(m.index > max_index, NextValue(max_index, m.index)),
                ),
                NextState("PROMOTE"),
            ),
        )
        fsm.act("PROMOTE",
            If(finished,
                NextValue(rx_active, 0),
                If(~complete,
                    set_error(C.ERR_FRAME_INCOMPLETE), inc["frames_incomplete"].eq(1),
                ).Elif(~target_ok,
                    set_error(C.ERR_FRAME_SCHEDULE), inc["frames_rejected"].eq(1),
                ).Else(
                    If(self.frame_pending, inc["frames_replaced"].eq(1)),
                    NextValue(spare, rxbuf), NextValue(rxbuf, spare),
                    NextValue(spare_frame_id, self.frame_id_rx),
                    NextValue(self.frame_pending, 1),
                    inc["frames_committed"].eq(1),
                ),
            ),
            NextState("GET"),
        )
        fsm.act("DISCARD",
            If(discard_left == 0,
                NextState("GET"),
            ).Elif(self.fifo_readable,
                self.fifo_re.eq(1),
                NextValue(discard_left, discard_left - 1),
            ),
        )
        # Schedule header: 8 words, one per cycle, from the FIFO.
        fsm.act("SCHED_HDR",
            If(self.fifo_readable,
                self.fifo_re.eq(1),
                NextValue(discard_left, discard_left - 1),
                NextValue(hdr_i, hdr_i + 1),
                Case(hdr_i, {
                    0: NextValue(st.id, hdr_w),
                    1: NextValue(st_tick, hdr_w),
                    2: [NextValue(st.entries, hdr_w[0:8]), NextValue(st_entries_hi, hdr_w[8:16]),
                        NextValue(st.rows, hdr_w[16:22]), NextValue(st_rows_hi, hdr_w[22:24]),
                        NextValue(st.planes, hdr_w[24:29]), NextValue(st_planes_hi, hdr_w[29:32])],
                    3: [NextValue(st.cols, hdr_w[0:9]), NextValue(st_cols_hi, hdr_w[9:16]),
                        NextValue(st.clk_div, hdr_w[16:24])],
                    4: [NextValue(st.blank_before, hdr_w[0:16]), NextValue(st.lat_ticks, hdr_w[16:32])],
                    5: NextValue(st.blank_after, hdr_w[0:16]),
                    6: [NextValue(st.frame_bytes, hdr_w[0:20]), NextValue(st_fb_hi, hdr_w[20:32])],
                    7: [NextValue(st_words, m.words), NextState("SCHED_MUL1")],
                }),
            ),
        )
        fsm.act("SCHED_MUL1",
            NextValue(prod_pr, st.planes * st.rows),
            NextState("SCHED_MUL2"),
        )
        fsm.act("SCHED_MUL2",
            NextValue(prod_prc, prod_pr * st.cols),
            NextState("SCHED_MUL3"),
        )
        fsm.act("SCHED_MUL3",
            NextValue(fb_expect, (prod_prc << 2) + (prod_prc << 1)),
            NextState("SCHED_CHECK"),
        )
        fsm.act("SCHED_CHECK",
            NextValue(ent_i, 0), NextValue(ent_err, 0),
            If(size_bad,
                NextValue(sched_err, C.ERR_SCHED_LIMITS), NextState("SCHED_REJECT"),
            ).Elif(tick_bad,
                NextValue(sched_err, C.ERR_SCHED_TICK), NextState("SCHED_REJECT"),
            ).Elif(limits_bad,
                NextValue(sched_err, C.ERR_SCHED_LIMITS), NextState("SCHED_REJECT"),
            ).Else(
                NextState("SCHED_ENTRY0"),
            ),
        )
        # Entries: word 0 holds the plane (low byte) and OE (high half), word 1 the visit (low
        # half). A bad entry rejects the table; the remaining words are still consumed.
        fsm.act("SCHED_ENTRY0",
            If(ent_i == st.entries,
                NextState("SCHED_ACCEPT"),
            ).Elif(self.fifo_readable,
                self.fifo_re.eq(1),
                NextValue(discard_left, discard_left - 1),
                NextValue(ent_plane, hdr_w[0:8]), NextValue(ent_oe, hdr_w[16:32]),
                NextState("SCHED_ENTRY1"),
            ),
        )
        fsm.act("SCHED_ENTRY1",
            If(self.fifo_readable,
                self.fifo_re.eq(1),
                NextValue(discard_left, discard_left - 1),
                If((ent_plane >= st.planes) | (hdr_w[0:16] == 0),
                    NextValue(sched_err, C.ERR_SCHED_ENTRY), NextState("SCHED_REJECT"),
                ).Else(
                    self.table_wr.we.eq(1),
                    NextValue(ent_i, ent_i + 1),
                    NextState("SCHED_ENTRY0"),
                ),
            ),
        )
        self.comb += [
            self.table_wr.adr.eq(staging_idx * TABLE_SLOT + ent_i),
            self.table_wr.dat_w.eq(Cat(ent_plane[0:4], ent_oe, hdr_w[0:16])),
        ]
        fsm.act("SCHED_REJECT",
            set_error(sched_err), inc["sched_rejected"].eq(1),
            NextState("SCHED_REJECT_DISCARD"),
        )
        fsm.act("SCHED_REJECT_DISCARD",
            If(discard_left == 0,
                NextState("REPLY"),
            ).Elif(self.fifo_readable,
                self.fifo_re.eq(1),
                NextValue(discard_left, discard_left - 1),
            ),
        )
        fsm.act("SCHED_ACCEPT",
            NextValue(self.pending.raw_bits(), st.raw_bits()),
            NextValue(self.pending_valid, 1),
            NextValue(pending_idx, staging_idx),
            # The next upload goes to the table that is neither active nor the new pending one:
            # the replaced pending table, or the third one.
            NextValue(staging_idx, Mux(self.pending_valid, pending_idx, 3 - self.active_idx - staging_idx)),
            inc["sched_accepted"].eq(1),
            # A pending frame packed for another table must not be shown under this one (7.3).
            If(self.frame_pending & (~self.active_valid | (st.id != self.active.id)),
                NextValue(self.frame_pending, 0), inc["frames_replaced"].eq(1),
            ),
            NextValue(settle, 0),
            NextState("SCHED_SETTLE"),
        )
        # With no table active the fetcher takes the new one at once (7.2); let that happen
        # before the reply reads the flags.
        fsm.act("SCHED_SETTLE",
            NextValue(settle, settle + 1),
            If(settle == 7, NextState("REPLY")),
        )
        fsm.act("REPLY",
            If(~self.reply_busy,
                self.reply_start.eq(1),
                NextState("GET"),
            ),
        )
        self.comb += [self.reply_seq.eq(m.seq), self.reply_port.eq(m.src_port), self.reply_ip.eq(m.src_ip)]
        # The writer pops the FIFO during its job; the receiver pops when discarding or reading tables.
        self.comb += If(fsm.ongoing("WRITE"), self.fifo_re.eq(self.wr_pop))

        # The boundary (7.2): served when no message is changing the shared state this cycle.
        touching = fsm.ongoing("PROMOTE") | fsm.ongoing("SCHED_ACCEPT")
        self.comb += self.boundary_ack.eq(self.boundary_req & ~touching)
        new_active_valid = self.active_valid | self.pending_valid
        swap = self.boundary_ack & self.frame_pending & new_active_valid
        self.comb += inc["frames_shown"].eq(swap)
        self.sync += If(self.boundary_ack,
            If(self.pending_valid,
                self.active.raw_bits().eq(self.pending.raw_bits()),
                self.active_valid.eq(1),
                self.active_idx.eq(pending_idx),
                self.pending_valid.eq(0),
                self.pattern_mode.eq(0),
            ),
            If(self.frame_pending & new_active_valid,
                self.shown.eq(spare), spare.eq(self.shown),
                self.frame_pending.eq(0),
                self.have_frame.eq(1),
                self.frame_id_shown.eq(spare_frame_id),
            ),
        )

    def flags(self):
        blank = ~self.have_frame | ~self.active_valid
        return Cat(self.have_frame, self.active_valid, self.pending_valid, self.frame_pending, blank)


class StatusReply(LiteXModule):
    """Streams one STATUS datagram (section 6.6) to the UDP port for each request."""

    def __init__(self, rx: Receiver, tick_hz, fw_build, sdram_bytes=C.SDRAM_BYTES, min_visit_ticks=C.MIN_VISIT_TICKS):
        self.source = source = stream.Endpoint(eth_udp_user_description(32))
        self.start = Signal()
        self.seq = Signal(32)
        self.port = Signal(16)
        self.ip = Signal(32)
        self.busy = Signal()

        # # #

        cnt = rx.counters.value
        flags = Signal(8)
        self.comb += flags.eq(rx.flags())
        body = [
            Cat(Constant8(C.PROTO_VERSION), Constant8(C.FW_VERSION[0]), Constant8(C.FW_VERSION[1]), flags),
            fw_build, tick_hz, rx.uptime_ms,
            Cat(Constant(min_visit_ticks, 16), Constant(C.MAX_ENTRIES, 16)),
            Cat(Constant(C.MAX_COLS, 16), Constant8(C.MAX_ROWS), Constant8(C.MAX_PLANES)),
            Cat(Constant8(C.PORTS), Constant8(C.BUFFERS), rx.last_error, Constant8(0)),
            sdram_bytes,
            Mux(rx.active_valid, rx.active.id, 0), Mux(rx.pending_valid, rx.pending.id, 0),
            rx.frame_id_shown, rx.frame_id_rx,
            cnt["frames_shown"], cnt["frames_committed"], cnt["frames_incomplete"], cnt["frames_rejected"],
            cnt["frames_replaced"], cnt["chunks_received"], cnt["chunks_dropped"], cnt["rx_dropped"],
            cnt["underruns"], rx.frame_ticks, cnt["bad_packets"], cnt["sched_accepted"], cnt["sched_rejected"],
        ]
        assert len(body) * 4 == C.STATUS_BYTES
        seq = Signal(32)
        words = [Constant(C.MAGIC_WORD, 32),
                 Constant(C.PROTO_VERSION | (C.TYPE_STATUS << 8) | (C.STATUS_BYTES << 16), 32),
                 seq, Constant(0, 32)] + body
        nwords = len(words)
        i = Signal(max=nwords)
        self.comb += [
            source.data.eq(Array(words)[i]),
            source.be.eq(0xF),
            source.last.eq(i == nwords - 1),
            source.src_port.eq(C.UDP_PORT),
            source.length.eq(nwords * 4),
        ]
        self.sync += [
            If(self.start & ~self.busy,
                self.busy.eq(1), i.eq(0),
                seq.eq(self.seq), source.dst_port.eq(self.port), source.ip_address.eq(self.ip),
            ),
            If(source.valid & source.ready,
                i.eq(i + 1),
                If(source.last, self.busy.eq(0)),
            ),
        ]
        self.comb += source.valid.eq(self.busy)
