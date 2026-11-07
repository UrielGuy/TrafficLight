"""The host's network port for the LED card (docs/FIRMWARE.md 6.1): is there an adapter on the sign's
network 10.75.0.0/16, with a netmask that reaches every card, a link, enough speed for the frames, and
does the traffic for the cards leave through it?

psutil reads the adapters (address, netmask, link up, speed) the same way on Windows and Linux. The
route is asked of the operating system itself: a UDP socket "connected" to a card address (nothing is
sent) reports the local address the system would send from.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional

from .hub75 import protocol as P

NETWORK = ipaddress.ip_network(f"{P.HOST_IP}/{P.NETMASK}", strict=False)        # 10.75.0.0/16
HOSTS = ipaddress.ip_network(f"{P.HOST_IP}/24", strict=False)                     # 6.1: hosts live here, cards never do
ROUTE_PROBES = (str(NETWORK.network_address + 0x0101), str(NETWORK.broadcast_address - 0x0101))  # both ends of the cards' range
CACHE_SECONDS = 2.0
# On the wire per datagram beyond its UDP payload: UDP 8 + IPv4 20 + Ethernet 14 + FCS 4 + preamble 8 + gap 12.
WIRE_OVERHEAD = 66


@dataclass
class Port:
    name: str
    address: str
    prefix: int                  # netmask length; 16 = 255.255.0.0
    up: Optional[bool]           # None: the system did not say
    speed_mbps: int              # 0: unknown

    @property
    def netmask(self) -> str:
        return str(ipaddress.ip_network(f"0.0.0.0/{self.prefix}").netmask)

    def reaches_all_cards(self) -> bool:
        return ipaddress.ip_network(f"{self.address}/{self.prefix}", strict=False).supernet_of(NETWORK)


def needed_mbps(frame_bytes: int, fps: float) -> float:
    """Link rate the frames take: chunks of MAX_CHUNK_DATA, each with its headers (the commit is noise)."""
    chunks = -(-frame_bytes // P.MAX_CHUNK_DATA)
    wire = frame_bytes + chunks * (P.MTU_PAYLOAD - P.MAX_CHUNK_DATA + WIRE_OVERHEAD)
    return wire * 8 * fps / 1e6


def scan(addrs: Optional[dict] = None, stats: Optional[dict] = None) -> list[Port]:
    """Adapters with an IPv4 address on the sign's network (or a wider one that contains it)."""
    if addrs is None or stats is None:
        import psutil
        addrs = psutil.net_if_addrs() if addrs is None else addrs
        stats = psutil.net_if_stats() if stats is None else stats
    out = []
    for name, entries in addrs.items():
        for a in entries:
            if a.family != socket.AF_INET or not a.netmask:
                continue
            try:
                ip = ipaddress.ip_address(a.address)
                prefix = ipaddress.ip_network(f"0.0.0.0/{a.netmask}").prefixlen
            except ValueError:
                continue
            if ip in NETWORK:
                st = stats.get(name)
                out.append(Port(name, a.address, prefix, st.isup if st else None, int(st.speed) if st else 0))
    return out


