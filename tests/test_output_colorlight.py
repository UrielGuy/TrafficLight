"""Colorlight packets byte for byte, and the driver against a fake raw socket."""
import struct
import threading
import time

import numpy as np
import pytest

from trafficlight import config
from trafficlight.model import OutputSettings
from trafficlight.output import colorlight as cl
from trafficlight.output.base import OutputError, create_driver
from trafficlight.output.mapping import Mapper
from trafficlight.output.rawsock_base import RawSocket


def hexdump(text: str) -> bytes:
    """Bytes from 'offset  b0 b1 ...' lines (Wireshark / Kubota style)."""
    out = bytearray()
    for line in text.strip().splitlines():
        out += bytes.fromhex("".join(line.split()[1:]))
    return bytes(out)


# Harald Kubota's capture of LEDVision's display frame at 75 % (hkubota.wordpress.com, 2022-01-31).
KUBOTA_DISPLAY = hexdump("""
0000 11 22 33 44 55 66 22 22  33 44 55 66 01 07 00 00
0010 00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
0020 00 00 00 bf 05 00 bf bf  bf 00 00 00 00 00 00 00
0030 00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
0040 00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
0050 00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
0060 00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
""")

# The first 0xc0 bytes of his card's detection reply (5A, firmware 10.16, 128x64); the rest is zero.
KUBOTA_REPLY = hexdump("""
0000   ff ff ff ff ff ff 11 22  33 44 55 66 08 05 04 0a
0010   10 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
0020   00 00 00 80 00 40 00 00  00 00 00 00 01 02 03 ff
0030   ff ff ff 00 00 0c a8 00  00 00 00 00 08 05 a0 00
0040   00 00 00 00 00 00 00 00  00 00 00 00 ba 00 00 00
0050   00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
0060   00 00 00 00 00 00 00 00  00 00 00 00 0f 04 08 00
0070   00 00 00 00 00 00 00 00  00 00 00 00 00 00 01 00
0080   01 00 01 00 00 28 00 ff  ff ff 00 00 00 00 00 00
0090   00 00 0c 03 00 00 00 00  00 00 00 00 00 00 00 00
00a0   00 00 00 00 00 00 00 00  01 00 00 00 00 00 04 00
00b0   00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
""").ljust(1070, b"\0")

MACS = bytes.fromhex("112233445566222233445566")


def reply(fw=(10, 16), size=(config.W, config.CARD_H), index=0) -> bytes:
    r = bytearray(KUBOTA_REPLY)
    r[15], r[16] = fw
    r[34:36] = size[0].to_bytes(2, "big")
    r[36:38] = size[1].to_bytes(2, "big")
    r[98] = index
    return bytes(r)


# ------------------------------------------------------------------ packets
def test_display_packet_matches_ledvision_capture():
    assert cl.display_packet(0xBF, (0xBF, 0xBF, 0xBF)) == KUBOTA_DISPLAY
    assert len(KUBOTA_DISPLAY) == cl.DISPLAY_LEN == 112


def test_display_packet_colour_temperature():
    p = cl.display_packet(0x1A, (0x1A, 0x0C, 0x01))          # Kubota: 2000 K at 10 %
    assert p[35] == 0x1A and p[38:41] == bytes([0x1A, 0x0C, 0x01]) and p[36] == 5 and p[13] == 7


def test_brightness_packet_layout():
    p = cl.brightness_packet((0x10, 0x20, 0x30))
    assert len(p) == 77
    assert p[:12] == MACS and p[12] == 0x0A and p[13:17] == bytes([0x10, 0x20, 0x30, 0xFF])
    assert not any(p[17:])


def test_row_header_layout_and_high_rows():
    h = cl.row_header(300, 0, 128)
    assert h == MACS + bytes([0x55, 0x01, 0x2C, 0x00, 0x00, 0x00, 0x80, 0x08, 0x88])
    assert cl.row_header(5, 497, 103)[13:19] == bytes([0, 5, 0x01, 0xF1, 0, 103])


def test_row_packets_split_at_497_pixels():
    px = bytes(range(256)) * 7 + bytes(600 * 3 - 256 * 7)
    pk = cl.row_packets(7, px)
    assert [len(p) for p in pk] == [21 + 497 * 3, 21 + 103 * 3]
    assert max(len(p) for p in pk) <= 1514
    assert pk[1][15:19] == bytes([0x01, 0xF1, 0, 103])
    assert pk[0][21:] + pk[1][21:] == px
    assert len(cl.row_packets(0, bytes(128 * 3))) == 1


