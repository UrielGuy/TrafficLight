"""Bundles: sets and media in one zip, to move them from one TrafficLight to another (build the show on a
laptop, run it on the sign's computer) or to keep a copy.

    trafficlight.json             the manifest (below)
    sets/<id>.json                one Set each
    media/<id>/meta.json          the MediaItem
    media/<id>/original.*         the upload, untouched
    media/<id>/source.mp4         if the item has one (the crop tool's view)
    media/<id>/v<N>/...           the prepared files of its current version: the other end needs no converting

Manifest: {"format": 1, "kind": "sets" | "all" | "everything", "version": app version, "created": unix
time, "sets": [ids in play order], "media": [ids], "settings": {...}}. "sets" = the chosen sets, "all" =
every set, both with the media they use; "everything" = every set, every media item and the show's
settings (SHOW_SETTINGS: what the show is, not what this computer is: no output, wiring or sound device).

The zip is written as it is sent (no temporary copy of a library that may be gigabytes); media files are
stored, not compressed (video, pictures and sound gain nothing). Importing keeps ids, so a set finds its
media at the other end; something already there under the same id is replaced (sets; for media the name
and the edit, which re-makes the prepared files there) or, with replace=False, kept as it is (media) or
kept next to an imported copy with new ids (sets).
"""
from __future__ import annotations

import io
import json
import re
import time
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Literal, Optional

from pydantic import ValidationError

from . import __version__
from .model import MediaEdit, Set, new_id
from .store import Store, valid_id

FORMAT = 1
MANIFEST = "trafficlight.json"
SHOW_SETTINGS = ("schedule", "face_colors", "off_style")
Kind = Literal["sets", "all", "everything"]

_CHUNK = 1 << 20
_SET_NAME = re.compile(r"^sets/([^/]+)\.json$")
_MEDIA_NAME = re.compile(r"^media/([^/]+)/(.+)$")


class BundleError(ValueError):
    """Not a bundle, or one this version cannot read."""


def media_used(sets: list[Set], known: set[str]) -> list[str]:
    """Ids of media items the sets use, in order of first use: any string in a clip's params that names
    an item (the same rule the library's "used by" check follows)."""
    out: dict[str, None] = {}

    def walk(v: Any) -> None:
        if isinstance(v, str):
            if v in known:
                out[v] = None
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)

    for s in sets:
        for t in s.tracks:
            for c in t.clips:
                walk(c.params)
    return list(out)


# ---------------------------------------------------------------- export
@dataclass
class Export:
    kind: Kind
    sets: list[Set]
    media: list[tuple[Any, list[tuple[str, Path]]]]    # (MediaItem, files) from MediaLibrary.bundle_files
    settings: Optional[dict] = None
    filename: str = "trafficlight.zip"


def plan_export(store: Store, media: Any, kind: Kind, set_ids: Optional[list[str]] = None) -> Export:
    """What a bundle will hold, taken now (later changes do not reach a download in progress). KeyError
    for an unknown set id."""
    if kind == "sets":
        sets = []
        for i in set_ids or []:
            s = store.get_set(i)
            if s is None:
                raise KeyError(i)
            sets.append(s)
    else:
        sets = store.list_sets()
    items = media.list() if media is not None else []
    if kind == "everything":
        ids = [m.id for m in sorted(items, key=lambda m: (m.added, m.id))]
    else:
        ids = media_used(sets, {m.id for m in items})
    carried = []
    for mid in ids:
        try:
            got = media.bundle_files(mid)
        except KeyError:            # deleted meanwhile
            continue
        if got is not None:
            carried.append(got)
    settings = None
    if kind == "everything":
        full = store.settings().model_dump(mode="json")
        settings = {k: full[k] for k in SHOW_SETTINGS}
    day = time.strftime("%Y-%m-%d")
    if kind == "sets" and len(sets) == 1:
        stem = _file_safe(sets[0].name) or "set"
    else:
        stem = {"sets": f"{len(sets)}-sets", "all": "all-sets", "everything": "everything"}[kind]
    return Export(kind, sets, carried, settings, f"trafficlight-{stem}-{day}.zip")


