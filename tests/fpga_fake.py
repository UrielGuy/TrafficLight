"""A fake custom-firmware card for driver tests: a UDP server on localhost wrapping the behavioural
model (output/hub75/model.py). A ticker thread plays the streamer's pass boundary."""
from __future__ import annotations

import socket
import threading
import time
from typing import Callable

from trafficlight.output.hub75 import protocol as P
from trafficlight.output.hub75.model import Card
from trafficlight.output.hub75.schedule import CardLimits


def wait_for(cond: Callable[[], object], timeout: float = 8.0, what: str = "condition"):
    end = time.monotonic() + timeout
    while True:
        v = cond()
        if v:
            return v
        if time.monotonic() > end:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.005)


class FakeCard:
    def __init__(self, limits: CardLimits = CardLimits(), boundary_ms: float = 4.0):
        self.limits = limits
        self.card = Card(limits)
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.01)
        self.port = self.sock.getsockname()[1]
        self.drop: Callable[[object], bool] | None = None      # packet -> lose it (chunks and commits)
        self.silent = False                                     # does not answer anything
        self.received: list[object] = []                        # parsed packets that reached the model
        self.shown: list[tuple[int, bytes]] = []                # (frame id, bytes) every frame that went on the panels
        self._boundary_s = boundary_ms / 1000.0
        self._stop = threading.Event()
        self._threads = [threading.Thread(target=self._serve, daemon=True),
                         threading.Thread(target=self._tick, daemon=True)]
        for t in self._threads:
            t.start()

    def close(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2)
        self.sock.close()

    def restart(self) -> None:
        """Power cycle: the model starts from nothing, uptime back to 0."""
        with self.lock:
            self.card = Card(self.limits)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                data, src = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                pkt = P.parse(data)
            except P.ProtocolError:
                pkt = None
            if self.silent:
                continue
            if pkt is not None and self.drop is not None and isinstance(pkt, (P.FrameChunk, P.FrameCommit)) \
                    and self.drop(pkt):
                continue
            with self.lock:
                if pkt is not None:
                    self.received.append(pkt)
                reply = self.card.receive(data)
            if reply is not None:
                self.sock.sendto(reply, src)

    def _tick(self) -> None:
        while not self._stop.is_set():
            time.sleep(self._boundary_s)
            with self.lock:
                c = self.card
                before = c.counters["frames_shown"]
                c.boundary()
                if c.counters["frames_shown"] != before:
                    self.shown.append((c.frame_id_shown, c.shown_frame))

    # -- what tests look at
    @property
    def active(self):
        with self.lock:
            return self.card.active

    @property
    def shown_frame(self) -> bytes | None:
        with self.lock:
            return self.card.shown_frame

    def counter(self, name: str) -> int:
        with self.lock:
            return self.card.counters[name]

    def bump(self, name: str, n: int) -> None:
        with self.lock:
            self.card.counters[name] += n
