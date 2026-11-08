"""UDP packets between the host and the card (docs/FIRMWARE.md section 6), byte-exact.

Every datagram starts with the 16-byte common header; the body depends on the type. All
integers are little-endian, except the addresses in ANNOUNCE (network order). ``parse`` is the
card's parser too: what it rejects, the gateware rejects (counted as bad packets), and the
receiver model in ``model.py`` builds on it.
"""
from __future__ import annotations

import socket
import struct

import numpy as np
from dataclasses import dataclass

from .schedule import ENTRY_BYTES, HEADER_BYTES

MAGIC = b"5A75"
VERSION = 1
HOST_IP = "10.75.0.1"            # the host's static address on the sign's network (section 6.1)
NETMASK = "255.255.0.0"          # /16: the cards are 10.75.1.1 .. 10.75.254.254, outside the hosts' 10.75.0.x
BROADCAST_IP = "10.75.255.255"   # every card on the network; where ANNOUNCE_REQ goes and ANNOUNCE comes from
MAC_PREFIX = bytes((0x02, 0x5A, 0x75, 0x00))     # locally administered; the last two bytes are the IP's
PORT = 5775                      # the card listens here; replies go to the request's source port
ANNOUNCE_PORT = 5776             # the card broadcasts its ANNOUNCE to this port every second

COMMON_BYTES = 16
MTU_PAYLOAD = 1472               # UDP payload on a 1500-byte MTU
CHUNK_HEADER_BYTES = 16
CHUNK_ALIGN = 32                 # offsets and lengths are multiples of this (whole SDRAM words, no byte masks)
MAX_CHUNK_DATA = (MTU_PAYLOAD - COMMON_BYTES - CHUNK_HEADER_BYTES) // CHUNK_ALIGN * CHUNK_ALIGN    # 1440
MAX_CHUNKS = 512                 # the card's chunk bitmap

STATUS_REQ, FRAME_DATA, FRAME_COMMIT, SCHEDULE, STATUS = 0x01, 0x02, 0x03, 0x04, 0x81
TYPE_ANNOUNCE_REQ, TYPE_ANNOUNCE = 0x05, 0x82

# Error codes in the status reply (last_error).
ERR_NONE, ERR_BAD_PACKET, ERR_SCHED_TICK, ERR_SCHED_LIMITS, ERR_SCHED_ENTRY = 0, 1, 2, 3, 4
ERR_CHUNK_RANGE, ERR_FRAME_INCOMPLETE, ERR_FRAME_SCHEDULE, ERR_STALE = 5, 6, 7, 8
ERROR_NAMES = {ERR_NONE: "ok", ERR_BAD_PACKET: "bad packet", ERR_SCHED_TICK: "schedule: wrong tick rate",
               ERR_SCHED_LIMITS: "schedule: over the card's limits", ERR_SCHED_ENTRY: "schedule: bad entry",
               ERR_CHUNK_RANGE: "chunk out of range", ERR_FRAME_INCOMPLETE: "frame incomplete at commit",
               ERR_FRAME_SCHEDULE: "frame for another schedule", ERR_STALE: "stale frame id"}

# Status flags.
FLAG_HAVE_FRAME, FLAG_SCHED_ACTIVE, FLAG_SCHED_PENDING, FLAG_FRAME_PENDING, FLAG_BLANK = 1, 2, 4, 8, 16
# Announce flags.
ANN_UID_VALID, ANN_FIXED_ADDRESS, ANN_HAVE_FRAME = 1, 2, 4

_COMMON = struct.Struct("<4sBBHII")
_CHUNK = struct.Struct("<IHHII")
_COMMIT = struct.Struct("<IIIHH")
_ANNOUNCE = struct.Struct("<BBBBI8s6sH4sI")
ANNOUNCE_BODY_BYTES = _ANNOUNCE.size        # 32
UID_BYTES = 8

