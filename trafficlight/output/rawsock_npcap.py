"""Windows raw Ethernet through Npcap (wpcap.dll via ctypes).

Interfaces are listed with the IP Helper API (GetAdaptersAddresses), not with
Npcap, so the list works without Npcap and never triggers Npcap's
administrator prompt. The id is the Npcap device name
(``\\Device\\NPF_{GUID}``), which survives restarts and renames.

A frame goes out with one `pcap_sendqueue_transmit`. The send queue is not
built with pcap_sendqueue_queue (one ctypes call and one copy per packet):
its buffer is our own packet buffer, which already has room for the 16-byte
record header in front of every packet (struct timeval with 32-bit longs on
Windows, caplen, len - the pcap file record layout).
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Sequence

import numpy as np

from .base import NetInterface, OutputError
from .rawsock_base import RawSocket, missing_interface_error, no_interface_error, open_failed_error

NPF_PREFIX = "\\Device\\NPF_"
PCAP_HDR = 16
REPLY_FILTER = b"ether proto 0x0805"
NPCAP_URL = "https://npcap.com"
IF_TYPE_ETHERNET = 6
IF_OPER_UP = 1


# ------------------------------------------------------------------ adapters (IP Helper)
class _IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


_IP_ADAPTER_ADDRESSES._fields_ = [
    ("Length", wintypes.ULONG), ("IfIndex", wintypes.DWORD),
    ("Next", ctypes.POINTER(_IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.c_void_p), ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p), ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p), ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * 8), ("PhysicalAddressLength", wintypes.ULONG),
    ("Flags", wintypes.ULONG), ("Mtu", wintypes.ULONG), ("IfType", wintypes.ULONG),
    ("OperStatus", ctypes.c_int), ("Ipv6IfIndex", wintypes.DWORD),
    ("ZoneIndices", wintypes.ULONG * 16), ("FirstPrefix", ctypes.c_void_p),
    ("TransmitLinkSpeed", ctypes.c_uint64), ("ReceiveLinkSpeed", ctypes.c_uint64),
]


class Adapter:
    def __init__(self, guid, friendly, description, mac, up, speed_bps, if_type):
        self.guid = guid
        self.friendly = friendly
        self.description = description
        self.mac = mac
        self.up = up
        self.speed_bps = speed_bps
        self.if_type = if_type

    @property
    def device(self) -> str:
        return NPF_PREFIX + self.guid

    @property
    def mbps(self) -> int | None:
        if not self.up or not self.speed_bps or self.speed_bps >= 0xFFFFFFFFFFFFFFFF:
            return None
        return int(self.speed_bps // 1_000_000)


def adapters() -> list[Adapter]:
    if sys.platform != "win32":
        return []
    iphlpapi = ctypes.WinDLL("iphlpapi")
    fn = iphlpapi.GetAdaptersAddresses
    fn.argtypes = [wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.POINTER(wintypes.ULONG)]
    fn.restype = wintypes.ULONG
    flags = 0x1 | 0x2 | 0x4 | 0x8        # skip unicast/anycast/multicast/DNS: we need none
    size = wintypes.ULONG(32 * 1024)
    for _ in range(4):
        buf = ctypes.create_string_buffer(size.value)
        r = fn(0, flags, None, buf, ctypes.byref(size))
        if r == 0:
            break
        if r != 111:                     # ERROR_BUFFER_OVERFLOW: size now holds what is needed
            return []
    else:
        return []
    out = []
    p = ctypes.cast(buf, ctypes.POINTER(_IP_ADAPTER_ADDRESSES))
    while p:
        a = p.contents
        n = a.PhysicalAddressLength
        mac = ":".join(f"{b:02x}" for b in a.PhysicalAddress[:n])
        out.append(Adapter(a.AdapterName.decode(errors="replace"), a.FriendlyName or "",
                           a.Description or "", mac, a.OperStatus == IF_OPER_UP,
                           a.TransmitLinkSpeed, a.IfType))
        p = a.Next
    return out


def list_interfaces() -> list[NetInterface]:
    out = []
    for a in adapters():
        if a.if_type != IF_TYPE_ETHERNET:
            continue
        state = (f"{a.mbps} Mb/s" if a.mbps else "link up") if a.up else "no link"
        out.append(NetInterface(id=a.device, name=a.friendly, description=f"{a.description}, {state}",
                                mac=a.mac, up=a.up))
    out.sort(key=lambda i: (not i.up, i.name.lower()))
    return out


def find_adapter(interface: str) -> Adapter | None:
    """By Npcap device name, bare GUID or friendly name ("Ethernet 2")."""
    key = interface.strip()
    guid = key[len(NPF_PREFIX):] if key.upper().startswith(NPF_PREFIX.upper()) else key
    all_ = adapters()
    for a in all_:
        if a.guid.lower() == guid.lower():
            return a
    for a in all_:
        if a.friendly.lower() == key.lower():
            return a
    return None


# ------------------------------------------------------------------ Npcap
class _pcap_pkthdr(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long),
                ("caplen", ctypes.c_uint32), ("len", ctypes.c_uint32)]


class _pcap_send_queue(ctypes.Structure):
    _fields_ = [("maxlen", ctypes.c_uint), ("len", ctypes.c_uint), ("buffer", ctypes.c_void_p)]


class _bpf_program(ctypes.Structure):
    _fields_ = [("bf_len", ctypes.c_uint), ("bf_insns", ctypes.c_void_p)]


_wpcap = None


def npcap_dir() -> Path:
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "Npcap"


def load_wpcap():
    """wpcap.dll with argtypes set. OutputError if Npcap is not installed."""
    global _wpcap
    if _wpcap is not None:
        return _wpcap
    d = npcap_dir()
    try:
        if d.is_dir():
            os.add_dll_directory(str(d))
            lib = ctypes.CDLL(str(d / "wpcap.dll"))
        else:                            # WinPcap-compatible install puts it in System32
            lib = ctypes.CDLL("wpcap.dll")
    except OSError as e:
        raise OutputError(
            "Npcap is not installed (wpcap.dll not found): Windows needs it to send raw Ethernet frames.",
            f"Install it from {NPCAP_URL}, leave 'Restrict Npcap driver's access to Administrators only' "
            "unticked, then restart the server.", check="output.access", title="Npcap is not installed") from e
    vp, cp, i = ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int
    sig = {
        "pcap_lib_version": ([], cp),
        "pcap_open_live": ([cp, i, i, i, ctypes.c_char_p], vp),
        "pcap_close": ([vp], None),
        "pcap_geterr": ([vp], cp),
        "pcap_setnonblock": ([vp, i, ctypes.c_char_p], i),
        "pcap_compile": ([vp, ctypes.POINTER(_bpf_program), cp, i, ctypes.c_uint32], i),
        "pcap_setfilter": ([vp, ctypes.POINTER(_bpf_program)], i),
        "pcap_freecode": ([ctypes.POINTER(_bpf_program)], None),
        "pcap_sendpacket": ([vp, vp, i], i),
        "pcap_sendqueue_transmit": ([vp, ctypes.POINTER(_pcap_send_queue), i], ctypes.c_uint),
        "pcap_next_ex": ([vp, ctypes.POINTER(ctypes.POINTER(_pcap_pkthdr)),
                          ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))], i),
    }
    for name, (args, res) in sig.items():
        f = getattr(lib, name)
        f.argtypes = args
        f.restype = res
    _wpcap = lib
    return lib


def npcap_admin_only() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Services\npcap\Parameters") as k:
            return bool(winreg.QueryValueEx(k, "AdminOnly")[0])
    except OSError:
        return False


def is_elevated() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


class _Batch:
    def __init__(self, buf: np.ndarray, start: int, end: int):
        self.buf = buf                   # keeps the memory alive
        self.queue = _pcap_send_queue(end - start, end - start, buf.ctypes.data + start)
        self.bytes = end - start


class NpcapRawSocket(RawSocket):
    header_room = PCAP_HDR

    def __init__(self, interface: str):
        if not interface:
            raise no_interface_error()
        lib = load_wpcap()
        adapter = find_adapter(interface)
        if adapter is None:
            raise missing_interface_error(interface)
        self.adapter = adapter
        self.name = adapter.device
        self.label = adapter.friendly or adapter.device
        if npcap_admin_only() and not is_elevated():
            # Opening would make Npcap ask for elevation with a UAC prompt (and block until
            # someone answers it); say what to do instead.
            raise OutputError(
                "Npcap is installed in 'Administrators only' mode, and the server is not running as administrator.",
                f"Reinstall Npcap ({NPCAP_URL}) with 'Restrict Npcap driver's access to Administrators only' "
                "unticked, then restart the server. Or run the server as administrator.",
                check="output.access", title="Npcap is restricted to administrators")
        self.lib = lib
        err = ctypes.create_string_buffer(512)
        self.p = lib.pcap_open_live(adapter.device.encode(), 2048, 0, 1, err)
        if not self.p:
            msg = err.value.decode(errors="replace").strip()
            raise open_failed_error(adapter.friendly or adapter.device, f"Npcap says {msg!r}")
        if lib.pcap_setnonblock(self.p, 1, err) != 0:
            pass                         # replies are then read with the 1 ms timeout instead
        prog = _bpf_program()
        if lib.pcap_compile(self.p, ctypes.byref(prog), REPLY_FILTER, 1, 0xFFFFFFFF) == 0:
            lib.pcap_setfilter(self.p, ctypes.byref(prog))
            lib.pcap_freecode(ctypes.byref(prog))
        self._hdr = ctypes.POINTER(_pcap_pkthdr)()
        self._data = ctypes.POINTER(ctypes.c_ubyte)()

    def _error(self) -> str:
        e = self.lib.pcap_geterr(self.p)
        return e.decode(errors="replace") if e else "unknown error"

    def prepare(self, buf: np.ndarray, spans: Sequence[tuple[int, int]]):
        spans = [(int(o), int(n)) for o, n in spans]
        pos = spans[0][0] - PCAP_HDR
        start = pos
        for off, n in spans:
            if off - PCAP_HDR != pos:
                raise ValueError("Npcap batches need packets back to back with header room")
            buf[pos:off] = np.frombuffer(struct.pack("<iiII", 0, 0, n, n), np.uint8)
            pos = off + n
        return _Batch(buf, start, pos)

    def transmit(self, batch: _Batch) -> None:
        sent = self.lib.pcap_sendqueue_transmit(self.p, ctypes.byref(batch.queue), 0)
        if sent < batch.bytes:
            raise OSError(f"Npcap sent {sent} of {batch.bytes} bytes: {self._error()}")

    def send(self, packet: bytes) -> None:
        b = bytes(packet)
        if self.lib.pcap_sendpacket(self.p, b, len(b)) != 0:
            raise OSError(f"Npcap send failed: {self._error()}")

    def recv(self) -> bytes | None:
        r = self.lib.pcap_next_ex(self.p, ctypes.byref(self._hdr), ctypes.byref(self._data))
        if r != 1:
            return None
        return ctypes.string_at(self._data, self._hdr.contents.caplen)

    def link(self):
        a = find_adapter(self.adapter.guid)
        if a is None:
            return False, None
        return a.up, a.mbps

    def close(self):
        if self.p:
            self.lib.pcap_close(self.p)
            self.p = None


def lib_version() -> str:
    return load_wpcap().pcap_lib_version().decode(errors="replace")
