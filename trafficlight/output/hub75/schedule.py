"""The visit schedule: what the card's streamer does, as a table the host builds.

One pass of the table is one refresh frame. An entry is one *plane pass*: for every row
address in turn, shift that bit plane's record out, latch, and light it for ``oe_ticks``;
the visit lasts ``visit_ticks`` from one OE-off to the next. Binary-coded modulation with
*bit splitting*: planes from ``split`` up are shown as 2**(p - split) segments of equal
length spread over the frame, so the frame is divided into 2**(planes - 1 - split)
sub-frames and each one carries a slice of the bright planes (the visible refresh is the
sub-frame rate, not the frame rate). Planes below ``split`` are shown once each.

Times are in ticks of the card's clock (``CardLimits.tick_hz``), the timebase of the whole
table. The budget and the rules the card relies on are in docs/FIRMWARE.md section 4.
"""
from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, replace

from ... import config

# The reference gateware (docs/FIRMWARE.md, section 3). The card reports its real values in
# the status reply; a schedule is built against those.
TICK_HZ = 50_000_000
MIN_VISIT_TICKS = 256          # a record (cols * 6 bytes) is fetched from SDRAM within this
MAX_ENTRIES = 176              # one UDP packet
MAX_ROWS = 32                  # five address lines A..E
MAX_COLS = 256                 # the line buffers
MAX_PLANES = 16
PORTS = config.PORTS
BYTES_PER_COLUMN = 6           # 8 connectors x 6 colour lines = 48 bits per shift clock
BUFFER_BYTES = MAX_PLANES * MAX_ROWS * MAX_COLS * BYTES_PER_COLUMN // 2    # 393,216: one SDRAM frame slot

HEADER_BYTES = 32
ENTRY_BYTES = 8
_HEADER = struct.Struct("<IIHBBHBBHHHHII")
_ENTRY = struct.Struct("<BBHHH")


class ScheduleError(ValueError):
    """The table cannot be built or is not valid for the card (the message says what to change).
    ``kind`` is what the card's error code distinguishes: "tick", "limits" or "entry"."""

    def __init__(self, message: str, kind: str = "limits"):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class CardLimits:
    """What the card is built with (from its status reply). Defaults: the reference gateware."""
    tick_hz: int = TICK_HZ
    min_visit_ticks: int = MIN_VISIT_TICKS
    max_entries: int = MAX_ENTRIES
    max_rows: int = MAX_ROWS
    max_cols: int = MAX_COLS
    max_planes: int = MAX_PLANES


@dataclass(frozen=True)
class ScheduleParams:
    """Host choices. ``lsb_ticks`` None = the longest LSB that still fits ``fps``."""
    planes: int = 12
    split: int = 6                 # planes >= split are shown as segments of 2**split LSBs
    clk_div: int = 2               # shift clock period in ticks (50 MHz / 2 = 25 MHz)
    fps: float = 60.0              # frame (pass) rate; 0 = no padding, as fast as the visits allow
    lsb_ticks: int | None = None
    rows: int = config.SCAN_ROWS
    cols: int = config.W
    blank_before: int = 10         # OE off -> LAT rise (ticks)
    lat_ticks: int = 4             # LAT pulse width
    blank_after: int = 16          # LAT fall -> OE on
    shift_margin: int = 4          # ticks between the last shift clock and the next LAT
    min_oe_ns: float = 100.0       # the shortest OE pulse the panel drivers render linearly (to confirm)


@dataclass(frozen=True)
class Entry:
    plane: int
    oe_ticks: int
    visit_ticks: int


