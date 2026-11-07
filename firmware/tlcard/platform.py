"""The 5A-75B V8.2 as the gateware sees it: litex-boards' platform (V8.0 pin tables, -7 speed
grade) plus the HUB75 lines as one resource in lane order (docs/FIRMWARE.md section 5.1,
docs/FPGA-BRINGUP.md section 3.1) and the configuration flash's data lines as found on V8.2."""
from litex.build.generic_platform import IOStandard, Misc, Pins, Subsignal
from litex_boards.platforms.colorlight_5a_75b import Platform as _Platform

from . import constants as C

# Connector pin index (litex-boards "jN:i" = HUB75 pin i+1) of each lane signal.
COLOUR_PINS = (0, 1, 2, 4, 5, 6)                # R1 G1 B1 R2 G2 B2
SHARED = dict(e=7, a=8, b=9, c=10, d=11, clk=12, lat=13, oe=14)


def hub75_resource():
    data = " ".join(f"j{c + 1}:{i}" for c in range(C.PORTS) for i in COLOUR_PINS)
    shared = {k: f"j1:{v}" for k, v in SHARED.items()}
    return ("hub75", 0,
            Subsignal("data", Pins(data)),
            Subsignal("clk", Pins(shared["clk"])),
            Subsignal("lat", Pins(shared["lat"])),
            Subsignal("oe", Pins(shared["oe"])),
            Subsignal("addr", Pins(" ".join(shared[k] for k in "abcde"))),
            IOStandard("LVCMOS33"), Misc("SLEWRATE=FAST"))


# The configuration flash's CS#, SI and SO (a GD25Q32 on the sign's card); its SCK is the dedicated MCLK pin, reached through USRMCLK.
CONFIG_FLASH = ("config_flash", 0,
                Subsignal("cs_n", Pins("N8")),
                Subsignal("mosi", Pins("T8")),
                Subsignal("miso", Pins("T7")),
                IOStandard("LVCMOS33"))


class Platform(_Platform):
    def __init__(self, revision="8.2"):
        super().__init__(revision=revision, toolchain="trellis")
        self.add_extension([hub75_resource(), CONFIG_FLASH])
