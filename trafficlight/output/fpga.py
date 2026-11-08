"""Output driver for the custom FPGA firmware of the Colorlight 5A-75B (docs/FIRMWARE.md).

Plain UDP to the card (port 5775), no privileges needed. The card is a streamer; everything
else happens on a sender thread, except the frames' datagrams, which a transmit thread paces out:

* Finding the card (section 6.1): every card broadcasts an ANNOUNCE to 10.75.255.255:5776 every second.
  The driver listens on that port and, from the same socket, asks (ANNOUNCE_REQ to 10.75.255.255:5775),
  because a firewall that drops unsolicited broadcasts still lets the answer to its own broadcast in. It
  drives the first card it hears, follows that card (same flash id) to a new address, reports any other
  card, and takes whichever card announces itself once its card has gone quiet. A card address in the
  settings pins the card instead; it is still asked who it is.
* ``send()`` / ``send_card()`` only copy the picture into a slot and wake the thread, which maps it
  (``Mapper``), packs it (``hub75.pack.Packer``: gamma, brightness, temporal dithering, bit planes) and
  hands the datagrams to the transmit thread; pictures it could not get to are dropped. The next frame
  is packed while the transmit thread sends this one (the datagrams are copies), so a frame costs the
  longer of the two, not their sum. On Linux the packing and the transmit loop run in C (hub75/native.c,
  when built) without the interpreter lock: on a Raspberry Pi the numpy packer takes several milliseconds,
  and the Python loop takes the lock back after each of its 200-odd sends, against the render thread.
* The thread asks the card for its status about twice a second. The status gives the card's constants
  (tick rate, minimum visit, limits), from which the visit schedule is built for the engine's frame rate
  and uploaded; the card restarting (uptime back, schedule gone) brings the same upload again. Frames are
  sent only for a schedule the card has accepted.
* A frame goes out as chunks and a commit, paced in bursts (section 9): the card's receive FIFO is small
  and it prefers the SDRAM for the panels. The card repeats the last frame by itself, so a frame whose
  packed bytes equal the last one sent is not sent again; a still picture with temporal dithering still
  changes from frame to frame and keeps flowing, which is what makes it smooth.
* A frame lost on the way is noticed from the status (the card does not show the id last sent) and sent
  again; nothing else is retransmitted.

Options that are not Settings come from the TRAFFICLIGHT_FPGA environment variable, e.g.
"planes=11,dither=off,burst=4" (native=off: the Python code even where the C code is built).
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field, fields, replace
from typing import Any, Callable

import numpy as np

from .. import config
from ..model import OutputSettings
from .base import OutputDriver, OutputError, problem
from .hub75 import native
from .hub75 import protocol as P
from .hub75.model import newer
from .hub75.pack import Packer
from .hub75.schedule import CardLimits, Schedule, ScheduleError, ScheduleParams, build
from .mapping import Mapper, validate
from .network import NetworkProbe, needed_mbps

POLL_SECONDS = 0.5           # spec section 9: the status about twice a second
ANSWER_TIMEOUT = 2.0         # no status for this long: the card does not answer
UPLOAD_RETRY = 1.0           # a schedule that was not acknowledged is sent again after this
ACK_WAIT = 0.25              # frames wait this long at most for the reply to a schedule upload
LOST_AFTER = 0.3             # a frame the card does not show this long after sending is sent again
RESEND_EVERY = 1.0           # ... but not more often than this
HEALTH_WINDOW = 10.0         # seconds over which the card's loss counters are judged
SWITCH_INTERVAL = 0.0002     # the Python pacer must get the GIL back within a fraction of a ms after each send
MIN_PLANES = 8               # below this the picture is too coarse to be worth showing: report instead
ASK_EVERY = 1.0              # ANNOUNCE_REQ this often while no card is known ...
ASK_EVERY_FOUND = 5.0        # ... and this often once one is (other cards show up through a firewall too)
OTHERS_FORGET = 15.0         # another card not heard for this long is no longer reported


@dataclass
class FpgaOptions:
    """Knobs that are not Settings; defaults from docs/FIRMWARE.md."""
    planes: int = 12
    split: int = 6
    clk_div: int = 2                  # shift clock = tick / clk_div (50 MHz / 2 = 25 MHz)
    blank_before: int = 10            # ticks (see FIRMWARE.md 4.1)
    lat_ticks: int = 4
    blank_after: int = 16
    min_oe_ns: float = 100.0
    dither: bool = True
    pace: bool = True
    burst: int = 8                    # section 9: at most this many packets back to back ...
    gap_us: float = 250.0             # ... then at least this long before the next group
    chunk_bytes: int = P.MAX_CHUNK_DATA
    broadcast: str = P.BROADCAST_IP   # where ANNOUNCE_REQ goes when no address is set ("/" between several)
    announce_port: int = P.ANNOUNCE_PORT    # listened on for the cards' announcements; 0 = any free port
    native: bool = True               # the C packer and transmit loop, where built (hub75/native.py)

    @classmethod
    def parse(cls, text: str | None) -> "FpgaOptions":
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
                raise ValueError(f"unknown FPGA option {k!r}")
            cur = getattr(o, k)
            if isinstance(cur, bool):
                kw[k] = v.lower() in ("1", "true", "yes", "on")
            elif isinstance(cur, int):
                kw[k] = int(v)
            elif isinstance(cur, float):
                kw[k] = float(v)
            else:
                kw[k] = v
        o = replace(o, **kw)
        if o.burst < 1 or o.gap_us < 0 or o.chunk_bytes % P.CHUNK_ALIGN or not 0 < o.chunk_bytes <= P.MAX_CHUNK_DATA:
            raise ValueError("burst must be >= 1, gap_us >= 0 and chunk_bytes a multiple of "
                             f"{P.CHUNK_ALIGN} up to {P.MAX_CHUNK_DATA}")
        return o

    @classmethod
    def from_env(cls) -> "FpgaOptions":
        return cls.parse(os.environ.get("TRAFFICLIGHT_FPGA"))

    @property
    def broadcasts(self) -> list[str]:
        return [a for a in self.broadcast.split("/") if a]


@dataclass(frozen=True)
class _Found:
    """A card that announced itself, and where from (its replies come from there too)."""
    announce: P.Announce
    addr: tuple[str, int]
    heard_at: float

    @property
    def uid(self) -> bytes:
        return self.announce.uid

    def info(self) -> dict[str, Any]:
        a = self.announce
        return {"ip": self.addr[0], "port": self.addr[1], "mac": a.mac, "uid": a.uid_hex,
                "uid_valid": a.flag(P.ANN_UID_VALID), "fixed_address": a.flag(P.ANN_FIXED_ADDRESS), "fw": a.firmware}

    def __str__(self) -> str:
        return f"{self.addr[0]} (MAC {self.announce.mac})"


@dataclass
class _Live:
    """A schedule with what is needed to pack for it: the packer and two frame buffers (the one being
    packed and the last one sent, to tell whether anything changed)."""
    schedule: Schedule
    packer: Packer
    bufs: list[np.ndarray]
    cur: int = 0
    has_prev: bool = False

    @property
    def id(self) -> int:
        return self.schedule.schedule_id


@dataclass
class _Stats:
    frames: int = 0
    dropped: int = 0
    held: int = 0                      # frames identical to the last one sent
    resent: int = 0                    # frames sent again because the card did not show them
    pack_ms: float = 0.0
    send_ms: float = 0.0
    times: deque = field(default_factory=lambda: deque(maxlen=120))


def _ema(old: float, new: float, first: bool) -> float:
    return new if first else 0.9 * old + 0.1 * new


# Windows' sleep() rounds up to a timer tick, a millisecond or more: far longer than the gaps.
_SPIN = sys.platform == "win32"


def wait_until(t: float) -> None:
    """Return at perf_counter time t, or a little after. Linux sleeps: its timers wake within some tens of
    microseconds, and a gap only has to be at least so long, while spinning through every gap kept a whole
    core busy (a quarter of a Raspberry Pi). Windows spins; sleep(0) hands the core over meanwhile."""
    if _SPIN:
        while time.perf_counter() < t:
            time.sleep(0)
        return
    while (left := t - time.perf_counter()) > 0:
        time.sleep(left)


# One probe for every driver the engine opens (it reopens the driver on each settings change).
_NETWORK = NetworkProbe()

class FpgaDriver(OutputDriver):
    def __init__(self, settings: OutputSettings, options: FpgaOptions | None = None,
                 socket_factory: Callable[[], socket.socket] | None = None, network: NetworkProbe | None = None):
        self.settings = settings.model_copy(deep=True)
        self._network = network or _NETWORK
        self.options = options or FpgaOptions.from_env()
        self._socket_factory = socket_factory or (lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        self._brightness = min(max(settings.brightness, 0.0), 1.0)
        self._fps = 60.0
        self._config_problem: dict[str, Any] | None = None
        self._mapper = self._make_mapper(self.settings)
        self._sock: socket.socket | None = None
        self._listen_problem: dict[str, Any] | None = None
        # The card's address: the one in the settings (pinned) or the one it announced; None until then.
        self._addr: tuple[str, int] | None = None
        self._pinned: tuple[str, int] | None = None
        self._found: _Found | None = None
        self._others: dict[bytes, _Found] = {}          # other cards heard, by flash id (replaced, never mutated)
        self._next_ask = 0.0
        self._ask_error: str | None = None
        self._thread: threading.Thread | None = None
        self._tx_thread: threading.Thread | None = None
        # the next frame for the transmit thread: (frame id, P.frame_rows); the sender packs no further ahead
        self._outbox: tuple[int, np.ndarray, np.ndarray] | None = None
        self._native = native.load() if self.options.native else None
        self._tx_cond = threading.Condition()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        # picture handover: (is_card_image, buffer) and buffers to reuse
        self._pending: tuple[bool, np.ndarray] | None = None
        self._free: dict[bool, list[np.ndarray]] = {False: [], True: []}
        self._img = np.zeros((config.CARD_H, config.W, 3), np.uint8)      # the newest picture, as the card's image
        self._have_img = False
        self._resend = False
        self._levels_dirty = True
        self._stats = _Stats()
        self._holding = False
        # card state (sender thread; status() reads)
        self._status: P.Status | None = None
        self._status_at = 0.0
        self._since = 0.0                                   # when the driver started talking to self._addr
        self._limits: CardLimits | None = None
        self._history: deque[tuple[float, P.Status]] = deque(maxlen=64)
        self._seq = 1
        self._min_seq = 0
        self._next_poll = 0.0
        self._next_upload = 0.0
        self._hold_until = 0.0
        self._target_key: tuple | None = None
        self._target: _Live | None = None
        self._live: _Live | None = None
        self._ready = False
        self._sched_problem: dict[str, Any] | None = None
        self._rejected_base = 0
        self._send_problem: dict[str, Any] | None = None
        self._next_id: int | None = None
        self._sent: tuple[int, float] | None = None       # (frame id, when) of the last frame sent whole
        self._next_resend = 0.0
        self._gap_min: float | None = None
        self._last_burst = 0.0       # perf_counter of the Python loop's last group; the C loop keeps its own

    # ---- contract
    def open(self) -> None:
        if self._thread is not None:
            return
        if self.settings.card_address:
            try:
                infos = socket.getaddrinfo(self.settings.card_address, self.settings.card_port,
                                           socket.AF_INET, socket.SOCK_DGRAM)
                self._addr = self._pinned = infos[0][4][:2]
            except (OSError, UnicodeError) as e:
                raise OutputError(f"The card's address {self.settings.card_address!r} cannot be resolved: {e}.",
                                  "Set a numeric address, or leave it empty to find the card automatically, in "
                                  "Settings → LED output.", check="output.settings",
                                  title="The card's address is not valid") from e
        port = self.options.announce_port
        try:
            try:
                sock = self._open_socket(port)
            except OSError as e:
                if not port:
                    raise
                # Most likely another server of ours. Without the port the card is still found by asking.
                sock = self._open_socket(0)
                self._listen_problem = problem(
                    "output.access", "Cannot listen for the LED card",
                    f"UDP port {port}, where the cards announce themselves, is taken ({e}): probably another "
                    "TrafficLight server is running on this computer. This one finds the card by asking instead.",
                    "Stop the other server: two programs sending to one card fight over it.", "warning")
        except OSError as e:
            raise OutputError(f"Cannot open a UDP socket: {e}.", "Check that no firewall blocks this program.",
                              check="output.access", title="The network socket cannot be opened") from e
        self._sock = sock
        self._since = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="fpga-sender", daemon=True)
        self._tx_thread = threading.Thread(target=self._tx_run, name="fpga-transmit", daemon=True)
        self._thread.start()
        self._tx_thread.start()

    def _open_socket(self, port: int) -> socket.socket:
        # No SO_REUSEADDR: with it a second server could bind the announce port too (always on Windows) and
        # take the card's answers meant for this one, unnoticed.
        sock = self._socket_factory()
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 18)
            sock.bind(("", port))
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        return sock

    @property
    def listen_port(self) -> int | None:
        """The UDP port the driver listens on (announcements and replies), once open."""
        return self._sock.getsockname()[1] if self._sock is not None else None

    def send(self, frame: np.ndarray) -> None:
        self._offer(frame, False)

    def send_card(self, image: np.ndarray) -> None:
        self._offer(image, True)

    def set_frame_rate(self, fps: float) -> None:
        if fps > 0:
            self._fps = float(fps)

    def set_brightness(self, value: float) -> None:
        self._brightness = min(max(float(value), 0.0), 1.0)
        self.settings.brightness = self._brightness
        self._levels_dirty = True
        self._wake.set()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._tx_cond:
            self._tx_cond.notify_all()
        for t in (self._thread, self._tx_thread):
            if t is not None:
                t.join(timeout=2.0)
        self._thread = self._tx_thread = None
        live = self._live
        if self._sock is not None:
            if live is not None and self._ready:
                try:      # the card keeps the last frame for ever: leave it dark
                    self._transmit(*self._datagrams(bytes(live.schedule.frame_bytes), live)[1:])
                except OSError:
                    pass
            self._sock.close()
            self._sock = None

    def status(self) -> dict[str, Any]:
        problems = self.problems()
        text = lambda p: f"{p['detail']} {p['fix']}" if p["fix"] else p["detail"]
        error = next((text(p) for p in problems if p["level"] == "error"), None)
        warnings = [text(p) for p in problems if p["level"] == "warning"]
        s = self._stats
        times = list(s.times)
        fps = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 and times[-1] > times[0] else 0.0
        if times and time.monotonic() - times[-1] > 1.0:
            fps = 0.0
        st, addr, found = self._status, self._addr, self._found
        return {
            "driver": "fpga", "ok": error is None, "error": error,
            "open": self._thread is not None, "warnings": warnings, "problems": problems,
            "address": f"{addr[0]}:{addr[1]}" if addr else None, "answering": self._answering(time.monotonic()),
            "found": {**found.info(), "auto": self._pinned is None} if found else None,
            "others": [o.info() for o in self._others.values()],
            "card": self._card_info(st), "schedule": self._schedule_info(),
            "sent_fps": round(fps, 1), "frames": s.frames, "dropped": s.dropped, "held": s.held,
            "resent": s.resent, "holding": self._holding,
            "pack_ms": round(s.pack_ms, 2), "send_ms": round(s.send_ms, 2),
            "transmit": "c" if self._c_loop() is not None else "python",
            "backlogs": self._native.backlogs if self._native is not None else 0,
            "min_gap_us": round(self._gap_min * 1e6) if self._gap_min is not None else None,
            "brightness": self._brightness,
            "network": self._network.status(), "need_mbps": self._need_mbps(), "frame_rate": self._fps,
        }

    def _need_mbps(self) -> float | None:
        live = self._live or self._target
        return round(needed_mbps(live.schedule.frame_bytes, self._fps), 1) if live is not None else None

    # ---- status pieces
    def _answering(self, now: float) -> bool:
        return self._status is not None and now - self._status_at < ANSWER_TIMEOUT

    def _card_info(self, st: P.Status | None) -> dict[str, Any] | None:
        if st is None:
            return None
        return {"firmware": st.firmware, "build": st.fw_build, "uptime_s": round(st.uptime_ms / 1000.0, 1),
                "tick_hz": st.tick_hz, "min_visit_ticks": st.min_visit_ticks,
                "schedule_id": st.sched_active_id, "frame_hz": round(st.tick_hz / st.frame_ticks, 2) if st.frame_ticks else None,
                "last_error": st.last_error, "last_error_name": P.ERROR_NAMES.get(st.last_error, str(st.last_error)),
                "rx_dropped": st.rx_dropped, "underruns": st.underruns, "frames_incomplete": st.frames_incomplete,
                "frames_rejected": st.frames_rejected, "frames_replaced": st.frames_replaced,
                "frames_shown": st.frames_shown, "bad_packets": st.bad_packets,
                "sched_accepted": st.sched_accepted, "sched_rejected": st.sched_rejected}

    def _schedule_info(self) -> dict[str, Any] | None:
        live = self._live or self._target
        if live is None:
            return None
        b = live.schedule.budget()
        return {"id": live.id, "planes": b["planes"], "frame_hz": round(b["frame_hz"], 2),
                "subframe_hz": round(b["subframe_hz_mean"]), "lsb_ns": round(b["lsb_ns"]),
                "duty": round(b["duty"], 3), "shift_mhz": b["shift_mhz"], "dither": self.options.dither}

    def _growth(self, name: str) -> int:
        """How much a card counter grew over the last HEALTH_WINDOW seconds (counters wrap)."""
        if len(self._history) < 2:
            return 0
        t_new, new = self._history[-1]
        old = next(s for t, s in self._history if t >= t_new - HEALTH_WINDOW)
        return (getattr(new, name) - getattr(old, name)) & 0xFFFFFFFF

    def problems(self) -> list[dict[str, Any]]:
        """What stands between this driver and a working sign, most basic first (output/base.problem)."""
        now = time.monotonic()
        out = [p for p in (self._config_problem,) if p is not None]
        if self._thread is None:
            return out
        st = self._status
        out += [p for p in (self._listen_problem, self._send_problem) if p is not None]
        if not self._answering(now):
            if now - max(self._since, self._status_at) >= ANSWER_TIMEOUT:
                out.append(self._silence(now))
            return out
        assert st is not None
        others = list(self._others.values())
        if others:
            out.append(problem("output.cards", "Several LED cards on the network",
                               f"Driving the card at {self._found or self._addr[0]}; also heard: "
                               f"{', '.join(map(str, others))}.",
                               "One computer drives one card: unplug the others, or type the address of the one to "
                               "drive in Settings → LED output → Card address.", "warning"))
        if st.proto_version != P.VERSION:
            out.append(problem("output.firmware", "The card's firmware is not compatible",
                               f"The card speaks protocol version {st.proto_version}, this program version {P.VERSION}.",
                               "Flash the firmware that belongs to this version of the program (docs/FIRMWARE.md)."))
        elif (st.ports != config.PORTS or st.max_cols < config.W or st.max_rows < config.SCAN_ROWS):
            out.append(problem("output.firmware", "The card's firmware does not fit this sign",
                               f"The card drives {st.ports} connectors, up to {st.max_cols} columns and {st.max_rows} row "
                               f"addresses; the sign needs {config.PORTS}, {config.W} and {config.SCAN_ROWS}.",
                               "Flash the firmware built for the 5A-75B with eight HUB75 connectors."))
        if self._sched_problem is not None:
            out.append(self._sched_problem)
        if self._growth("rx_dropped"):
            out.append(problem("output.packets", "The card is losing packets",
                               f"The card dropped {self._growth('rx_dropped')} packets in the last {HEALTH_WINDOW:.0f} s "
                               "(its receive buffer was full or the link made errors).",
                               "Plug the computer straight into the card with a good gigabit cable (no switch); "
                               "if it goes on, send in smaller bursts (TRAFFICLIGHT_FPGA burst=4,gap_us=300).", "warning"))
        elif self._growth("frames_incomplete") >= 3:
            out.append(problem("output.packets", "Frames are being lost on the way",
                               f"{self._growth('frames_incomplete')} frames did not arrive whole in the last "
                               f"{HEALTH_WINDOW:.0f} s; the card kept showing the previous one.",
                               "Plug the computer straight into the card with a good gigabit cable; avoid Wi-Fi bridges "
                               "and busy switches.", "warning"))
        if self._growth("underruns"):
            out.append(problem("output.timing", "The card's refresh is stretching",
                               f"The card was late fetching {self._growth('underruns')} rows of picture in the last "
                               f"{HEALTH_WINDOW:.0f} s, so its refresh ran slower than planned.",
                               "Lower the frame rate or the bit depth (TRAFFICLIGHT_FPGA planes=11) so the schedule "
                               "has more room (docs/FIRMWARE.md section 4).", "warning"))
        return out

    def _silence(self, now: float) -> dict[str, Any]:
        """The output.receiver problem when the card does not answer, with the likeliest cause."""
        fix = (f"Check that the card is powered and runs the custom firmware, that the cable goes from its Ethernet "
               f"port to this computer, and that this computer's port for the card has the address {P.HOST_IP} with "
               f"netmask {P.NETMASK} (with 255.255.255.0 the cards' announcements and addresses are off its network).")
        f, others = self._found, list(self._others.values())
        if self._addr is None:
            asked = ", ".join(self.options.broadcasts)
            detail = "No card has announced itself" + (f" or answered a request sent to {asked}" if asked else "") + "."
            if self._ask_error:
                detail += f" Sending the request failed: {self._ask_error}."
            return problem("output.receiver", "No LED card found", detail, fix)
        host, port = self._addr
        if f is not None and f.addr == self._addr and now - f.heard_at < ANSWER_TIMEOUT:
            return problem("output.receiver", "The LED card does not answer",
                           f"The card {f} announces itself but does not answer requests.",
                           f"Give this computer's port for the card the netmask {P.NETMASK} (address {P.HOST_IP}): "
                           f"with a smaller network the card's address {host} is sent elsewhere.")
        if self._pinned is not None and others:
            return problem("output.receiver", "The LED card does not answer",
                           f"No answer from {host}:{port}, the address in the settings; a card announces itself at "
                           f"{others[0]}.",
                           f"Clear the card address in Settings → LED output to find the card automatically, or set "
                           f"it to {others[0].addr[0]}.")
        return problem("output.receiver", "The LED card does not answer",
                       f"No answer from {host}:{port}." if self._status is None
                       else f"The card at {host}:{port} stopped answering.", fix)

    # ---- internals
    def _make_mapper(self, s: OutputSettings) -> Mapper:
        try:
            validate(s)
            self._config_problem = None
            return Mapper(s)
        except ValueError as e:
            self._config_problem = problem("output.settings", "Output settings ignored", f"Output settings ignored: {e}",
                                           "Run Settings → Panel wiring again, or fix the values in data/settings.json.")
            return Mapper(OutputSettings())

    def _gamma(self) -> float:
        return self.settings.gamma if self.settings.gamma > 0 else 2.2

    def _offer(self, picture: np.ndarray, card: bool) -> None:
        if self._thread is None:
            return
        shape = (config.CARD_H if card else config.H, config.W, 3)
        if picture.shape != shape:
            raise ValueError(f"{'card image' if card else 'frame'} must be {shape}, got {picture.shape}")
        with self._lock:
            free = self._free[card]
            slot = free.pop() if free else np.empty(shape, np.uint8)
        np.copyto(slot, picture, casting="unsafe")
        with self._lock:
            old = self._pending
            if old is not None:
                self._stats.dropped += 1
                self._free[old[0]].append(old[1])
            self._pending = (card, slot)
        self._wake.set()

    # -- sender thread
    def _run(self) -> None:
        if self.options.pace and self._c_loop() is None:
            sys.setswitchinterval(min(sys.getswitchinterval(), SWITCH_INTERVAL))
        while not self._stop.is_set():
            self._wake.wait(0.02)
            self._wake.clear()
            if self._stop.is_set():
                break
            now = time.monotonic()
            try:
                self._receive(now)
                self._maintain(now)
                self._work(now)
            except OSError as e:
                self._send_failed(e)

    def _receive(self, now: float) -> None:
        sock = self._sock
        assert sock is not None
        for _ in range(64):
            try:
                data, src = sock.recvfrom(2048)
            except BlockingIOError:
                return
            except OSError:        # Windows reports an ICMP "port unreachable" for an earlier send here
                continue
            try:
                pkt = P.parse(data)
            except P.ProtocolError:
                continue
            if isinstance(pkt, P.Status):
                self._on_status(pkt, src[:2], now)
            elif isinstance(pkt, P.Announce):
                self._on_announce(pkt, src[:2], now)

    def _on_announce(self, a: P.Announce, src: tuple[str, int], now: float) -> None:
        """A card said who it is (periodically or asked). The address it spoke from is where it listens."""
        f = _Found(a, src, now)
        cur = self._found
        if self._pinned is not None:
            mine = src == self._pinned
        else:
            # The card in use (followed to a new address: reflashed, or built with a fixed one), or the first
            # card heard since there was none.
            mine = cur is None or cur.uid == a.uid
        if not mine:
            self._others = {**self._others, a.uid: f}
            return
        self._found = f
        if a.uid in self._others:
            self._others = {u: o for u, o in self._others.items() if u != a.uid}
        if self._pinned is None:
            self._use(src, now)

    def _use(self, addr: tuple[str, int], now: float) -> None:
        """Talk to the card at addr from now on: what the driver knew about the card at the old address
        (status, frame ids, the schedule it runs) no longer holds."""
        if addr == self._addr:
            return
        self._addr = addr
        self._since = now
        self._status = None
        self._history.clear()
        self._next_id = self._sent = None
        with self._tx_cond:
            self._outbox = None              # numbered for the card at the old address
        self._ready = False
        self._resend = self._have_img
        self._next_poll = self._next_upload = self._hold_until = 0.0

    def _discover(self, now: float) -> None:
        """Forget cards gone quiet and ask who is there: often while no card is known, now and then after."""
        sock = self._sock
        assert sock is not None
        f = self._found
        if self._pinned is None and f is not None and not self._answering(now) and now - f.heard_at >= ANSWER_TIMEOUT:
            # Neither answering nor announcing: gone. The next card to announce itself is taken (the address is
            # kept meanwhile, so this one coming back is no change).
            self._found = None
            self._next_ask = now
        if any(now - o.heard_at > OTHERS_FORGET for o in self._others.values()):
            self._others = {u: o for u, o in self._others.items() if now - o.heard_at <= OTHERS_FORGET}
        if now < self._next_ask:
            return
        self._next_ask = now + (ASK_EVERY if self._found is None else ASK_EVERY_FOUND)
        targets = [self._pinned] if self._pinned else [(h, self.settings.card_port) for h in self.options.broadcasts]
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        req = P.build_announce_request(self._seq)
        self._ask_error = None
        for t in targets:
            try:
                sock.sendto(req, t)
            except OSError as e:
                self._ask_error = str(e)

    def _on_status(self, st: P.Status, src: tuple[str, int], now: float) -> None:
        if src != self._addr:
            return          # from a card this driver does not drive
        self._status_at = now
        if not (st.seq == self._min_seq or newer(st.seq, self._min_seq)):
            return          # the reply to a request sent before the last schedule upload: stale
        self._hold_until = 0.0
        prev = self._status
        self._status = st
        restarted = prev is not None and st.uptime_ms < prev.uptime_ms
        if restarted:
            self._history.clear()
            self._resend = self._have_img
        self._history.append((now, st))
        if self._next_id is None:
            # Frame ids must be newer than anything the card has seen (a restarted program starts over).
            self._next_id = (st.frame_id_rx + 1) & 0xFFFFFFFF
        limits = CardLimits(tick_hz=st.tick_hz, min_visit_ticks=st.min_visit_ticks, max_entries=st.max_entries,
                            max_rows=st.max_rows, max_cols=st.max_cols, max_planes=st.max_planes)
        if st.proto_version == P.VERSION and st.tick_hz:
            self._limits = limits
        ids = (st.sched_active_id, st.sched_pending_id)
        target = self._target
        if target is not None and target.id in ids:
            if self._live is not target:
                self._live = target
                self._resend = self._have_img
            if self._sched_problem is not None and self._sched_problem["id"] == "output.schedule" \
                    and self._sched_problem["level"] == "error":
                self._sched_problem = None
            self._ready = True
        else:
            self._ready = self._live is not None and self._live.id in ids
            if (target is not None and st.sched_rejected != self._rejected_base and target.id not in ids
                    and self._sched_problem is None):
                self._sched_problem = problem(
                    "output.schedule", "The card rejected the refresh schedule",
                    f"The card says: {P.ERROR_NAMES.get(st.last_error, st.last_error)}.",
                    "Update the card's firmware or the program so they agree (docs/FIRMWARE.md section 6.5).")
        # A frame the card does not show some time after we sent it was lost: the next one may be a while
        # coming (a still picture), so send it again.
        sent = self._sent
        if (sent is not None and self._ready and now - sent[1] > LOST_AFTER and now >= self._next_resend
                and st.frame_id_shown != sent[0] and not st.flag(P.FLAG_FRAME_PENDING) and self._have_img):
            self._resend = True
            self._next_resend = now + RESEND_EVERY
            self._stats.resent += 1

    def _maintain(self, now: float) -> None:
        sock = self._sock
        assert sock is not None
        self._discover(now)
        if self._addr is not None and now >= self._next_poll:
            self._next_poll = now + POLL_SECONDS
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            try:
                sock.sendto(P.build_status_request(self._seq), self._addr)
            except OSError as e:
                self._send_failed(e)
        if self._limits is not None:
            key = (self._limits, round(self._fps, 3))
            if key != self._target_key:
                self._rebuild(key)
        t = self._target
        if (t is not None and self._answering(now) and now >= self._next_upload
                and self._status is not None and t.id not in (self._status.sched_active_id, self._status.sched_pending_id)):
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            self._min_seq = self._seq
            self._rejected_base = self._status.sched_rejected
            self._next_upload = now + UPLOAD_RETRY
            self._hold_until = now + ACK_WAIT
            sock.sendto(P.build_schedule(t.schedule.to_bytes(), self._seq), self._addr)

    def _rebuild(self, key: tuple) -> None:
        """Build the schedule for the card's limits and the frame rate; fewer planes when it does not fit."""
        o, limits, fps = self.options, key[0], key[1]
        self._target_key = key
        error = None
        for planes in range(o.planes, MIN_PLANES - 1, -1):
            p = ScheduleParams(planes=planes, split=min(o.split, planes), clk_div=o.clk_div, fps=fps,
                               blank_before=o.blank_before, lat_ticks=o.lat_ticks, blank_after=o.blank_after,
                               min_oe_ns=o.min_oe_ns)
            try:
                s = build(p, limits)
            except ScheduleError as e:
                error = error or e
                continue
            if self._live is not None and self._live.id == s.schedule_id:
                self._target = self._live
            else:
                packer = Packer(s, self._gamma(), self._brightness, o.dither, self._native)
                self._target = _Live(s, packer, [np.empty(s.frame_bytes, np.uint8) for _ in range(2)])
            self._levels_dirty = True
            self._next_upload = 0.0
            self._sched_problem = None if planes == o.planes else problem(
                "output.schedule", "Fewer bit planes than planned",
                f"{o.planes} planes do not fit the card at {fps:g} frames per second; running with {planes} "
                f"({error}).", "A lower frame rate in Settings gives the full depth.", "warning")
            return
        self._target = None
        self._sched_problem = problem(
            "output.schedule", "The refresh schedule does not fit the card", f"{error}.",
            "Lower the frame rate in Settings, or change TRAFFICLIGHT_FPGA (planes, split, clk_div; "
            "docs/FIRMWARE.md section 4).")

    def _work(self, now: float) -> None:
        if self._outbox is not None:
            return          # a packed frame still waits for the transmit thread: it wakes us when it takes it
        for live in (self._live, self._target):
            if live is not None and self._levels_dirty:
                live.packer.set_gamma(self._gamma(), self._brightness)
        if self._levels_dirty:
            self._levels_dirty = False
            self._resend = self._have_img
        with self._lock:
            picture, self._pending = self._pending, None
            resend, self._resend = self._resend, False
        if picture is not None:
            card, buf = picture
            (self._mapper.apply_card if card else self._mapper.apply)(buf, out=self._img)
            self._have_img = True
            with self._lock:
                self._free[card].append(buf)
        if picture is None and not resend:
            return
        live = self._live
        if live is None or not self._ready or now < self._hold_until:
            self._resend = self._have_img      # the newest picture goes out as soon as the card can take it
            return
        self._deliver(live, force=resend)

    def _deliver(self, live: _Live, force: bool) -> None:
        t0 = time.perf_counter()
        buf = live.bufs[live.cur]
        live.packer.pack(self._img, out=buf)
        t1 = time.perf_counter()
        s = self._stats
        s.pack_ms = _ema(s.pack_ms, (t1 - t0) * 1000, s.frames == 0)
        if not force and live.has_prev and np.array_equal(buf, live.bufs[1 - live.cur]):
            s.held += 1
            self._holding = True
            return
        self._holding = False
        frame = self._datagrams(memoryview(buf), live)       # copies: buf is free again
        live.cur, live.has_prev = 1 - live.cur, True
        with self._tx_cond:
            self._outbox = frame
            self._tx_cond.notify()

    def _datagrams(self, frame, live: _Live) -> tuple[int, np.ndarray, np.ndarray]:
        assert self._next_id is not None        # set by the first status, before anything is ready
        frame_id, self._next_id = self._next_id, (self._next_id + 1) & 0xFFFFFFFF
        rows, lengths = P.frame_rows(frame, frame_id, live.id, self._seq, self.options.chunk_bytes)
        self._seq = (self._seq + len(lengths)) & 0xFFFFFFFF
        return frame_id, rows, lengths

    # -- transmit thread
    def _tx_run(self) -> None:
        while True:
            with self._tx_cond:
                while self._outbox is None and not self._stop.is_set():
                    self._tx_cond.wait()
                if self._stop.is_set():
                    return
                frame_id, rows, lengths = self._outbox
                self._outbox = None
            self._wake.set()                   # the sender may pack the next frame while this one goes out
            t0 = time.perf_counter()
            try:
                self._transmit(rows, lengths)
            except OSError as e:
                self._send_failed(e)
                continue
            s = self._stats
            self._sent = (frame_id, time.monotonic())
            s.frames += 1
            s.send_ms = _ema(s.send_ms, (time.perf_counter() - t0) * 1000, s.frames == 1)
            s.times.append(self._sent[1])
            self._send_problem = None

    def _c_loop(self) -> native.Native | None:
        # Only on a plain socket: a test's socket subclass sees each datagram in Python.
        return self._native if type(self._sock) is socket.socket else None

    def _transmit(self, rows: np.ndarray, lengths: np.ndarray) -> None:
        """Section 9: bursts of at most `burst` packets, `gap_us` between the groups, also between the last
        group of one frame and the first of the next (a packed frame is ready as soon as the last is out)."""
        sock, addr, o = self._sock, self._addr, self.options
        assert sock is not None and addr is not None
        gap = o.gap_us / 1e6
        c = self._c_loop()
        if c is not None:
            kept = c.send_paced(sock.fileno(), rows, lengths, addr, o.burst if o.pace else len(lengths),
                                gap if o.pace else 0.0)
            if kept is not None:
                self._gap_min = kept if self._gap_min is None else min(self._gap_min, kept)
            return
        for i, data in enumerate(P.frame_datagrams(rows, lengths)):
            last = self._last_burst
            if o.pace and last and i % o.burst == 0:
                wait_until(last + gap)
                started = time.perf_counter()
                self._gap_min = started - last if self._gap_min is None else min(self._gap_min, started - last)
            while True:
                try:
                    sock.sendto(data, addr)
                    break
                except BlockingIOError:
                    time.sleep(0.0001)
                finally:
                    self._last_burst = time.perf_counter()

    def _send_failed(self, e: OSError) -> None:
        self._send_problem = problem(
            "output.send", "Sending to the card failed", f"Sending failed: {e}",
            f"Check that the computer's network port for the card is up and has an address in the card's network "
            f"({P.HOST_IP}, netmask {P.NETMASK}); the driver keeps retrying.")