def test_detect_packet_layout():
    p = cl.detect_packet(1)
    assert len(p) == 284 and p[:12] == MACS and p[12] == 0x07 and p[16] == 1
    assert not any(p[13:16]) and not any(p[17:])


def test_parse_reply_from_capture():
    r = cl.parse_reply(KUBOTA_REPLY)
    assert r is not None
    assert (r.firmware, r.firmware_major, r.width, r.height, r.index, r.card) == ("10.16", 10, 128, 64, 0, 4)
    assert r.packets == 0x0CA8 and r.uptime_s == pytest.approx(0x0805A0 / 1000)


def test_parse_reply_rejects_other_frames():
    assert cl.parse_reply(KUBOTA_DISPLAY) is None
    assert cl.parse_reply(KUBOTA_REPLY[:50]) is None
    wrong_src = bytearray(KUBOTA_REPLY)
    wrong_src[6] = 0x22
    assert cl.parse_reply(bytes(wrong_src)) is None


def test_brightness_levels():
    assert [cl.linear_level(p / 100) for p in (0, 1, 10, 25, 50, 75, 100)] == [0, 3, 26, 64, 128, 191, 255]
    # Kubota's table for the display packet: 1 % 0x03, 10 % 0x1a, 25 % 0x40, 50 % 0x80, 75 % 0xbf.
    assert cl.ledvision_level(0.5) == 0xC1 and cl.ledvision_level(0.01) == 0x28 and cl.ledvision_level(1) == 0xFF


def test_frame_encoder_packets():
    enc = cl.FrameEncoder(config.W, config.CARD_H)
    rng = np.random.default_rng(1)
    img = rng.integers(0, 256, (config.CARD_H, config.W, 3), dtype=np.uint8)
    enc.pixels[...] = img
    enc.set_levels(128, (128, 128, 128), (128, 128, 128))
    pk = enc.packets()
    assert len(pk) == 1 + config.CARD_H + 1
    assert pk[0] == cl.brightness_packet((128, 128, 128))
    for r in (0, 1, 255, 256, config.H, config.CARD_H - 1):
        assert pk[1 + r] == cl.row_header(r, 0, config.W) + img[r].tobytes()
    assert pk[config.CARD_H][13:15] == (config.CARD_H - 1).to_bytes(2, "big")
    assert pk[-1] == cl.display_packet(128)
    assert enc.wire_bytes() == 77 + 24 + config.CARD_H * (21 + config.W * 3 + 24)


def test_frame_encoder_with_header_room_and_repeat():
    enc = cl.FrameEncoder(config.W, config.CARD_H, room=16, sync_repeat=2)
    for off, n in [enc.brightness_span] + enc.row_spans + enc.sync_spans:
        assert off >= 16
    assert enc.row_spans[1][0] - enc.row_spans[0][0] == 16 + 21 + config.W * 3
    assert len(enc.packets()) == 1 + config.CARD_H + 2


def test_options_parse():
    o = cl.ColorlightOptions.parse("sync_gap_ms=5, sync_repeat=2,brightness_packet=off,white_balance=1/0.9/0.5,detect=0")
    assert (o.sync_gap_ms, o.sync_repeat, o.brightness_packet, o.white_balance, o.detect) == (5.0, 2, "off", (1.0, 0.9, 0.5), False)
    with pytest.raises(ValueError):
        cl.ColorlightOptions.parse("nope=1")
    with pytest.raises(ValueError):
        cl.ColorlightOptions.parse("brightness_mode=loud")


def test_npcap_send_queue_records():
    from trafficlight.output.rawsock_npcap import NpcapRawSocket
    enc = cl.FrameEncoder(config.W, config.CARD_H, room=16)
    sock = object.__new__(NpcapRawSocket)            # prepare() needs no Npcap
    batch = sock.prepare(enc.rows, [enc.brightness_span] + enc.row_spans)
    raw = enc.rows[:batch.bytes].tobytes()
    pos, lens = 0, []
    while pos < len(raw):                             # pcap record: ts_sec, ts_usec, caplen, len
        _, _, caplen, length = struct.unpack_from("<iiII", raw, pos)
        assert caplen == length
        lens.append(caplen)
        pos += 16 + caplen
    assert pos == len(raw) == batch.bytes
    assert lens == [77] + [21 + config.W * 3] * config.CARD_H


