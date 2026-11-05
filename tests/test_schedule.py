import random

from trafficlight.model import ScheduleSettings
from trafficlight.schedule import Pick, Schedule


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def sched(seed=1):
    c = Clock()
    return c, Schedule(clock=c, rng=random.Random(seed))


SETS = [("d", True), ("a", True), ("b", True), ("c", True)]


def test_no_sets_is_idle():
    _, s = sched()
    assert s.next([], ScheduleSettings(default_set="d"), None) is None


def test_default_loops_until_due():
    c, s = sched()
    cfg = ScheduleSettings(default_set="d", random_minutes=6)
    p = s.next(SETS, cfg, None)
    assert p == Pick("d", "default")
    for _ in range(10):
        c.now += 30
        p = s.next(SETS, cfg, p)
        assert p.set_id == "d"
    assert s.random_in(SETS, cfg) == 60.0
    c.now += 60
    p = s.next(SETS, cfg, p)
    assert p.kind == "random" and p.set_id in "abc" and p.source == "schedule"
    # after the random set: the default again, and the interval restarted at the pick
    c.now += 400
    p2 = s.next(SETS, cfg, p)
    assert p2 == Pick("d", "default")
    assert s.random_in(SETS, cfg) == 0.0   # 400 s since the random set started > 360


def test_random_never_repeats_previous_pick():
    c, s = sched(seed=5)
    cfg = ScheduleSettings(default_set="d", random_minutes=0)
    p = Pick("d", "default")
    picks = []
    for _ in range(60):
        r = s.next(SETS, cfg, p)
        assert r.kind == "random"
        picks.append(r.set_id)
        p = s.next(SETS, cfg, r)
        assert p.set_id == "d"
    assert all(x != y for x, y in zip(picks, picks[1:]))
    assert set(picks) == {"a", "b", "c"}


def test_single_candidate_repeats_and_rotation_respected():
    _, s = sched()
    cfg = ScheduleSettings(default_set="d", random_minutes=0)
    sets = [("d", False), ("a", True), ("b", False)]
    for _ in range(5):
        r = s.next(sets, cfg, Pick("d", "default"))
        assert r == Pick("a", "random")
    # nobody in rotation: the default just loops and random_in is n/a
    sets = [("d", True), ("a", False)]
    assert s.next(sets, cfg, Pick("d", "default")) == Pick("d", "default")
    assert s.random_in(sets, cfg) is None


def test_random_disabled():
    _, s = sched()
    cfg = ScheduleSettings(default_set="d", random_minutes=0, random_enabled=False)
    assert s.next(SETS, cfg, Pick("d", "default")) == Pick("d", "default")
    assert s.random_in(SETS, cfg) is None


def test_show_now_returns_to_default_and_restarts_interval():
    c, s = sched()
    cfg = ScheduleSettings(default_set="d", random_minutes=6)
    c.now += 1000                         # long overdue
    shown = s.show("b")
    assert shown == Pick("b", "show") and shown.source == "show"
    c.now += 20
    p = s.next(SETS, cfg, shown)
    assert p == Pick("d", "default")      # never random straight after a show
    assert s.random_in(SETS, cfg) == 340.0
    c.now += 30
    assert s.next(SETS, cfg, p) == Pick("d", "default")
    # the shown set may itself be the default set
    c.now += 400
    assert s.next(SETS, cfg, s.show("d")) == Pick("d", "default")


def test_no_default_plays_in_order_forever():
    _, s = sched()
    cfg = ScheduleSettings(default_set=None)
    p = None
    seen = []
    for _ in range(9):
        p = s.next(SETS, cfg, p)
        assert p.kind == "order"
        seen.append(p.set_id)
    assert seen == ["d", "a", "b", "c", "d", "a", "b", "c", "d"]
    assert s.random_in(SETS, cfg) is None


def test_order_after_show_and_after_deletion():
    _, s = sched()
    cfg = ScheduleSettings()
    assert s.next(SETS, cfg, s.show("b")).set_id == "c"
    p = s.next(SETS, cfg, Pick("a", "order"))
    assert p.set_id == "b"
    # "b" deleted while playing: carry on from where it stood
    remaining = [("d", True), ("a", True), ("c", True)]
    assert s.next(remaining, cfg, Pick("b", "order")).set_id == "c"


def test_unknown_default_means_in_order():
    _, s = sched()
    cfg = ScheduleSettings(default_set="gone")
    assert s.next(SETS, cfg, None).kind == "order"
    assert s.random_in(SETS, cfg) is None


def test_lone_set():
    _, s = sched()
    assert s.next([("x", True)], ScheduleSettings(), Pick("x", "order")) == Pick("x", "order")
    cfg = ScheduleSettings(default_set="x", random_minutes=0)
    assert s.next([("x", True)], cfg, Pick("x", "default")) == Pick("x", "default")


def test_order_resumes_after_one_off_show():
    _, s = sched()
    cfg = ScheduleSettings()
    assert s.next(SETS, cfg, None).set_id == "d"
    p = s.next(SETS, cfg, Pick("d", "order"))
    assert p.set_id == "a"
    assert s.next(SETS, cfg, s.show("temp-media-set")).set_id == "b"
