"""Colorlight 5A-75B (stock firmware) output: raw Ethernet frames.

Protocol (reverse engineered by others; sources and the open questions are in
docs/HARDWARE.md, "Protocol reference"). Byte offsets are from the start of
the Ethernet frame. Every frame goes to 11:22:33:44:55:66 from
22:22:33:44:55:66; byte 12 (where the EtherType would be) is the packet type
and byte 13 is already payload.

* 0x55 row data, 21-byte header + pixels: [13..14] row, [15..16] first
  pixel, [17..18] pixel count (all big-endian), [19] 0x08, [20] 0x88, then 3
  bytes per pixel. At most 497 pixels per packet.
* 0x0A brightness, 77 bytes: [13..15] R, G, B level, [16] 0xFF.
* 0x01 display ("latch the rows received so far"), 112 bytes: [13] 0x07,
  [35] brightness, [36] 0x05, [38..40] R, G, B level (colour temperature).
* 0x07 detect, 284 bytes, [16] receiver index; the card answers with
  EtherType 0x0805, 1070 bytes (see parse_reply).

A frame is: brightness packet, one row packet per row of the card's image
(CARD_H rows: every connector's band, also those without a panel; 128
pixels: one packet each), a pause until the rows are on the wire plus a gap, then the
display packet (twice for firmware 13 and later, as FPP does).

send() (and send_card()) only copies the picture into a slot; a sender
thread maps it, builds the packets in place and sends the newest one,
dropping any it could not get to.

A picture equal to the one the card shows is not sent again: each display
packet swaps the card's two picture buffers, and the swap visibly disturbs
its LED refresh (firmware 13.39 on the 5A-75B), so a still picture re-sent
60 times a second flickers while the same picture sent once is steady.
The card keeps showing the last picture without traffic. It is sent again
after anything that may have lost it (brightness or mapping change, link
back up, send failure, the card restarted: its uptime goes back).
"""
from __future__ import annotations

import errno
import os
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Callable, Literal

import numpy as np

from .. import config
from ..model import OutputSettings
from .base import NetInterface, OutputDriver, OutputError, problem
from .mapping import Mapper, gamma_lut, validate
from .rawsock_base import RawSocket, list_raw_interfaces, open_raw

DST_MAC = bytes.fromhex("112233445566")
SRC_MAC = bytes.fromhex("222233445566")
TYPE_OFFSET = 12
CHECK_SECONDS = 2.0      # how often a card that answered is asked again (notices a restart)

TYPE_DISPLAY, DISPLAY_LEN = 0x01, 112
TYPE_BRIGHTNESS, BRIGHTNESS_LEN = 0x0A, 77
TYPE_ROW, ROW_HEADER_LEN = 0x55, 21
TYPE_DETECT, DETECT_LEN = 0x07, 284
TYPE_REPLY, REPLY_SUBTYPE = 0x08, 0x05
REPLY_MIN_LEN = 100            # the card sends 1070; the fields we read end at byte 98
MAX_ROW_PIXELS = 497           # 21 + 497 * 3 = 1512 <= 1514, the largest standard frame
WIRE_OVERHEAD = 24             # preamble 8 + FCS 4 + inter-frame gap 12 bytes per packet

# LEDVision's 0x0A levels for 1..100 % (Harald Kubota's capture): not linear.
LEDVISION_LEVELS = [(0, 0x00), (1, 0x28), (2, 0x35), (5, 0x4D), (25, 0x92), (50, 0xC1), (75, 0xE3), (100, 0xFF)]


# ------------------------------------------------------------------ packets
def _packet(ptype: int, length: int) -> bytearray:
    p = bytearray(length)
    p[0:6] = DST_MAC
    p[6:12] = SRC_MAC
    p[TYPE_OFFSET] = ptype
    return p


