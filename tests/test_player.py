import time

import numpy as np
import pytest

from test_player_fakes import LOG, make_set
from trafficlight import config
from trafficlight.model import Clip, Settings, Track
from trafficlight.player import (PRE_ROLL, Player, face_mask, render_still, sign_mask, validate_set)
from trafficlight.render.base import RenderEnv

F = config.FACE
C = F // 2   # a pixel well inside a light


def env(**kw):
    return RenderEnv(settings=Settings(**kw))


def solid(color="#ff0000", start=0.0, duration=4.0, tag="", **kw):
    return Clip(type="test:solid", start=start, duration=duration, params={"color": color, "tag": tag}, **kw)


def px(frame, face, y=C, x=C):
    return tuple(int(v) for v in frame[face * F + y, x])


def test_empty_set_dark_and_ring():
    p = Player(env(off_style="dark"), make_set(), background_init=False)
    f, cues = p.render(1.0)
    assert f.shape == (config.H, config.W, 3) and f.dtype == np.uint8 and not f.any() and cues == []
    p = Player(env(), make_set(), background_init=False)
    f, _ = p.render(1.0)
    # the ring sits on the disc's edge in each light's colour at ~15 %
    row = f[F + C]  # middle light, centre row
    assert px(f, 1) == (0, 0, 0)
    edge = row[: C].max(axis=0)
    assert 30 <= edge[0] <= 45 and 20 <= edge[1] <= 30 and edge[2] == 0      # 15 % of #ffb000
    assert tuple(f[0, 0]) == (0, 0, 0)


def test_a_light_clip_shows_over_a_whole_sign_clip_wherever_the_tracks_are():
    whole = ("all", [solid("#0000ff")])
    light = ("face1", [Clip(type="off", start=0, duration=4)])
    for tracks in ([whole, light], [light, whole]):
        f, _ = Player(env(off_style="dark"), make_set(*tracks), background_init=False).render(0.5)
        # the whole-sign clip carries on in the other two lights; the middle one is switched off
        assert px(f, 0) == px(f, 2) == (0, 0, 255) and px(f, 1) == (0, 0, 0)
        assert tuple(f[F + C, 2]) == (0, 0, 0)          # right up to the rim of the middle light


def test_a_light_clip_replaces_the_clip_below_right_up_to_the_rim():
    """Both are cut to the same soft-edged disc; the lower one must not glow through the rim."""
    whole = ("all", [solid("#0000ff")])
    alone = Player(env(off_style="dark"), make_set(("face1", [solid("#ff0000")])), background_init=False).render(0.5)[0]
    over = Player(env(off_style="dark"), make_set(whole, ("face1", [solid("#ff0000")])), background_init=False).render(0.5)[0]
    assert (over[F:2 * F] == alone[F:2 * F]).all()                    # no blue anywhere in the middle light
    assert (over[:F, :, 2] > 0).any() and (over[F:2 * F, :, 2] == 0).all()
    # Off blacks out its two panels entirely, even under a clip that fills the rectangle
    box = ("all", [solid("#0000ff", mask=False)])
    for mask in (None, True, False):
        f, _ = Player(env(off_style="dark"), make_set(box, ("face1", [Clip(type="off", start=0, duration=4, mask=mask)])),
                      background_init=False).render(0.5)
        assert not f[F:2 * F].any() and (f[:F] == (0, 0, 255)).all() and (f[2 * F:] == (0, 0, 255)).all()
    # a soft-edged clip over a rectangle-filling one still has its anti-aliased rim
    f, _ = Player(env(off_style="dark"), make_set(box, ("face1", [solid("#ff0000")])), background_init=False).render(0.5)
    assert tuple(f[F, 0]) == (0, 0, 255) and tuple(f[F + C, C]) == (255, 0, 0)
    rim = f[F:2 * F][(face_mask() > 0.2) & (face_mask() < 0.8)]
    assert len(rim) and (rim[:, 0] > 0).all() and (rim[:, 2] > 0).all()


