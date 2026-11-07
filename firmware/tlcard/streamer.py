"""The streamer: record fetcher, line buffers, visit sequencer and shifter (docs/FIRMWARE.md
sections 1, 4.1 and 5.1), plus the built-in pattern of the self-test build.

The fetcher walks (entry, row) over the active table two visits ahead of the light, reading
each record out of the shown SDRAM slot into the free line buffer and queueing a visit
descriptor. The sequencer times each visit from the descriptor: OE off, LAT, the shift of the
next record out of its line buffer, OE on for the entry's time. Every HUB75 pin is registered
once at the output so their relative timing is exactly the sequencer's.
"""
from migen import *

from litex.gen import *
from litex.soc.interconnect import stream

from . import constants as C
from .receiver import ENTRY_WIDTH, TABLE_SLOT

LB_COLS = C.MAX_COLS                 # columns per line buffer

# What the sequencer needs to run one visit, captured when its record was fetched.
desc_layout = [
    ("oe", 16),
    ("visit", 16),
    ("row", 5),
    ("cols", 9),
    ("clk_div", 8),
    ("blank_before", 16),
    ("lat_ticks", 16),
    ("blank_after", 16),
    ("lb", 1),                        # the line buffer holding the record
    ("pass_start", 1),                # entry 0, row 0: the pass boundary
    ("lit", 1),                       # a frame had been shown when fetched: OE may go on
]


def pattern_column(row, col):
    """The self-test picture (firmware/README.md): 48 lane bits for a column of row address `row`.

    Top half (R1 G1 B1 of every connector): rows 0..7 red, rows 24..31 blue; between them white
    blocks of 16 columns, as many as the connector number (J1: 1 block .. J8: 8); column 0 green.
    Bottom half (R2 G2 B2): red, green, blue, white bars of 32 columns, with a black diagonal
    at column = row + 48.
    """
    lanes = []
    block = col[4:8]
    bar = col[5:7]
    for c in range(C.PORTS):
        red = row < 8
        blue = row >= 24
        white = ~red & ~blue & (block <= c)
        col0 = col == 0
        r1 = Mux(col0, 0, red | white)
        g1 = Mux(col0, 1, white)
        b1 = Mux(col0, 0, blue | white)
        diag = col == (row + 48)
        r2 = ~diag & ((bar == 0) | (bar == 3))
        g2 = ~diag & ((bar == 1) | (bar == 3))
        b2 = ~diag & ((bar == 2) | (bar == 3))
        lanes += [r1, g1, b1, r2, g2, b2]
    return Cat(*lanes)