# (name, struct code) of the status body, in wire order; offsets follow from the codes (no padding).
STATUS_FIELDS: tuple[tuple[str, str], ...] = (
    ("proto_version", "B"), ("fw_major", "B"), ("fw_minor", "B"), ("flags", "B"),
    ("fw_build", "I"), ("tick_hz", "I"), ("uptime_ms", "I"),
    ("min_visit_ticks", "H"), ("max_entries", "H"), ("max_cols", "H"),
    ("max_rows", "B"), ("max_planes", "B"), ("ports", "B"), ("buffers", "B"), ("last_error", "B"), ("reserved", "B"),
    ("sdram_bytes", "I"), ("sched_active_id", "I"), ("sched_pending_id", "I"),
    ("frame_id_shown", "I"), ("frame_id_rx", "I"),
    ("frames_shown", "I"), ("frames_committed", "I"), ("frames_incomplete", "I"), ("frames_rejected", "I"),
    ("frames_replaced", "I"), ("chunks_received", "I"), ("chunks_dropped", "I"), ("rx_dropped", "I"),
    ("underruns", "I"), ("frame_ticks", "I"), ("bad_packets", "I"), ("sched_accepted", "I"), ("sched_rejected", "I"),
)
_STATUS = struct.Struct("<" + "".join(code for _, code in STATUS_FIELDS))
STATUS_BYTES = _STATUS.size


def status_offsets() -> list[tuple[int, int, str, str]]:
    """(offset, size, name, code) of every status field: the table in the spec."""
    out, off = [], 0
    for name, code in STATUS_FIELDS:
        size = struct.calcsize("<" + code)
        out.append((off, size, name, code))
        off += size
    return out


class ProtocolError(ValueError):
    """A datagram the card drops (and counts as a bad packet)."""


@dataclass(frozen=True)
class StatusRequest:
    seq: int = 0


@dataclass(frozen=True)
class FrameChunk:
    frame_id: int
    chunk_index: int
    offset: int
    data: bytes
    seq: int = 0


@dataclass(frozen=True)
class FrameCommit:
    frame_id: int
    schedule_id: int
    frame_bytes: int
    chunk_count: int
    seq: int = 0


@dataclass(frozen=True)
class SchedulePacket:
    table: bytes          # the schedule's bytes (schedule.Schedule.to_bytes)
    seq: int = 0

    @property
    def schedule_id(self) -> int:
        return struct.unpack_from("<I", self.table)[0]


@dataclass(frozen=True)
class Status:
    """The card's reply (every field of STATUS_FIELDS is an attribute)."""
    proto_version: int = VERSION
    fw_major: int = 0
    fw_minor: int = 0
    flags: int = 0
    fw_build: int = 0
    tick_hz: int = 0
    uptime_ms: int = 0
    min_visit_ticks: int = 0
    max_entries: int = 0
    max_cols: int = 0
    max_rows: int = 0
    max_planes: int = 0
    ports: int = 0
    buffers: int = 0
    last_error: int = 0
    reserved: int = 0
    sdram_bytes: int = 0
    sched_active_id: int = 0
    sched_pending_id: int = 0
    frame_id_shown: int = 0
    frame_id_rx: int = 0
    frames_shown: int = 0
    frames_committed: int = 0
    frames_incomplete: int = 0
    frames_rejected: int = 0
    frames_replaced: int = 0
    chunks_received: int = 0
    chunks_dropped: int = 0
    rx_dropped: int = 0
    underruns: int = 0
    frame_ticks: int = 0
    bad_packets: int = 0
    sched_accepted: int = 0
    sched_rejected: int = 0
    seq: int = 0

    @property
    def firmware(self) -> str:
        return f"{self.fw_major}.{self.fw_minor}"

    def flag(self, bit: int) -> bool:
        return bool(self.flags & bit)


@dataclass(frozen=True)
class AnnounceRequest:
    seq: int = 0


@dataclass(frozen=True)
class Announce:
    """Who a card is and where (section 6.7). ``uid`` is the 8 flash id bytes in the order read; ``ip`` and
    ``mac`` are in their usual text forms."""
    uid: bytes
    ip: str
    mac: str
    udp_port: int = PORT
    proto_version: int = VERSION
    fw_major: int = 0
    fw_minor: int = 0
    flags: int = 0
    fw_build: int = 0
    uptime_ms: int = 0
    seq: int = 0

    @property
    def firmware(self) -> str:
        return f"{self.fw_major}.{self.fw_minor}"

    @property
    def uid_hex(self) -> str:
        return self.uid.hex()

    def flag(self, bit: int) -> bool:
        return bool(self.flags & bit)


