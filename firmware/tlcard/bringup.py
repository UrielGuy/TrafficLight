"""Bring-up bitstreams (docs/FPGA-BRINGUP.md section 6), SRAM-load only. Every result is shown
on the user LED (T6, active low), the only user output besides the connectors.

blink    Test B: the LED blinks at 1 Hz from the 25 MHz clock.
mdio     Test D: PHY addresses 0..31 are read on MDIO; for every address whose id registers
         say RTL8211F (0x001CC916) the LED blinks (address + 1) times at 2 Hz, then pauses
         1.5 s; 3 s between rounds. A steady 5 Hz blink means no PHY answered.
memtest  Test E: the 8 MiB are written and read back with two patterns; 0.5 Hz blink = pass,
         5 Hz = fail, steady on = running, off = the SDRAM init never finished.
The HUB75 pin test (Test C) is the main gateware's pattern build (build.py --pattern).
"""
from migen import *
from migen.fhdl.specials import Tristate

from litex.gen import *
from litex.build.io import DDROutput
from litex.soc.cores.clock import ECP5PLL
from litedram.modules import M12L64322A
from litedram.phy import GENSDRPHY

from . import constants as C
from .sdram import SdramAccess, SdramCore

PHY_ID = 0x001CC916


class Blink(Module):
    def __init__(self, platform, clk_freq=25e6):
        self.clock_domains.cd_sys = ClockDomain()
        self.comb += self.cd_sys.clk.eq(platform.request("clk25"))
        div = Signal(max=int(clk_freq))
        led = Signal()
        self.sync += If(div == int(clk_freq) - 1, div.eq(0), led.eq(~led)).Else(div.eq(div + 1))
        self.comb += platform.request("user_led_n").eq(~led)


