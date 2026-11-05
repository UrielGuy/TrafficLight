"""The media library: uploads, their proxies and thumbnails, on disk under data/media/<id>/.

Thread model: one lock guards the in-memory items (held only for dict work, never for I/O, so the
render thread asking for a path never waits on a disk); one ingest thread works through uploads
one at a time (a show may be running: ingest must not take every core). meta.json is written
through a second lock that always writes the newest state of the item.

Deleting on Windows: a clip on screen (or the HTTP server) may still hold proxy.mp4 open, and
Windows refuses to delete an open file. delete() therefore first drops a `.deleted` marker into
the item's folder and forgets the item, then removes what it can; the ingest thread retries the
rest every few seconds and start-up finishes the job. A folder with the marker, or without
meta.json, is never loaded as an item.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

from ..model import MediaItem, new_id
from . import ingest
from .ingest import Cancelled, IngestError

log = logging.getLogger(__name__)

_DELETED = ".deleted"
_SWEEP_SECONDS = 5.0


class MediaLibrary:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._io = threading.Lock()
        self._items: dict[str, MediaItem] = {}
        self._originals: dict[str, Path] = {}
        self._browser: dict[str, Path] = {}       # what file_path() serves, decided once per item
        self._trash: set[Path] = set()
        self._cancel: set[str] = set()
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._closed = False
        # Starts from the clock so a restart never makes it go backwards for a client that kept it.
        self._rev = int(time.time())
        self._load()
        self._thread = threading.Thread(target=self._worker, name="media-ingest", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ queries
    @property
    def rev(self) -> int:
        with self._lock:
            return self._rev

    def list(self) -> list[MediaItem]:
        with self._lock:
            items = [m.model_copy() for m in self._items.values()]
        return sorted(items, key=lambda m: (m.added, m.id), reverse=True)

    def get(self, media_id: str) -> Optional[MediaItem]:
        with self._lock:
            m = self._items.get(media_id)
            return m.model_copy() if m is not None else None

    def _ready(self, media_id: str) -> Optional[MediaItem]:
        with self._lock:
            m = self._items.get(media_id)
            return m if m is not None and m.status == "ready" else None

    def video_path(self, media_id: str) -> Optional[str]:
        m = self._ready(media_id)
        return str(self.root / media_id / "proxy.mp4") if m is not None and m.kind == "video" else None

    def image_path(self, media_id: str) -> Optional[str]:
        m = self._ready(media_id)
        return str(self.root / media_id / "proxy.png") if m is not None and m.kind == "image" else None

    def audio_path(self, media_id: str) -> Optional[str]:
        m = self._ready(media_id)
        return str(self.root / media_id / "audio.wav") if m is not None and m.has_audio else None

    def thumb_path(self, media_id: str) -> Optional[Path]:
        if self._ready(media_id) is None:
            return None
        p = self.root / media_id / "thumb.jpg"
        return p if p.is_file() else None

    def file_path(self, media_id: str) -> Optional[Path]:
        m = self._ready(media_id)
        if m is None:
            return None
        with self._lock:
            p = self._browser.get(media_id)
            orig = self._originals.get(media_id)
        if p is None:
            p = self._choose_browser_file(media_id, m.kind, orig)
            with self._lock:
                if media_id in self._items:
                    self._browser[media_id] = p
        return p if p is not None and p.is_file() else None

    def peaks(self, media_id: str) -> Optional[dict]:
        m = self._ready(media_id)
        if m is None or not m.has_audio:
            return None
        try:
            return json.loads((self.root / media_id / "peaks.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def wait_ready(self, media_id: str, timeout: float = 30) -> MediaItem:
        """Blocks until the item is no longer "processing" (or the timeout passes); KeyError if unknown."""
        end = time.monotonic() + timeout
        with self._changed:
            while True:
                m = self._items.get(media_id)
                if m is None:
                    raise KeyError(media_id)
                left = end - time.monotonic()
                if m.status != "processing" or left <= 0:
                    return m.model_copy()
                self._changed.wait(left)

    # ------------------------------------------------------------------ changes
    def add(self, src: Path, name: str) -> MediaItem:
        src = Path(src)
        if not src.is_file():
            raise ValueError("The upload is missing")
        try:
            kind = ingest.probe(src).kind
        except IngestError:
            # Unreadable content with a media extension is kept and ends as an error item, so the
            # person sees why; anything else is refused outright.
            kind = ingest.kind_from_ext(src)
            if kind is None:
                raise ValueError(f"Unsupported file type: {src.suffix or src.name}") from None
        # An odd extension is kept (not replaced by one that would misname the content); the browser
        # then gets the converted file instead of the original.
        ext = src.suffix.lower()
        if not (1 < len(ext) <= 6 and ext[1:].isalnum()):
            ext = ".bin"
        mid = new_id()
        d = self.root / mid
        d.mkdir(parents=True)
        orig = d / f"original{ext}"
        size = src.stat().st_size
        try:
            _move(src, orig)
        except OSError as e:
            shutil.rmtree(d, ignore_errors=True)
            raise ValueError(f"The upload could not be stored: {e}") from e
        item = MediaItem(id=mid, name=(name or "").strip() or src.stem, kind=kind, status="processing",
                         size=size, added=time.time())
        with self._lock:
            self._items[mid] = item
            self._originals[mid] = orig
            self._bump()
        self._save(mid)
        self._queue.put(mid)
        return item.model_copy()

    def rename(self, media_id: str, name: str) -> MediaItem:
        with self._lock:
            m = self._items.get(media_id)
            if m is None:
                raise KeyError(media_id)
            m.name = (name or "").strip() or m.name
            self._bump()
            out = m.model_copy()
        self._save(media_id)
        return out

    def delete(self, media_id: str) -> None:
        d = self.root / media_id
        with self._lock:
            if media_id not in self._items:
                raise KeyError(media_id)
            del self._items[media_id]
            self._originals.pop(media_id, None)
            self._browser.pop(media_id, None)
            self._cancel.add(media_id)
            self._bump()
        with self._io:
            try:
                (d / _DELETED).touch()
            except OSError:
                log.exception("cannot mark %s as deleted", d)
        if not self._remove_dir(d):
            with self._lock:
                self._trash.add(d)
            self._queue.put(None)      # wake the worker so it starts retrying

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._cancel.update(self._items)
        self._queue.put(None)
        self._thread.join(timeout=10)

    # ------------------------------------------------------------------ internals
    def _bump(self) -> None:
        self._rev += 1
        self._changed.notify_all()

    def _save(self, media_id: str) -> None:
        with self._io:
            with self._lock:
                m = self._items.get(media_id)
                data = m.model_dump_json(indent=1) if m is not None else None
            if data is None:
                return
            p = self.root / media_id / "meta.json"
            tmp = p.with_name("meta.json.tmp")
            try:
                tmp.write_text(data, encoding="utf-8")
                _replace(tmp, p)
            except OSError:
                log.exception("cannot write %s", p)

    def _load(self) -> None:
        for d in sorted(self.root.iterdir()) if self.root.is_dir() else []:
            if not d.is_dir():
                continue
            meta = d / "meta.json"
            if (d / _DELETED).exists() or not meta.is_file():
                if not self._remove_dir(d):
                    self._trash.add(d)
                continue
            try:
                m = MediaItem.model_validate_json(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                log.warning("skipping %s: unreadable meta.json (%s)", d, e)
                continue
            if m.id != d.name:
                log.warning("skipping %s: meta.json names id %s", d, m.id)
                continue
            originals = sorted(d.glob("original.*"))
            if originals:
                self._originals[m.id] = originals[0]
            self._items[m.id] = m
            if m.status == "processing":
                # Interrupted by a restart: start again (outputs are only replaced when complete).
                if originals:
                    self._queue.put(m.id)
                else:
                    m.status, m.error = "error", "The upload was lost during a restart"
                    self._save(m.id)

    def _worker(self) -> None:
        while True:
            with self._lock:
                closed = self._closed
                trash = bool(self._trash)
            if closed:
                return
            try:
                mid = self._queue.get(timeout=_SWEEP_SECONDS if trash else None)
            except queue.Empty:
                mid = None
            if trash or mid is None:
                self._sweep()
            if mid is None:
                continue
            try:
                self._ingest(mid)
            except Exception as e:
                log.exception("ingest of %s crashed", mid)
                self._finish(mid, None, f"Processing failed ({ingest.short_error(e)})")

    def _sweep(self) -> None:
        with self._lock:
            pending = list(self._trash)
        for d in pending:
            if self._remove_dir(d):
                with self._lock:
                    self._trash.discard(d)

    def _ingest(self, mid: str) -> None:
        with self._lock:
            m = self._items.get(mid)
            orig = self._originals.get(mid)
            if m is None or mid in self._cancel:
                return
            kind = m.kind
        if orig is None or not orig.is_file():
            self._finish(mid, None, "The uploaded file is missing")
            return
        out = self.root / mid

        def check() -> None:
            with self._lock:
                if mid in self._cancel or self._closed:
                    raise Cancelled()

        t0 = time.monotonic()
        try:
            pr = ingest.probe(orig)
            if pr.kind != kind:
                with self._lock:
                    if mid in self._items:
                        self._items[mid].kind = pr.kind
                kind = pr.kind
            if kind == "image":
                res = ingest.ingest_image(orig, out)
            elif pr.animated:
                res = ingest.ingest_animation(orig, out, check)
            elif kind == "video":
                res = ingest.ingest_video(orig, out, check)
            else:
                res = ingest.ingest_audio(orig, out, check)
            browser = self._choose_browser_file(mid, kind, orig)
        except Cancelled:
            return
        except IngestError as e:
            log.info("ingest of %s (%s) failed: %s", mid, orig.name, e)
            self._finish(mid, None, str(e))
            return
        log.info("ingested %s (%s) in %.1f s", mid, orig.name, time.monotonic() - t0)
        with self._lock:
            if mid in self._items:
                self._browser[mid] = browser
        self._finish(mid, res, None)

    def _finish(self, mid: str, res: Optional[ingest.Result], error: Optional[str]) -> None:
        with self._lock:
            m = self._items.get(mid)
            if m is None:
                return
            if error is not None:
                m.status, m.error = "error", error
            else:
                m.status, m.error = "ready", None
                m.duration, m.width, m.height = res.duration, res.width, res.height
                m.fps, m.has_audio = res.fps, res.has_audio
            self._bump()
        self._save(mid)

    def _choose_browser_file(self, mid: str, kind: str, orig: Optional[Path]) -> Optional[Path]:
        d = self.root / mid
        if orig is not None and orig.is_file() and ingest.browser_playable(orig, kind):
            return orig
        return {"video": d / "proxy.mp4", "image": d / "proxy.png", "audio": d / "audio.wav"}[kind]

    @staticmethod
    def _remove_dir(d: Path) -> bool:
        """Delete what can be deleted now; True when the folder is gone."""
        if not d.exists():
            return True
        left = False
        for p in d.iterdir():
            if p.name == _DELETED:
                continue
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
            except OSError:
                left = True
        if left:
            return False     # the marker stays until the last file is gone, so a restart never revives it
        try:
            (d / _DELETED).unlink(missing_ok=True)
            d.rmdir()
            return True
        except OSError:
            return False


def _move(src: Path, dest: Path) -> None:
    try:
        os.replace(src, dest)
    except OSError:
        # Another drive, or the upload is still open on Windows: copy, then tidy up if possible.
        shutil.copyfile(src, dest)
        try:
            src.unlink()
        except OSError:
            pass


def _replace(tmp: Path, dest: Path) -> None:
    # On Windows a reader holding dest open makes os.replace fail for a moment.
    for i in range(10):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if i == 9:
                raise
            time.sleep(0.02)
