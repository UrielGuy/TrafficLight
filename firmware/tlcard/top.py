"""The main gateware: PLL, RGMII PHY + LiteEth UDP/IP, LiteDRAM on the SDR chip, the flash id
reader on the configuration flash, and the card core (docs/FIRMWARE.md). No CPU, no CSR bus."""
from migen import *

from litex.gen import *
from litex.build.io import DDROutput
from litex.soc.cores.clock import ECP5PLL
from litedram.modules import M12L64322A
from litedram.phy import GENSDRPHY
from liteeth.core import LiteEthUDPIPCore
from liteeth.phy.parallel.rgmii.ecp5 import LiteEthPHYRGMII

from . import constants as C
from .core import CardCore
from .sdram import SdramAccess, SdramCore


class CRG(LiteXModule):
    """25 MHz from the PHY -> sys (the tick clock, also LiteDRAM's) and a phase-shifted copy for
    the SDRAM clock pin. No reset input: the only button shares a pin with the serial RX."""

    def __init__(self, platform, sys_clk_freq):
        self.cd_sys = ClockDomain()
        self.cd_sys_ps = ClockDomain()
        clk25 = platform.request("clk25")
        self.pll = pll = ECP5PLL()
        pll.register_clkin(clk25, 25e6)
        pll.create_clkout(self.cd_sys, sys_clk_freq)
        pll.create_clkout(self.cd_sys_ps, sys_clk_freq, phase=180)
        self.specials += DDROutput(1, 0, platform.request("sdram_clock"), ClockSignal("sys_ps"))


class TLCard(LiteXModule):
    def __init__(self, platform, sys_clk_freq=C.DEFAULT_TICK_HZ, eth_phy=0, address=None,
                 pattern=False, fw_build=0, tx_delay=0.0, rx_delay=2e-9, sdram_cl=2):
        """address: None derives the card's IP and MAC from the flash id at start (section 6.1);
        (ip, mac) as LiteEth integers builds them in."""
        sys_clk_freq = int(sys_clk_freq)
        self.crg = CRG(platform, sys_clk_freq)

        # SDRAM.
        self.sdrphy = GENSDRPHY(platform.request("sdram"), sys_clk_freq, cl=sdram_cl)
        self.sdram = SdramCore(self.sdrphy, M12L64322A(sys_clk_freq, "1:1"), sys_clk_freq, cl=sdram_cl)
        self.access = SdramAccess(self.sdram.port)

        # The card, with its identity: the flash's SCK is the MCLK pin, only reachable via USRMCLK.
        self.core = core = CardCore(sys_clk_freq, self.access, fw_build=fw_build, pattern=pattern, address=address)
        flash = platform.request("config_flash")
        self.comb += [flash.cs_n.eq(core.flash.cs_n), flash.mosi.eq(core.flash.mosi), core.flash.miso.eq(flash.miso)]
        self.specials += Instance("USRMCLK", i_USRMCLKI=core.flash.sck, i_USRMCLKTS=0)

        # Ethernet: hardware ARP, ICMP and UDP; one user port, 32 bits wide so the sys-clocked
        # path outruns the wire (125 MB/s) at 50 MHz. The MAC's own datapath (CRC, converters)
        # runs in sys too: only the CDC FIFOs are on the 125 MHz PHY clocks, which keeps timing
        # there (the 8-bit TX converter chain failed 125 MHz on this part). The addresses are the
        # core's registers, set once from the flash id microseconds after start, before the core
        # sends anything; LiteEth only compares and copies them.
        self.ethphy = LiteEthPHYRGMII(
            clock_pads=platform.request("eth_clocks", eth_phy), pads=platform.request("eth", eth_phy),
            tx_delay=tx_delay, rx_delay=rx_delay)
        self.ethcore = LiteEthUDPIPCore(phy=self.ethphy, mac_address=core.mac, ip_address=core.ip, clk_freq=sys_clk_freq,
                                        dw=32, with_icmp=True, with_sys_datapath=True)
        udp_port = self.ethcore.udp.crossbar.get_port(C.UDP_PORT, dw=32)
        self.comb += [
            udp_port.source.connect(core.udp_sink),
            core.udp_source.connect(udp_port.sink),
        ]
        hub75 = platform.request("hub75")
        self.comb += [
            hub75.data.eq(core.hub75.data),
            hub75.clk.eq(core.hub75.clk),
            hub75.lat.eq(core.hub75.lat),
            hub75.oe.eq(core.hub75.oe_n),
            hub75.addr.eq(core.hub75.addr),
        ]

        # LED (active low): steady while a frame is shown, 1 Hz blink before, off until the SDRAM
        # init is through.
        blink = Signal(max=sys_clk_freq)
        led_on = Signal()
        self.sync += If(blink == sys_clk_freq - 1, blink.eq(0)).Else(blink.eq(blink + 1))
        self.comb += led_on.eq(self.sdram.ready & (core.rx.have_frame | (blink < sys_clk_freq // 2)))
        self.comb += platform.request("user_led_n").eq(~led_on)

        platform.add_period_constraint(self.crg.cd_sys.clk, 1e9 / sys_clk_freq)
