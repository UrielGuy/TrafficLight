"""The SDRAM path on LiteDRAM's own simulation model of the chip: the hardware init sequence,
the controller, and the one-port arbiter with the fetcher ahead of the writer. A record read
back equals what the writer put there, a fetch of a full record completes within
min_visit_ticks while the writer streams, and the writer still gets through."""
import os

import pytest
from migen import *
from migen.sim import passive, run_simulation
from litedram.modules import M12L64322A
from litedram.phy.model import SDRAMPHYModel

from tlcard import constants as C
from tlcard.sdram import SdramAccess, SdramCore, sdr_phy_settings

CLK = 50_000_000
RECORD_WORDS = 192                 # 128 columns x 6 bytes


class Dut(Module):
    def __init__(self):
        module = M12L64322A(CLK, "1:1")
        self.submodules.phy = SDRAMPHYModel(module, settings=sdr_phy_settings(cl=2), clk_freq=CLK)
        self.submodules.sdram = SdramCore(self.phy, module, CLK, powerup_cycles=40)
        self.submodules.access = SdramAccess(self.sdram.port)


def write_words(access, addr, words):
    """Drive one write job; the data is presented as a FIFO head would be."""
    i = 0
    yield access.wr_addr.eq(addr)
    yield access.wr_count.eq(len(words))
    yield access.wr_data.eq(words[0])
    yield access.wr_start.eq(1)
    yield
    yield access.wr_start.eq(0)
    while True:
        if (yield access.wr_pop):
            i += 1
            if i < len(words):
                yield access.wr_data.eq(words[i])
        if (yield access.wr_done):
            break
        yield
    assert i == len(words)


def read_words(access, addr, count):
    yield access.rd_addr.eq(addr)
    yield access.rd_count.eq(count)
    yield access.rd_start.eq(1)
    yield
    yield access.rd_start.eq(0)
    out, cycles = [], 0
    while len(out) < count:
        if (yield access.rd_valid):
            out.append((yield access.rd_data))
        cycles += 1
        yield
    return out, cycles


def wait_ready(dut):
    while not (yield dut.sdram.ready):
        yield


def run(dut, gens):
    run_simulation(dut, gens, vcd_name=os.environ.get("TLCARD_VCD"))


def test_init_then_write_and_read_back_across_pages():
    dut = Dut()
    words = [(i * 0x9E3779B1 + 7) & 0xFFFFFFFF for i in range(RECORD_WORDS)]
    base = 2 * C.BUFFER_WORDS + 1000          # the third slot, straddling a 256-word page

    def tb():
        yield from wait_ready(dut)
        yield from write_words(dut.access, base, words)
        got, _ = yield from read_words(dut.access, base, RECORD_WORDS)
        assert got == words
        # A second record right after it, then both again: the first one is intact.
        yield from write_words(dut.access, base + RECORD_WORDS, [w ^ 0xFFFFFFFF for w in words])
        got, _ = yield from read_words(dut.access, base, 2 * RECORD_WORDS)
        assert got == words + [w ^ 0xFFFFFFFF for w in words]

    run(dut, [tb()])


def test_fetch_fits_min_visit_ticks_while_writer_streams():
    dut = Dut()
    rec = [i for i in range(RECORD_WORDS)]
    stats = {}

    def writer():
        # Keep the writer busy with back-to-back 360-word chunks (the host's biggest) elsewhere,
        # once the record is in place (there is one writer interface).
        while not stats.get("record"):
            yield
        for _ in range(20):
            if stats.get("done"):
                break
            yield from write_words(dut.access, 0x10000, list(range(360)))
            stats["writes"] = stats.get("writes", 0) + 1

    def reader():
        yield from wait_ready(dut)
        yield from write_words(dut.access, 0, rec)
        stats["record"] = True
        for _ in range(400):
            yield
        worst = 0
        for i in range(6):
            got, cycles = yield from read_words(dut.access, 0, RECORD_WORDS)
            assert got == rec
            worst = max(worst, cycles)
            for _ in range(50):
                yield
        stats["worst"] = worst
        stats["done"] = True

    run(dut, [reader(), writer()])
    assert stats["worst"] <= C.MIN_VISIT_TICKS, stats
    assert stats["writes"] >= 2
