"""Shared pieces of the simulation tests: UDP words from datagrams, and stream drivers."""
from __future__ import annotations

import struct

from migen.sim import passive


def datagram_words(data: bytes) -> list[tuple[int, int]]:
    """(word, byte-enable) pairs as LiteEth's 32-bit UDP user port delivers a datagram."""
    out = []
    for i in range(0, len(data), 4):
        chunk = data[i:i + 4]
        be = (1 << len(chunk)) - 1
        out.append((int.from_bytes(chunk.ljust(4, b"\0"), "little"), be))
    return out


def send_datagram(sink, data: bytes, src_port=40000, src_ip=0x0A4B0001, error_last=False, gap=0):
    """Generator: drive one datagram into a UDP user-port style endpoint (ready is not waited on
    beyond one cycle each, as the parser never stalls)."""
    words = datagram_words(data)
    for i, (w, be) in enumerate(words):
        yield sink.valid.eq(1)
        yield sink.data.eq(w)
        yield sink.be.eq(be)
        yield sink.last.eq(i == len(words) - 1)
        yield sink.error.eq(0xF if (error_last and i == len(words) - 1) else 0)
        yield sink.length.eq(len(data))
        yield sink.src_port.eq(src_port)
        yield sink.ip_address.eq(src_ip)
        yield
        while not (yield sink.ready):
            yield
    yield sink.valid.eq(0)
    yield sink.last.eq(0)
    for _ in range(gap):
        yield


def le32(v: int) -> bytes:
    return struct.pack("<I", v & 0xFFFFFFFF)


class FlashModel:
    """A configuration flash (GD25Q32 / W25Q32JV) answering command 0x4B with `uid` (8 bytes), on the reader's SPI lines, as a
    simulation generator. It shifts SI in at SCK rising edges and its id bits out after falling
    edges, and records what it saw: the bytes received, SCK half-periods and any SPI mode 0
    violation (SCK high when CS# moves, SI moving in the cycle of a rising edge)."""

    def __init__(self, spi, uid: bytes):
        self.spi = spi
        self.uid_bits = [(b >> (7 - k)) & 1 for b in uid for k in range(8)]
        self.sessions: list[list[int]] = []      # bits received per CS# low period
        self.clocks_per_session: list[int] = []
        self.violations: list[str] = []
        self.halves: list[int] = []              # cycles between consecutive SCK edges
        self.clocks_cs_high = 0
        self.first_select = None                 # cycle of the first CS# fall

    @passive
    def run(self):
        s = self.spi
        prev_sck, prev_cs, prev_mosi = 0, 1, 0
        bits: list[int] = []
        last_edge = None
        cycle = 0
        # Before the command the flash leaves SO high-Z: drive 1 so a reader sampling too early is caught.
        yield s.miso.eq(1)
        while True:
            sck, cs, mosi = (yield s.sck), (yield s.cs_n), (yield s.mosi)
            if cs != prev_cs:
                if sck or prev_sck:
                    self.violations.append(f"CS# moved with SCK high at cycle {cycle}")
                if cs == 0:
                    bits = []
                    if self.first_select is None:
                        self.first_select = cycle
                else:
                    self.sessions.append(bits)
                    yield s.miso.eq(1)
            if sck != prev_sck:
                if last_edge is not None:
                    self.halves.append(cycle - last_edge)
                last_edge = cycle
                if cs:
                    self.clocks_cs_high += sck
                elif sck:
                    if mosi != prev_mosi:
                        self.violations.append(f"SI moved at a rising edge, cycle {cycle}")
                    bits.append(mosi)
                elif len(bits) >= 40 and bits[:8] == [0, 1, 0, 0, 1, 0, 1, 1]:
                    k = len(bits) - 40
                    yield s.miso.eq(self.uid_bits[k] if k < 64 else 1)
            prev_sck, prev_cs, prev_mosi = sck, cs, mosi
            cycle += 1
            yield

    def received_bytes(self, session=0) -> bytes:
        bits = self.sessions[session]
        return bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits) - len(bits) % 8, 8))


def fold_address(uid: bytes):
    """docs/FIRMWARE.md 6.1, written out from the spec: (fold, c, d, uid_valid)."""
    u = int.from_bytes(uid, "big")
    fold = ((u >> 48) ^ (u >> 32) ^ (u >> 16) ^ u) & 0xFFFF

    def clamp(x):
        return 1 if x == 0 else 254 if x == 255 else x
    return fold, clamp(fold >> 8), clamp(fold & 0xFF), u not in (0, (1 << 64) - 1)


ANNOUNCE_BODY = struct.Struct("<BBBBI8s6sH4sI")


def parse_announce(data: bytes) -> dict:
    """An ANNOUNCE datagram (section 6.7) -> its fields; the header checked."""
    magic, version, ptype, body_len, seq, _r = struct.unpack_from("<4sBBHII", data)
    assert (magic, version, ptype, body_len, len(data)) == (b"5A75", 1, 0x82, 32, 48), data
    names = ("proto_version", "fw_major", "fw_minor", "flags", "fw_build", "uid", "mac", "udp_port", "ip", "uptime_ms")
    return dict(zip(names, ANNOUNCE_BODY.unpack_from(data, 16)), seq=seq)
