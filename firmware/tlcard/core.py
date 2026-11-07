"""Everything between the UDP port and the HUB75 pins, with the SDRAM behind an access interface
(sdram.SdramAccess on the card, a Python model in the tests)."""
from migen import *

from litex.gen import *

from . import constants as C
from .packet import PacketParser
from .receiver import Receiver, StatusReply
from .streamer import Fetcher, Sequencer, Shifter, LB_COLS

hub75_layout = [
    ("clk", 1),
    ("lat", 1),
    ("oe_n", 1),
    ("addr", 5),
    ("data", C.LANES),
]


class CardCore(Module):
    def __init__(self, tick_hz, access, fw_build=0, pattern=False, pattern_table=C.PATTERN_TABLE):
        self.submodules.parser = parser = PacketParser()
        self.submodules.rx = rx = Receiver(tick_hz, pattern=pattern, pattern_table=pattern_table)
        self.submodules.reply = reply = StatusReply(rx, tick_hz, fw_build)
        self.udp_sink = parser.sink
        self.udp_source = reply.source
        self.hub75 = Record(hub75_layout)
        self.hub75.oe_n.reset = 1

        # # #

        fifo = parser.fifo
        self.comb += [
            parser.source.connect(rx.sink),
            rx.fifo_dout.eq(fifo.dout), rx.fifo_readable.eq(fifo.readable), fifo.re.eq(rx.fifo_re),
            rx.bad_packet.eq(parser.bad_packet),
            rx.counters.inc["rx_dropped"].eq(parser.dropped),
            access.wr_start.eq(rx.wr_start), access.wr_addr.eq(rx.wr_addr), access.wr_count.eq(rx.wr_count),
            access.wr_data.eq(fifo.dout), rx.wr_pop.eq(access.wr_pop), rx.wr_done.eq(access.wr_done),
            reply.start.eq(rx.reply_start), reply.seq.eq(rx.reply_seq), reply.port.eq(rx.reply_port),
            reply.ip.eq(rx.reply_ip), rx.reply_busy.eq(reply.busy),
        ]

        lb_mem = Memory(C.LANES, 2 * LB_COLS)
        lb_wr = lb_mem.get_port(write_capable=True)
        lb_rd = lb_mem.get_port()
        self.specials += lb_mem, lb_wr, lb_rd
        self.lb_wr, self.lb_rd = lb_wr, lb_rd
        lb_claim = Signal(2)
        self.submodules.shifter = shifter = Shifter(lb_rd, lb_claim)
        self.submodules.fetcher = fetcher = Fetcher(rx, access, rx.table_mem, lb_wr, shifter.lb_free, lb_claim)
        self.submodules.sequencer = seq = Sequencer(fetcher.desc, shifter)
        self.comb += [
            rx.boundary_req.eq(fetcher.boundary_req),
            rx.inc_underruns.eq(seq.underrun),
            rx.frame_ticks.eq(seq.frame_ticks),
        ]
        # One output register stage for all HUB75 lines: their relative timing is the sequencer's.
        self.sync += [
            self.hub75.clk.eq(shifter.clk),
            self.hub75.data.eq(shifter.data),
            self.hub75.lat.eq(seq.lat),
            self.hub75.oe_n.eq(seq.oe_n),
            self.hub75.addr.eq(seq.addr),
        ]
