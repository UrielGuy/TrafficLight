"""Shared pieces of the simulation tests: UDP words from datagrams, and stream drivers."""
from __future__ import annotations

import struct


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
