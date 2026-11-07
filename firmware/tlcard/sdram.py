"""The SDRAM: LiteDRAM's controller on the one 32-bit SDR chip, a hardware init sequencer (there
is no CPU to run LiteDRAM's software init), and the two masters on one native port with the
record fetcher ahead of the chunk writer (docs/FIRMWARE.md section 4.4).

One port keeps every access in order (the crossbar locks a master to a bank until its queue
drains), so a read issued after a write returns the written data; the fetcher's reads simply
go first whenever it has any.
"""
from migen import *

from litex.gen import *
from litedram.common import PhySettings
from litedram.core.controller import ControllerSettings, LiteDRAMController
from litedram.core.crossbar import LiteDRAMCrossbar
from litedram.phy import dfi as dfi_module

# SDR command encodings on (ras_n, cas_n, we_n).
NOP, PRECHARGE, AUTO_REFRESH, LOAD_MODE = (1, 1, 1), (0, 1, 0), (0, 0, 1), (0, 0, 0)


def sdr_phy_settings(cl):
    """What GENSDRPHY reports, for the simulation model (one phase, CL+1 read latency)."""
    return PhySettings(phytype="GENSDRPHY", memtype="SDR", databits=32, dfi_databits=32, nranks=1, nphases=1,
                       rdphase=0, wrphase=0, cl=cl, read_latency=cl + 1, write_latency=0)


class SdrInit(Module):
    """Drives the DFI through the JEDEC SDR power-up sequence, then raises ``done`` for good.
    ``powerup_cycles``: the >= 200 us of NOP with CKE high the chips want after power."""

    def __init__(self, dfi, cl, powerup_cycles):
        self.dfi = dfi
        self.done = Signal()

        # # #

        mode = (cl << 4)                  # burst length 1, sequential, CAS latency cl
        # (address, command, cycles to wait after it)
        steps = [
            (0x400, PRECHARGE, 4),
            (mode | 0x100, LOAD_MODE, 4),  # with the DLL-reset bit LiteDRAM's own sequence sets
            (0x400, PRECHARGE, 4),
            (0, AUTO_REFRESH, 8),
            (0, AUTO_REFRESH, 8),
            (mode, LOAD_MODE, 4),
        ]
        p = dfi.phases[0]
        self.comb += [p.cke.eq(1), p.cs_n.eq(0), p.ras_n.eq(1), p.cas_n.eq(1), p.we_n.eq(1), p.bank.eq(0)]
        wait = Signal(max=max(powerup_cycles, 8) + 1)
        step = Signal(max=len(steps) + 1)
        issue = Signal()
        cases = {}
        for i, (addr, (ras, cas, we), hold) in enumerate(steps):
            cases[i] = [If(issue, p.address.eq(addr), p.ras_n.eq(ras), p.cas_n.eq(cas), p.we_n.eq(we))]
        self.comb += Case(step, cases)
        fsm = FSM(reset_state="POWERUP")
        self.submodules += fsm
        fsm.act("POWERUP",
            NextValue(wait, powerup_cycles),
            NextState("WAIT"),
        )
        fsm.act("WAIT",
            If(wait == 0,
                If(step == len(steps),
                    NextState("DONE"),
                ).Else(
                    issue.eq(1),
                    NextValue(wait, Array([Constant(hold) for _, _, hold in steps])[step]),
                    NextValue(step, step + 1),
                ),
            ).Else(
                NextValue(wait, wait - 1),
            ),
        )
        fsm.act("DONE", self.done.eq(1))


class SdramCore(Module):
    """LiteDRAM controller + crossbar behind the init sequencer; exposes one native port."""

    def __init__(self, phy, module, clk_freq, powerup_cycles=None, cl=2):
        if powerup_cycles is None:
            powerup_cycles = int(250e-6 * clk_freq)
        geom, timing = module.geom_settings, module.timing_settings
        self.submodules.init = init = SdrInit(
            dfi_module.Interface(geom.addressbits, geom.bankbits, phy.settings.nranks, phy.settings.dfi_databits, 1),
            cl, powerup_cycles)
        controller = LiteDRAMController(phy.settings, geom, timing, clk_freq,
                                        ControllerSettings(cmd_buffer_depth=8, read_time=64, write_time=16))
        self.submodules.controller = ResetInserter()(controller)
        self.comb += controller.reset.eq(~init.done)
        self.submodules.crossbar = LiteDRAMCrossbar(controller.interface)
        self.port = self.crossbar.get_port()
        self.comb += If(init.done, controller.dfi.connect(phy.dfi)).Else(init.dfi.connect(phy.dfi))
        self.ready = init.done


class SdramAccess(Module):
    """Two masters on one native port: the fetcher's reads before the writer's writes.

    Reads: ``rd_start`` with ``rd_addr``/``rd_count`` (words); the words come back in order on
    ``rd_data``/``rd_valid``; ``rd_busy`` falls after the last. Writes: ``wr_start`` with
    ``wr_addr``/``wr_count``; the data is taken from ``wr_data`` with a ``wr_pop`` pulse per word
    (a FIFO's head), ``wr_done`` pulses when the last word is in the controller.
    """

    def __init__(self, port):
        aw = port.address_width
        self.rd_start = Signal()
        self.rd_addr = Signal(aw)
        self.rd_count = Signal(10)
        self.rd_data = Signal(32)
        self.rd_valid = Signal()
        self.rd_busy = Signal()
        self.wr_start = Signal()
        self.wr_addr = Signal(aw)
        self.wr_count = Signal(10)
        self.wr_data = Signal(32)
        self.wr_pop = Signal()
        self.wr_done = Signal()
        self.wr_busy = Signal()

        # # #

        rd_cmds = Signal(10)
        rd_left = Signal(10)
        rd_a = Signal(aw)
        wr_cmds = Signal(10)
        wr_left = Signal(10)
        wr_a = Signal(aw)
        fetching = rd_cmds != 0

        self.comb += [
            port.cmd.valid.eq(fetching | (wr_cmds != 0)),
            port.cmd.we.eq(~fetching),
            port.cmd.addr.eq(Mux(fetching, rd_a, wr_a)),
            port.wdata.valid.eq(1),
            port.wdata.data.eq(self.wr_data),
            port.wdata.we.eq(0xF),
            self.wr_pop.eq(port.wdata.ready),
            port.rdata.ready.eq(1),
            self.rd_data.eq(port.rdata.data),
            self.rd_valid.eq(port.rdata.valid),
            self.rd_busy.eq(rd_left != 0),
            self.wr_busy.eq((wr_cmds != 0) | (wr_left != 0)),
        ]
        self.sync += [
            self.wr_done.eq(0),
            If(self.rd_start,
                rd_a.eq(self.rd_addr), rd_cmds.eq(self.rd_count), rd_left.eq(self.rd_count),
            ).Elif(port.cmd.valid & port.cmd.ready & fetching,
                rd_a.eq(rd_a + 1), rd_cmds.eq(rd_cmds - 1),
            ),
            If(port.rdata.valid & ~self.rd_start, rd_left.eq(rd_left - 1)),
            If(self.wr_start,
                wr_a.eq(self.wr_addr), wr_cmds.eq(self.wr_count), wr_left.eq(self.wr_count),
            ).Elif(port.cmd.valid & port.cmd.ready & ~fetching,
                wr_a.eq(wr_a + 1), wr_cmds.eq(wr_cmds - 1),
            ),
            If(port.wdata.ready & ~self.wr_start,
                wr_left.eq(wr_left - 1),
                If((wr_left == 1) & (wr_cmds == 0), self.wr_done.eq(1)),
            ),
        ]