Packet = StatusRequest | FrameChunk | FrameCommit | SchedulePacket | Status | AnnounceRequest | Announce


# -- the card's address (section 6.1) ---------------------------------------------------

def uid_valid(uid: bytes) -> bool:
    """A flash that does not answer reads all 0 or all 1."""
    return uid not in (bytes(UID_BYTES), b"\xff" * UID_BYTES)


def uid_fold(uid: bytes) -> int:
    """The 64-bit id folded to 16 bits: cards of one batch differ in a few low bits, so every bit counts."""
    if len(uid) != UID_BYTES:
        raise ValueError(f"a flash id is {UID_BYTES} bytes, got {len(uid)}")
    u = int.from_bytes(uid, "big")
    return ((u >> 48) ^ (u >> 32) ^ (u >> 16) ^ u) & 0xFFFF


def address_from_uid(uid: bytes) -> tuple[str, str]:
    """(IP, MAC) a card with this flash id gives itself: 10.75.c.d and 02:5a:75:00:c:d, where c, d are the
    fold's bytes kept off 0 and 255 (a network and a broadcast address in every /24 view of the network)."""
    fold = uid_fold(uid)
    c, d = (min(max(b, 1), 254) for b in (fold >> 8, fold & 0xFF))
    ip = f"10.75.{c}.{d}"
    return ip, mac_for_ip(ip)


def mac_for_ip(ip: str) -> str:
    """02:5a:75:00:c:d for 10.75.c.d: the MAC follows the address, also for a build with a fixed one."""
    return format_mac(MAC_PREFIX + socket.inet_aton(ip)[2:])


def format_mac(mac: bytes) -> str:
    return ":".join(f"{b:02x}" for b in mac)


def parse_mac(text: str) -> bytes:
    mac = bytes.fromhex(text.replace(":", "").replace("-", ""))
    if len(mac) != 6:
        raise ValueError(f"not a MAC address: {text!r}")
    return mac


# -- building ----------------------------------------------------------------------------

def _common(ptype: int, body_len: int, seq: int) -> bytes:
    return _COMMON.pack(MAGIC, VERSION, ptype, body_len, seq & 0xFFFFFFFF, 0)


def build_status_request(seq: int = 0) -> bytes:
    return _common(STATUS_REQ, 0, seq)


def build_frame_chunk(frame_id: int, chunk_index: int, offset: int, data: bytes, seq: int = 0) -> bytes:
    if len(data) == 0 or len(data) > MAX_CHUNK_DATA or len(data) % CHUNK_ALIGN or offset % CHUNK_ALIGN:
        raise ValueError(f"chunk of {len(data)} bytes at {offset}: both must be multiples of {CHUNK_ALIGN}, "
                         f"length 1..{MAX_CHUNK_DATA}")
    if not 0 <= chunk_index < MAX_CHUNKS:
        raise ValueError(f"chunk index {chunk_index} not in 0..{MAX_CHUNKS - 1}")
    body = _CHUNK.pack(frame_id & 0xFFFFFFFF, chunk_index, len(data), offset, 0)
    return _common(FRAME_DATA, len(body) + len(data), seq) + body + data


def build_frame_commit(frame_id: int, schedule_id: int, frame_bytes: int, chunk_count: int, seq: int = 0) -> bytes:
    body = _COMMIT.pack(frame_id & 0xFFFFFFFF, schedule_id & 0xFFFFFFFF, frame_bytes, chunk_count, 0)
    return _common(FRAME_COMMIT, len(body), seq) + body


def build_schedule(table: bytes, seq: int = 0) -> bytes:
    if len(table) < HEADER_BYTES or (len(table) - HEADER_BYTES) % ENTRY_BYTES:
        raise ValueError("not a schedule table")
    return _common(SCHEDULE, len(table), seq) + table


def build_announce_request(seq: int = 0) -> bytes:
    return _common(TYPE_ANNOUNCE_REQ, 0, seq)


def build_announce(a: Announce) -> bytes:
    body = _ANNOUNCE.pack(a.proto_version, a.fw_major, a.fw_minor, a.flags, a.fw_build & 0xFFFFFFFF, a.uid,
                          parse_mac(a.mac), a.udp_port, socket.inet_aton(a.ip), a.uptime_ms & 0xFFFFFFFF)
    return _common(TYPE_ANNOUNCE, len(body), a.seq) + body


