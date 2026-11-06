"""The render thread (docs/ARCHITECTURE.md, "Engine").

One thread, Settings.fps times a second, deadline-paced on time.perf_counter
(no drift: a late frame does not delay the next deadline; after a long stall
the missed frames are dropped, not rushed):

* the sign player plays what the schedule says -> output driver + audio engine;
* an editor session (at most one) plays a draft set; when live it replaces the
  sign player on the LEDs and speakers and the schedule is suspended;
* a test pattern, while one is set, replaces everything on the LEDs (the
  "wiring" one goes to the driver as a ready card image, past the panel
  mapping; the preview shows it as the mapped sign would);
* the newest sign frame and editor frame are kept in FrameSlots for the
  preview sockets, which encode them only when they want one.

Threading: request handlers only change plain fields under `_lock` (cheap) or
post requests the render thread picks up at its next frame. Players, the
output driver and audio sync are touched by the render thread only, and
renderers run outside the lock, so a handler never waits for a frame.
"""
from __future__ import annotations

import io
import logging
import threading
import time
from typing import Any, Callable, Optional

import numpy as np

from . import __version__, config
from .audio.base import AudioEngine, NullAudio, backend_problem
from .checks import setup_checks
from .model import Set, Settings
from .output.base import OutputDriver, OutputError, create_driver, problem
from .player import PRE_ROLL, Player, idle_frame, log_once
from .render.base import AudioCue, RenderEnv
from .schedule import Pick, Schedule
from .store import Store

log = logging.getLogger(__name__)

MIN_SET_SECONDS = 0.1      # a shorter set would spin the schedule
DRIVER_RETRY = 3.0         # seconds between attempts to (re)open a failed output
PNG_LIMIT = 32 * 1024      # a PNG bigger than this (video, noise) is sent as JPEG instead
JPEG_QUALITY = 88
JPEG_STICKY = 1.0          # after a frame went JPEG, re-try PNG this often (s)
TEST_PATTERNS = ("off", "panels", "white", "red", "green", "blue", "gradient", "wiring")
CARD_PATTERNS = ("wiring",)    # drawn in the receiver card's image (output/patterns.make_card)


# ---------------------------------------------------------------- preview frames
def encode_png(frame: np.ndarray) -> bytes:
    from PIL import Image
    b = io.BytesIO()
    Image.fromarray(frame, "RGB").save(b, "PNG", compress_level=1)
    return b.getvalue()


def encode_jpeg(frame: np.ndarray) -> bytes:
    from PIL import Image
    b = io.BytesIO()
    Image.fromarray(frame, "RGB").save(b, "JPEG", quality=JPEG_QUALITY)
    return b.getvalue()