def test_text_with_a_transparent_background_sits_over_a_whole_sign_clip():
    whole = ("all", [solid("#0000ff")])
    text = ("face1", [Clip(type="text", start=0, duration=4, params={"text": "I", "bg": "transparent", "ink": "#ffffff"})])
    f, _ = Player(env(off_style="dark"), make_set(whole, text), background_init=False).render(0.5)
    middle = f[F:2 * F]
    assert px(f, 0) == px(f, 2) == (0, 0, 255)
    assert tuple(middle[C, 20]) == (0, 0, 255)                       # beside the letter: the clip below
    assert (middle == 255).all(axis=2).sum() > 200                   # the letter itself, in white


def test_face_clip_masked_and_placed():
    s = make_set(("face1", [solid("#00ff00")]))
    f, _ = Player(env(off_style="dark"), s, background_init=False).render(1.0)
    assert px(f, 1) == (0, 255, 0)
    assert px(f, 0) == (0, 0, 0) and px(f, 2) == (0, 0, 0)
    assert tuple(f[F, 0]) == (0, 0, 0)          # corner of the box: outside the disc
    # mask=False fills the whole box
    s = make_set(("face1", [solid("#00ff00", mask=False)]))
    f, _ = Player(env(off_style="dark"), s, background_init=False).render(1.0)
    assert tuple(f[F, 0]) == (0, 255, 0) and tuple(f[F - 1, 0]) == (0, 0, 0)


def test_all_track_default_three_discs_and_unmasked_on_request():
    s = make_set(("all", [solid("#0000ff", mask=False)]))
    f, _ = Player(env(off_style="dark"), s, background_init=False).render(0.5)
    assert (f == (0, 0, 255)).all()
    for mask in (None, True):
        s = make_set(("all", [solid("#0000ff", mask=mask)]))
        f, _ = Player(env(off_style="dark"), s, background_init=False).render(0.5)
        assert px(f, 0) == px(f, 2) == (0, 0, 255) and tuple(f[0, 0]) == (0, 0, 0)
    assert sign_mask().shape == (config.H, config.W)
    assert face_mask()[C, C] == 1.0 and face_mask()[0, 0] == 0.0
    # anti-aliased edge: some partial values
    assert ((face_mask() > 0) & (face_mask() < 1)).any()


def test_layer_order_first_track_on_top():
    s = make_set(("face0", [solid("#ff0000")]), ("all", [solid("#0000ff")]))
    f, _ = Player(env(), s, background_init=False).render(1.0)
    assert px(f, 0) == (255, 0, 0)       # tracks[0] on top
    assert px(f, 1) == (0, 0, 255)


def test_opacity_fades_and_alpha():
    s = make_set(("face0", [solid("#ffffff", opacity=0.5)]))
    f, _ = Player(env(off_style="dark"), s, background_init=False).render(1.0)
    assert px(f, 0) == (128, 128, 128)
    s = make_set(("face0", [solid("#ffffff", duration=4, fade_in=2, fade_out=1)]))
    p = Player(env(off_style="dark"), s, background_init=False)
    assert px(p.render(0.5)[0], 0) == (64, 64, 64)
    assert px(p.render(2.5)[0], 0) == (255, 255, 255)
    assert px(p.render(3.5)[0], 0) == (128, 128, 128)
    assert px(p.render(0.0)[0], 0) == (0, 0, 0)
    # RGBA renderer over a red layer: half alpha
    s = make_set(("face0", [Clip(type="test:rgba", duration=4, params={"color": "#0000ff"})]),
                 ("face0", [solid("#ff0000")]))
    f, _ = Player(env(), s, background_init=False).render(1.0)
    assert px(f, 0) == (127, 0, 128)


def test_clip_bounds_and_set_time():
    s = make_set(("face0", [solid("#ff0000", start=2, duration=2)]))
    p = Player(env(off_style="dark"), s, background_init=False)
    assert px(p.render(1.99)[0], 0) == (0, 0, 0)
    assert px(p.render(2.0)[0], 0) == (255, 0, 0)
    assert px(p.render(3.99)[0], 0) == (255, 0, 0)
    assert px(p.render(4.0)[0], 0) == (0, 0, 0)


