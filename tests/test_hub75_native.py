"""The C transmit loop and packer (output/hub75/native.c): built here with the system's C compiler (skipped without
one, or off Linux), checked against what the Python loop sends, over localhost."""
import shutil
import socket
import subprocess
import sys
import time

import numpy as np
import pytest

from fpga_fake import FakeCard, wait_for
from trafficlight.output import fpga
from trafficlight.output.fpga import FpgaDriver, FpgaOptions
from trafficlight.output.hub75 import native
from trafficlight.output.hub75 import protocol as P
from trafficlight.output.network import NetworkProbe, Port
from trafficlight import config
from trafficlight.model import OutputSettings
from trafficlight.output.hub75.pack import Packer
from trafficlight.output.hub75.schedule import ScheduleParams, build

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux") or not shutil.which("cc"),
                                reason="needs Linux and a C compiler")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    path = tmp_path_factory.mktemp("native") / "_native.so"
    subprocess.run(["cc", "-O2", "-Wall", "-Werror", "-shared", "-fPIC", "-o", str(path), str(native.SOURCE)],
                   check=True)
    return path


def frame_rows(seed=1, frame_id=7):
    frame = np.random.default_rng(seed).integers(0, 256, 294912, np.uint8)
    return P.frame_rows(frame, frame_id, 0x1234, seq=5)


SO_TIMESTAMPNS = getattr(socket, "SO_TIMESTAMPNS", 35)      # Linux's value; Python does not export it


def receiver():
    r = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    r.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 22)
    r.setsockopt(socket.SOL_SOCKET, SO_TIMESTAMPNS, 1)
    r.bind(("127.0.0.1", 0))
    r.settimeout(2.0)
    # The kernel switches arrival stamping on from a work queue; datagrams before that are stamped
    # when read, which made a gap look negative.
    time.sleep(0.1)
    return r


def receive(r, n):
    """n datagrams with the kernel's arrival times (seconds)."""
    out = []
    for _ in range(n):
        data, anc, _, _ = r.recvmsg(2048, 64)
        sec, nsec = next(np.frombuffer(d, np.int64)[:2] for level, kind, d in anc if kind == SO_TIMESTAMPNS)
        out.append((data, sec + nsec * 1e-9))
    return out


def test_load_says_none_without_a_build(tmp_path):
    assert native.load(tmp_path / "missing.so") is None


def test_the_c_loop_sends_every_datagram_whole_in_order_with_the_gaps(built):
    c = native.load(built)
    assert c is not None
    rows, lengths = frame_rows()
    r, s = receiver(), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    try:
        kept = c.send_paced(s.fileno(), rows, lengths, r.getsockname(), 8, 250e-6)
        got = receive(r, len(lengths))
    finally:
        r.close()
        s.close()
    assert [d for d, _ in got] == [bytes(p) for p in P.frame_datagrams(rows, lengths)]
    t = [at for _, at in got]
    for i in range(8, len(t), 8):
        assert t[i] - t[i - 1] >= 250e-6 * 0.98, f"group at {i} came {1e6 * (t[i] - t[i - 1]):.0f} us after the last"
    assert kept is not None and kept >= 250e-6


def test_the_c_loop_keeps_the_gap_between_frames_too(built):
    """A frame's first group comes at least the gap after the last frame's last one, not straight after:
    the next frame is usually packed and waiting."""
    c = native.load(built)
    rows, lengths = frame_rows()
    r, s = receiver(), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    try:
        for _ in range(2):
            c.send_paced(s.fileno(), rows, lengths, r.getsockname(), 8, 250e-6)
        got = receive(r, 2 * len(lengths))
    finally:
        r.close()
        s.close()
    n = len(lengths)
    assert got[n][1] - got[n - 1][1] >= 250e-6 * 0.98


def test_the_c_loop_without_pacing_waits_for_nothing(built):
    c = native.load(built)
    rows, lengths = frame_rows()
    r, s = receiver(), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        assert c.send_paced(s.fileno(), rows, lengths, r.getsockname(), len(lengths), 0.0) is None
        assert len(receive(r, len(lengths))) == len(lengths)
    finally:
        r.close()
        s.close()


def test_a_failed_send_is_an_oserror(built):
    c = native.load(built)
    rows, lengths = frame_rows()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    fd = s.fileno()
    s.close()
    with pytest.raises(OSError):
        c.send_paced(fd, rows, lengths, ("127.0.0.1", 9), 8, 0.0)


def test_the_driver_drives_the_card_through_the_c_loop(built, monkeypatch):
    monkeypatch.setattr(native, "library_path", lambda: built)
    monkeypatch.setattr(fpga, "POLL_SECONDS", 0.05)
    card = FakeCard()
    network = NetworkProbe(lambda: [Port("eth-sign", "10.75.0.1", 16, True, 1000)], lambda dest: "10.75.0.1")
    d = FpgaDriver(OutputSettings(driver="fpga", card_address="127.0.0.1", card_port=card.port),
                   FpgaOptions(announce_port=0, broadcast="", dither=False), network=network)
    try:
        assert d._native is not None
        d.open()
        assert d._c_loop() is not None
        wait_for(lambda: d._ready, what="the schedule")
        for seed in range(3):
            d.send(np.random.default_rng(seed).integers(0, 256, (384, 128, 3), np.uint8))
            wait_for(lambda: d.status()["frames"] > seed, what=f"frame {seed}")
        wait_for(lambda: card.shown_frame is not None and card.counter("frames_incomplete") == 0)
        assert d.status()["min_gap_us"] >= 250
    finally:
        d.close()
        card.close()


@pytest.mark.parametrize("planes", [8, 10, 12])
@pytest.mark.parametrize("dither", [True, False])
def test_the_c_packer_packs_what_the_numpy_one_does_to_the_bit(built, planes, dither):
    sched = build(ScheduleParams(planes=planes, split=min(planes, 6)))
    ref = Packer(sched, 2.2, 0.8, dither)
    fast = Packer(sched, 2.2, 0.8, dither, native.load(built))
    rng = np.random.default_rng(planes)
    for i in range(6):
        image = rng.integers(0, 256, (config.CARD_H, config.W, 3), np.uint8)
        if i == 3:
            image = image[::-1]          # not contiguous
        assert np.array_equal(fast.pack(image), ref.pack(image)), f"frame {i}"
    assert np.array_equal(fast._err, ref._err)