class FrameSlot:
    """The newest frame of one stream. put() is called by the render thread and only
    swaps a reference; encoding happens in the reader's thread, once per frame however
    many readers there are. PNG keeps flat graphics and text pixel-exact; frames that
    PNG cannot squeeze (video) go as JPEG, which is 5-10x smaller and faster there."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Optional[np.ndarray] = None
        self._no = 0
        self._enc_no = 0
        self._enc: bytes = b""
        self._jpeg_until = 0.0

    def put(self, frame: np.ndarray) -> None:
        with self._lock:
            self._frame = frame
            self._no += 1

    @property
    def no(self) -> int:
        return self._no

    def get(self) -> tuple[int, Optional[np.ndarray]]:
        with self._lock:
            return self._no, self._frame

    def encoded(self) -> tuple[int, Optional[bytes]]:
        """(frame number, PNG or JPEG bytes) of the newest frame; (0, None) before the first."""
        with self._lock:
            no, frame = self._no, self._frame
            if frame is None:
                return 0, None
            if no == self._enc_no:
                return no, self._enc
            jpeg_first = time.monotonic() < self._jpeg_until
        if jpeg_first:
            data = encode_jpeg(frame)
        else:
            data = encode_png(frame)
            if len(data) > PNG_LIMIT:
                data = encode_jpeg(frame)
                with self._lock:
                    self._jpeg_until = time.monotonic() + JPEG_STICKY
        with self._lock:
            if no > self._enc_no:
                self._enc_no, self._enc = no, data
        return no, data


# ---------------------------------------------------------------- test patterns
def _fallback_pattern(name: str) -> Optional[np.ndarray]:
    """Used only if output/patterns.py cannot be imported."""
    f = np.zeros((config.H, config.W, 3), np.uint8)
    solid = {"white": (255, 255, 255), "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255)}
    if name in solid:
        f[:] = solid[name]
    elif name == "gradient":
        f[:] = np.linspace(0, 255, config.W).astype(np.uint8)[None, :, None]
    elif name == "panels":
        cols = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255)]
        for i in range(config.PANELS):
            f[i * config.PANEL_H:(i + 1) * config.PANEL_H] = cols[i % len(cols)]
    else:
        return None
    return f


def make_pattern(name: str) -> Optional[np.ndarray]:
    try:
        from .output import patterns
    except Exception:
        log_once("patterns-import", "output.patterns unavailable; using built-in test patterns", exc=True)
        return _fallback_pattern(name)
    try:
        f = patterns.make(name)
    except Exception:
        log_once(("pattern", name), "patterns.make(%r) raised; using built-in pattern", name, exc=True)
        return _fallback_pattern(name)
    return None if f is None else np.array(f, dtype=np.uint8, copy=True)


def make_card_pattern(name: str) -> Optional[np.ndarray]:
    try:
        from .output import patterns
        return patterns.make_card(name)
    except Exception:
        log_once(("card-pattern", name), "patterns.make_card(%r) failed", name, exc=True)
        return None


def card_preview(settings: Settings) -> np.ndarray:
    """The wiring picture as the sign shows it under the current mapping (what the web preview gets)."""
    o = settings.output
    try:
        from .output import patterns
        return patterns.wiring_preview(tuple(o.panel_order), tuple(o.panel_flip), o.rotate180)
    except Exception:
        log_once("wiring-preview", "wiring preview failed; showing black", exc=True)
        return np.zeros((config.H, config.W, 3), np.uint8)


def _failure(e: Exception, msg: str) -> dict[str, Any]:
    if isinstance(e, OutputError):
        return e.as_problem()
    return problem("output.driver", "The LED output cannot start", msg)


# ---------------------------------------------------------------- editor session
class EditorSession:
    """State of one editor socket. Methods are called from the server's threads; the
    engine's render thread advances `t` and renders the draft."""

    def __init__(self, engine: "Engine"):
        self._engine = engine
        self.draft: Optional[Set] = None
        self.draft_rev = 0
        self.t = 0.0
        self.playing = False
        self.live = True
        self.loop_on = True
        self.loop_from = 0.0
        self.loop_to: Optional[float] = None
        self.problems: list[dict[str, str]] = []
        self.validate_cache: dict = {}
        self.closed_reason: Optional[str] = None
        self.frame = FrameSlot()

    @property
    def duration(self) -> float:
        return max(MIN_SET_SECONDS, float(self.draft.duration)) if self.draft is not None else 0.0

    def loop_range(self) -> tuple[float, float]:
        end = self.duration
        lo = max(0.0, min(float(self.loop_from), end))
        hi = end if self.loop_to is None else max(0.0, min(float(self.loop_to), end))
        if hi - lo < MIN_SET_SECONDS:
            return 0.0, end
        return lo, hi

    def set_draft(self, s: Set, problems: list[dict[str, str]]) -> None:
        with self._engine._lock:
            self.draft = s
            self.draft_rev += 1
            self.problems = problems
            self.t = min(self.t, self.duration)

    def play(self) -> None:
        with self._engine._lock:
            if self.draft is not None and self.t >= self.duration:
                self.t = self.loop_range()[0] if self.loop_on else 0.0
            self.playing = True

    def pause(self) -> None:
        with self._engine._lock:
            self.playing = False

    def seek(self, t: float) -> None:
        with self._engine._lock:
            self.t = max(0.0, min(float(t), self.duration if self.draft is not None else float(t)))

    def loop(self, on: bool, start: Optional[float] = None, end: Optional[float] = None) -> None:
        with self._engine._lock:
            self.loop_on = bool(on)
            self.loop_from = float(start) if start is not None else 0.0
            self.loop_to = float(end) if end is not None else None

    def set_live(self, on: bool) -> None:
        with self._engine._lock:
            self.live = bool(on)

    def status(self) -> dict[str, Any]:
        with self._engine._lock:
            lo, hi = self.loop_range()
            return {"type": "status", "t": self.t, "playing": self.playing, "live": self.live,
                    "duration": self.duration, "problems": list(self.problems),
                    "loop": {"on": self.loop_on, "from": lo, "to": hi}}


