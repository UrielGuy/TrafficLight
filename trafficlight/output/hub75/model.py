"""Behavioural model of the card (docs/FIRMWARE.md sections 7 and 8).

``Card`` is the receiver: three SDRAM frame slots, the chunk bitmap, commits, the schedule
tables, and the pass boundary where frames and schedules switch. It takes datagrams and gives
back the reply datagram, with the same counters the real card reports. ``Streamer`` turns a
frame buffer plus a schedule into light: the OE-on ticks of every pixel per frame, and the
pulse train of one pixel. Tests prove the packer and the schedule through it, and gateware
tests can compare against it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ... import config
from . import protocol as P
from .pack import unpack_bits
from .schedule import BUFFER_BYTES, BYTES_PER_COLUMN, CardLimits, PORTS, Schedule, ScheduleError

BUFFERS = 3
FW_VERSION = (0, 1)


def newer(a: int, b: int) -> bool:
    """Frame id a is newer than b, with 32-bit wrap (equal is not newer)."""
    return 0 < ((a - b) & 0xFFFFFFFF) < 0x80000000


@dataclass
class _Rx:
    """The frame being received (one at a time)."""
    frame_id: int = 0
    active: bool = False
    got: set[int] = field(default_factory=set)
    bytes_received: int = 0
    commit: P.FrameCommit | None = None


class Card:
    """The receiver state machine, counters and status reply. ``boundary()`` is the streamer
    reaching the end of a pass (or, with no schedule active, any moment)."""

    def __init__(self, limits: CardLimits = CardLimits(), sdram_bytes: int = 8 << 20):
        self.limits = limits
        self.sdram_bytes = sdram_bytes
        self.buffers = [bytearray(BUFFER_BYTES) for _ in range(BUFFERS)]
        self.shown, self.spare, self.rx_buf = 0, 1, 2       # slot roles; `spare` holds the pending frame
        self.frame_pending = False
        self.spare_frame_id = 0
        self.have_frame = False
        self.rx = _Rx()
        self.rx_seen = False               # any frame id since power-up: before that, every id is new
        self.active: Schedule | None = None
        self.pending: Schedule | None = None
        self.frame_id_shown = 0
        self.uptime_ms = 0
        self.frame_ticks = 0
        self.last_error = P.ERR_NONE
        self.counters = {name: 0 for name in ("frames_shown", "frames_committed", "frames_incomplete",
                                              "frames_rejected", "frames_replaced", "chunks_received",
                                              "chunks_dropped", "rx_dropped", "underruns", "bad_packets",
                                              "sched_accepted", "sched_rejected")}

    # -- what the panels get ------------------------------------------------------------
    @property
    def shown_frame(self) -> bytes | None:
        """The frame buffer the streamer reads (None = dark: nothing shown since power-up)."""
        if not self.have_frame or self.active is None:
            return None
        return bytes(self.buffers[self.shown][:self.active.frame_bytes])

    # -- datagrams ----------------------------------------------------------------------
    def receive(self, datagram: bytes) -> bytes | None:
        """Handle one datagram; returns the reply to send back to its source, if any."""
        try:
            pkt = P.parse(datagram)
        except P.ProtocolError:
            self._error(P.ERR_BAD_PACKET, "bad_packets")
            return None
        if isinstance(pkt, P.StatusRequest):
            return P.build_status(self.status(pkt.seq))
        if isinstance(pkt, P.FrameChunk):
            self._chunk(pkt)
        elif isinstance(pkt, P.FrameCommit):
            self._commit(pkt)
        elif isinstance(pkt, P.SchedulePacket):
            self._schedule(pkt)
            return P.build_status(self.status(pkt.seq))
        return None

    def _error(self, code: int, counter: str) -> None:
        self.last_error = code
        self.counters[counter] += 1

    def _accept(self, frame_id: int) -> bool:
        """Route a packet to the frame being received, start a newer frame (abandoning an unfinished
        one: it can only be told lost from late when something newer arrives), or drop a stale one."""
        if self.rx.active and frame_id == self.rx.frame_id:
            return True
        if self.rx_seen and not newer(frame_id, self.rx.frame_id):
            self._error(P.ERR_STALE, "chunks_dropped")
            return False
        if self.rx.active:
            self.counters["frames_incomplete"] += 1
        self.rx = _Rx(frame_id=frame_id, active=True)
        self.rx_seen = True
        return True

    def _chunk(self, c: P.FrameChunk) -> None:
        if c.offset + len(c.data) > BUFFER_BYTES:
            self._error(P.ERR_CHUNK_RANGE, "chunks_dropped")
            return
        if not self._accept(c.frame_id):
            return
        if c.chunk_index not in self.rx.got:
            self.rx.got.add(c.chunk_index)
            self.rx.bytes_received += len(c.data)
        self.buffers[self.rx_buf][c.offset:c.offset + len(c.data)] = c.data
        self.counters["chunks_received"] += 1
        self._try_promote()

    def _commit(self, c: P.FrameCommit) -> None:
        # The commit may arrive before the chunks: it is remembered and the frame waits for them.
        if not self._accept(c.frame_id):
            return
        self.rx.commit = c
        self._try_promote()

    def _try_promote(self) -> None:
        rx, c = self.rx, self.rx.commit
        if c is None or len(rx.got) < c.chunk_count:
            return
        rx.active = False
        if len(rx.got) != c.chunk_count or rx.bytes_received != c.frame_bytes or max(rx.got) >= c.chunk_count:
            self._error(P.ERR_FRAME_INCOMPLETE, "frames_incomplete")
            return
        target = self.pending or self.active
        if target is None or c.schedule_id != target.schedule_id or c.frame_bytes != target.frame_bytes:
            self._error(P.ERR_FRAME_SCHEDULE, "frames_rejected")
            return
        if self.frame_pending:
            self.counters["frames_replaced"] += 1
        self.spare, self.rx_buf = self.rx_buf, self.spare
        self.spare_frame_id = c.frame_id
        self.frame_pending = True
        self.counters["frames_committed"] += 1

    def _schedule(self, pkt: P.SchedulePacket) -> None:
        try:
            s = Schedule.from_bytes(pkt.table, self.limits)
        except ScheduleError as e:
            self._error({"tick": P.ERR_SCHED_TICK, "entry": P.ERR_SCHED_ENTRY}.get(e.kind, P.ERR_SCHED_LIMITS),
                        "sched_rejected")
            return
        if self.frame_pending and s.schedule_id != (self.active.schedule_id if self.active else None):
            # A pending frame packed for the old layout must not be shown under the new table.
            self.frame_pending = False
            self.counters["frames_replaced"] += 1
        self.pending = s
        self.counters["sched_accepted"] += 1
        if self.active is None:
            self.boundary()

    # -- the streamer's pass boundary ---------------------------------------------------
    def boundary(self) -> None:
        """What happens when the fetcher starts a new pass: the pending schedule and then the pending
        frame take over, in that order, and the pass restarts at entry 0, row 0."""
        if self.pending is not None:
            self.active, self.pending = self.pending, None
        if self.frame_pending and self.active is not None:
            self.shown, self.spare = self.spare, self.shown
            self.frame_pending = False
            self.have_frame = True
            self.frame_id_shown = self.spare_frame_id
            self.counters["frames_shown"] += 1
        if self.active is not None:
            self.frame_ticks = self.active.frame_ticks
            self.uptime_ms += self.frame_ticks * 1000 // self.limits.tick_hz

    # -- status -------------------------------------------------------------------------
    def status(self, seq: int = 0) -> P.Status:
        lim = self.limits
        flags = (P.FLAG_HAVE_FRAME * self.have_frame) | (P.FLAG_SCHED_ACTIVE * (self.active is not None)) \
            | (P.FLAG_SCHED_PENDING * (self.pending is not None)) | (P.FLAG_FRAME_PENDING * self.frame_pending) \
            | (P.FLAG_BLANK * (self.shown_frame is None))
        return P.Status(proto_version=P.VERSION, fw_major=FW_VERSION[0], fw_minor=FW_VERSION[1], flags=flags,
                        tick_hz=lim.tick_hz, uptime_ms=self.uptime_ms & 0xFFFFFFFF,
                        min_visit_ticks=lim.min_visit_ticks, max_entries=lim.max_entries, max_cols=lim.max_cols,
                        max_rows=lim.max_rows, max_planes=lim.max_planes, ports=PORTS, buffers=BUFFERS,
                        last_error=self.last_error, sdram_bytes=self.sdram_bytes,
                        sched_active_id=self.active.schedule_id if self.active else 0,
                        sched_pending_id=self.pending.schedule_id if self.pending else 0,
                        frame_id_shown=self.frame_id_shown, frame_id_rx=self.rx.frame_id,
                        frame_ticks=self.frame_ticks, seq=seq, **self.counters)


class Streamer:
    """Light output of a frame buffer under a schedule, as the gateware produces it."""

    def __init__(self, schedule: Schedule):
        self.s = schedule
        visits = np.array([e.visit_ticks for e in schedule.entries], np.int64)
        self.oe = np.array([e.oe_ticks for e in schedule.entries], np.int64)
        self.plane = np.array([e.plane for e in schedule.entries], np.int64)
        # Visit (entry k, row a) starts at entry_start[k] + a * visit_ticks[k]; OE goes on blank_ticks later.
        self.entry_start = np.concatenate([[0], np.cumsum(visits * schedule.rows)[:-1]])
        self.visit = visits

    def _bits(self, frame: bytes | np.ndarray) -> np.ndarray:
        s = self.s
        return unpack_bits(frame, s.planes, s.rows, s.cols)               # (planes, rows, cols, 48)

    def on_ticks(self, frame: bytes | np.ndarray) -> np.ndarray:
        """int64 (CARD_H, W, 3): ticks each LED is lit per frame = sum of the OE times of the visits
        whose plane bit is set. Linear intensity = on_ticks / frame_ticks."""
        s = self.s
        w = np.array(s.plane_weight_ticks(), np.int64)
        on = np.tensordot(w, self._bits(frame).astype(np.int64), axes=(0, 0))     # (rows, cols, 48)
        v = on.reshape(s.rows, s.cols, PORTS, 2, 3).transpose(2, 3, 0, 1, 4)
        return np.ascontiguousarray(v.reshape(config.CARD_H, config.W, 3))

    @staticmethod
    def lane(y: int, x: int, channel: int) -> tuple[int, int, int]:
        """Card image pixel -> (row address, column, lane bit) in a record."""
        c, r = divmod(y, config.PANEL_H)
        g, a = divmod(r, config.SCAN_ROWS)
        return a, x, c * BYTES_PER_COLUMN + g * 3 + channel

    def pulses(self, frame: bytes | np.ndarray, y: int, x: int, channel: int) -> np.ndarray:
        """int64 (n, 2): (start tick, length) of every light pulse of one LED during one frame, in time order."""
        a, col, lane = self.lane(y, x, channel)
        bits = self._bits(frame)[:, a, col, lane]                          # (planes,)
        lit = bits[self.plane].astype(bool)
        start = self.entry_start + a * self.visit + self.s.blank_ticks
        return np.stack([start[lit], self.oe[lit]], axis=1)

    def visit_times(self) -> np.ndarray:
        """int64 (entries * rows, 3): (start tick, entry, row) of every visit of a pass, in order."""
        s = self.s
        k = np.repeat(np.arange(len(s.entries)), s.rows)
        a = np.tile(np.arange(s.rows), len(s.entries))
        return np.stack([self.entry_start[k] + a * self.visit[k], k, a], axis=1)