class MdioMaster(Module):
    """One Clause 22 register read; MDC at clk / div (32 -> 780 kHz from 25 MHz)."""

    def __init__(self, pads, div=32):
        self.start = Signal()
        self.phy_addr = Signal(5)
        self.reg = Signal(5)
        self.data = Signal(16)
        self.done = Signal()
        self.busy = Signal()

        # # #

        mdio_i = Signal()
        mdio_o = Signal()
        mdio_oe = Signal()
        self.specials += Tristate(pads.mdio, mdio_o, mdio_oe, mdio_i)
        # Bit times: 0..31 preamble, 32..45 ST OP PHYAD REGAD (MSB first), 46..47 turnaround
        # (released), 48..63 data, sampled on the rising edge of MDC.
        hdr = Signal(14)
        bit = Signal(7)
        tick = Signal(max=div // 2)
        mdc = Signal()
        self.comb += [
            pads.mdc.eq(mdc),
            mdio_oe.eq(self.busy & (bit < 46)),
            mdio_o.eq(Mux(bit < 32, 1, (hdr >> (13 - (bit - 32)))[0])),
        ]
        self.sync += [
            self.done.eq(0),
            If(~self.busy,
                If(self.start,
                    self.busy.eq(1), bit.eq(0), tick.eq(0), mdc.eq(0),
                    hdr.eq(Cat(self.reg, self.phy_addr, Constant(0b10, 2), Constant(0b01, 2))),
                ),
            ).Elif(tick == div // 2 - 1,
                tick.eq(0), mdc.eq(~mdc),
                If(mdc,
                    bit.eq(bit + 1),
                    If(bit == 63, self.busy.eq(0), self.done.eq(1)),
                ).Elif(bit >= 48,
                    self.data.eq(Cat(mdio_i, self.data[0:15])),
                ),
            ).Else(tick.eq(tick + 1)),
        ]


class MdioScan(Module):
    """Reads registers 2 and 3 of PHY addresses 0..31 and shows the Realtek ones on the LED."""

    def __init__(self, platform, clk_freq=25e6):
        self.clock_domains.cd_sys = ClockDomain()
        self.comb += self.cd_sys.clk.eq(platform.request("clk25"))
        clk_freq = int(clk_freq)
        self.submodules.mdio = mdio = MdioMaster(platform.request("eth", 0))
        found = Signal(32)
        addr = Signal(5)
        id_hi = Signal(16)
        led = Signal()
        self.comb += platform.request("user_led_n").eq(~led)

        wait = Signal(max=3 * clk_freq + 1)
        blinks = Signal(7)
        show = Signal(5)
        self.comb += mdio.phy_addr.eq(addr)
        fsm = FSM(reset_state="READ_HI")
        self.submodules += fsm
        fsm.act("READ_HI", mdio.start.eq(1), mdio.reg.eq(2), NextState("READ_HI_WAIT"))
        fsm.act("READ_HI_WAIT", mdio.reg.eq(2), If(mdio.done, NextValue(id_hi, mdio.data), NextState("READ_LO")))
        fsm.act("READ_LO", mdio.start.eq(1), mdio.reg.eq(3), NextState("READ_LO_WAIT"))
        fsm.act("READ_LO_WAIT", mdio.reg.eq(3),
            If(mdio.done,
                If(Cat(mdio.data, id_hi) == PHY_ID, NextValue(found, found | (1 << addr))),
                NextValue(addr, addr + 1),
                If(addr == 31, NextValue(show, 0), NextState("SHOW")).Else(NextState("READ_HI")),
            ),
        )
        fsm.act("SHOW",
            If(found == 0,
                NextValue(wait, clk_freq // 10), NextState("NONE"),
            ).Elif((found >> show)[0],
                NextValue(blinks, (show << 1) + 2), NextValue(wait, clk_freq // 4), NextState("BLINK"),
            ).Else(
                NextValue(show, show + 1),
                If(show == 31, NextValue(wait, 3 * clk_freq), NextState("PAUSE")),
            ),
        )
        fsm.act("BLINK",
            led.eq(blinks[0]),
            If(wait == 0,
                NextValue(wait, clk_freq // 4), NextValue(blinks, blinks - 1),
                If(blinks == 1,
                    NextValue(show, show + 1),
                    NextValue(wait, Mux(show == 31, 3 * clk_freq, 3 * clk_freq // 2)), NextState("PAUSE"),
                ),
            ).Else(NextValue(wait, wait - 1)),
        )
        fsm.act("PAUSE", If(wait == 0, NextState("SHOW")).Else(NextValue(wait, wait - 1)))
        fsm.act("NONE",
            led.eq(wait < clk_freq // 20),
            If(wait == 0, NextValue(wait, clk_freq // 10)).Else(NextValue(wait, wait - 1)),
        )


class MemTest(LiteXModule):
    """Writes then reads the whole SDRAM in 256-word bursts, twice: word = address, then
    address ^ 0xA5A55A5A."""

    def __init__(self, platform, sys_clk_freq=C.DEFAULT_TICK_HZ, cl=2):
        sys_clk_freq = int(sys_clk_freq)
        self.cd_sys = ClockDomain()
        self.cd_sys_ps = ClockDomain()
        self.pll = pll = ECP5PLL()
        pll.register_clkin(platform.request("clk25"), 25e6)
        pll.create_clkout(self.cd_sys, sys_clk_freq)
        pll.create_clkout(self.cd_sys_ps, sys_clk_freq, phase=180)
        self.specials += DDROutput(1, 0, platform.request("sdram_clock"), ClockSignal("sys_ps"))
        platform.add_period_constraint(self.cd_sys.clk, 1e9 / sys_clk_freq)

        self.sdrphy = GENSDRPHY(platform.request("sdram"), sys_clk_freq, cl=cl)
        self.sdram = SdramCore(self.sdrphy, M12L64322A(sys_clk_freq, "1:1"), sys_clk_freq, cl=cl)
        self.access = access = SdramAccess(self.sdram.port)
        self.errors = errors = Signal(32)
        self.done = Signal()

        words = C.SDRAM_BYTES // 4
        burst = 256
        addr = Signal(21)                      # wraps to 0 after the last word
        pattern = Signal()
        rd_count = Signal(max=burst + 1)
        wr_next = Signal(21)
        xor = Mux(pattern, 0xA5A55A5A, 0)
        self.comb += [
            access.wr_data.eq(wr_next ^ xor),
            access.rd_addr.eq(addr), access.rd_count.eq(burst),
            access.wr_addr.eq(addr), access.wr_count.eq(burst),
        ]
        fsm = FSM(reset_state="INIT")
        self.submodules += fsm
        fsm.act("INIT", If(self.sdram.ready, NextValue(addr, 0), NextValue(wr_next, 0), NextState("WRITE")))
        fsm.act("WRITE", access.wr_start.eq(1), NextState("WRITE_WAIT"))
        fsm.act("WRITE_WAIT",
            If(access.wr_pop, NextValue(wr_next, wr_next + 1)),
            If(access.wr_done,
                NextValue(addr, addr + burst),
                If(addr + burst == words, NextState("READ")).Else(NextState("WRITE")),
            ),
        )
        fsm.act("READ", access.rd_start.eq(1), NextValue(rd_count, 0), NextState("READ_WAIT"))
        fsm.act("READ_WAIT",
            If(access.rd_valid,
                If(access.rd_data != ((addr + rd_count) ^ xor), NextValue(errors, errors + 1)),
                NextValue(rd_count, rd_count + 1),
                If(rd_count == burst - 1,
                    NextValue(addr, addr + burst),
                    If(addr + burst == words,
                        If(pattern, NextState("DONE")).Else(NextValue(pattern, 1), NextValue(wr_next, 0), NextState("WRITE")),
                    ).Else(NextState("READ")),
                ),
            ),
        )
        fsm.act("DONE", self.done.eq(1))

        slow = Signal()
        fast = Signal()
        div_slow = Signal(max=sys_clk_freq)
        div_fast = Signal(max=sys_clk_freq // 10)
        self.sync += [
            If(div_slow == sys_clk_freq - 1, div_slow.eq(0), slow.eq(~slow)).Else(div_slow.eq(div_slow + 1)),
            If(div_fast == sys_clk_freq // 10 - 1, div_fast.eq(0), fast.eq(~fast)).Else(div_fast.eq(div_fast + 1)),
        ]
        led = Signal()
        self.comb += If(self.done, led.eq(Mux(errors == 0, slow, fast))).Else(led.eq(self.sdram.ready))
        self.comb += platform.request("user_led_n").eq(~led)