def route_source(dest: str) -> Optional[str]:
    """The local address the system sends to `dest` from (no packet leaves); None without any route."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((dest, P.PORT))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


class NetworkProbe:
    """network_status() for the driver's status, at most every CACHE_SECONDS (the UI asks several
    times a second; reading the adapters takes milliseconds on Windows)."""

    def __init__(self, scanner: Callable[[], list[Port]] = scan, router: Callable[[str], Optional[str]] = route_source,
                 clock: Callable[[], float] = time.monotonic):
        self._scan, self._route, self._clock = scanner, router, clock
        self._lock = threading.Lock()
        self._at = -1e9
        self._last: dict[str, Any] = {}

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            if now - self._at >= CACHE_SECONDS:
                self._last = self._probe()
                self._at = now
            return self._last

    def _probe(self) -> dict[str, Any]:
        try:
            ports = self._scan()
        except Exception as e:      # psutil missing or refused: say so rather than guess
            return {"ports": None, "error": f"{type(e).__name__}: {e}", "routes": {}}
        return {"ports": [asdict(p) | {"netmask": p.netmask, "reaches_all": p.reaches_all_cards()} for p in ports],
                "error": None, "routes": {d: self._route(d) for d in ROUTE_PROBES}}


def problem(net: dict[str, Any], need_mbps: Optional[float], fps: float) -> tuple[str, str, str, Optional[str]]:
    """(level, title, detail, fix) of the output.network check from NetworkProbe.status()."""
    fix_addr = ("Give the network port the LED card is plugged into the address 10.75.0.1 with netmask "
                "255.255.0.0 (no gateway). Windows, as administrator: netsh interface ip set address "
                "\"<adapter>\" static 10.75.0.1 255.255.0.0. On the Pi: see docs/HARDWARE.md section 4.")
    if net.get("ports") is None:
        return ("warning", "Cannot read the network adapters", net.get("error") or "unknown error",
                "Install the Python package psutil (requirements.txt).")
    ports = [Port(**{k: p[k] for k in ("name", "address", "prefix", "up", "speed_mbps")}) for p in net["ports"]]
    if not ports:
        return ("error", "No network port is set up for the LED card",
                "No adapter has an address in 10.75.0.0/16, the sign's network, so nothing reaches the card.", fix_addr)
    live = [p for p in ports if p.up is not False]
    if not live:
        names = ", ".join(f"{p.name} ({p.address})" for p in ports)
        return ("error", "The LED card's network port has no link",
                f"{names} is set up for the sign but its link is down.",
                "Check the cable between this computer and the card, and that the card has power.")
    port = next((p for p in live if p.reaches_all_cards()), live[0])
    where = f"{port.name}: {port.address}, netmask {port.netmask}"
    if not port.reaches_all_cards():
        return ("error", f"The netmask of {port.name} is too narrow",
                f"{where}. Cards take addresses anywhere in 10.75.1.1 .. 10.75.254.254, which needs netmask "
                f"255.255.0.0; with this one most cards cannot be reached and their announcements are missed.", fix_addr)
    notes: list[str] = []
    level, fix = "ok", None
    if ipaddress.ip_address(port.address) not in HOSTS:
        level, fix = "warning", fix_addr
        notes.append(f"{port.address} is in the range the cards pick their addresses from; a card could take "
                     f"the same one. Use 10.75.0.1 (hosts keep to 10.75.0.x).")
    if len(live) > 1:
        level, fix = "warning", "Unplug or reconfigure the other adapters on 10.75.0.0/16."
        notes.append("More than one adapter is on the sign's network: " + ", ".join(p.name for p in live) +
                     ". Frames may leave through the wrong one; keep only the one the card is plugged into.")
    routed = {d: src for d, src in (net.get("routes") or {}).items()}
    wrong = [d for d, src in routed.items() if src is not None and src != port.address]
    if wrong:
        level = "warning"
        fix = fix or "Remove the other adapter's address on the sign's network, or give it a narrower netmask."
        notes.append(f"Traffic for {', '.join(wrong)} would leave from {routed[wrong[0]]}, not {port.address}: "
                     f"another adapter's route wins.")
    speed = f"link {port.speed_mbps} Mbit/s" if port.speed_mbps else "link speed unknown"
    if need_mbps and port.speed_mbps:
        if port.speed_mbps < need_mbps * 1.25:
            level = "warning" if port.speed_mbps >= need_mbps and level != "error" else "error"
            fix = "Use a gigabit port and cable, or lower the frame rate in Settings."
            notes.append(f"{fps:g} fps takes about {need_mbps:.0f} Mbit/s on the wire, too close to or above "
                         f"this {port.speed_mbps} Mbit/s link.")
        else:
            speed += f" ({need_mbps:.0f} Mbit/s used at {fps:g} fps)"
    detail = f"{where}, {speed}." + ("" if not notes else " " + " ".join(notes))
    titles = {"ok": f"Network port {port.name} is set up for the sign",
              "warning": f"Network port {port.name} works, with a catch", "error": f"Network port {port.name} is too slow"}
    return (level, titles[level], detail, fix)