def build_status(status: Status) -> bytes:
    body = _STATUS.pack(*(getattr(status, name) for name, _ in STATUS_FIELDS))
    return _common(STATUS, len(body), status.seq) + body


def chunk_frame(frame: bytes | memoryview, frame_id: int, schedule_id: int, seq: int = 0,
                chunk_bytes: int = MAX_CHUNK_DATA) -> list[bytes]:
    """The datagrams of one frame: its chunks in order, then the commit. ``seq`` numbers them."""
    if chunk_bytes % CHUNK_ALIGN or not 0 < chunk_bytes <= MAX_CHUNK_DATA:
        raise ValueError(f"chunk_bytes must be a multiple of {CHUNK_ALIGN} up to {MAX_CHUNK_DATA}")
    view = memoryview(frame)
    if len(view) % CHUNK_ALIGN:
        raise ValueError(f"frame of {len(view)} bytes is not a multiple of {CHUNK_ALIGN}")
    count = (len(view) + chunk_bytes - 1) // chunk_bytes
    if count > MAX_CHUNKS:
        raise ValueError(f"{count} chunks, the card tracks {MAX_CHUNKS}")
    packets = [build_frame_chunk(frame_id, i, i * chunk_bytes, bytes(view[i * chunk_bytes:(i + 1) * chunk_bytes]), seq + i)
               for i in range(count)]
    packets.append(build_frame_commit(frame_id, schedule_id, len(view), count, seq + count))
    return packets



# One FRAME_DATA header (common + chunk fields), little-endian and unpadded like _COMMON + _CHUNK.
_CHUNK_HEAD = np.dtype([("magic", "S4"), ("version", "u1"), ("type", "u1"), ("body_len", "<u2"), ("seq", "<u4"),
                        ("reserved", "<u4"), ("frame_id", "<u4"), ("index", "<u2"), ("length", "<u2"),
                        ("offset", "<u4"), ("reserved2", "<u4")])
assert _CHUNK_HEAD.itemsize == COMMON_BYTES + CHUNK_HEADER_BYTES


def frame_rows(frame: bytes | memoryview | np.ndarray, frame_id: int, schedule_id: int, seq: int = 0,
               chunk_bytes: int = MAX_CHUNK_DATA) -> tuple[np.ndarray, np.ndarray]:
    """chunk_frame's datagrams, built at once: datagram i is rows[i, :lengths[i]], the commit last.
    All headers are made in one numpy array and the frame copied into one buffer: 200-odd bytes objects
    a frame cost the sender thread more than packing on a Raspberry Pi, and the C transmit loop
    (native.c) takes the buffer as it is."""
    if chunk_bytes % CHUNK_ALIGN or not 0 < chunk_bytes <= MAX_CHUNK_DATA:
        raise ValueError(f"chunk_bytes must be a multiple of {CHUNK_ALIGN} up to {MAX_CHUNK_DATA}")
    data = np.frombuffer(frame, np.uint8) if not isinstance(frame, np.ndarray) else frame.reshape(-1)
    n = data.size
    if n % CHUNK_ALIGN:
        raise ValueError(f"frame of {n} bytes is not a multiple of {CHUNK_ALIGN}")
    count = (n + chunk_bytes - 1) // chunk_bytes
    if count > MAX_CHUNKS:
        raise ValueError(f"{count} chunks, the card tracks {MAX_CHUNKS}")
    head = _CHUNK_HEAD.itemsize
    stride = head + chunk_bytes
    commit = build_frame_commit(frame_id, schedule_id, n, count, seq + count)
    lengths = np.full(count + 1, stride, np.uint32)
    lengths[count - 1] = head + n - (count - 1) * chunk_bytes
    lengths[count] = len(commit)
    index = np.arange(count, dtype=np.uint32)
    h = np.zeros(count, _CHUNK_HEAD)
    h["magic"], h["version"], h["type"] = MAGIC, VERSION, FRAME_DATA
    h["body_len"] = lengths[:count] - np.uint32(COMMON_BYTES)
    h["seq"] = (index + np.uint32(seq & 0xFFFFFFFF)) & np.uint32(0xFFFFFFFF)
    h["frame_id"], h["index"], h["length"] = frame_id & 0xFFFFFFFF, index, lengths[:count] - head
    h["offset"] = index * np.uint32(chunk_bytes)
    rows = np.empty((count + 1, stride), np.uint8)
    rows[:count, :head] = h.view(np.uint8).reshape(count, head)
    full = n // chunk_bytes
    rows[:full, head:] = data[:full * chunk_bytes].reshape(full, chunk_bytes)
    if full < count:
        rows[full, head:head + n - full * chunk_bytes] = data[full * chunk_bytes:]
    rows[count, :len(commit)] = np.frombuffer(commit, np.uint8)
    return rows, lengths