@dataclass(frozen=True)
class Schedule:
    params: ScheduleParams
    limits: CardLimits
    entries: tuple[Entry, ...]
    subframes: tuple[tuple[int, ...], ...] = ()     # entry indices per sub-frame (not on the wire)
    schedule_id: int = 0

    # -- derived -------------------------------------------------------------------------
    @property
    def rows(self) -> int:
        return self.params.rows

    @property
    def cols(self) -> int:
        return self.params.cols

    @property
    def planes(self) -> int:
        return self.params.planes

    @property
    def record_bytes(self) -> int:
        return self.cols * BYTES_PER_COLUMN

    @property
    def frame_bytes(self) -> int:
        return self.planes * self.rows * self.record_bytes

    @property
    def blank_ticks(self) -> int:
        p = self.params
        return p.blank_before + p.lat_ticks + p.blank_after

    @property
    def shift_ticks(self) -> int:
        return self.cols * self.params.clk_div

    @property
    def frame_ticks(self) -> int:
        return self.rows * sum(e.visit_ticks for e in self.entries)

    def plane_weight_ticks(self) -> list[int]:
        """Total OE time per frame for each plane: the linear weight of that bit."""
        w = [0] * self.planes
        for e in self.entries:
            w[e.plane] += e.oe_ticks
        return w

    # -- validation ---------------------------------------------------------------------
    def min_visit_ticks(self, oe_ticks: int) -> int:
        """The shortest legal visit for an OE time: the light, the shift of the next record and the
        card's fetch must all fit (docs/FIRMWARE.md, section 4.2)."""
        return max(self.blank_ticks + oe_ticks,
                   self.shift_ticks + self.params.lat_ticks + self.params.shift_margin,
                   self.limits.min_visit_ticks)

    def validate(self) -> None:
        p, lim = self.params, self.limits
        if not 1 <= len(self.entries) <= lim.max_entries:
            raise ScheduleError(f"{len(self.entries)} entries, the card takes 1..{lim.max_entries}")
        if not 1 <= p.rows <= lim.max_rows:
            raise ScheduleError(f"rows {p.rows} not in 1..{lim.max_rows}")
        if not 1 <= p.cols <= lim.max_cols:
            raise ScheduleError(f"cols {p.cols} not in 1..{lim.max_cols}")
        if not 1 <= p.planes <= lim.max_planes:
            raise ScheduleError(f"planes {p.planes} not in 1..{lim.max_planes}")
        if p.clk_div < 2 or p.clk_div > 255:
            raise ScheduleError(f"clk_div {p.clk_div} not in 2..255")
        if p.lat_ticks < 1:
            raise ScheduleError("lat_ticks must be >= 1")
        for name in ("blank_before", "blank_after", "lat_ticks"):
            if not 0 <= getattr(p, name) <= 0xFFFF:
                raise ScheduleError(f"{name} does not fit 16 bits")
        if self.frame_bytes > BUFFER_BYTES:
            raise ScheduleError(f"frame of {self.frame_bytes} bytes exceeds the {BUFFER_BYTES}-byte buffer")
        for i, e in enumerate(self.entries):
            if not 0 <= e.plane < p.planes:
                raise ScheduleError(f"entry {i}: plane {e.plane} not in 0..{p.planes - 1}", "entry")
            if not 0 <= e.oe_ticks <= 0xFFFF or not 1 <= e.visit_ticks <= 0xFFFF:
                raise ScheduleError(f"entry {i}: oe/visit ticks do not fit 16 bits", "entry")
            need = self.min_visit_ticks(e.oe_ticks)
            if e.visit_ticks < need:
                raise ScheduleError(f"entry {i}: visit of {e.visit_ticks} ticks is shorter than the {need} "
                                    f"the light, the shift and the fetch need", "entry")
        lsb = min((e.oe_ticks for e in self.entries if e.oe_ticks), default=0)
        if lsb and lsb * 1e9 / lim.tick_hz < p.min_oe_ns - 1e-9:
            raise ScheduleError(f"shortest OE pulse {lsb * 1e9 / lim.tick_hz:.0f} ns is under the "
                                f"{p.min_oe_ns:.0f} ns minimum: fewer planes, a lower fps or a faster shift clock")
        if p.fps and self.frame_ticks > lim.tick_hz / p.fps:
            raise ScheduleError(f"frame takes {self.frame_ticks / lim.tick_hz * 1e3:.2f} ms, more than 1/{p.fps:g} s")

    # -- the numbers ---------------------------------------------------------------------
    def budget(self) -> dict:
        """The trade-off in one place (docs/FIRMWARE.md section 4 shows these for the defaults)."""
        tick = 1.0 / self.limits.tick_hz
        sub_ticks = [self.rows * sum(self.entries[i].visit_ticks for i in sf) for sf in self.subframes] or [self.frame_ticks]
        min_visit = min(e.visit_ticks for e in self.entries)
        w = self.plane_weight_ticks()
        frame_s = self.frame_ticks * tick
        return {
            "planes": self.planes, "entries": len(self.entries), "visits_per_row": len(self.entries),
            "subframes": len(sub_ticks),
            "frame_ms": frame_s * 1e3, "frame_hz": 1.0 / frame_s,
            "subframe_hz_min": 1.0 / (max(sub_ticks) * tick), "subframe_hz_max": 1.0 / (min(sub_ticks) * tick),
            "subframe_hz_mean": len(sub_ticks) / frame_s,
            "lsb_ns": min(x for x in w if x) * tick * 1e9 if any(w) else 0.0,
            "lsb_ticks": min((e.oe_ticks for e in self.entries if e.oe_ticks), default=0),
            "shift_mhz": self.limits.tick_hz / self.params.clk_div / 1e6,
            "shift_us": self.shift_ticks * tick * 1e6,
            "duty": sum(e.oe_ticks for e in self.entries) / sum(e.visit_ticks for e in self.entries),
            "read_mbps_peak": self.record_bytes / (min_visit * tick) / 1e6,
            "read_mbps_mean": len(self.entries) * self.rows * self.record_bytes / frame_s / 1e6,
            "frame_bytes": self.frame_bytes,
            "levels": sum(w) // (w[0] or 1) + 1 if w and w[0] else 0,
        }

    # -- wire format (docs/FIRMWARE.md section 6.4) ----------------------------------------
    def to_bytes(self) -> bytes:
        p = self.params
        head = _HEADER.pack(self.schedule_id, self.limits.tick_hz, len(self.entries), p.rows, p.planes, p.cols,
                            p.clk_div, 0, p.blank_before, p.lat_ticks, p.blank_after, 0, self.frame_bytes, 0)
        body = b"".join(_ENTRY.pack(e.plane, 0, e.oe_ticks, e.visit_ticks, 0) for e in self.entries)
        return head + body

    @classmethod
    def from_bytes(cls, data: bytes, limits: CardLimits | None = None, params: ScheduleParams | None = None) -> "Schedule":
        """Parse a table. ``params`` fills in what is not on the wire (fps, margins); the timing fields
        come from the bytes. Raises ScheduleError on a malformed table (what the card does too)."""
        if len(data) < HEADER_BYTES:
            raise ScheduleError("schedule shorter than its header")
        (sid, tick_hz, n, rows, planes, cols, clk_div, _flags, b1, lat, b2, _r0, frame_bytes, _r1) = \
            _HEADER.unpack_from(data)
        if len(data) != HEADER_BYTES + n * ENTRY_BYTES:
            raise ScheduleError(f"schedule of {len(data)} bytes does not hold {n} entries")
        if frame_bytes != planes * rows * cols * BYTES_PER_COLUMN:
            raise ScheduleError("frame_bytes does not match planes x rows x cols x 6")
        lim = limits or CardLimits()
        if tick_hz != lim.tick_hz:
            raise ScheduleError(f"schedule built for {tick_hz} Hz ticks, the card runs {lim.tick_hz}", "tick")
        base = params or ScheduleParams()
        p = replace(base, planes=planes, rows=rows, cols=cols, clk_div=clk_div,
                    blank_before=b1, lat_ticks=lat, blank_after=b2)
        entries = tuple(Entry(plane, oe, visit)
                        for plane, _r, oe, visit, _r2 in _ENTRY.iter_unpack(data[HEADER_BYTES:]))
        s = cls(p, lim, entries, schedule_id=sid)
        s.validate()
        return s


