"""Which set the sign plays next (docs/ARCHITECTURE.md, "Schedule").

Pure logic over a clock: no threads, no I/O. The engine asks `next()` when a
set ends and `show()` when a set is shown by hand.

* With a default set: the default set loops. When it ends and
  `random_minutes` have passed since the last random set *started* (or since
  start-up, or since a set was shown by hand), one other set with
  `in_rotation` is picked at random -- never the previous pick while there are
  two or more candidates -- plays once, then the default set again. The
  interval is only looked at when the default set ends, so nothing is cut short.
* "Show now" plays a set once, then the default set; the interval restarts
  when the shown set starts.
* Without a default set: every set once, in play order, forever (after a set
  shown by hand, the one after it in the order).
* No sets: None (the sign shows unlit lights).

Ported from the old sign's src/Schedule.h ("regular set" there = default set).
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, Literal, Optional, Sequence

from .model import ScheduleSettings

Kind = Literal["default", "random", "order", "show"]


@dataclass(frozen=True)
class Pick:
    set_id: str
    kind: Kind

    @property
    def source(self) -> str:
        """State.now.source for this pick."""
        return "show" if self.kind == "show" else "schedule"


class Schedule:
    def __init__(self, clock: Callable[[], float] = time.monotonic, rng: Optional[random.Random] = None):
        self._clock = clock
        self._rng = rng or random.Random()
        self._since = clock()            # start of the current random interval
        self._last_pick: Optional[str] = None
        self._order_pos = -1             # index of the last in-order pick

    @staticmethod
    def _minutes(cfg: ScheduleSettings) -> float:
        return max(0.0, float(cfg.random_minutes))

    def _due(self, cfg: ScheduleSettings) -> bool:
        return self._clock() - self._since >= self._minutes(cfg) * 60.0

    @staticmethod
    def _default(sets: Sequence[tuple[str, bool]], cfg: ScheduleSettings) -> Optional[str]:
        d = cfg.default_set
        return d if d is not None and any(i == d for i, _ in sets) else None

    @staticmethod
    def _candidates(sets: Sequence[tuple[str, bool]], default: str) -> list[str]:
        return [i for i, rot in sets if rot and i != default]

    def show(self, set_id: str) -> Pick:
        """A set shown by hand: it plays once and restarts the random interval."""
        self._since = self._clock()
        return Pick(set_id, "show")

    def next(self, sets: Sequence[tuple[str, bool]], cfg: ScheduleSettings,
             prev: Optional[Pick]) -> Optional[Pick]:
        """The set to play after `prev` ended (None at start-up).
        sets: (id, in_rotation) in play order."""
        if not sets:
            return None
        ids = [i for i, _ in sets]
        default = self._default(sets, cfg)
        if default is None:
            if prev is not None and prev.set_id in ids:
                pos = ids.index(prev.set_id) + 1
            elif prev is not None and prev.kind == "order":
                pos = self._order_pos        # it was deleted: the set that moved into its place
            else:
                pos = self._order_pos + 1    # start-up, or after a one-off show
            pos %= len(ids)
            self._order_pos = pos
            return Pick(ids[pos], "order")
        if (prev is not None and prev.kind == "default" and prev.set_id == default
                and cfg.random_enabled and self._due(cfg)):
            cands = self._candidates(sets, default)
            if len(cands) >= 2 and self._last_pick in cands:
                cands.remove(self._last_pick)
            if cands:
                pick = self._rng.choice(cands)
                self._last_pick = pick
                self._since = self._clock()
                return Pick(pick, "random")
        return Pick(default, "default")

    def random_in(self, sets: Sequence[tuple[str, bool]], cfg: ScheduleSettings) -> Optional[float]:
        """Seconds until a random set is due (0 = at the end of the default set); None if
        no random set can come (no default set, random off, no candidates)."""
        default = self._default(sets, cfg)
        if default is None or not cfg.random_enabled or not self._candidates(sets, default):
            return None
        return max(0.0, self._since + self._minutes(cfg) * 60.0 - self._clock())