def _file_safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")[:60]


class _Sink(io.RawIOBase):
    """The zip writes here; the generator hands on whatever has piled up."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        self.buf += b
        return len(b)

    def take(self) -> bytes:
        out = bytes(self.buf)
        self.buf.clear()
        return out


def stream(plan: Export) -> Iterator[bytes]:
    """The zip, piece by piece."""
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as z:
        manifest = {"format": FORMAT, "kind": plan.kind, "version": __version__, "created": time.time(),
                    "sets": [s.id for s in plan.sets], "media": [m.id for m, _ in plan.media]}
        if plan.settings is not None:
            manifest["settings"] = plan.settings
        z.writestr(MANIFEST, json.dumps(manifest, indent=1))
        for s in plan.sets:
            z.writestr(f"sets/{s.id}.json", s.model_dump_json(indent=1))
            yield sink.take()
        for item, files in plan.media:
            yield from _write_item(z, sink, item, files)
    yield sink.take()


def _write_item(z: zipfile.ZipFile, sink: _Sink, item: Any, files: list[tuple[str, Path]]) -> Iterator[bytes]:
    # Every file is opened before any is written: a new edit finishing meanwhile deletes the old
    # prepared files (at once on Linux), and the bundle must not hold half of one version. Open files
    # survive that. If the prepared ones are gone already, the other end prepares the item itself.
    with ExitStack() as stack:
        opened: list[tuple[str, BinaryIO, int]] = []
        prepared_ok = True
        for name, path in files:
            try:
                f = stack.enter_context(open(path, "rb"))
            except OSError:
                if name.startswith("v"):
                    prepared_ok = False
                    continue
                return             # no original after all (deleted meanwhile): leave the item out
            opened.append((name, f, path.stat().st_size))
        meta = item.model_dump(mode="json")
        if not prepared_ok:
            opened = [o for o in opened if not o[0].startswith("v")]
            meta.update(version=0, status="processing")
        z.writestr(f"media/{item.id}/meta.json", json.dumps(meta, indent=1))
        for name, f, size in opened:
            info = zipfile.ZipInfo(f"media/{item.id}/{name}", time.localtime(time.time())[:6])
            info.compress_type = zipfile.ZIP_DEFLATED if name.endswith(".json") else zipfile.ZIP_STORED
            info.file_size = size
            with z.open(info, "w", force_zip64=size > 0x7FFF0000) as out:
                while chunk := f.read(_CHUNK):
                    out.write(chunk)
                    yield sink.take()


# ---------------------------------------------------------------- import
@dataclass
class ImportResult:
    sets_added: list[str] = field(default_factory=list)       # names
    sets_replaced: list[str] = field(default_factory=list)
    sets_copied: list[str] = field(default_factory=list)      # kept both: the imported one under a new id
    sets_unchanged: list[str] = field(default_factory=list)
    media_added: list[str] = field(default_factory=list)
    media_updated: list[str] = field(default_factory=list)
    media_unchanged: list[str] = field(default_factory=list)
    media_failed: list[str] = field(default_factory=list)     # "name: why"
    settings: bool = False

    def to_json(self) -> dict:
        return dict(self.__dict__)


def needed_bytes(path: Path) -> int:
    with zipfile.ZipFile(path) as z:
        return sum(i.file_size for i in z.infolist())


def import_bundle(store: Store, media: Any, path: Path, replace: bool = True) -> ImportResult:
    """Bring a bundle's media, then its sets (and the show settings of an "everything" bundle) into
    this TrafficLight. BundleError for something that is not a bundle."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise BundleError("This is not a zip file") from e
    with z:
        try:
            manifest = json.loads(z.read(MANIFEST))
        except (KeyError, ValueError) as e:
            raise BundleError("This zip is not a TrafficLight bundle (no trafficlight.json in it)") from e
        if not isinstance(manifest, dict) or not isinstance(manifest.get("format"), int):
            raise BundleError("This zip is not a TrafficLight bundle")
        if manifest["format"] > FORMAT:
            raise BundleError("The bundle comes from a newer TrafficLight: update this one first")

        sets: dict[str, Set] = {}
        files: dict[str, dict[str, zipfile.ZipInfo]] = {}
        for info in z.infolist():
            if info.is_dir():
                continue
            if m := _SET_NAME.match(info.filename):
                try:
                    s = Set.model_validate_json(z.read(info))
                except ValidationError as e:
                    raise BundleError(f"{info.filename} is not a set this version can read") from e
                if valid_id(m.group(1)):
                    s.id = m.group(1)
                    sets[s.id] = s
            elif m := _MEDIA_NAME.match(info.filename):
                files.setdefault(m.group(1), {})[m.group(2)] = info

        out = ImportResult()
        for mid, entries in files.items():
            _import_media(z, media, mid, entries, replace, out)

        order = [i for i in manifest.get("sets", []) if i in sets] + [i for i in sets if i not in manifest.get("sets", [])]
        ids: dict[str, str] = {}          # id in the bundle -> id here
        for sid in order:
            ids[sid] = _import_set(store, sets[sid], replace, out)

        if manifest.get("kind") in ("all", "everything") and ids:
            # A whole show: its play order comes along (sets only here stay after it).
            store.set_order([ids[i] for i in order])
        show = manifest.get("settings")
        if manifest.get("kind") == "everything" and isinstance(show, dict):
            patch = {k: show[k] for k in SHOW_SETTINGS if k in show}
            sched = patch.get("schedule")
            if isinstance(sched, dict) and sched.get("default_set") is not None:
                sched["default_set"] = ids.get(sched["default_set"])
            try:
                store.update_settings(patch)
                out.settings = True
            except ValidationError:
                pass              # settings from a version that differs too much: the sets still count
        return out


