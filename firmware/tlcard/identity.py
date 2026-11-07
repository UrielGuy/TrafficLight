"""Who the card is (docs/FIRMWARE.md section 6.1): the configuration flash's 64-bit unique id,
read once after start, and the IPv4 and MAC addresses folded from it (or fixed by the build).

Addresses are LiteEth's integers: IPv4 10.75.c.d is 0x0A4Bccdd, the MAC 0x025A7500ccdd."""
from migen import *

from litex.gen import *

from . import constants as C

UID_BITS = 8 * C.FLASH_UID_BYTES
XFER_BITS = 8 * (1 + C.FLASH_UID_DUMMY_BYTES + C.FLASH_UID_BYTES)       # command, dummies, id


def ip_int(ip: str) -> int:
    a, b, c, d = (int(x) for x in ip.split("."))
    return (a << 24) | (b << 16) | (c << 8) | d


def mac_for_ip(ip: int) -> int:
    """02:5a:75:00:c:d for 10.75.c.d."""
    return (C.MAC_PREFIX << 16) | (ip & 0xFFFF)


class FlashUidReader(LiteXModule):
    """Reads the W25Q32JV unique id once (command 0x4B, 4 dummy bytes, 8 id bytes), SPI mode 0:
    SCK idles low, MOSI changes after a falling edge, MISO is sampled at the rising edge.
    `sck` goes to the MCLK pin through USRMCLK on the card. `uid` is the id as a big-endian
    integer (first byte read in bits 63:56); `done` rises once it is complete and stays high."""

    def __init__(self, sys_clk_freq, start_us=10, sck_hz=6.25e6):
        self.sck = Signal()
        self.cs_n = Signal(reset=1)
        self.mosi = Signal()
        self.miso = Signal()
        self.uid = Signal(UID_BITS)
        self.done = Signal()

        # # #

        # At least two cycles per SCK half: MISO is sampled a whole half after the flash moved it.
        half = max(2, -(-int(sys_clk_freq) // int(2 * sck_hz)))
        start = max(16, int(sys_clk_freq * start_us // 1_000_000))
        timer = Signal(max=max(start, half) + 1, reset=start)
        edge = Signal()
        self.comb += edge.eq(timer == 0)
        self.sync += If(edge, timer.eq(half - 1)).Else(timer.eq(timer - 1))

        out = Signal(8, reset=C.FLASH_CMD_UID)          # zeros follow the command: the dummy bytes
        nbits = Signal(max=XFER_BITS + 1)
        self.comb += self.mosi.eq(out[7])

        fsm = FSM(reset_state="WAIT")
        self.fsm = fsm
        fsm.act("WAIT",
            If(edge, NextValue(nbits, 0), NextState("WAKE")),
        )
        # Eight clocks with CS# high, which the flash ignores: an MCLK edge lost while the pin
        # leaves its configuration role then cannot cut a bit from the command.
        fsm.act("WAKE",
            If(edge,
                NextValue(self.sck, ~self.sck),
                If(self.sck,
                    NextValue(nbits, nbits + 1),
                    If(nbits == 7, NextValue(nbits, 0), NextState("SELECT")),
                ),
            ),
        )
        # CS# falls half a clock after SCK did and half a clock before the first rising edge.
        fsm.act("SELECT",
            If(edge, NextValue(self.cs_n, 0), NextState("XFER")),
        )
        fsm.act("XFER",
            If(edge,
                NextValue(self.sck, ~self.sck),
                If(~self.sck,
                    NextValue(self.uid, Cat(self.miso, self.uid[:-1])),
                ).Else(
                    NextValue(out, out << 1),
                    NextValue(nbits, nbits + 1),
                    If(nbits == XFER_BITS - 1, NextState("DESELECT")),
                ),
            ),
        )
        fsm.act("DESELECT",
            If(edge, NextValue(self.cs_n, 1), NextState("DONE")),
        )
        fsm.act("DONE",
            self.done.eq(1),
        )


class CardAddress(LiteXModule):
    """The section 6.1 fold, registered at the first `load` (the id is complete): fold, the
    clamped c and d, IP and MAC, and uid_valid (the flash answered: not all 0 or all 1). With a
    fixed address, `ip` and `mac` are constants and only fold and uid_valid come from the id.
    `ready` is high from then on; later loads change nothing, so the addresses never move."""

    def __init__(self, fixed=None):
        self.uid = Signal(UID_BITS)
        self.load = Signal()
        self.ready = Signal()
        self.fold = Signal(16)
        self.uid_valid = Signal()
        self.fixed = fixed is not None

        # # #

        fold = Signal(16)
        self.comb += fold.eq(self.uid[0:16] ^ self.uid[16:32] ^ self.uid[32:48] ^ self.uid[48:64])

        def clamp(x):
            return Mux(x == 0, 1, Mux(x == 255, 254, x))

        first = Signal()
        self.comb += first.eq(self.load & ~self.ready)
        self.sync += If(first,
            self.ready.eq(1),
            self.fold.eq(fold),
            self.uid_valid.eq((self.uid != 0) & (self.uid != (1 << UID_BITS) - 1)),
        )
        if fixed is None:
            self.ip = Signal(32)
            self.mac = Signal(48)
            cd = Cat(clamp(fold[0:8]), clamp(fold[8:16]))
            self.sync += If(first,
                self.ip.eq(Cat(cd, Constant(C.NET_PREFIX[1], 8), Constant(C.NET_PREFIX[0], 8))),
                self.mac.eq(Cat(cd, Constant(C.MAC_PREFIX, 32))),
            )
        else:
            ip, mac = fixed
            self.ip = Constant(ip, 32)
            self.mac = Constant(mac, 48)
