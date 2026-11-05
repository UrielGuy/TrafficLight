"""Sets and settings on disk (see "Files on disk" in docs/ARCHITECTURE.md).

    data/settings.json
    data/sets/index.json     {"order": [ids]}  play order
    data/sets/<id>.json      one Set

Everything is kept in memory and written through atomically, so readers never
see a half-written file and a crash never loses more than the change in flight.
All methods are thread-safe; returned models are copies the caller may change.

`rev` grows on every change to the sets (State.sets_rev), `settings_rev` on
every change to the settings: the engine polls both to pick up edits.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

from .model import Set, SetSummary, Settings, default_tracks, new_id

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SetExists(ValueError):
    pass


def valid_id(set_id: str) -> bool:
    """Set ids become file names: only letters, digits, '-' and '_'."""
    return isinstance(set_id, str) and bool(_ID_RE.match(set_id))


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    # On Windows the replace fails while another process (indexer, antivirus,
    # an editor) has the target open; that lasts milliseconds, so retry.
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    try:
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Dicts merge key by key; anything else (lists included) replaces."""
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _uses_media(value: Any, media_id: str) -> bool:
    if isinstance(value, str):
        return value == media_id
    if isinstance(value, (list, tuple)):
        return any(_uses_media(v, media_id) for v in value)
    if isinstance(value, dict):
        return any(_uses_media(v, media_id) for v in value.values())
    return False


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.sets_dir = self.root / "sets"
        self.sets_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()       # the in-memory state
        self._io_lock = threading.Lock()     # keeps file writes in order
        self._sets: dict[str, Set] = {}
        self._order: list[str] = []
        self._settings = Settings()
        self.rev = 0
        self.settings_rev = 0
        self._load()

    # ------------------------------------------------------------ loading
    def _load(self) -> None:
        sp = self.root / "settings.json"
        if sp.exists():
            try:
                self._settings = Settings.model_validate_json(sp.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                # Keep the broken file for inspection; run on defaults rather than not at all.
                log.error("settings.json unreadable (%s); using defaults", e)
                try:
                    os.replace(sp, sp.with_suffix(".json.bad"))
                except OSError:
                    pass
        for p in sorted(self.sets_dir.glob("*.json")):
            if p.name == "index.json" or not valid_id(p.stem):
                continue
            try:
                s = Set.model_validate_json(p.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                log.error("set file %s unreadable: %s", p.name, e)
                continue
            s.id = p.stem
            self._sets[s.id] = s
        order: list[str] = []
        ip = self.sets_dir / "index.json"
        if ip.exists():
            try:
                order = [i for i in json.loads(ip.read_text(encoding="utf-8")).get("order", []) if isinstance(i, str)]
            except (OSError, ValueError, AttributeError) as e:
                log.error("sets/index.json unreadable (%s); ordering by name", e)
        seen: set[str] = set()
        for i in order:
            if i in self._sets and i not in seen:
                self._order.append(i)
                seen.add(i)
        # Sets whose file exists but are missing from the index (copied in by hand) go last.
        rest = sorted((s for i, s in self._sets.items() if i not in seen), key=lambda s: s.name.lower())
        self._order.extend(s.id for s in rest)
        if self._order != order:
            self._persist(index=True)

    def _persist(self, sets: Iterable[str] = (), index: bool = False, settings: bool = False) -> None:
        """Write what a change touched, from the newest in-memory state. Called after the
        change, outside _lock, so readers (the render thread) never wait for the disk;
        writers queue on _io_lock and each writes the newest state, so the last one wins."""
        with self._io_lock:
            with self._lock:
                texts = {i: self._sets[i].model_dump_json(indent=1) if i in self._sets else None for i in sets}
                idx = json.dumps({"order": self._order}, indent=1) if index else None
                st = self._settings.model_dump_json(indent=1) if settings else None
            for i, text in texts.items():
                path = self.sets_dir / f"{i}.json"
                if text is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, text)
            if idx is not None:
                atomic_write(self.sets_dir / "index.json", idx)
            if st is not None:
                atomic_write(self.root / "settings.json", st)

    # ------------------------------------------------------------ sets
    def order(self) -> list[str]:
        with self._lock:
            return list(self._order)

    def list_sets(self) -> list[Set]:
        """All sets in play order."""
        with self._lock:
            return [self._sets[i].model_copy(deep=True) for i in self._order]

    def summaries(self) -> list[SetSummary]:
        with self._lock:
            return [SetSummary(id=s.id, name=s.name, bpm=s.bpm, duration=s.duration, in_rotation=s.in_rotation)
                    for s in (self._sets[i] for i in self._order)]

    def rotation(self) -> list[tuple[str, bool]]:
        """(id, in_rotation) in play order: what the schedule needs."""
        with self._lock:
            return [(i, self._sets[i].in_rotation) for i in self._order]

    def get_set(self, set_id: str) -> Optional[Set]:
        with self._lock:
            s = self._sets.get(set_id)
            return s.model_copy(deep=True) if s is not None else None

    def has_set(self, set_id: str) -> bool:
        with self._lock:
            return set_id in self._sets

    def create_set(self, data: dict[str, Any]) -> Set:
        """A new set from a (partial) Set dict. No id -> a fresh one; no tracks -> the
        default five. SetExists for a taken id, ValueError for a bad one,
        pydantic.ValidationError for bad data."""
        data = dict(data)
        if data.get("tracks") is None:
            data["tracks"] = [t.model_dump() for t in default_tracks()]
        if not data.get("id"):
            data["id"] = new_id()
        s = Set.model_validate(data)
        if not valid_id(s.id):
            raise ValueError(f"Invalid set id {s.id!r}")
        with self._lock:
            if s.id in self._sets:
                raise SetExists(f"A set with id {s.id!r} exists")
            self._sets[s.id] = s
            self._order.append(s.id)
            self.rev += 1
            out = s.model_copy(deep=True)
        self._persist([s.id], index=True)
        return out

    def save_set(self, s: Set) -> Set:
        """Create or replace (PUT). A new id goes to the end of the play order."""
        if not valid_id(s.id):
            raise ValueError(f"Invalid set id {s.id!r}")
        s = s.model_copy(deep=True)
        with self._lock:
            new = s.id not in self._sets
            self._sets[s.id] = s
            if new:
                self._order.append(s.id)
            self.rev += 1
            out = s.model_copy(deep=True)
        self._persist([s.id], index=new)
        return out

    def delete_set(self, set_id: str) -> None:
        """KeyError if unknown. Deleting the default set clears Settings.schedule.default_set
        (the sign then plays all sets in order)."""
        with self._lock:
            if set_id not in self._sets:
                raise KeyError(set_id)
            del self._sets[set_id]
            self._order.remove(set_id)
            self.rev += 1
            was_default = self._settings.schedule.default_set == set_id
            if was_default:
                self._settings.schedule.default_set = None
                self.settings_rev += 1
        self._persist([set_id], index=True, settings=was_default)

    def duplicate_set(self, set_id: str) -> Set:
        """A copy with fresh ids (set, tracks, clips), placed right after the original."""
        with self._lock:
            src = self._sets.get(set_id)
            if src is None:
                raise KeyError(set_id)
            s = src.model_copy(deep=True)
            s.id = new_id()
            s.name = f"{src.name} copy"
            for t in s.tracks:
                t.id = new_id()
                for c in t.clips:
                    c.id = new_id()
            self._sets[s.id] = s
            self._order.insert(self._order.index(set_id) + 1, s.id)
            self.rev += 1
            out = s.model_copy(deep=True)
        self._persist([s.id], index=True)
        return out

    def set_order(self, ids: Iterable[str]) -> None:
        """The given ids first, in that order; unknown ids are ignored; sets not named
        keep their relative order after them."""
        with self._lock:
            first: list[str] = []
            for i in ids:
                if i in self._sets and i not in first:
                    first.append(i)
            new = first + [i for i in self._order if i not in first]
            if new == self._order:
                return
            self._order = new
            self.rev += 1
        self._persist(index=True)

    def sets_using_media(self, media_id: str) -> list[Set]:
        with self._lock:
            return [self._sets[i].model_copy(deep=True) for i in self._order
                    if any(_uses_media(c.params, media_id) for t in self._sets[i].tracks for c in t.clips)]

    # ------------------------------------------------------------ settings
    def settings(self) -> Settings:
        with self._lock:
            return self._settings.model_copy(deep=True)

    def update_settings(self, patch: dict[str, Any]) -> Settings:
        """Deep-merge a partial Settings dict. pydantic.ValidationError if the result is invalid
        (nothing is changed then)."""
        with self._lock:
            merged = deep_merge(self._settings.model_dump(), patch)
            new = Settings.model_validate(merged)
            if new == self._settings:
                return new.model_copy(deep=True)
            self._settings = new
            self.settings_rev += 1
        self._persist(settings=True)
        return new.model_copy(deep=True)

    def save_settings(self, settings: Settings) -> Settings:
        with self._lock:
            self._settings = settings.model_copy(deep=True)
            self.settings_rev += 1
        self._persist(settings=True)
        return settings.model_copy(deep=True)
