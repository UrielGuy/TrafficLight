"""The card's one UDP transmitter (docs/FIRMWARE.md sections 6.6 and 6.7): a STATUS or an
ANNOUNCE to the source of a request, and the ANNOUNCE broadcast every second.

One datagram at a time. A due broadcast goes first: `busy` covers it as well as a datagram
in flight, so the receiver holds its next request (and the message FIFO behind it queues the
rest) until the broadcast is out; a stream of requests therefore cannot starve the broadcast.
Nothing is sent before the address is known (`ident.ready`, a few microseconds after start).
"""
from migen import *

from litex.gen import *
from litex.soc.interconnect import stream
from liteeth.common import eth_udp_user_description

from . import constants as C
from .identity import ip_int


def Constant8(v):
    return Constant(v, 8)


def bytes_be(value, n):
    """The n bytes of a big-endian field, first byte first, as a list of 8-bit slices."""
    return [value[8 * (n - 1 - k):8 * (n - k)] for k in range(n)]


def header(ptype, body_bytes, seq):
    return [Constant(C.MAGIC_WORD, 32), Constant(C.PROTO_VERSION | (ptype << 8) | (body_bytes << 16), 32),
            seq, Constant(0, 32)]


class Replies(LiteXModule):
    def __init__(self, rx, ident, tick_hz, fw_build, sdram_bytes=C.SDRAM_BYTES, min_visit_ticks=C.MIN_VISIT_TICKS,
                 first_ms=C.ANNOUNCE_FIRST_MS, period_ms=C.ANNOUNCE_PERIOD_MS):
        self.source = source = stream.Endpoint(eth_udp_user_description(32))
        # A request from the receiver, taken when `start` is high and `busy` low.
        self.start = Signal()
        self.announce = Signal()            # 1: ANNOUNCE, 0: STATUS
        self.seq = Signal(32)
        self.port = Signal(16)
        self.ip = Signal(32)
        self.busy = Signal()

        # # #

        sending = Signal()
        is_ann = Signal()
        seq = Signal(32)

        # The broadcast timer, in ms since start: first_ms, then (fold & 0xFF) more (the id is
        # long read by then), then every period_ms.
        left = Signal(max=max(first_ms, period_ms, 256) + 1, reset=first_ms)
        offset_done = Signal()
        offset = ident.fold[0:8]
        bcast_due = Signal()
        bcast_count = Signal(32)
        fire = Signal()
        self.comb += fire.eq(rx.ms_tick & (left == 1) & (offset_done | (offset == 0)))
        self.sync += If(rx.ms_tick,
            If(left == 1,
                offset_done.eq(1),
                left.eq(Mux(offset_done | (offset == 0), period_ms, offset)),
            ).Else(left.eq(left - 1)),
        )

        cnt = rx.counters.value
        status_flags = Signal(8)
        self.comb += status_flags.eq(rx.flags())
        status = header(C.TYPE_STATUS, C.STATUS_BYTES, seq) + [
            Cat(Constant8(C.PROTO_VERSION), Constant8(C.FW_VERSION[0]), Constant8(C.FW_VERSION[1]), status_flags),
            fw_build, tick_hz, rx.uptime_ms,
            Cat(Constant(min_visit_ticks, 16), Constant(C.MAX_ENTRIES, 16)),
            Cat(Constant(C.MAX_COLS, 16), Constant8(C.MAX_ROWS), Constant8(C.MAX_PLANES)),
            Cat(Constant8(C.PORTS), Constant8(C.BUFFERS), rx.last_error, Constant8(0)),
            sdram_bytes,
            Mux(rx.active_valid, rx.active.id, 0), Mux(rx.pending_valid, rx.pending.id, 0),
            rx.frame_id_shown, rx.frame_id_rx,
            cnt["frames_shown"], cnt["frames_committed"], cnt["frames_incomplete"], cnt["frames_rejected"],
            cnt["frames_replaced"], cnt["chunks_received"], cnt["chunks_dropped"], cnt["rx_dropped"],
            cnt["underruns"], rx.frame_ticks, cnt["bad_packets"], cnt["sched_accepted"], cnt["sched_rejected"],
        ]
        assert len(status) * 4 == C.COMMON_BYTES + C.STATUS_BYTES

        # Addresses and the id go out in network order (first byte first), the rest little-endian.
        ann_flags = Cat(ident.uid_valid, Constant(int(ident.fixed), 1), rx.have_frame, Constant(0, 5))
        uid, mac, ip = bytes_be(ident.uid, 8), bytes_be(ident.mac, 6), bytes_be(ident.ip, 4)
        announce = header(C.TYPE_ANNOUNCE, C.ANNOUNCE_BYTES, seq) + [
            Cat(Constant8(C.PROTO_VERSION), Constant8(C.FW_VERSION[0]), Constant8(C.FW_VERSION[1]), ann_flags),
            fw_build,
            Cat(*uid[0:4]), Cat(*uid[4:8]),
            Cat(*mac[0:4]), Cat(*mac[4:6], Constant(C.UDP_PORT, 16)),
            Cat(*ip),
            rx.uptime_ms,
        ]
        assert len(announce) * 4 == C.COMMON_BYTES + C.ANNOUNCE_BYTES

        # The two share header and index: word k is the announce's while it has one.
        words = [Mux(is_ann, a, s) for a, s in zip(announce, status)] + status[len(announce):]
        i = Signal(max=len(words))
        self.comb += [
            source.valid.eq(sending),
            source.data.eq(Array(words)[i]),
            source.be.eq(0xF),
            source.last.eq(Mux(is_ann, i == len(announce) - 1, i == len(status) - 1)),
            source.src_port.eq(C.UDP_PORT),
            source.length.eq(Mux(is_ann, len(announce) * 4, len(status) * 4)),
            self.busy.eq(sending | bcast_due | ~ident.ready),
        ]
        self.sync += [
            If(~sending,
                If(bcast_due & ident.ready,
                    sending.eq(1), i.eq(0), is_ann.eq(1), bcast_due.eq(0),
                    seq.eq(bcast_count), bcast_count.eq(bcast_count + 1),
                    source.dst_port.eq(C.ANNOUNCE_PORT), source.ip_address.eq(ip_int(C.ANNOUNCE_IP)),
                ).Elif(self.start & ~self.busy,
                    sending.eq(1), i.eq(0), is_ann.eq(self.announce),
                    seq.eq(self.seq), source.dst_port.eq(self.port), source.ip_address.eq(self.ip),
                ),
            ),
            If(source.valid & source.ready,
                i.eq(i + 1),
                If(source.last, sending.eq(0)),
            ),
            # Last, so a due broadcast is never lost to a launch in the same cycle.
            If(fire, bcast_due.eq(1)),
        ]
