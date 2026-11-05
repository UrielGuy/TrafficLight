"""Raw Ethernet senders: the interface the Colorlight driver needs from the OS.

A frame's packets live back to back in one numpy buffer that the driver fills
in place. `prepare()` turns a list of packet spans in that buffer into
whatever the platform sends in one call (an `mmsghdr` array for Linux
`sendmmsg`, a pcap send queue for Npcap), so a frame costs one system call and
no per-packet Python. Each span is preceded by `header_room` bytes the sender
may use for its own per-packet header (Npcap's send queue keeps a 16-byte
record header in front of every packet).
"""
from __future__ import annotations

import sys
from typing import Any, Sequence

import numpy as np

from .base import NetInterface, OutputError


class RawSocket:
    header_room: int = 0
    name: str = ""          # interface id as opened

    def prepare(self, buf: np.ndarray, spans: Sequence[tuple[int, int]]) -> Any:
        """A batch handle for the packets at buf[off:off+length] (in order). The sender
        reads `buf` at transmit time, so it must stay alive and in place."""
        raise NotImplementedError

    def transmit(self, batch: Any) -> None:
        """Send a prepared batch. Raises OSError (or OutputError) if not all of it went."""
        raise NotImplementedError

    def send(self, packet: bytes) -> None:
        """One packet, for rare control traffic (detection)."""
        raise NotImplementedError

    def recv(self) -> bytes | None:
        """One received receiver-reply frame (EtherType 0x0805) or None; never blocks."""
        raise NotImplementedError

    def link(self) -> tuple[bool | None, int | None]:
        """(link up, speed in Mb/s); None where the OS cannot tell."""
        return None, None

    def queued_bytes(self) -> int | None:
        """Bytes handed to the OS but not yet on the wire, if the OS says (Linux)."""
        return None

    def close(self) -> None: ...


def open_raw(interface: str) -> RawSocket:
    """Open a raw sender on this interface id. OutputError with a message for the UI."""
    if sys.platform.startswith("linux"):
        from .rawsock_linux import LinuxRawSocket
        return LinuxRawSocket(interface)
    if sys.platform == "win32":
        from .rawsock_npcap import NpcapRawSocket
        return NpcapRawSocket(interface)
    raise OutputError(f"Raw Ethernet output is not supported on {sys.platform}")


def list_raw_interfaces() -> list[NetInterface]:
    if sys.platform.startswith("linux"):
        from .rawsock_linux import list_interfaces
        return list_interfaces()
    if sys.platform == "win32":
        from .rawsock_npcap import list_interfaces
        return list_interfaces()
    return []
