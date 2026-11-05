"""Platform raw senders: what can be checked without privileges or hardware."""
import socket
import sys

import numpy as np
import pytest

from trafficlight.output import colorlight as cl
from trafficlight.output.base import OutputError

linux = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only")
windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def can_open_raw() -> bool:
    try:
        socket.socket(socket.AF_PACKET, socket.SOCK_RAW, 0).close()
        return True
    except (PermissionError, AttributeError, OSError):
        return False


@linux
def test_linux_unknown_interface():
    from trafficlight.output.rawsock_linux import LinuxRawSocket
    with pytest.raises(OutputError, match="No network interface named"):
        LinuxRawSocket("no-such-if0")


@linux
def test_linux_permission_message():
    if can_open_raw():
        pytest.skip("this process may open raw sockets")
    from trafficlight.output.rawsock_linux import LinuxRawSocket
    with pytest.raises(OutputError, match="CAP_NET_RAW") as e:
        LinuxRawSocket(socket.if_nameindex()[0][1])
    assert "setcap" in str(e.value)


@linux
def test_linux_sendmmsg_batch_layout():
    """The ctypes mmsghdr/iovec arrays, exercised on a datagram socketpair."""
    from trafficlight.output import rawsock_linux as rl
    if rl._sendmmsg is None:
        pytest.skip("no sendmmsg in libc")
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    b.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    try:
        sock = object.__new__(rl.LinuxRawSocket)
        sock.sock, sock.fd, sock.name = a, a.fileno(), "pair"
        enc = cl.FrameEncoder(128, 40)
        enc.pixels[...] = np.random.default_rng(3).integers(0, 256, enc.pixels.shape, dtype=np.uint8)
        spans = [enc.brightness_span] + enc.row_spans
        sock.transmit(sock.prepare(enc.rows, spans))
        got = [b.recv(4096) for _ in spans]
        assert got == enc.packets()[:-1]
    finally:
        a.close()
        b.close()


@linux
def test_linux_list_interfaces():
    from trafficlight.output.rawsock_linux import list_interfaces
    ifs = list_interfaces()
    assert all(i.id != "lo" and i.id == i.name for i in ifs)
    assert all(i.description for i in ifs)


@windows
def test_windows_list_interfaces_and_lookup():
    from trafficlight.output import rawsock_npcap as n
    ifs = n.list_interfaces()
    assert all(i.id.startswith(n.NPF_PREFIX) for i in ifs)
    for i in ifs:
        a = n.find_adapter(i.name)
        assert a is not None and a.device == i.id
        assert n.find_adapter(i.id).friendly == i.name


@windows
def test_windows_unknown_adapter():
    from trafficlight.output import rawsock_npcap as n
    try:
        n.load_wpcap()
    except OutputError as e:
        assert "Npcap is not installed" in str(e)
        return
    with pytest.raises(OutputError, match="not found"):
        n.NpcapRawSocket("\\Device\\NPF_{00000000-0000-0000-0000-000000000000}")