def entry_count(planes: int, split: int) -> int:
    """Visits per row per frame: one per plane below the split, 2**(p - split) per plane above."""
    if planes <= split:
        return planes
    return split + (2 ** (planes - split) - 1)


def subframe_count(planes: int, split: int) -> int:
    return 2 ** (planes - 1 - split) if planes > split else 1


def _layout(p: ScheduleParams) -> list[list[int]]:
    """Which planes each sub-frame shows, in order (brightest first).

    Split planes follow a ruler pattern: the MSB in every sub-frame, the next plane in every second
    one (offset by half the step), and so on, so each plane's segments are evenly spaced in time.
    Unsplit planes go one by one to the sub-frame with the least time so far (ties: the earliest),
    so sub-frames come out nearly equal and the visible refresh is steady.
    """
    n_sub = subframe_count(p.planes, p.split)
    subs: list[list[int]] = [[] for _ in range(n_sub)]
    for plane in range(p.planes - 1, p.split - 1, -1):
        n = 2 ** (plane - p.split)
        step = n_sub // n
        for i in range(n):
            subs[i * step + step // 2].append(plane)
    return subs


def _visit_ticks(p: ScheduleParams, lim: CardLimits, oe: int) -> int:
    return max(p.blank_before + p.lat_ticks + p.blank_after + oe,
               p.cols * p.clk_div + p.lat_ticks + p.shift_margin,
               lim.min_visit_ticks)


def _oe_ticks(p: ScheduleParams, lsb: int, plane: int) -> int:
    return lsb * 2 ** min(plane, p.split)


def _frame_ticks(p: ScheduleParams, lim: CardLimits, lsb: int) -> int:
    total = 0
    for plane in range(p.planes):
        segments = 2 ** (plane - p.split) if plane >= p.split else 1
        total += segments * _visit_ticks(p, lim, _oe_ticks(p, lsb, plane))
    return p.rows * total


def max_lsb_ticks(p: ScheduleParams, lim: CardLimits = CardLimits()) -> int:
    """The longest LSB whose frame still fits 1/fps (0 if even one tick does not fit)."""
    if not p.fps:
        return 0xFFFF >> min(p.planes - 1, p.split)
    budget = lim.tick_hz / p.fps
    lo, hi = 0, 0xFFFF >> min(p.planes - 1, p.split)     # the longest OE pulse must fit u16
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _frame_ticks(p, lim, mid) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo


def build(p: ScheduleParams = ScheduleParams(), lim: CardLimits = CardLimits(), schedule_id: int | None = None) -> Schedule:
    """Build and validate the table for these parameters. ``schedule_id`` defaults to a CRC of the
    table, so equal tables get equal ids and any change gets a new one."""
    if not 0 <= p.split <= p.planes:
        raise ScheduleError(f"split {p.split} not in 0..{p.planes}")
    if entry_count(p.planes, p.split) > lim.max_entries:
        raise ScheduleError(f"{p.planes} planes split at {p.split} need {entry_count(p.planes, p.split)} "
                            f"entries, the card takes {lim.max_entries}: raise split or lower planes")
    lsb = p.lsb_ticks if p.lsb_ticks is not None else max_lsb_ticks(p, lim)
    if lsb < 1:
        raise ScheduleError(f"{p.planes} planes do not fit 1/{p.fps:g} s at a {lim.tick_hz / p.clk_div / 1e6:.3g} MHz "
                            f"shift clock: fewer planes, a higher split, a faster clock or a lower fps")
    subs = _layout(p)
    short = sorted(range(min(p.split, p.planes)), reverse=True)
    load = [sum(_visit_ticks(p, lim, _oe_ticks(p, lsb, pl)) for pl in s) for s in subs]
    for plane in short:
        j = min(range(len(subs)), key=lambda k: (load[k], k))
        subs[j].append(plane)
        load[j] += _visit_ticks(p, lim, _oe_ticks(p, lsb, plane))
    entries: list[Entry] = []
    subframes: list[tuple[int, ...]] = []
    for s in subs:
        s.sort(reverse=True)
        subframes.append(tuple(range(len(entries), len(entries) + len(s))))
        for plane in s:
            oe = _oe_ticks(p, lsb, plane)
            entries.append(Entry(plane, oe, _visit_ticks(p, lim, oe)))
    if p.fps:
        # Pad the visits evenly so a pass takes exactly 1/fps (within `rows` ticks): the card then runs
        # one frame per host frame, and the pad is dark time that changes no intensity.
        spare = int(lim.tick_hz / p.fps) - p.rows * sum(e.visit_ticks for e in entries)
        if spare < 0:
            raise ScheduleError(f"lsb of {lsb} ticks does not fit 1/{p.fps:g} s (max {max_lsb_ticks(p, lim)})")
        per, extra = divmod(spare // p.rows, len(entries))
        entries = [replace(e, visit_ticks=e.visit_ticks + per + (1 if i < extra else 0)) for i, e in enumerate(entries)]
    s = Schedule(replace(p, lsb_ticks=lsb), lim, tuple(entries), tuple(subframes))
    if schedule_id is None:
        schedule_id = zlib.crc32(s.to_bytes()[4:]) or 1
    s = replace(s, schedule_id=schedule_id)
    s.validate()
    return s