def test_context_values_and_seed():
    s = make_set(("face0", [Clip(type="test:probe", start=1, duration=10, mask=False)]), bpm=120, beat_offset=0.5)
    p = Player(env(), s, background_init=False, seed=42)
    f, _ = p.render(3.0)
    r, g, b = px(f, 0)
    assert r == 20               # t = 2.0 s into the clip
    assert b == 50               # beat = (3.0 - 0.5) * 2 = 5
    seed = g
    assert px(p.render(4.0)[0], 0)[1] == seed     # stable during the pass
    p.new_pass()
    seeds = {px(p.render(4.0)[0], 0)[1]}
    for _ in range(5):
        p.new_pass()
        seeds.add(px(p.render(4.0)[0], 0)[1])
    assert len(seeds) > 1


def test_muted_track_and_unknown_type_and_raising_renderers(caplog):
    s = make_set(("face0", [Clip(type="no:such", duration=5)]),
                 ("face0", [Clip(type="test:boom", duration=5, params={"where": "render"})]),
                 ("face0", [Clip(type="test:boom", duration=5, params={"where": "init"})]),
                 ("face0", [Clip(type="test:boom", duration=5, params={"where": "shape"})]),
                 ("face0", [Clip(type="test:boom", duration=5, params={"where": "audio"})]),
                 ("face1", [solid("#00ff00")]))
    s.tracks.append(Track(target="face2", muted=True, clips=[solid("#ffffff")]))
    p = Player(env(off_style="dark"), s, background_init=False)
    with caplog.at_level("WARNING"):
        for t in (0.5, 1.0, 1.5):
            f, cues = p.render(t)
    assert px(f, 1) == (0, 255, 0) and px(f, 0) == (0, 0, 0) and px(f, 2) == (0, 0, 0)
    assert cues == []
    msgs = [r.getMessage() for r in caplog.records]
    for needle in ("unknown clip type", "raised in render", "failed to start", "returned shape", "raised in audio"):
        assert sum(needle in m for m in msgs) == 1, (needle, msgs)


def test_audio_cues_keys_and_gain():
    snd = Clip(type="test:sound", start=1, duration=4, params={"media": "song"}, opacity=0.8, fade_in=2)
    vid = Clip(type="test:sound", start=0, duration=9, params={"media": "vid"})
    s = make_set(("all", [vid]), ("audio", [snd]))
    p = Player(env(), s, background_init=False)
    _, cues = p.render(2.0)
    by = {c.key: c for c in cues}
    assert set(by) == {snd.id, vid.id}
    assert by[snd.id].position == pytest.approx(1.0)
    assert by[snd.id].gain == pytest.approx(0.5 * 0.8 * 0.5)
    assert by[vid.id].gain == pytest.approx(0.5) and by[vid.id].path == "/sounds/vid.wav"
    _, cues = p.render(2.0, audio=False)
    assert cues == []


def test_instances_lifecycle_and_seeks():
    LOG.clear()
    a = solid(start=0, duration=2, tag="A")
    b = solid(start=5, duration=2, tag="B")
    s = make_set(("face0", [a, b]))
    p = Player(env(), s, background_init=False)
    p.render(0.0)
    assert LOG == [("init", "A")]
    p.render(1.9)
    p.render(5.0 - PRE_ROLL + 0.1)        # B pre-rolled, A closed (past its linger)
    assert ("close", "A") in LOG and ("init", "B") in LOG
    LOG.clear()
    p.render(6.0)
    assert LOG == []                      # B already there
    p.render(0.5)                         # seek back
    assert ("close", "B") in LOG and ("init", "A") in LOG
    LOG.clear()
    p.close()
    assert LOG == [("close", "A")]