class Fetcher(Module):
    """Reads records into the line buffers and queues visit descriptors (section 4.1, 5.1)."""

    def __init__(self, rx, access, table_mem, lb_wr, lb_free, lb_claim):
        self.desc = stream.SyncFIFO(desc_layout, 4)
        self.submodules += self.desc
        self.boundary_req = Signal()

        # # #

        table_rd = table_mem.get_port()
        self.specials += table_rd
        entry = Signal(8)
        row = Signal(5)
        lb = Signal()
        plane = Signal(4)
        oe = Signal(16)
        visit = Signal(16)
        rec = Signal(10)                 # plane * rows + row
        cols6 = Signal(11)               # bytes per record
        byte_off = Signal(21)
        word_addr = Signal(21)
        half_skip = Signal()             # the record starts in the upper half of its first word
        nwords = Signal(10)
        words_left = Signal(10)
        col = Signal(9)
        acc = Signal(32)                 # half-words waiting for a whole 48-bit column
        nacc = Signal(2)
        first_word = Signal()
        active = rx.active

        self.comb += [
            access.rd_addr.eq(word_addr), access.rd_count.eq(nwords),
            table_rd.adr.eq(rx.active_idx * TABLE_SLOT + entry),
            cols6.eq((active.cols << 2) + (active.cols << 1)),
            lb_wr.adr.eq(Cat(col[0:8], lb)),
        ]
        h0, h1 = access.rd_data[0:16], access.rd_data[16:32]
        word_in = access.rd_valid & (words_left != 0)

        self.submodules.fsm = fsm = FSM(reset_state="BOUNDARY")
        fsm.act("BOUNDARY",
            self.boundary_req.eq(1),
            If(rx.boundary_ack, NextState("SETTLE")),
        )
        fsm.act("SETTLE",
            NextValue(entry, 0), NextValue(row, 0),
            If(rx.active_valid, NextState("READ_ENTRY")).Else(NextState("BOUNDARY")),
        )
        fsm.act("READ_ENTRY", NextState("LATCH_ENTRY"))
        fsm.act("LATCH_ENTRY",
            NextValue(plane, table_rd.dat_r[0:4]),
            NextValue(oe, table_rd.dat_r[4:20]),
            NextValue(visit, table_rd.dat_r[20:36]),
            NextState("MUL1"),
        )
        fsm.act("MUL1",
            NextValue(rec, plane * active.rows + row),
            NextState("MUL2"),
        )
        fsm.act("MUL2",
            NextValue(byte_off, rec * cols6),
            NextState("ADDR"),
        )
        fsm.act("ADDR",
            NextValue(word_addr, rx.shown * C.BUFFER_WORDS + byte_off[2:21]),
            NextValue(half_skip, byte_off[1]),
            NextValue(nwords, (cols6 + Cat(Constant(0, 1), byte_off[1]) + 3)[2:12]),
            NextState("WAIT_LB"),
        )
        fsm.act("WAIT_LB",
            If((lb_free >> lb)[0],
                lb_claim.eq(1 << lb),
                NextValue(col, 0), NextValue(nacc, 0), NextValue(first_word, 1),
                If(rx.pattern_mode,
                    NextState("PATTERN"),
                ).Else(
                    access.rd_start.eq(1),
                    NextValue(words_left, nwords),
                    NextState("FETCH"),
                ),
            ),
        )
        fsm.act("FETCH",
            If(word_in,
                NextValue(words_left, words_left - 1),
                NextValue(first_word, 0),
                If(first_word & half_skip,
                    NextValue(acc[0:16], h1), NextValue(nacc, 1),
                ).Elif(nacc == 0,
                    NextValue(acc, access.rd_data), NextValue(nacc, 2),
                ).Elif(nacc == 1,
                    lb_wr.dat_w.eq(Cat(acc[0:16], h0, h1)), lb_wr.we.eq(col < active.cols),
                    NextValue(col, col + 1), NextValue(nacc, 0),
                ).Else(
                    lb_wr.dat_w.eq(Cat(acc, h0)), lb_wr.we.eq(col < active.cols),
                    NextValue(col, col + 1), NextValue(acc[0:16], h1), NextValue(nacc, 1),
                ),
            ),
            If((words_left == 0) | (word_in & (words_left == 1)), NextState("PUSH")),
        )
        fsm.act("PATTERN",
            lb_wr.dat_w.eq(pattern_column(row, col[0:8])), lb_wr.we.eq(1),
            NextValue(col, col + 1),
            If(col == active.cols - 1, NextState("PUSH")),
        )
        d = self.desc.sink
        self.comb += [
            d.oe.eq(oe), d.visit.eq(visit), d.row.eq(row), d.cols.eq(active.cols), d.clk_div.eq(active.clk_div),
            d.blank_before.eq(active.blank_before), d.lat_ticks.eq(active.lat_ticks),
            d.blank_after.eq(active.blank_after), d.lb.eq(lb),
            d.pass_start.eq((entry == 0) & (row == 0)), d.lit.eq(rx.have_frame),
        ]
        fsm.act("PUSH",
            d.valid.eq(1),
            If(d.ready,
                NextValue(lb, ~lb),
                If(row == active.rows - 1,
                    NextValue(row, 0),
                    If(entry == active.entries - 1, NextValue(entry, 0), NextState("BOUNDARY")
                    ).Else(NextValue(entry, entry + 1), NextState("READ_ENTRY")),
                ).Else(
                    NextValue(row, row + 1), NextState("READ_ENTRY"),
                ),
            ),
        )


class Shifter(Module):
    """Clocks one record out of a line buffer: CLK low then high for each column, data changing
    on the falling edge (section 4.1)."""

    def __init__(self, lb_rd, lb_claim):
        self.start = Signal()
        self.lb = Signal()
        self.cols = Signal(9)
        self.clk_div = Signal(8)
        self.data = Signal(C.LANES)
        self.clk = Signal()
        self.busy = Signal()
        self.lb_free = Signal(2, reset=0b11)

        # # #

        col = Signal(9)
        ph = Signal(8)
        cols = Signal(9)
        clk_div = Signal(8)
        lb = Signal()
        last_ph = ph == clk_div - 1
        self.comb += [
            self.data.eq(lb_rd.dat_r),
            lb_rd.adr.eq(Mux(self.busy, Cat((col + last_ph)[0:8], lb), Cat(Constant(0, 8), self.lb))),
        ]
        self.sync += [
            self.lb_free.eq((self.lb_free & ~lb_claim)),
            If(self.start & ~self.busy,
                self.busy.eq(1), col.eq(0), ph.eq(0), cols.eq(self.cols), clk_div.eq(self.clk_div), lb.eq(self.lb),
            ).Elif(self.busy,
                self.clk.eq((ph + 1) >= (clk_div - clk_div[1:8])),
                If(last_ph,
                    ph.eq(0), self.clk.eq(0),
                    If(col == cols - 1,
                        self.busy.eq(0),
                        self.lb_free.eq((self.lb_free & ~lb_claim) | (1 << lb)),
                    ).Else(col.eq(col + 1)),
                ).Else(ph.eq(ph + 1)),
            ),
        ]