# ---------------------------------------------------------------- engine
class Engine:
    def __init__(self, store: Store, media: Any = None, audio: Optional[AudioEngine] = None,
                 driver_factory: Callable[[Any], OutputDriver] = create_driver,
                 clock: Callable[[], float] = time.perf_counter, schedule: Optional[Schedule] = None):
        self.store = store
        self.media = media
        self.audio = audio or NullAudio()
        self._driver_factory = driver_factory
        self._clock = clock
        self._lock = threading.RLock()
        self.env = RenderEnv(settings=store.settings(), media=media)
        self.schedule = schedule or Schedule(clock=clock)
        self.sign_frame = FrameSlot()

        # sign state; written by the render thread under _lock, read by state()
        self._pick: Optional[Pick] = None
        self._set: Optional[Set] = None
        self._stored = True                  # _set lives in the store (not a one-off media show)
        self._t = 0.0
        self._paused = False
        self._upcoming: Optional[tuple[Pick, Optional[Set], Optional[Player]]] = None
        # requests from handlers
        self._req_show: Optional[tuple[Pick, Set, bool]] = None
        self._req_next = False
        self._test: Optional[np.ndarray] = None
        self._test_card: Optional[np.ndarray] = None   # with a card pattern: what the driver gets
        self._test_name: Optional[str] = None
        self._editor: Optional[EditorSession] = None

        # render-thread only
        self._player: Optional[Player] = None
        self._ed_owner: Optional[EditorSession] = None
        self._ed_player: Optional[Player] = None
        self._ed_rev = -1
        self._driver: Optional[OutputDriver] = None
        self._driver_error: Optional[str] = None
        self._driver_problem: Optional[dict[str, Any]] = None   # the same failure, for the setup checks
        self._driver_retry_at = 0.0
        self._settings_rev = -1
        self._sets_rev = store.rev
        self._last: Optional[float] = None

        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._fps = 0.0
        self._fps_count = 0
        self._fps_since = clock()
        self._render_ms = 0.0
        self.frames = 0

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="render", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        for p in (self._player, self._ed_player, self._upcoming[2] if self._upcoming else None):
            if p is not None:
                p.close()
        self._player = self._ed_player = None
        self._upcoming = None
        try:
            self.audio.sync([], False)
            self.audio.close()
        except Exception:
            log.exception("closing the audio engine")
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception:
                log.exception("closing the output driver")
            self._driver = None

    def _run(self) -> None:
        deadline = time.perf_counter()
        while not self._stop.is_set():
            period = 1.0 / max(1, min(240, int(self.env.settings.fps or 60)))
            t0 = time.perf_counter()
            try:
                self.step()
            except Exception as e:  # the show goes on whatever one frame does
                log_once(("step", repr(e)), "frame failed: %r", e, exc=True)
            t1 = time.perf_counter()
            self._render_ms = 0.9 * self._render_ms + 0.1 * (t1 - t0) * 1000.0
            deadline += period
            if t1 - deadline > period:
                deadline = t1          # more than a frame behind: drop the missed ones
            delay = deadline - time.perf_counter()
            if delay > 0:
                time.sleep(delay)

    # ------------------------------------------------------------ requests (any thread)
    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def play(self) -> None:
        with self._lock:
            self._paused = False

    def next(self) -> None:
        with self._lock:
            self._req_next = True

    def show_set(self, set_id: str) -> bool:
        """Show a stored set now. False if there is no such set."""
        s = self.store.get_set(set_id)
        if s is None:
            return False
        with self._lock:
            self._req_show = (self.schedule.show(s.id), s, True)
            self._paused = False
        return True

    def show_temp(self, s: Set) -> None:
        """Show a set that is not stored (a media item on its own), once."""
        with self._lock:
            self._req_show = (self.schedule.show(s.id), s, False)
            self._paused = False

    def test_pattern(self, name: str) -> None:
        """ValueError for an unknown pattern; "off" ends the test picture."""
        card = None
        if name == "off":
            frame = None
        elif name in CARD_PATTERNS:
            card = make_card_pattern(name)
            if card is None or card.shape != (config.CARD_H, config.W, 3):
                raise ValueError(f"Test pattern {name!r} is not available")
            card.flags.writeable = False
            frame = card_preview(self.env.settings)
        else:
            frame = make_pattern(name) if name in TEST_PATTERNS else None
            if frame is None or frame.shape != (config.H, config.W, 3):
                raise ValueError(f"Unknown test pattern {name!r}")
            frame.flags.writeable = False
        with self._lock:
            self._test = frame
            self._test_card = card
            self._test_name = None if frame is None else name

    def open_editor(self, live: bool = True) -> EditorSession:
        """A new editor session; an open one is closed (its socket sees closed_reason). `live` is the
        session's first state, so a client that had Live off never takes over the sign, not even briefly."""
        s = EditorSession(self)
        s.live = bool(live)
        with self._lock:
            if self._editor is not None:
                self._editor.closed_reason = "Another editor was opened"
            self._editor = s
        return s

    def close_editor(self, session: EditorSession) -> None:
        with self._lock:
            if self._editor is session:
                self._editor = None

    @property
    def editor(self) -> Optional[EditorSession]:
        return self._editor

    # ------------------------------------------------------------ state
    def state(self) -> dict[str, Any]:
        settings = self.env.settings
        rotation = self.store.rotation()
        with self._lock:
            ed = self._editor
            if self._test is not None:
                now = {"source": "test", "set_id": None, "set_name": self._test_name, "t": 0.0,
                       "duration": 0.0, "paused": False}
            elif ed is not None and ed.live:
                now = {"source": "editor", "set_id": ed.draft.id if ed.draft else None,
                       "set_name": ed.draft.name if ed.draft else None, "t": ed.t,
                       "duration": ed.duration, "paused": not ed.playing}
            elif self._set is None:
                now = {"source": "idle", "set_id": None, "set_name": None, "t": 0.0, "duration": 0.0,
                       "paused": self._paused}
            else:
                now = {"source": self._pick.source if self._pick else "schedule",
                       "set_id": self._set.id if self._stored else None, "set_name": self._set.name,
                       "t": self._t, "duration": max(MIN_SET_SECONDS, self._set.duration), "paused": self._paused}
            random_in = self.schedule.random_in(rotation, settings.schedule)
        sch = settings.schedule
        output, audio = self._output_status(settings), self._audio_status(settings)
        return {
            "now": now,
            "schedule": {"default_set": sch.default_set, "random_minutes": sch.random_minutes,
                         "random_enabled": sch.random_enabled, "random_in": random_in},
            "output": output,
            "audio": audio,
            "checks": setup_checks(settings, output, audio, web_ui=(config.WEB_DIST / "index.html").is_file()),
            "sets_rev": self.store.rev,
            "media_rev": int(getattr(self.media, "rev", 0) or 0),
            "version": __version__,
        }

    def _output_status(self, settings: Settings) -> dict[str, Any]:
        d = self._driver
        st: dict[str, Any] = {"driver": settings.output.driver, "ok": False, "error": self._driver_error,
                              "interface": settings.output.interface,
                              "problems": [self._driver_problem] if self._driver_problem else []}
        if d is not None:
            try:
                st.update(d.status())
            except Exception as e:
                st.update(ok=False, error=f"status failed: {e}")
        elif self._driver_error is None:
            st.update(ok=False, error="not started")
        st["fps"] = round(self._fps, 1)
        st["render_ms"] = round(self._render_ms, 2)
        return st

    def _audio_status(self, settings: Settings) -> dict[str, Any]:
        st: dict[str, Any] = {"ok": True, "error": None, "device": settings.audio.device, "device_name": None,
                              "volume": settings.audio.volume, "voices": 0}
        if type(self.audio) is NullAudio:     # the sound libraries did not load
            p = backend_problem(self.audio.reason)
            st.update(ok=False, error=p["title"], problems=[p])
            return st
        try:
            st.update(self.audio.status())
        except Exception as e:
            st.update(ok=False, error=f"status failed: {e}")
        return st

    @property
    def fps(self) -> float:
        return self._fps

    # ------------------------------------------------------------ the frame (render thread)
    def step(self, now: Optional[float] = None) -> None:
        now = self._clock() if now is None else now
        dt = 0.0 if self._last is None else max(0.0, now - self._last)
        self._last = now
        self._poll_settings(now)

        with self._lock:
            self._poll_sets()
            test = self._test
            test_card = self._test_card
            ed = self._editor
            ed_live = ed is not None and ed.live
            sign_lookahead, upcoming_player, sign_t = self._advance_sign(dt, frozen=ed_live)
            sign_player, sign_paused = self._player, self._paused
            ed_player, ed_t, ed_lookahead, ed_playing = self._advance_editor(ed, dt)

        settings = self.env.settings
        cues: list[AudioCue] = []
        playing = True
        if upcoming_player is not None:
            upcoming_player.prepare(sign_lookahead)    # another set comes next: warm it up
            sign_lookahead = None
        if not ed_live and test is None:
            if sign_player is not None:
                frame, cues = sign_player.render(sign_t, lookahead=sign_lookahead)
            else:
                frame = idle_frame(settings)
            playing = not sign_paused
        ed_frame = None
        if ed is not None:
            if ed_player is not None:
                ed_frame, ed_cues = ed_player.render(ed_t, audio=ed_live and test is None, lookahead=ed_lookahead)
            else:
                ed_frame, ed_cues = idle_frame(settings), []
            ed.frame.put(ed_frame)
            if ed_live and test is None:
                frame, cues, playing = ed_frame, ed_cues, ed_playing
        if test is not None:
            # The wizard changes the mapping while the wiring picture is up: the preview follows it.
            frame, cues = (test if test_card is None else card_preview(settings)), []

        self.sign_frame.put(frame)
        self._send(frame, now, test_card)
        try:
            self.audio.sync(cues, playing)
        except Exception as e:
            log_once(("audio-sync", repr(e)), "audio sync failed: %r", e, exc=True)
        self._count_frame(now)

    def _count_frame(self, now: float) -> None:
        self.frames += 1
        self._fps_count += 1
        if now - self._fps_since >= 1.0:
            self._fps = self._fps_count / (now - self._fps_since)
            self._fps_count = 0
            self._fps_since = now

    # -- sign
    def _switch(self, pick: Optional[Pick], s: Optional[Set], stored: bool, t: float = 0.0,
                prepared: Optional[Player] = None) -> None:
        if (s is not None and self._set is not None and self._player is not None and prepared is None
                and stored and self._stored and s.id == self._set.id):
            if s != self._set:
                self._player.set_set(s)
            self._player.new_pass()          # the same set again: keep its renderers (no video hiccup)
        else:
            if self._player is not None:
                self._player.close()
            if prepared is not None:
                prepared.set_set(s)
                self._player = prepared
            else:
                self._player = Player(self.env, s) if s is not None else None
        self._pick, self._set, self._stored, self._t = pick, s, stored, t

    def _drop_upcoming(self) -> None:
        if self._upcoming is not None and self._upcoming[2] is not None:
            self._upcoming[2].close()
        self._upcoming = None

    def _next_pick(self) -> tuple[Optional[Pick], Optional[Set]]:
        settings = self.env.settings
        prev = self._pick
        for _ in range(3):   # the picked set may vanish between asking and loading
            pick = self.schedule.next(self.store.rotation(), settings.schedule, prev)
            if pick is None:
                return None, None
            s = self.store.get_set(pick.set_id)
            if s is not None:
                return pick, s
        return None, None

    def _advance(self, carry: float = 0.0) -> None:
        """The current set is over: on to the next one."""
        if self._upcoming is not None:
            pick, _, prepared = self._upcoming
            self._upcoming = None
            s = self.store.get_set(pick.set_id)
            if s is not None:
                self._switch(pick, s, True, carry, prepared)
                return
            if prepared is not None:
                prepared.close()
        pick, s = self._next_pick()
        self._switch(pick, s, True, carry)

    def _advance_sign(self, dt: float, frozen: bool) -> tuple[Optional[float], Optional[Player], float]:
        """Move the sign's clock (under _lock). Returns (time the next pass starts from, seen
        from now (negative), or None; the next set's player if it is another set; t)."""
        if self._req_show is not None:
            pick, s, stored = self._req_show
            self._req_show = None
            self._drop_upcoming()
            self._switch(pick, s, stored)
        if self._req_next:
            self._req_next = False
            self._advance()
        if self._set is None:
            self._drop_upcoming()
            self._advance()
            if self._set is None:
                return None, None, 0.0
        if not frozen and not self._paused:
            self._t += dt
        for _ in range(8):
            dur = max(MIN_SET_SECONDS, float(self._set.duration)) if self._set is not None else None
            if dur is None or self._t < dur:
                break
            self._advance(self._t - dur)
        else:
            self._t = 0.0
        if self._set is None:
            return None, None, 0.0
        dur = max(MIN_SET_SECONDS, float(self._set.duration))
        if not frozen and not self._paused and self._t >= dur - PRE_ROLL:
            if self._upcoming is None:
                pick, s = self._next_pick()
                if pick is not None:
                    same = self._stored and s.id == self._set.id
                    self._upcoming = (pick, s, None if same else Player(self.env, s))
            if self._upcoming is not None:
                lookahead = self._t - dur
                return lookahead, self._upcoming[2], self._t
        return None, None, self._t

    def _poll_sets(self) -> None:
        """Stored sets changed: pick up edits of the playing set, move on if it was deleted."""
        rev = self.store.rev
        if rev == self._sets_rev:
            return
        self._sets_rev = rev
        if self._upcoming is not None and not self.store.has_set(self._upcoming[0].set_id):
            self._drop_upcoming()
        if self._set is None or not self._stored:
            return
        s = self.store.get_set(self._set.id)
        if s is None:
            self._drop_upcoming()
            self._advance()
        elif s != self._set:
            self._set = s
            if self._player is not None:
                self._player.set_set(s)

    # -- editor
    def _advance_editor(self, ed: Optional[EditorSession], dt: float
                        ) -> tuple[Optional[Player], float, Optional[float], bool]:
        if ed is not self._ed_owner:
            if self._ed_player is not None:
                self._ed_player.close()
            self._ed_player, self._ed_owner, self._ed_rev = None, ed, -1
        if ed is None:
            return None, 0.0, None, False
        if ed.draft is not None and ed.draft_rev != self._ed_rev:
            self._ed_rev = ed.draft_rev
            if self._ed_player is None:
                self._ed_player = Player(self.env, ed.draft)
            else:
                self._ed_player.set_set(ed.draft)    # renderers of unchanged clips keep running
        if ed.draft is None or self._ed_player is None:
            return None, 0.0, None, False
        end = ed.duration
        lo, hi = ed.loop_range()
        lookahead = None
        if ed.playing:
            prev = ed.t
            ed.t += dt
            if ed.loop_on and prev < hi <= ed.t:
                ed.t = lo + min(ed.t - hi, hi - lo - 1e-6)
                self._ed_player.new_pass()
            elif ed.t >= end:
                if ed.loop_on:
                    ed.t = lo
                    self._ed_player.new_pass()
                else:
                    ed.t = end
                    ed.playing = False
            if ed.loop_on and ed.t < hi and ed.t >= hi - PRE_ROLL:
                lookahead = lo + (ed.t - hi)
        # At the very end nothing covers t (clips are start <= t < end): show the last frame.
        t = min(ed.t, end - 1e-4) if end > 0 else 0.0
        return self._ed_player, max(0.0, t), lookahead, ed.playing

    # -- settings and output
    def _poll_settings(self, now: float) -> None:
        rev = self.store.settings_rev
        first = self._settings_rev < 0
        if rev != self._settings_rev:
            self._settings_rev = rev
            old = self.env.settings
            new = self.store.settings()
            self.env.settings = new
            if first or new.output != old.output:
                same_but_brightness = (not first and self._driver is not None
                                       and new.output.model_copy(update={"brightness": old.output.brightness})
                                       == old.output)
                if same_but_brightness:
                    self._safe(lambda: self._driver.set_brightness(new.output.brightness), "set_brightness")
                else:
                    self._open_driver(new, now)
            if first or new.audio.device != old.audio.device:
                self._safe(lambda: self.audio.set_device(new.audio.device), "audio set_device")
            if first or new.audio.volume != old.audio.volume:
                self._safe(lambda: self.audio.set_volume(new.audio.volume), "audio set_volume")
        if self._driver is None and now >= self._driver_retry_at:
            self._open_driver(self.env.settings, now)

    def _safe(self, fn: Callable[[], Any], what: str) -> None:
        try:
            fn()
        except Exception as e:
            log_once((what, repr(e)), "%s failed: %r", what, e, exc=True)

    def _open_driver(self, settings: Settings, now: float) -> None:
        if self._driver is not None:
            self._safe(self._driver.close, "driver close")
            self._driver = None
        try:
            d = self._driver_factory(settings.output)
            d.open()
            d.set_brightness(settings.output.brightness)
        except Exception as e:
            msg = str(e) or type(e).__name__
            if msg != self._driver_error:
                log.warning("output %s unavailable: %s", settings.output.driver, msg)
            self._driver_error, self._driver_problem = msg, _failure(e, msg)
            self._driver_retry_at = now + DRIVER_RETRY
            return
        self._driver = d
        self._driver_error = self._driver_problem = None

    def _send(self, frame: np.ndarray, now: float, card: Optional[np.ndarray] = None) -> None:
        d = self._driver
        if d is None:
            return
        try:
            if card is not None:
                d.send_card(card.copy())
            else:
                d.send(frame.copy())   # drivers may work in place; the preview keeps the original
        except Exception as e:
            msg = str(e) or type(e).__name__
            log.warning("output send failed, reopening in %.0f s: %s", DRIVER_RETRY, msg)
            self._safe(d.close, "driver close")
            self._driver = None
            self._driver_error, self._driver_problem = msg, _failure(e, msg)
            self._driver_retry_at = now + DRIVER_RETRY
