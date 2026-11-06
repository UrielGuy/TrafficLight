"""Linux raw Ethernet through an AF_PACKET socket (the Raspberry Pi path).

Needs root or CAP_NET_RAW. A whole frame goes out in one `sendmmsg` call
(through ctypes: Python's socket module has no sendmmsg), the same approach
as FPP's ColorLight output.
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import socket
import struct
import sys
from pathlib import Path
from typing import Sequence

import numpy as np

from .base import NetInterface, OutputError
from .rawsock_base import RawSocket, missing_interface_error, no_interface_error, open_failed_error

REPLY_ETHERTYPE = 0x0805     # receiver card's answer to detection
SIOCOUTQ = 0x5411            # == TIOCOUTQ: bytes still in the socket's send queue
SO_SNDBUFFORCE = 32
SEND_BUFFER = 4 << 20        # several frames; the kernel caps it without CAP_NET_ADMIN
SYS_NET = Path("/sys/class/net")


class _iovec(ctypes.Structure):
    _fields_ = [("iov_base", ctypes.c_void_p), ("iov_len", ctypes.c_size_t)]


class _msghdr(ctypes.Structure):
    _fields_ = [("msg_name", ctypes.c_void_p), ("msg_namelen", ctypes.c_uint32),
                ("msg_iov", ctypes.c_void_p), ("msg_iovlen", ctypes.c_size_t),
                ("msg_control", ctypes.c_void_p), ("msg_controllen", ctypes.c_size_t),
                ("msg_flags", ctypes.c_int)]


class _mmsghdr(ctypes.Structure):
    _fields_ = [("msg_hdr", _msghdr), ("msg_len", ctypes.c_uint)]


def _libc_sendmmsg():
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        fn = libc.sendmmsg
    except (OSError, AttributeError):
        return None
    fn.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_int]
    fn.restype = ctypes.c_int
    return fn


_sendmmsg = _libc_sendmmsg()


def permission_error() -> OutputError:
    exe = os.path.realpath(sys.executable)
    return OutputError(
        "No permission to send raw Ethernet frames (the server needs CAP_NET_RAW).",
        "Run it as the systemd service that scripts/install_pi.sh sets up (its unit has "
        "AmbientCapabilities=CAP_NET_RAW CAP_NET_ADMIN), or give this Python the capability with "
        f"`sudo setcap cap_net_raw,cap_net_admin+eip {exe}` and restart the server, or run it as root.",
        check="output.access", title="No permission for raw Ethernet")


class _Batch:
    """sendmmsg arguments for a fixed set of spans in one buffer."""

    def __init__(self, buf: np.ndarray, spans: Sequence[tuple[int, int]]):
        self.buf = buf                      # keeps the memory alive
        self.count = len(spans)
        self.spans = [(int(o), int(n)) for o, n in spans]
        self.iov = (_iovec * self.count)()
        self.msgs = (_mmsghdr * self.count)()
        base = buf.ctypes.data
        iov_base = ctypes.addressof(self.iov)
        for i, (off, n) in enumerate(self.spans):
            self.iov[i].iov_base = base + off
            self.iov[i].iov_len = n
            self.msgs[i].msg_hdr.msg_iov = iov_base + i * ctypes.sizeof(_iovec)
            self.msgs[i].msg_hdr.msg_iovlen = 1


class LinuxRawSocket(RawSocket):
    header_room = 0

    def __init__(self, interface: str):
        self.name = self.label = interface
        if not interface:
            raise no_interface_error()
        try:
            socket.if_nametoindex(interface)
        except OSError:
            raise missing_interface_error(interface) from None
        try:
            self.sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(REPLY_ETHERTYPE))
        except PermissionError as e:
            raise permission_error() from e
        try:
            self.sock.bind((interface, REPLY_ETHERTYPE))
        except OSError as e:
            self.sock.close()
            raise open_failed_error(interface, str(e.strerror or e)) from e
        for opt in (SO_SNDBUFFORCE, socket.SO_SNDBUF):
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, opt, SEND_BUFFER)
                break
            except OSError:
                continue
        self.fd = self.sock.fileno()

    def prepare(self, buf, spans):
        return _Batch(buf, spans)

    def transmit(self, batch: _Batch) -> None:
        if _sendmmsg is None:
            mv = memoryview(batch.buf).cast("B")
            for off, n in batch.spans:
                self.sock.send(mv[off:off + n])
            return
        sent = 0
        size = ctypes.sizeof(_mmsghdr)
        base = ctypes.addressof(batch.msgs)
        while sent < batch.count:
            r = _sendmmsg(self.fd, base + sent * size, batch.count - sent, 0)
            if r < 0:
                err = ctypes.get_errno()
                if err == errno.EINTR:
                    continue
                raise OSError(err, os.strerror(err))
            sent += r

    def send(self, packet: bytes) -> None:
        self.sock.send(packet)

    def recv(self) -> bytes | None:
        try:
            return self.sock.recv(2048, socket.MSG_DONTWAIT)
        except (BlockingIOError, InterruptedError):
            return None
        except OSError:
            return None

    def link(self):
        return link_state(self.name)

    def queued_bytes(self):
        try:
            return struct.unpack("i", fcntl.ioctl(self.fd, SIOCOUTQ, b"\0\0\0\0"))[0]
        except OSError:
            return None

    def close(self):
        self.sock.close()


def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def link_state(name: str) -> tuple[bool | None, int | None]:
    d = SYS_NET / name
    oper = _read(d / "operstate")
    if oper is None:
        return False, None
    if oper == "up":
        up = True
    elif oper == "unknown":     # drivers that do not report operstate: trust the carrier
        up = _read(d / "carrier") == "1"
    else:
        up = False
    speed = _read(d / "speed") if up else None
    try:
        mbps = int(speed) if speed is not None else None
    except ValueError:
        mbps = None
    return up, (mbps if mbps and mbps > 0 else None)


def list_interfaces() -> list[NetInterface]:
    out = []
    try:
        names = sorted(p.name for p in SYS_NET.iterdir())
    except OSError:
        return []
    for name in names:
        d = SYS_NET / name
        if name == "lo" or _read(d / "type") != "1":     # 1 = ARPHRD_ETHER
            continue
        if (d / "wireless").exists() or (d / "phy80211").exists():
            continue                                      # raw frames to the card need a wire
        up, mbps = link_state(name)
        if (d / "device").exists():
            driver = os.path.basename(os.path.realpath(d / "device" / "driver")) if (d / "device" / "driver").exists() else ""
            kind = f"Ethernet ({driver})" if driver else "Ethernet"
        else:
            kind = "virtual"
        state = (f"{mbps} Mb/s" if mbps else "link up") if up else "no link"
        out.append(NetInterface(id=name, name=name, description=f"{kind}, {state}",
                                mac=_read(d / "address") or "", up=bool(up)))
    # Physical Ethernet first: that is where the card is.
    out.sort(key=lambda i: (not i.description.startswith("Ethernet"), i.name))
    return out
