"""The whole card without Ethernet and SDRAM silicon: CardCore on a Python-backed SDRAM, driven
with real datagrams, with its HUB75 pins traced and its status replies decoded."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from migen import *
from migen.sim import passive, run_simulation

from trafficlight.output.hub75 import protocol as P

from tlcard import constants as C
from tlcard.core import CardCore
from simhelpers import send_datagram


class FakeSdram(Module):
    """sdram.SdramAccess's interface on a bytearray, with a fixed read latency."""

    def __init__(self, read_latency=6, write_latency=2):
        self.rd_start = Signal()
        self.rd_addr = Signal(21)
        self.rd_count = Signal(10)
        self.rd_data = Signal(32)
        self.rd_valid = Signal()
        self.rd_busy = Signal()
        self.wr_start = Signal()
        self.wr_addr = Signal(21)
        self.wr_count = Signal(10)
        self.wr_data = Signal(32)
        self.wr_pop = Signal()
        self.wr_done = Signal()
        self.wr_busy = Signal()
        self.mem = bytearray(C.BUFFERS * C.BUFFER_BYTES)
        self.read_latency = read_latency
        self.write_latency = write_latency
        self.reads = 0
        self.writes = 0

    def word(self, addr):
        return int.from_bytes(self.mem[addr * 4:addr * 4 + 4], "little")

    @passive
    def run(self):
        rd_queue = []          # (cycles until the first word, addr, left)
        wr_job = None          # [addr, left, delay]
        while True:
            if (yield self.rd_start):
                rd_queue.append([self.read_latency, (yield self.rd_addr), (yield self.rd_count)])
                self.reads += 1
            if (yield self.wr_start):
                wr_job = [(yield self.wr_addr), (yield self.wr_count), self.write_latency]
                self.writes += 1
            # Words written: wr_pop was set last cycle, so the FIFO's head seen now is the word.
            if wr_job is not None and (yield self.wr_pop):
                w = yield self.wr_data
                self.mem[wr_job[0] * 4:wr_job[0] * 4 + 4] = w.to_bytes(4, "little")
                wr_job[0] += 1
                wr_job[1] -= 1
            yield self.wr_done.eq(0)
            yield self.wr_pop.eq(0)
            if wr_job is not None:
                if wr_job[2] > 0:
                    wr_job[2] -= 1
                elif wr_job[1] > 0:
                    yield self.wr_pop.eq(1)
                else:
                    yield self.wr_done.eq(1)
                    wr_job = None
            yield self.rd_valid.eq(0)
            if rd_queue:
                job = rd_queue[0]
                if job[0] > 0:
                    job[0] -= 1
                else:
                    yield self.rd_data.eq(self.word(job[1]))
                    yield self.rd_valid.eq(1)
                    job[1] += 1
                    job[2] -= 1
                    if job[2] == 0:
                        rd_queue.pop(0)
            yield


@dataclass
class Trace:
    cycle: int
    clk: int
    lat: int
    oe_n: int
    addr: int
    data: int


@dataclass
class Pulse:
    start: int
    length: int
    row: int
    words: list[int]            # the latched record, column 0 first

    def lit(self, x, lane):
        return (self.words[x] >> lane) & 1


@dataclass
class Light:
    """What the panels saw: LAT events and OE pulses, reconstructed from the pin trace."""
    lats: list[tuple[int, int]] = field(default_factory=list)      # (cycle, row)
    pulses: list[Pulse] = field(default_factory=list)


def reconstruct(trace: list[Trace], cols: int) -> Light:
    out = Light()
    shift: list[int] = []
    latched: list[int] = []
    latched_row = 0
    prev_clk = prev_lat = 0
    oe_start = None
    for t in trace:
        if t.clk and not prev_clk:
            shift.append(t.data)
        if t.lat and not prev_lat:
            latched, latched_row = shift[-cols:], t.addr
            shift = []
            out.lats.append((t.cycle, t.addr))
        if not t.oe_n and oe_start is None:
            oe_start, oe_row, oe_words = t.cycle, latched_row, latched
        if t.oe_n and oe_start is not None:
            out.pulses.append(Pulse(oe_start, t.cycle - oe_start, oe_row, oe_words))
            oe_start = None
        prev_clk, prev_lat = t.clk, t.lat
    return out


class CardSim:
    """Build the core on a FakeSdram; `run(script)` executes a generator that sends datagrams
    (through `self.send`) and waits (`self.wait`), while the pins and replies are recorded."""

    def __init__(self, tick_hz=C.DEFAULT_TICK_HZ, pattern=False, pattern_table=C.PATTERN_TABLE, read_latency=6):
        self.sdram = FakeSdram(read_latency=read_latency)
        self.dut = CardCore(tick_hz, self.sdram, fw_build=0x20261106, pattern=pattern, pattern_table=pattern_table)
        self.dut.submodules.sdram = self.sdram
        self.trace: list[Trace] = []
        self.replies: list[P.Status] = []
        self.cycle = 0

    def send(self, datagram: bytes, **kw):
        yield from send_datagram(self.dut.udp_sink, datagram, **kw)

    def wait(self, cycles: int):
        for _ in range(cycles):
            yield

    @passive
    def _trace(self):
        h = self.dut.hub75
        while True:
            self.trace.append(Trace(self.cycle, (yield h.clk), (yield h.lat), (yield h.oe_n), (yield h.addr), (yield h.data)))
            self.cycle += 1
            yield

    @passive
    def _replies(self):
        src = self.dut.udp_source
        yield src.ready.eq(1)
        words = []
        while True:
            if (yield src.valid):
                words.append((yield src.data))
                if (yield src.last):
                    data = b"".join(w.to_bytes(4, "little") for w in words)
                    self.replies.append(P.parse(data))
                    words = []
            yield

    def run(self, script):
        run_simulation(self.dut, [script, self.sdram.run(), self._trace(), self._replies()],
                       vcd_name=os.environ.get("TLCARD_VCD"))
        return self

    def light(self, cols: int) -> Light:
        return reconstruct(self.trace, cols)