def frame_datagrams(rows: np.ndarray, lengths: np.ndarray) -> list:
    """frame_rows' datagrams as memoryviews, for sendto."""
    buf, stride = memoryview(rows.reshape(-1)), rows.shape[1]
    return [buf[i * stride:i * stride + int(length)] for i, length in enumerate(lengths)]


# -- parsing -----------------------------------------------------------------------------

def parse(datagram: bytes) -> Packet:
    """Datagram -> packet object. Raises ProtocolError for anything the card would drop."""
    if len(datagram) < COMMON_BYTES:
        raise ProtocolError("shorter than the common header")
    magic, version, ptype, body_len, seq, _reserved = _COMMON.unpack_from(datagram)
    if magic != MAGIC:
        raise ProtocolError("bad magic")
    if version != VERSION:
        raise ProtocolError(f"protocol version {version}, expected {VERSION}")
    body = datagram[COMMON_BYTES:]
    if len(body) != body_len:
        raise ProtocolError(f"body length {len(body)} does not match the header's {body_len}")
    if ptype == STATUS_REQ:
        return StatusRequest(seq)
    if ptype == FRAME_DATA:
        if len(body) < CHUNK_HEADER_BYTES:
            raise ProtocolError("chunk shorter than its header")
        frame_id, index, length, offset, _r = _CHUNK.unpack_from(body)
        data = body[CHUNK_HEADER_BYTES:]
        if len(data) != length:
            raise ProtocolError("chunk data length does not match its header")
        if length == 0 or length > MAX_CHUNK_DATA or length % CHUNK_ALIGN or offset % CHUNK_ALIGN:
            raise ProtocolError("chunk offset/length not aligned or too long")
        if index >= MAX_CHUNKS:
            raise ProtocolError("chunk index out of range")
        return FrameChunk(frame_id, index, offset, bytes(data), seq)
    if ptype == FRAME_COMMIT:
        if len(body) != _COMMIT.size:
            raise ProtocolError("commit body size")
        frame_id, sched_id, frame_bytes, count, _r = _COMMIT.unpack_from(body)
        return FrameCommit(frame_id, sched_id, frame_bytes, count, seq)
    if ptype == SCHEDULE:
        if len(body) < HEADER_BYTES or (len(body) - HEADER_BYTES) % ENTRY_BYTES:
            raise ProtocolError("schedule body size")
        return SchedulePacket(bytes(body), seq)
    if ptype == STATUS:
        if len(body) != STATUS_BYTES:
            raise ProtocolError("status body size")
        values = _STATUS.unpack_from(body)
        return Status(**dict(zip((n for n, _ in STATUS_FIELDS), values)), seq=seq)
    if ptype == TYPE_ANNOUNCE_REQ:
        if body:
            raise ProtocolError("announce request with a body")
        return AnnounceRequest(seq)
    if ptype == TYPE_ANNOUNCE:
        if len(body) != ANNOUNCE_BODY_BYTES:
            raise ProtocolError("announce body size")
        ver, major, minor, flags, build, uid, mac, port, ip, uptime = _ANNOUNCE.unpack_from(body)
        return Announce(uid=uid, ip=socket.inet_ntoa(ip), mac=format_mac(mac), udp_port=port, proto_version=ver,
                        fw_major=major, fw_minor=minor, flags=flags, fw_build=build, uptime_ms=uptime, seq=seq)
    raise ProtocolError(f"unknown packet type {ptype:#x}")