def _import_media(z: zipfile.ZipFile, media: Any, mid: str, entries: dict[str, zipfile.ZipInfo],
                  replace: bool, out: ImportResult) -> None:
    meta_info = entries.pop("meta.json", None)
    try:
        meta = json.loads(z.read(meta_info)) if meta_info is not None else None
    except ValueError:
        meta = None
    if not isinstance(meta, dict) or meta.get("id") != mid:
        out.media_failed.append(f"{mid}: no readable meta.json")
        return
    name = str(meta.get("name") or mid)
    if media is None:
        out.media_failed.append(f"{name}: the media library is not available")
        return
    here = media.get(mid)
    if here is not None:
        try:
            edit = MediaEdit.model_validate(meta.get("edit") or {})
        except ValidationError:
            edit = here.edit
        if not replace or (here.name == name and here.edit == edit):
            out.media_unchanged.append(here.name)
            return
        try:
            media.update(mid, name=name, edit=edit)
            out.media_updated.append(name)
        except (KeyError, ValueError) as e:
            out.media_failed.append(f"{name}: {e}")
        return
    try:
        media.receive(meta, [(n, (lambda i=i: z.open(i))) for n, i in entries.items()])
        out.media_added.append(name)
    except (ValueError, ValidationError, OSError, zipfile.BadZipFile) as e:
        out.media_failed.append(f"{name}: {e}")


def _import_set(store: Store, s: Set, replace: bool, out: ImportResult) -> str:
    here = store.get_set(s.id)
    if here is None:
        store.save_set(s)
        out.sets_added.append(s.name)
        return s.id
    if here == s:
        out.sets_unchanged.append(s.name)
        return s.id
    if replace:
        store.save_set(s)
        out.sets_replaced.append(s.name)
        return s.id
    s = s.model_copy(deep=True)
    s.id = new_id()
    s.name = f"{s.name} (imported)"
    for t in s.tracks:
        t.id = new_id()
        for c in t.clips:
            c.id = new_id()
    store.save_set(s)
    out.sets_copied.append(s.name)
    return s.id
