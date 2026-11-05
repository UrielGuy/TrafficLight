import json

import pytest
from pydantic import ValidationError

from trafficlight.model import Clip, Set
from trafficlight.store import SetExists, Store, deep_merge


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


def test_create_defaults_and_files(store, tmp_path):
    s = store.create_set({"name": "Morning"})
    assert [t.target for t in s.tracks] == ["all", "face0", "face1", "face2", "audio"]
    assert (tmp_path / "sets" / f"{s.id}.json").exists()
    assert json.loads((tmp_path / "sets" / "index.json").read_text()) == {"order": [s.id]}
    assert store.rev == 1
    # explicit id and explicit (empty) tracks are kept
    s2 = store.create_set({"id": "abc", "name": "B", "tracks": []})
    assert s2.id == "abc" and s2.tracks == []
    with pytest.raises(SetExists):
        store.create_set({"id": "abc"})
    with pytest.raises(ValueError):
        store.create_set({"id": "../evil"})
    with pytest.raises(ValidationError):
        store.create_set({"name": "x", "duration": "long"})


def test_reload_from_disk_keeps_order(tmp_path):
    st = Store(tmp_path)
    a = st.create_set({"name": "A"})
    b = st.create_set({"name": "B"})
    c = st.create_set({"name": "C"})
    st.set_order([c.id, a.id])
    assert st.order() == [c.id, a.id, b.id]
    again = Store(tmp_path)
    assert again.order() == [c.id, a.id, b.id]
    assert again.get_set(a.id) == st.get_set(a.id)


def test_index_repairs_missing_and_stray(tmp_path):
    st = Store(tmp_path)
    a = st.create_set({"name": "Zed"})
    # a set file copied in by hand and an index entry whose file is gone
    stray = Set(id="handmade", name="Alpha")
    (tmp_path / "sets" / "handmade.json").write_text(stray.model_dump_json())
    (tmp_path / "sets" / "index.json").write_text(json.dumps({"order": ["gone", a.id]}))
    (tmp_path / "sets" / "broken.json").write_text("{not json")
    again = Store(tmp_path)
    assert again.order() == [a.id, "handmade"]


def test_put_get_returns_copies(store):
    s = store.create_set({"name": "A"})
    got = store.get_set(s.id)
    got.name = "changed locally"
    assert store.get_set(s.id).name == "A"
    got.tracks[1].clips.append(Clip(type="light"))
    rev = store.rev
    saved = store.save_set(got)
    assert saved.name == "changed locally" and store.rev == rev + 1
    assert len(store.get_set(s.id).tracks[1].clips) == 1
    # PUT of an unknown id creates it at the end
    store.save_set(Set(id="newone", name="N"))
    assert store.order()[-1] == "newone"


def test_delete_default_set_clears_default(store, tmp_path):
    a = store.create_set({"name": "A"})
    store.update_settings({"schedule": {"default_set": a.id}})
    srev = store.settings_rev
    store.delete_set(a.id)
    assert store.settings().schedule.default_set is None
    assert store.settings_rev == srev + 1
    assert not (tmp_path / "sets" / f"{a.id}.json").exists()
    with pytest.raises(KeyError):
        store.delete_set(a.id)


def test_duplicate_gets_fresh_ids_and_sits_after(store):
    a = store.create_set({"name": "A"})
    b = store.create_set({"name": "B"})
    s = store.get_set(a.id)
    s.tracks[1].clips.append(Clip(type="light"))
    store.save_set(s)
    d = store.duplicate_set(a.id)
    assert d.name == "A copy" and d.id != a.id
    assert store.order() == [a.id, d.id, b.id]
    assert d.tracks[1].clips[0].id != s.tracks[1].clips[0].id
    assert {t.id for t in d.tracks}.isdisjoint({t.id for t in s.tracks})


def test_order_ignores_unknown_and_appends_rest(store):
    ids = [store.create_set({"name": n}).id for n in "ABCD"]
    rev = store.rev
    store.set_order(["nope", ids[3], ids[1], ids[3]])
    assert store.order() == [ids[3], ids[1], ids[0], ids[2]]
    assert store.rev == rev + 1
    store.set_order([ids[3]])          # no change -> no rev bump
    assert store.rev == rev + 1


def test_settings_deep_merge_and_persist(tmp_path):
    st = Store(tmp_path)
    assert st.settings().fps == 60
    out = st.update_settings({"audio": {"volume": 0.3}, "face_colors": ["#000000", "#111111", "#222222"]})
    assert out.audio.volume == 0.3 and out.audio.device is None
    assert out.face_colors[1] == "#111111"
    rev = st.settings_rev
    st.update_settings({"audio": {"volume": 0.3}})      # unchanged -> no rev bump
    assert st.settings_rev == rev
    with pytest.raises(ValidationError):
        st.update_settings({"off_style": "sparkly"})
    assert st.settings().off_style == "ring"
    assert Store(tmp_path).settings().audio.volume == 0.3


def test_corrupt_settings_fall_back(tmp_path):
    (tmp_path / "settings.json").write_text("{oops")
    st = Store(tmp_path)
    assert st.settings().fps == 60
    assert (tmp_path / "settings.json.bad").exists()


def test_sets_using_media(store):
    a = store.create_set({"name": "A"})
    store.create_set({"name": "B"})
    s = store.get_set(a.id)
    s.tracks[0].clips.append(Clip(type="video", params={"media": "m1", "loop": True}))
    store.save_set(s)
    assert [x.name for x in store.sets_using_media("m1")] == ["A"]
    assert store.sets_using_media("m2") == []


def test_deep_merge():
    assert deep_merge({"a": {"b": 1, "c": 2}, "l": [1]}, {"a": {"b": 3}, "l": [2]}) == {"a": {"b": 3, "c": 2}, "l": [2]}


def test_no_temp_files_left(store, tmp_path):
    for n in range(5):
        store.create_set({"name": str(n)})
    store.update_settings({"fps": 30})
    assert not [p for p in tmp_path.rglob("*.tmp")]


def test_concurrent_writers_leave_newest_on_disk(tmp_path):
    import threading
    st = Store(tmp_path)
    a = st.create_set({"name": "A"})

    def writer(k):
        for n in range(10):
            s = st.get_set(a.id)
            s.name = f"{k}-{n}"
            st.save_set(s)

    threads = [threading.Thread(target=writer, args=(k,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert Store(tmp_path).get_set(a.id) == st.get_set(a.id)