# ------------------------------------------------------------------ driver with a fake socket
class FakeSocket(RawSocket):
    def __init__(self, room=0, answer: bytes | None = None, link=(True, 1000), delay=0.0):
        self.header_room = room
        self.answer = answer
        self._link = link
        self.delay = delay
        self.batches: list[list[bytes]] = []
        self.sent: list[bytes] = []
        self.inbox: list[bytes] = []
        self.closed = False
        self.lock = threading.Lock()

    def prepare(self, buf, spans):
        return buf, list(spans)

    def transmit(self, batch):
        if self.delay:
            time.sleep(self.delay)
        buf, spans = batch
        with self.lock:
            self.batches.append([buf[o:o + n].tobytes() for o, n in spans])

    def send(self, packet):
        self.sent.append(bytes(packet))
        if packet[12] == cl.TYPE_DETECT and self.answer is not None and packet[16] == 0:
            self.inbox.append(self.answer)

    def recv(self):
        return self.inbox.pop(0) if self.inbox else None

    def link(self):
        return self._link

    def close(self):
        self.closed = True


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


def frames_of(sock: FakeSocket) -> list[tuple[list[bytes], list[bytes]]]:
    """(rows batch, sync batch) pairs."""
    with sock.lock:
        b = list(sock.batches)
    return [(b[i], b[i + 1]) for i in range(0, len(b) - 1, 2)]


def make_driver(sock, **settings):
    s = OutputSettings(driver="colorlight", interface="fake0", **settings)
    return cl.ColorlightDriver(s, options=cl.ColorlightOptions(sync_gap_ms=0.0), opener=lambda iface: sock)


def test_create_driver_picks_colorlight():
    assert isinstance(create_driver(OutputSettings(driver="colorlight")), cl.ColorlightDriver)


def test_driver_sends_mapped_frame():
    sock = FakeSocket(answer=reply())
    s = dict(panel_order=[5, 4, 3, 2, 1, 0], color_order="bgr", brightness=0.5)
    drv = make_driver(sock, **s)
    drv.open()
    try:
        frame = np.random.default_rng(2).integers(0, 256, (config.H, config.W, 3), dtype=np.uint8)
        drv.send(frame)
        assert wait_for(lambda: len(frames_of(sock)) >= 1)
        rows, sync = frames_of(sock)[0]
        want = Mapper(OutputSettings(**s)).apply(frame)
        assert want.shape == (config.CARD_H, config.W, 3)
        assert len(rows) == 1 + config.CARD_H and rows[0] == cl.brightness_packet((128, 128, 128))
        for r in range(config.CARD_H):
            assert rows[1 + r] == cl.row_header(r, 0, config.W) + want[r].tobytes()
        for r in range(config.H, config.CARD_H):            # connectors without a panel get black
            assert not any(rows[1 + r][cl.ROW_HEADER_LEN:])
        assert sync == [cl.display_packet(128)]
        assert wait_for(lambda: drv.status()["receiver_found"])
        st = drv.status()
        assert st["ok"] and st["receiver"]["firmware"] == "10.16" and st["frames"] >= 1
        assert st["packets_per_frame"] == config.CARD_H + 2
    finally:
        drv.close()
    assert sock.closed