class Sequencer(Module):
    """Runs the visits (section 4.1) from the descriptor queue; counts underruns and the pass length."""

    def __init__(self, desc, shifter):
        self.lat = Signal()
        self.oe_n = Signal(reset=1)
        self.addr = Signal(5)
        self.underrun = Signal()
        self.frame_ticks = Signal(32)

        # # #

        cur = Record(desc_layout)
        nxt = Record(desc_layout)
        nxt_valid = Signal()
        t = Signal(17)
        t_lat_on = Signal(16)
        t_lat_off = Signal(17)
        t_oe_on = Signal(17)
        t_oe_off = Signal(18)
        ftc = Signal(32)
        stalled = Signal()

        head = desc.source
        load = Record(desc_layout)        # the descriptor that becomes `cur` at the visit end
        self.comb += If(nxt_valid, load.raw_bits().eq(nxt.raw_bits())).Else(load.raw_bits().eq(head.payload.raw_bits()))
        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        in_visit = fsm.ongoing("VISIT")
        end = (t + 1) >= cur.visit
        need_next = in_visit & ((t == t_lat_off) | end) & ~nxt_valid
        stall = need_next & ~head.valid
        take = need_next & head.valid
        warmup_take = fsm.ongoing("IDLE") & head.valid & (desc.level >= 2)
        self.comb += [
            head.ready.eq(take | warmup_take),
            shifter.start.eq(take | warmup_take),
            shifter.lb.eq(head.lb), shifter.cols.eq(head.cols), shifter.clk_div.eq(head.clk_div),
            self.underrun.eq(stall & ~stalled),
        ]
        load_cur = [
            cur.raw_bits().eq(load.raw_bits()),
            t_lat_on.eq(load.blank_before),
            t_lat_off.eq(load.blank_before + load.lat_ticks),
            t_oe_on.eq(load.blank_before + load.lat_ticks + load.blank_after),
            t_oe_off.eq(load.blank_before + load.lat_ticks + load.blank_after + load.oe),
            nxt_valid.eq(0), t.eq(0), stalled.eq(0),
        ]
        self.sync += ftc.eq(ftc + 1)
        self.cur, self.nxt, self.t, self.nxt_valid = cur, nxt, t, nxt_valid    # visible to the simulation tests
        fsm.act("IDLE",
            # Warm-up: shift record 0 with nothing lit, then visit 0 latches it. Record 1 must be
            # in its line buffer too, as in the steady state (record k+1 is fetched before visit k).
            If(warmup_take,
                NextValue(nxt.raw_bits(), head.payload.raw_bits()), NextValue(nxt_valid, 1),
                NextState("WARMUP"),
            ),
        )
        fsm.act("WARMUP",
            If(~shifter.busy & ~shifter.start,
                NextValue(cur.raw_bits(), nxt.raw_bits()),
                NextValue(t_lat_on, nxt.blank_before),
                NextValue(t_lat_off, nxt.blank_before + nxt.lat_ticks),
                NextValue(t_oe_on, nxt.blank_before + nxt.lat_ticks + nxt.blank_after),
                NextValue(t_oe_off, nxt.blank_before + nxt.lat_ticks + nxt.blank_after + nxt.oe),
                NextValue(nxt_valid, 0), NextValue(t, 0), NextValue(stalled, 0),
                NextState("VISIT"),
            ),
        )
        fsm.act("VISIT",
            If(stall,
                NextValue(stalled, 1),
            ).Else(
                NextValue(t, t + 1),
                If(take, NextValue(nxt.raw_bits(), head.payload.raw_bits()), NextValue(nxt_valid, 1)),
            ),
            If(t == 0, NextValue(self.oe_n, 1)),
            If(t == t_lat_on,
                NextValue(self.lat, 1), NextValue(self.addr, cur.row),
                If(cur.pass_start, NextValue(self.frame_ticks, ftc + 1), NextValue(ftc, 0)),
            ),
            If(t == t_lat_off, NextValue(self.lat, 0)),
            If((t == t_oe_on) & (cur.oe != 0) & cur.lit, NextValue(self.oe_n, 0)),
            If(t == t_oe_off, NextValue(self.oe_n, 1)),
            If(end & ~stall, *[NextValue(s.l, s.r) for s in load_cur]),
        )