def display_packet(brightness: int, rgb: tuple[int, int, int] | None = None) -> bytes:
    """rgb: per-channel levels (brightness x colour temperature); default all = brightness."""
    p = _packet(TYPE_DISPLAY, DISPLAY_LEN)
    p[13] = 0x07                 # "sent from PC / network card" (0x00 when from an S2 sender box)
    p[35] = brightness
    p[36] = 0x05
    p[38:41] = bytes(rgb if rgb is not None else (brightness,) * 3)
    return bytes(p)


def brightness_packet(rgb: tuple[int, int, int]) -> bytes:
    p = _packet(TYPE_BRIGHTNESS, BRIGHTNESS_LEN)
    p[13:16] = bytes(rgb)
    p[16] = 0xFF
    return bytes(p)


def row_header(row: int, offset: int, count: int) -> bytes:
    p = _packet(TYPE_ROW, ROW_HEADER_LEN)
    p[13:15] = row.to_bytes(2, "big")
    p[15:17] = offset.to_bytes(2, "big")
    p[17:19] = count.to_bytes(2, "big")
    p[19] = 0x08
    p[20] = 0x88
    return bytes(p)


def row_packets(row: int, pixels: bytes) -> list[bytes]:
    """All packets for one row of pixels (3 bytes each, wire order), split at MAX_ROW_PIXELS."""
    n = len(pixels) // 3
    out = []
    for off in range(0, n, MAX_ROW_PIXELS):
        cnt = min(MAX_ROW_PIXELS, n - off)
        out.append(row_header(row, off, cnt) + bytes(pixels[off * 3:(off + cnt) * 3]))
    return out


def detect_packet(index: int = 0) -> bytes:
    p = _packet(TYPE_DETECT, DETECT_LEN)
    p[16] = index
    return bytes(p)


def linear_level(b: float) -> int:
    return int(round(255 * min(max(b, 0.0), 1.0)))


def ledvision_level(b: float) -> int:
    pct, lv = zip(*LEDVISION_LEVELS)
    return int(round(float(np.interp(100 * min(max(b, 0.0), 1.0), pct, lv))))


@dataclass
class ReceiverInfo:
    index: int
    card: int                  # 0x04 on the 5A cards seen so far
    firmware: str              # "major.minor"
    firmware_major: int
    width: int                 # the image size configured in the card (LEDVision)
    height: int
    packets: int               # counter in the card; meaning not fully known
    uptime_s: float

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def parse_reply(frame: bytes) -> ReceiverInfo | None:
    """A detection reply (EtherType 0x0805 from 11:22:33:44:55:66), else None."""
    if len(frame) < REPLY_MIN_LEN or frame[TYPE_OFFSET] != TYPE_REPLY or frame[13] != REPLY_SUBTYPE:
        return None
    if frame[6:12] != DST_MAC:
        return None
    u16 = lambda i: (frame[i] << 8) | frame[i + 1]
    u32 = lambda i: int.from_bytes(frame[i:i + 4], "big")
    return ReceiverInfo(index=frame[98], card=frame[14], firmware=f"{frame[15]}.{frame[16]}",
                        firmware_major=frame[15], width=u16(34), height=u16(36),
                        packets=u32(51), uptime_s=u32(59) / 1000.0)