def test_send_is_fast_and_stale_frames_are_dropped():
    sock = FakeSocket(answer=reply(), delay=0.02)          # a slow link: 20 ms per batch
    drv = make_driver(sock)
    drv.open()
    try:
        frames = [np.full((config.H, config.W, 3), i, np.uint8) for i in range(20)]
        times = []
        for f in frames:
            t = time.perf_counter()
            drv.send(f)
            times.append(time.perf_counter() - t)
            time.sleep(0.002)
        assert sorted(times)[len(times) // 2] < 0.003        # render thread: well under 3 ms
        assert wait_for(lambda: frames_of(sock) and frames_of(sock)[-1][0][1][21] == 19)
        st = drv.status()
        assert st["dropped"] > 0 and st["frames"] < 20
    finally:
        drv.close()


def test_brightness_change_resends_last_frame():
    sock = FakeSocket(answer=reply())
    drv = make_driver(sock, brightness=1.0)
    drv.open()
    try:
        drv.send(np.zeros((config.H, config.W, 3), np.uint8))
        assert wait_for(lambda: len(frames_of(sock)) >= 1)
        drv.set_brightness(0.25)
        assert wait_for(lambda: frames_of(sock)[-1][1] == [cl.display_packet(64)])
        assert frames_of(sock)[-1][0][0] == cl.brightness_packet((64, 64, 64))
    finally:
        drv.close()


def test_firmware_13_sends_display_twice():
    sock = FakeSocket(answer=reply(fw=(13, 5)))
    drv = make_driver(sock)
    drv.open()
    try:
        assert wait_for(lambda: drv.status()["receiver_found"])
        drv.send(np.zeros((config.H, config.W, 3), np.uint8))
        assert wait_for(lambda: frames_of(sock) and len(frames_of(sock)[-1][1]) == 2)
    finally:
        drv.close()


def test_old_firmware_uses_software_brightness():
    sock = FakeSocket(answer=reply(fw=(3, 6)))
    drv = make_driver(sock, brightness=0.5)
    drv.open()
    try:
        assert wait_for(lambda: drv.status()["receiver_found"])
        drv.send(np.full((config.H, config.W, 3), 200, np.uint8))
        assert wait_for(lambda: frames_of(sock) and frames_of(sock)[-1][1] == [cl.display_packet(255)])
        assert frames_of(sock)[-1][0][1][21] == 100
        assert drv.status()["software_brightness"]
    finally:
        drv.close()


def test_white_balance_and_no_brightness_packet():
    sock = FakeSocket()
    s = OutputSettings(driver="colorlight", interface="x", brightness=1.0)
    o = cl.ColorlightOptions(sync_gap_ms=0, white_balance=(1.0, 0.5, 0.25), brightness_packet="off", detect=False)
    drv = cl.ColorlightDriver(s, options=o, opener=lambda i: sock)
    drv.open()
    try:
        drv.send(np.zeros((config.H, config.W, 3), np.uint8))
        assert wait_for(lambda: len(frames_of(sock)) >= 1)
        rows, sync = frames_of(sock)[0]
        assert len(rows) == config.CARD_H and rows[0][12] == cl.TYPE_ROW
        assert sync == [cl.display_packet(255, (255, 128, 64))]
        assert sock.sent == [] and drv.status()["ok"]
    finally:
        drv.close()


def test_status_reports_missing_receiver_and_wrong_size():
    sock = FakeSocket(answer=None)
    drv = make_driver(sock)
    drv.open()
    try:
        assert wait_for(lambda: not drv.status()["ok"])
        assert "No receiver card answered" in drv.status()["error"]
        assert all(p[12] == cl.TYPE_DETECT for p in sock.sent)
        sock.inbox.append(reply(size=(128, 64)))
        assert wait_for(lambda: drv.status()["receiver_found"])
        st = drv.status()                                        # a card not set up: the sign cannot work
        assert not st["ok"] and "128x64" in st["error"] and f"{config.W}x{config.CARD_H}" in st["error"]
        assert [p["id"] for p in st["problems"]] == ["output.card_size"] and "LEDVision" in st["problems"][0]["fix"]
        sock.inbox.append(reply(size=(config.W, config.H)))      # the old six-port layout is wrong now
        assert wait_for(lambda: drv.status()["receiver"]["height"] == config.H)
        assert f"must be {config.W}x{config.CARD_H}" in drv.status()["error"]
        sock.answer = reply()                                    # set up in LEDVision meanwhile: the driver asks again
        assert wait_for(lambda: drv.status()["ok"], timeout=8.0) and drv.status()["problems"] == []
    finally:
        drv.close()


def test_link_down_is_an_error_and_frames_are_held_back():
    sock = FakeSocket(answer=reply(), link=(False, None))
    drv = make_driver(sock)
    drv.open()
    try:
        drv.send(np.zeros((config.H, config.W, 3), np.uint8))
        assert wait_for(lambda: drv.status()["dropped"] >= 1)
        st = drv.status()
        assert not st["ok"] and "No link" in st["error"] and sock.batches == []
    finally:
        drv.close()


def test_slow_link_is_an_error():
    sock = FakeSocket(answer=reply(), link=(True, 100))
    drv = make_driver(sock)
    drv.open()
    try:
        st = drv.status()
        assert not st["ok"] and "100 Mb/s" in st["error"] and "gigabit" in st["problems"][0]["fix"]
        assert st["problems"][0]["id"] == "output.link"
    finally:
        drv.close()


def test_open_error_is_reported():
    def opener(iface):
        raise OutputError("nope")
    drv = cl.ColorlightDriver(OutputSettings(driver="colorlight", interface="x"), opener=opener)
    with pytest.raises(OutputError):
        drv.open()
    st = drv.status()
    assert not st["ok"] and st["error"] == "nope"
    drv.send(np.zeros((config.H, config.W, 3), np.uint8))   # not open: ignored
    drv.close()


def test_bad_mapping_settings_fall_back_and_report():
    sock = FakeSocket(answer=reply())
    s = OutputSettings(driver="colorlight", interface="fake0")
    s.panel_order = [0, 0, 1, 2, 3, 4]                # assignment is not validated (the bring-up tool does it)
    drv = cl.ColorlightDriver(s, options=cl.ColorlightOptions(sync_gap_ms=0.0), opener=lambda iface: sock)
    assert "panel_order" in drv.status()["error"]
    drv.update_settings(OutputSettings(driver="colorlight", interface="fake0", panel_order=[1, 0, 2, 3, 4, 5]))
    assert drv.status()["error"] is None


def test_update_settings_remaps_without_reopen():
    sock = FakeSocket(answer=reply())
    drv = make_driver(sock)
    drv.open()
    try:
        frame = np.zeros((config.H, config.W, 3), np.uint8)
        frame[0, 0] = (255, 0, 0)
        drv.send(frame)
        assert wait_for(lambda: len(frames_of(sock)) >= 1)
        drv.update_settings(OutputSettings(driver="colorlight", interface="fake0", rotate180=True, brightness=0.5))
        # rotated: the frame's top-left pixel is the last pixel of the sixth band (row H - 1)
        assert wait_for(lambda: frames_of(sock)[-1][0][config.H][-3:] == bytes([255, 0, 0]))
        assert not sock.closed
    finally:
        drv.close()


def test_send_card_skips_the_mapping_but_not_colour_order():
    sock = FakeSocket(answer=reply())
    s = dict(panel_order=[7, 5, 3, 1, 0, 2], panel_flip=[True, False] * 3, rotate180=True, color_order="bgr",
             brightness=0.5)
    drv = make_driver(sock, **s)
    drv.open()
    try:
        card = np.random.default_rng(4).integers(0, 256, (config.CARD_H, config.W, 3), dtype=np.uint8)
        drv.send_card(card)
        assert wait_for(lambda: len(frames_of(sock)) >= 1)
        rows, sync = frames_of(sock)[0]
        assert len(rows) == 1 + config.CARD_H and sync == [cl.display_packet(128)]
        for r in range(config.CARD_H):
            assert rows[1 + r] == cl.row_header(r, 0, config.W) + card[r, :, ::-1].tobytes()
        # back to ordinary frames: the mapping applies again
        frame = np.random.default_rng(5).integers(0, 256, (config.H, config.W, 3), dtype=np.uint8)
        drv.send(frame)
        want = Mapper(OutputSettings(**s)).apply(frame)
        assert wait_for(lambda: len(frames_of(sock)) >= 2)
        assert frames_of(sock)[1][0][1:] == [cl.row_header(r, 0, config.W) + want[r].tobytes()
                                             for r in range(config.CARD_H)]
        with pytest.raises(ValueError):
            drv.send_card(card[:config.H])
        with pytest.raises(ValueError):
            drv.send(card)
    finally:
        drv.close()


def test_mixed_frames_and_card_images_drop_cleanly():
    sock = FakeSocket(answer=reply(), delay=0.01)
    drv = make_driver(sock)
    drv.open()
    try:
        for i in range(30):
            if i % 3 == 0:
                drv.send_card(np.full((config.CARD_H, config.W, 3), i, np.uint8))
            else:
                drv.send(np.full((config.H, config.W, 3), i, np.uint8))
            time.sleep(0.001)
        drv.send_card(np.full((config.CARD_H, config.W, 3), 200, np.uint8))
        last = lambda: frames_of(sock)[-1][0]
        assert wait_for(lambda: frames_of(sock) and last()[config.CARD_H][21] == 200 and last()[1][21] == 200)
        assert drv.status()["dropped"] > 0
    finally:
        drv.close()


def test_null_driver_ignores_card_images():
    from trafficlight.output.base import NullDriver
    NullDriver().send_card(np.zeros((config.CARD_H, config.W, 3), np.uint8))


def test_detect_helper_stops_at_first_silent_index():
    sock = FakeSocket(answer=reply())
    found = cl.detect(sock, timeout=0.05)
    assert [r.index for r in found] == [0]
    assert [p[16] for p in sock.sent] == [0, 1]