def test_set_set_keeps_unchanged_renderers():
    LOG.clear()
    a = solid(start=0, duration=10, tag="A")
    b = solid(start=0, duration=10, tag="B")
    s = make_set(("face0", [a]), ("face1", [b]))
    p = Player(env(), s, background_init=False)
    p.render(1.0)
    assert sorted(LOG) == [("init", "A"), ("init", "B")]
    LOG.clear()
    s2 = s.model_copy(deep=True)
    s2.tracks[0].clips[0].start = 0.5            # timing change: same instance
    s2.tracks[0].clips[0].opacity = 0.5
    s2.tracks[1].clips[0].params["color"] = "#123456"   # params change: new instance
    p.set_set(s2)
    p.render(1.0)
    assert LOG == [("close", "B"), ("init", "B")]
    LOG.clear()
    s3 = s2.model_copy(deep=True)
    s3.tracks[0].clips = []                       # clip removed
    p.set_set(s3)
    assert LOG == [("close", "A")]


def test_background_preroll_and_lookahead():
    LOG.clear()
    c = Clip(type="test:slow", start=3, duration=2, params={"tag": "S"})
    s = make_set(("face0", [c]))
    p = Player(env(), s)
    p.render(2.5)                                   # inside pre-roll: made on a worker thread
    deadline = time.time() + 2
    while not any(e.startswith("init-thread") for e, _ in LOG) and time.time() < deadline:
        time.sleep(0.01)
    made_on = [e for e, w in LOG if w == "S"]
    assert made_on and "clip" in made_on[0]
    f, _ = p.render(3.1)
    assert px(f, 0) == (200, 200, 200)
    assert len([e for e, w in LOG if w == "S"]) == 1
    # a loop back to 0: lookahead keeps a clip at the start alive
    LOG.clear()
    a = solid(start=0, duration=2, tag="L")
    p2 = Player(env(), make_set(("face0", [a]), duration=10), background_init=False)
    p2.render(1.0)
    p2.render(9.5, lookahead=-0.5)
    p2.render(0.01)
    assert LOG == [("init", "L")]
    p.close()
    p2.close()


def test_seek_into_middle_makes_renderer_at_once():
    LOG.clear()
    c = Clip(type="test:slow", start=3, duration=4, params={"tag": "M"})
    p = Player(env(), make_set(("face0", [c])))
    f, _ = p.render(5.0)
    assert px(f, 0) == (200, 200, 200)
    p.close()


def test_overlapping_clips_latest_start_wins():
    s = make_set(("face0", [solid("#ff0000", start=0, duration=5), solid("#00ff00", start=2, duration=5)]))
    p = Player(env(off_style="dark"), s, background_init=False)
    assert px(p.render(3.0)[0], 0) == (0, 255, 0)


def test_validate_set():
    s = make_set(("face0", [solid(start=0, duration=3), solid(start=2, duration=3, tag="bad"),
                            Clip(type="no:such", start=20, duration=0)]),
                 ("audio", [solid(start=0, duration=1)]),
                 duration=10)
    probs = validate_set(s, env())
    msgs = [p["message"] for p in probs]
    assert "tag says bad" in msgs
    assert any("Overlaps" in m for m in msgs)
    assert any("Unknown clip type" in m for m in msgs)
    assert any("after the set ends" in m for m in msgs)
    assert any("no length" in m for m in msgs)
    assert any("cannot go on an audio track" in m for m in msgs)
    assert all(set(p) == {"clip_id", "track_id", "message"} for p in probs)
    cache = {}
    assert validate_set(s, env(), cache) == probs
    assert cache and validate_set(s, env(), cache) == probs


def test_render_still():
    s = make_set(("face2", [solid("#ffffff")]))
    f = render_still(s, 1.0, env())
    assert px(f, 2) == (255, 255, 255)


def test_render_speed_many_layers():
    clips = [("all", [solid("#ff0000", duration=100, opacity=0.5, mask=True)])]
    clips += [(f"face{i}", [solid("#00ff00", duration=100, fade_in=200)]) for i in range(3)]
    p = Player(env(), make_set(*clips, duration=100), background_init=False)
    p.render(0.5)
    t0 = time.perf_counter()
    for i in range(50):
        p.render(1.0 + i * 0.01)
    per_frame = (time.perf_counter() - t0) / 50
    assert per_frame < 0.01, per_frame