# ------------------------------------------------------------------ frame buffer
class FrameEncoder:
    """One frame's packets, back to back in one buffer, headers written once.

    rows buffer: [room][brightness][room][row 0]...[room][row H-1]
    sync buffer: [room][display] (x repeat)
    `pixels` is a (H, W, 3) view of the rows' pixel bytes.
    """

    def __init__(self, width: int, height: int, room: int = 0, sync_repeat: int = 1):
        if width > MAX_ROW_PIXELS:
            raise ValueError(f"rows wider than {MAX_ROW_PIXELS} pixels need splitting; the sign is {width}")
        self.width, self.height, self.room = width, height, room
        row_len = ROW_HEADER_LEN + width * 3
        stride = room + row_len
        first_row = 2 * room + BRIGHTNESS_LEN
        self.rows = np.zeros(first_row + height * stride, np.uint8)
        self.brightness_span = (room, BRIGHTNESS_LEN)
        self.row_spans = [(first_row + r * stride, row_len) for r in range(height)]
        for r, (off, _) in enumerate(self.row_spans):
            self.rows[off:off + ROW_HEADER_LEN] = np.frombuffer(row_header(r, 0, width), np.uint8)
        start = first_row + ROW_HEADER_LEN
        self.pixels = np.lib.stride_tricks.as_strided(
            self.rows[start:], shape=(height, width, 3), strides=(stride, 3, 1), writeable=True)
        self.sync_repeat = sync_repeat
        self.sync = np.zeros(sync_repeat * (room + DISPLAY_LEN), np.uint8)
        self.sync_spans = [(k * (room + DISPLAY_LEN) + room, DISPLAY_LEN) for k in range(sync_repeat)]
        self.set_levels(255, (255, 255, 255), (255, 255, 255))

    def set_levels(self, brightness: int, display_rgb: tuple[int, int, int], brightness_rgb: tuple[int, int, int]):
        off, n = self.brightness_span
        self.rows[off:off + n] = np.frombuffer(brightness_packet(brightness_rgb), np.uint8)
        d = np.frombuffer(display_packet(brightness, display_rgb), np.uint8)
        for off, n in self.sync_spans:
            self.sync[off:off + n] = d

    def packets(self, with_brightness: bool = True) -> list[bytes]:
        """Everything one frame sends, in order (for tests and captures)."""
        spans = ([self.brightness_span] if with_brightness else []) + self.row_spans
        return [self.rows[o:o + n].tobytes() for o, n in spans] + [self.sync[o:o + n].tobytes() for o, n in self.sync_spans]

    def wire_bytes(self, with_brightness: bool = True) -> int:
        spans = ([self.brightness_span] if with_brightness else []) + self.row_spans
        return sum(n + WIRE_OVERHEAD for _, n in spans)


# ------------------------------------------------------------------ options
@dataclass
class ColorlightOptions:
    """Protocol knobs the sources disagree on. Not part of Settings: set them with the
    TRAFFICLIGHT_COLORLIGHT environment variable, e.g. "sync_gap_ms=5,sync_repeat=2"."""
    sync_gap_ms: float = 1.0          # pause after the rows have left before the display packet
    sync_repeat: int = 0              # display packets per frame; 0 = 2 on firmware >= 13 else 1 (FPP)
    brightness_packet: Literal["linear", "ledvision", "off"] = "linear"
    brightness_mode: Literal["auto", "card", "software"] = "auto"
    white_balance: tuple[float, float, float] = (1.0, 1.0, 1.0)
    detect: bool = True
    skip_unchanged: bool = True       # do not re-send a picture the card already shows (see the module doc)
    assumed_mbps: int = 1000          # link speed for timing when the OS does not report one

    @classmethod
    def parse(cls, text: str | None) -> "ColorlightOptions":
        o = cls()
        if not text:
            return o
        names = {f.name for f in fields(cls)}
        kw: dict[str, Any] = {}
        for part in text.split(","):
            if not part.strip():
                continue
            k, _, v = part.partition("=")
            k, v = k.strip(), v.strip()
            if k not in names:
                raise ValueError(f"unknown Colorlight option {k!r}")
            cur = getattr(o, k)
            if isinstance(cur, bool):
                kw[k] = v.lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, int):
                kw[k] = int(v)
            elif isinstance(cur, float):
                kw[k] = float(v)
            elif isinstance(cur, tuple):
                kw[k] = tuple(float(x) for x in v.split("/"))
            else:
                kw[k] = v
        o = replace(o, **kw)
        if o.brightness_packet not in ("linear", "ledvision", "off"):
            raise ValueError(f"brightness_packet must be linear, ledvision or off, not {o.brightness_packet!r}")
        if o.brightness_mode not in ("auto", "card", "software"):
            raise ValueError(f"brightness_mode must be auto, card or software, not {o.brightness_mode!r}")
        return o

    @classmethod
    def from_env(cls) -> "ColorlightOptions":
        return cls.parse(os.environ.get("TRAFFICLIGHT_COLORLIGHT"))


