"""The output.network check: the host's port on the sign's network (output/network.py), from fake adapters."""
from __future__ import annotations

import socket
from types import SimpleNamespace as NS

from trafficlight.output import network as N
from trafficlight.output.network import NetworkProbe, Port


def status(*ports: Port, route: str | None = "10.75.0.1") -> dict:
    return NetworkProbe(lambda: list(ports), lambda dest: route).status()


def level_title(net: dict, need: float | None = 76.0, fps: float = 30) -> tuple[str, str, str]:
    level, title, detail, _fix = N.problem(net, need, fps)
    return level, title, detail


GOOD = Port("Ethernet 4", "10.75.0.1", 16, True, 1000)


def test_needed_rate_matches_the_spec():
    # 205 chunks of up to 1440 bytes for the 294,912-byte frame: about 76 Mbit/s at 30 fps, 151 at 60.
    assert round(N.needed_mbps(294_912, 30)) == 76 and round(N.needed_mbps(294_912, 60)) == 151


def test_scan_keeps_the_signs_network_only():
    addrs = {"Wi-Fi": [NS(family=socket.AF_INET, address="192.168.2.5", netmask="255.255.255.0")],
             "Ethernet 4": [NS(family=socket.AF_INET6, address="fe80::1", netmask=None),
                            NS(family=socket.AF_INET, address="10.75.0.1", netmask="255.255.0.0")],
             "USB": [NS(family=socket.AF_INET, address="10.75.0.9", netmask="255.255.255.0")]}
    stats = {"Ethernet 4": NS(isup=True, speed=1000), "USB": NS(isup=False, speed=0)}
    assert N.scan(addrs, stats) == [Port("Ethernet 4", "10.75.0.1", 16, True, 1000), Port("USB", "10.75.0.9", 24, False, 0)]


def test_all_good():
    level, title, detail = level_title(status(GOOD))
    assert level == "ok" and "Ethernet 4" in title
    assert "10.75.0.1, netmask 255.255.0.0" in detail and "1000 Mbit/s" in detail and "76 Mbit/s used at 30 fps" in detail


def test_no_port_on_the_signs_network():
    level, title, _ = level_title(status())
    assert level == "error" and "No network port" in title
    assert "255.255.0.0" in N.problem(status(), 76, 30)[3]


def test_netmask_24_is_too_narrow():
    level, title, detail = level_title(status(Port("Ethernet 4", "10.75.0.1", 24, True, 1000)))
    assert level == "error" and "netmask" in title and "255.255.255.0" in detail


def test_wider_netmask_is_fine():
    assert level_title(status(Port("eth0", "10.75.0.1", 8, True, 1000), route="10.75.0.1"))[0] == "ok"


def test_link_down():
    level, title, _ = level_title(status(Port("Ethernet 4", "10.75.0.1", 16, False, 0)))
    assert level == "error" and "no link" in title


def test_host_address_in_the_cards_range():
    level, _, detail = level_title(status(Port("eth0", "10.75.3.7", 16, True, 1000), route="10.75.3.7"))
    assert level == "warning" and "range the cards pick" in detail


def test_two_ports_on_the_signs_network():
    net = status(GOOD, Port("USB", "10.75.0.2", 16, True, 1000))
    level, _, detail = level_title(net)
    assert level == "warning" and "More than one adapter" in detail


def test_route_through_another_adapter():
    level, _, detail = level_title(status(GOOD, route="192.168.2.5"))
    assert level == "warning" and "192.168.2.5" in detail


def test_slow_link():
    hundred = status(Port("eth0", "10.75.0.1", 16, True, 100))
    assert level_title(hundred, need=151, fps=60)[0] == "error"
    assert level_title(hundred, need=88, fps=35)[0] == "warning"      # fits, without headroom
    assert level_title(hundred, need=50, fps=20)[0] == "ok"


def test_unknown_speed_and_unreadable_adapters():
    assert level_title(status(Port("eth0", "10.75.0.1", 16, None, 0)))[0] == "ok"
    broken = NetworkProbe(lambda: (_ for _ in ()).throw(ImportError("no psutil")), lambda d: None).status()
    level, title, _ = level_title(broken)
    assert level == "warning" and "Cannot read" in title


def test_probe_caches():
    calls = []
    t = [0.0]
    p = NetworkProbe(lambda: calls.append(1) or [GOOD], lambda d: "10.75.0.1", clock=lambda: t[0])
    p.status(), p.status()
    t[0] += N.CACHE_SECONDS
    p.status()
    assert len(calls) == 2