# ------------------------------------------------------------------ driver
@dataclass
class _Stats:
    frames: int = 0
    dropped: int = 0
    held: int = 0                     # unchanged pictures not sent again
    send_ms: float = 0.0
    times: deque = field(default_factory=lambda: deque(maxlen=120))


class ColorlightDriver(OutputDriver):
    def __init__(self, settings: OutputSettings, options: ColorlightOptions | None = None,
                 opener: Callable[[str], RawSocket] = open_raw):
        self.settings = settings.model_copy(deep=True)
        self.options = options or ColorlightOptions.from_env()
        self._opener = opener
        self._brightness = min(max(settings.brightness, 0.0), 1.0)
        self._config_problem: dict[str, Any] | None = None
        self._mapper = self._make_mapper(self.settings)
        self._sock: RawSocket | None = None
        self._open_problem: dict[str, Any] | None = None
        self._send_problem: dict[str, Any] | None = None
        self._link: tuple[bool | None, int | None] = (None, None)
        self._receiver: ReceiverInfo | None = None
        self._detect_tries = 0
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # A picture is (is_card, buffer): a sign frame for the mapper, or a ready card image.
        self._pending: tuple[bool, np.ndarray] | None = None
        self._last: tuple[bool, np.ndarray] | None = None
        self._spare: dict[bool, np.ndarray | None] = {False: np.empty((config.H, config.W, 3), np.uint8), True: None}
        self._resend = False
        self._levels_dirty = True
        self._card_current = False      # the card shows self._last as last sent
        self._holding = False           # the newest picture was unchanged, so not sent
        self._stats = _Stats()

    # ---- contract
    def open(self) -> None:
        if self._thread is not None:
            return
        try:
            self._sock = self._opener(self.settings.interface or "")
        except OutputError as e:
            self._open_problem = e.as_problem()
            raise
        self._open_problem = None
        self._link = self._sock.link()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="colorlight-sender", daemon=True)
        self._thread.start()

    def send(self, frame: np.ndarray) -> None:
        self._offer(frame, False)

    def send_card(self, image: np.ndarray) -> None:
        self._offer(image, True)

    def set_brightness(self, value: float) -> None:
        self._brightness = min(max(float(value), 0.0), 1.0)
        self.settings.brightness = self._brightness
        self._levels_dirty = True
        self._resend = True
        self._wake.set()

    def close(self) -> None:
        t = self._thread
        if t is not None:
            self._stop.set()
            self._wake.set()
            t.join(timeout=2.0)
            self._thread = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def problems(self) -> list[dict[str, Any]]:
        """What stands between this driver and a working sign, most basic first (output/base.problem)."""
        up, mbps = self._link
        rec = self._receiver
        out = [p for p in (self._open_problem, self._config_problem) if p is not None]
        if self._thread is not None and up is False:
            out.append(problem("output.link", "No link on the network port",
                               "No link on the Ethernet port the card is on.",
                               "Check the cable between the computer and the card's port A or B, and that the card is "
                               "powered (its red LED is on)."))
        if up and mbps is not None and mbps < 1000:
            out.append(problem("output.link", "The link is not gigabit",
                               f"The link runs at {mbps} Mb/s; the card only works at 1000 Mb/s.",
                               "Use a gigabit port and a Cat5e or Cat6 cable plugged straight into the card, "
                               "not through a 100 Mb/s switch."))
        if self._send_problem is not None:
            out.append(self._send_problem)
        if (self._thread is not None and up is not False and self.options.detect and rec is None
                and self._detect_tries >= 2):
            out.append(problem("output.receiver", "No receiver card answered",
                               "No receiver card answered the detection packets on this port.",
                               "Check the cable, the card's power (red LED) and that the link is gigabit; check that "
                               "the chosen interface is the port the card is plugged into."))
        if rec and (rec.width, rec.height) != (config.W, config.CARD_H):
            out.append(problem("output.card_size", "The receiver card is not set up for this sign",
                               f"The card is configured for {rec.width}x{rec.height}; it must be {config.W}x{config.CARD_H} "
                               f"({config.PORTS} modules of {config.PANEL_W}x{config.PANEL_H}, J1..J{config.PORTS} "
                               "from the top).",
                               "Run the receiver setup in LEDVision once (docs/HARDWARE.md, section 4). The server "
                               "asks the card again every few seconds, so this clears by itself afterwards."))
        return out

    def status(self) -> dict[str, Any]:
        up, mbps = self._link
        rec = self._receiver
        problems = self.problems()
        text = lambda p: f"{p['detail']} {p['fix']}" if p["fix"] else p["detail"]
        error = next((text(p) for p in problems if p["level"] == "error"), None)
        warnings = [text(p) for p in problems if p["level"] == "warning"]
        s = self._stats
        times = list(s.times)
        fps = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 and times[-1] > times[0] else 0.0
        if times and time.monotonic() - times[-1] > 1.0:
            fps = 0.0
        return {
            "driver": "colorlight", "ok": error is None, "error": error,
            "interface": self.settings.interface, "interface_name": self._sock.label if self._sock else None,
            "open": self._thread is not None, "link": up, "link_mbps": mbps, "warnings": warnings,
            "problems": problems, "detecting": self.options.detect,
            "receiver": rec.to_json() if rec else None,
            "receiver_found": rec is not None,
            "sent_fps": round(fps, 1), "frames": s.frames, "dropped": s.dropped,
            "held": s.held, "holding": self._holding,
            "send_ms": round(s.send_ms, 2), "packets_per_frame": self._packets_per_frame(),
            "brightness": self._brightness,
            "software_brightness": self._software_brightness(),
        }

    # ---- beyond the contract
    def update_settings(self, settings: OutputSettings) -> None:
        """Apply changed OutputSettings without reopening unless the interface changed."""
        reopen = settings.interface != self.settings.interface and self._thread is not None
        self.settings = settings.model_copy(deep=True)
        self._mapper = self._make_mapper(self.settings)
        self.set_brightness(settings.brightness)
        if reopen:
            self.close()
            self.open()

    # ---- internals
    def _offer(self, picture: np.ndarray, card: bool) -> None:
        if self._thread is None:
            return
        shape = (config.CARD_H if card else config.H, config.W, 3)
        if picture.shape != shape:
            raise ValueError(f"{'card image' if card else 'frame'} must be {shape}, got {picture.shape}")
        with self._lock:
            pending = self._pending
            if pending is not None:
                self._stats.dropped += 1
            if pending is not None and pending[0] == card:
                slot = pending[1]
            else:
                if pending is not None:
                    self._spare[pending[0]] = pending[1]
                slot = self._spare[card]
                self._spare[card] = None
                if slot is None:
                    slot = np.empty(shape, np.uint8)
            np.copyto(slot, picture, casting="unsafe")
            self._pending = (card, slot)
        self._wake.set()

    def _lost_picture(self) -> None:
        """The card may not show the last picture any more: send it again."""
        self._card_current = False
        with self._lock:
            self._resend = self._last is not None
        self._wake.set()

    def _make_mapper(self, s: OutputSettings) -> Mapper:
        try:
            validate(s)
            self._config_problem = None
            return Mapper(s)
        except ValueError as e:
            self._config_problem = problem("output.settings", "Output settings ignored", f"Output settings ignored: {e}",
                                           "Run Settings → Panel wiring again, or fix the values in data/settings.json.")
            return Mapper(OutputSettings(gamma=s.gamma if s.gamma > 0 else 1.0))

    def _packets_per_frame(self) -> int:
        enc = getattr(self, "_enc", None)
        if enc is None:
            return 0
        return len(enc.row_spans) + (self.options.brightness_packet != "off") + enc.sync_repeat

    def _software_brightness(self) -> bool:
        m = self.options.brightness_mode
        if m == "auto":
            # FPP: brightness "doesn't seem to work on v2.x and v3.x" firmware.
            return self._receiver is not None and self._receiver.firmware_major < 4
        return m == "software"

    def _sync_repeat(self) -> int:
        if self.options.sync_repeat > 0:
            return self.options.sync_repeat
        return 2 if self._receiver is not None and self._receiver.firmware_major >= 13 else 1

    def _apply_levels(self, enc: FrameEncoder) -> np.ndarray | None:
        """Write brightness into the packets; returns the software LUT (or None)."""
        wb = [min(max(c, 0.0), 1.0) for c in self.options.white_balance]
        b = self._brightness
        soft = self._software_brightness()
        card_b = 1.0 if soft else b
        level = linear_level(card_b)
        display_rgb = tuple(linear_level(card_b * c) for c in wb)
        if self.options.brightness_packet == "ledvision":
            bright_rgb = tuple(ledvision_level(card_b * c) for c in wb)
        else:
            bright_rgb = display_rgb
        enc.set_levels(level, display_rgb, bright_rgb)
        return gamma_lut(1.0, b) if soft else None

    def _build(self, sock: RawSocket):
        enc = FrameEncoder(config.W, config.CARD_H, sock.header_room, self._sync_repeat())
        spans = ([enc.brightness_span] if self.options.brightness_packet != "off" else []) + enc.row_spans
        self._enc = enc
        self._rows_batch = sock.prepare(enc.rows, spans)
        self._sync_batch = sock.prepare(enc.sync, enc.sync_spans)
        self._wire_bytes = enc.wire_bytes(self.options.brightness_packet != "off")
        self._levels_dirty = True

    def _run(self) -> None:
        sock = self._sock
        assert sock is not None
        self._build(sock)
        soft_lut = None
        next_link = 0.0
        next_detect = 0.0
        next_reopen = 0.0
        while not self._stop.is_set():
            woke = self._wake.wait(0.05)
            now = time.monotonic()
            if self._sock is None:
                if now >= next_reopen:
                    next_reopen = now + 2.0
                    try:
                        self._sock = sock = self._opener(self.settings.interface or "")
                        self._build(sock)
                        self._send_problem = None
                    except OutputError as e:
                        self._send_problem = e.as_problem()
                continue
            if now >= next_link:
                next_link = now + 1.0
                was_up = self._link[0]
                self._link = sock.link()
                if self._link[0] and was_up is False:
                    self._receiver, self._detect_tries, next_detect = None, 0, now
                    self._lost_picture()
            self._poll_replies(sock)
            # Ask until a card answers, and keep asking while it reports the wrong size: its setup in
            # LEDVision can be fixed while the server runs.
            rec = self._receiver
            # Once it answers right, keep asking now and then: a restarted card (uptime back) needs the picture
            # again, which an unchanged picture would otherwise never bring.
            if self.options.detect and now >= next_detect and self._link[0] is not False:
                try:
                    sock.send(detect_packet(0))
                except OSError:
                    pass
                self._detect_tries += 1
                settled = rec is not None and (rec.width, rec.height) == (config.W, config.CARD_H)
                next_detect = now + (CHECK_SECONDS if settled else 0.5 if self._detect_tries < 3 else 3.0)
            if self._enc.sync_repeat != self._sync_repeat():
                self._build(sock)
            if not woke or self._stop.is_set():
                continue
            self._wake.clear()
            with self._lock:
                picture = self._pending
                same = False
                if picture is not None:
                    self._pending = None
                    last = self._last
                    if last is not None:
                        same = last[0] == picture[0] and np.array_equal(last[1], picture[1])
                        self._spare[last[0]] = last[1]
                    self._last = picture
                elif self._resend:
                    picture = self._last
                    self._card_current = False
                self._resend = False
            if picture is None:
                continue
            if (same and self.options.skip_unchanged and self._card_current and not self._levels_dirty):
                self._stats.held += 1
                self._holding = True
                continue
            self._holding = False
            if self._link[0] is False:
                self._stats.dropped += 1
                self._card_current = False
                continue
            if self._levels_dirty:
                self._levels_dirty = False
                soft_lut = self._apply_levels(self._enc)
            try:
                self._send_frame(sock, picture, soft_lut)
                self._send_problem = None
                self._card_current = True
            except (OSError, OutputError) as e:
                self._stats.dropped += 1
                self._card_current = False
                self._send_problem = problem("output.send", "Sending to the card failed", f"Sending failed: {e}",
                                             "The driver keeps retrying. If it goes on, check the adapter (a USB "
                                             "adapter may have been unplugged) and its cable.")
                if (sock.link()[0] is False or isinstance(e, OutputError)
                        or getattr(e, "errno", None) in (errno.ENOBUFS, errno.EAGAIN)):
                    continue
                # The adapter may have gone away (USB): reopen it.
                sock.close()
                self._sock = None
                next_reopen = now + 1.0

    def _send_frame(self, sock: RawSocket, picture: tuple[bool, np.ndarray], soft_lut: np.ndarray | None) -> None:
        t0 = time.perf_counter()
        card, data = picture
        img = self._mapper.apply_card(data) if card else self._mapper.apply(data)
        if soft_lut is not None:
            np.take(soft_lut, img, out=img)
        self._enc.pixels[...] = img
        t_tx = time.perf_counter()
        sock.transmit(self._rows_batch)
        mbps = self._link[1] or self.options.assumed_mbps
        done = max(time.perf_counter(), t_tx + self._wire_bytes * 8 / (mbps * 1e6))
        limit = t_tx + 0.02
        q = sock.queued_bytes()
        while q and time.perf_counter() < limit:      # Linux: wait until the NIC has taken them
            time.sleep(0.0002)
            q = sock.queued_bytes()
            done = time.perf_counter()
        target = min(done + self.options.sync_gap_ms / 1000.0, limit)
        delay = target - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        sock.transmit(self._sync_batch)
        t1 = time.perf_counter()
        s = self._stats
        s.frames += 1
        s.send_ms = (t1 - t0) * 1000 if s.frames == 1 else 0.9 * s.send_ms + 0.1 * (t1 - t0) * 1000
        s.times.append(time.monotonic())

    def _poll_replies(self, sock: RawSocket) -> None:
        for _ in range(16):
            data = sock.recv()
            if data is None:
                return
            info = parse_reply(data)
            if info is not None and info.index == 0:
                prev = self._receiver
                changed = prev is None or prev.firmware_major != info.firmware_major
                if prev is not None and info.uptime_s < prev.uptime_s:
                    self._lost_picture()
                self._receiver = info
                if changed:
                    self._levels_dirty = True


def detect(sock: RawSocket, timeout: float = 0.4, max_cards: int = 8) -> list[ReceiverInfo]:
    """Ask receivers 0, 1, ... until one does not answer (blocking; for tools)."""
    found = []
    for index in range(max_cards):
        sock.send(detect_packet(index))
        end = time.monotonic() + timeout
        info = None
        while time.monotonic() < end and info is None:
            data = sock.recv()
            if data is None:
                time.sleep(0.005)
                continue
            r = parse_reply(data)
            if r is not None and r.index == index:
                info = r
        if info is None:
            break
        found.append(info)
    return found


def list_interfaces() -> list[NetInterface]:
    return list_raw_interfaces()
