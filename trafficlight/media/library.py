"""The media library: uploads and link imports, their proxies and thumbnails, on disk under data/media/<id>/.

    <id>/meta.json              the MediaItem
    <id>/original.<ext>         the upload or download, never changed
    <id>/source.mp4             only when a browser cannot play the original: the whole picture for the crop tool
    <id>/v<N>/                  the prepared files of version N (MediaItem.version): proxy.mp4 or proxy.png,
                                audio.wav, peaks.json, thumb.jpg
    <id>/download/              a link import while it downloads

Thread model: one lock guards the in-memory items (held only for dict work, never for I/O, so the
render thread asking for a path never waits on a disk); one ingest thread works through items to
prepare one at a time (a show may be running: ingest must not take every core); one download thread
fetches link imports one at a time and hands them to the ingest thread. meta.json is written through
a second lock that always writes the newest state of the item.

Changing the edit while the item plays: the new files go to a new v<N+1>/ folder; the old folder is
left alone until the new one is complete. Then the item's version moves to N+1 in one step (every
path the library hands out from then on is the new one) and v<N>/ is put in the bin. A clip that is
playing notices the new path on its next frame, opens it, shows the old picture until the new one
has its first frame, then closes the old file (render/media/video.py); the sound switches at the
same moment (a new path for the clip's voice). On Windows a file that is open cannot be deleted, so
the bin is emptied by retrying every few seconds until the last reader has let go; on Linux the
folder goes at once and readers keep their open files. Nothing is renamed or replaced in place.

Deleting on Windows works the same way: delete() first drops a `.deleted` marker into the item's
folder and forgets the item, then removes what it can; the bin retries the rest and start-up
finishes the job. A folder with the marker, or without meta.json, is never loaded as an item.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from .. import config
from ..model import MediaEdit, MediaItem, new_id
from . import ingest
from .ingest import Cancelled, IngestError

log = logging.getLogger(__name__)

_DELETED = ".deleted"
_SWEEP_SECONDS = 5.0
_PROGRESS_EVERY = 0.5        # seconds between progress reports that bump rev (clients refetch on a bump)
_VDIR = re.compile(r"^v(\d+)$")
_PREPARED = ("proxy.mp4", "proxy.png", "audio.wav", "peaks.json", "thumb.jpg")


def _default_fetcher():
    from .download import fetch
    return fetch


class MediaLibrary:
    def __init__(self, root: Path, fetcher: Optional[Callable] = None, prober: Optional[Callable] = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._fetch = fetcher
        self._probe = prober
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._io = threading.Lock()
        self._items: dict[str, MediaItem] = {}
        self._originals: dict[str, Path] = {}
        self._browser: dict[str, tuple[int, Optional[Path]]] = {}   # what file_path() serves, per version
        self._sources: dict[str, Optional[Path]] = {}                # what source_path() serves
        self._good_edit: dict[str, MediaEdit] = {}                   # the edit the current version was made with
        self._jobs: dict[str, int] = {}          # bumped when an item's work must start over (edit changed)
        self._auto_name: set[str] = set()        # imports still named after their link: the title replaces it
        self._imports: dict[str, dict] = {}      # link imports without an original yet: what to fetch
        self._trash: set[Path] = set()
        self._cancel: set[str] = set()
        self._queue: "queue.Queue[Optional[str]]" = queue.Queue()
        self._downloads: "queue.Queue[Optional[tuple[str, dict]]]" = queue.Queue()
        self._closed = False
        # Starts from the clock so a restart never makes it go backwards for a client that kept it.
        self._rev = int(time.time())
        self._last_bump = 0.0
        self._load()
        self._thread = threading.Thread(target=self._worker, name="media-ingest", daemon=True)
        self._thread.start()
        self._dthread = threading.Thread(target=self._download_worker, name="media-download", daemon=True)
        self._dthread.start()

    # ------------------------------------------------------------------ queries
    @property
    def rev(self) -> int:
        with self._lock:
            return self._rev

    def list(self) -> list[MediaItem]:
        with self._lock:
            items = [m.model_copy(deep=True) for m in self._items.values()]
        return sorted(items, key=lambda m: (m.added, m.id), reverse=True)

    def get(self, media_id: str) -> Optional[MediaItem]:
        with self._lock:
            m = self._items.get(media_id)
            return m.model_copy(deep=True) if m is not None else None

    def _prepared(self, media_id: str) -> Optional[tuple[MediaItem, Path]]:
        """The item and its folder of prepared files, if it has any (also while a new edit is being
        applied: the previous files keep playing until the new ones are complete)."""
        with self._lock:
            m = self._items.get(media_id)
            if m is None or m.version <= 0:
                return None
            return m, self.root / media_id / f"v{m.version}"

    def video_path(self, media_id: str) -> Optional[str]:
        p = self._prepared(media_id)
        return str(p[1] / "proxy.mp4") if p is not None and p[0].kind == "video" else None

    def image_path(self, media_id: str) -> Optional[str]:
        p = self._prepared(media_id)
        return str(p[1] / "proxy.png") if p is not None and p[0].kind == "image" else None

    def audio_path(self, media_id: str) -> Optional[str]:
        p = self._prepared(media_id)
        return str(p[1] / "audio.wav") if p is not None and p[0].has_audio else None

    def thumb_path(self, media_id: str) -> Optional[Path]:
        p = self._prepared(media_id)
        if p is None:
            return None
        t = p[1] / "thumb.jpg"
        return t if t.is_file() else None

    def file_path(self, media_id: str) -> Optional[Path]:
        """What the library page plays: the original when a browser can, else the crop tool's view of
        it, else the proxy."""
        p = self._prepared(media_id)
        if p is None:
            return None
        m, vdir = p
        with self._lock:
            cached = self._browser.get(media_id)
            orig = self._originals.get(media_id)
        if cached is not None and cached[0] == m.version:
            f = cached[1]
        else:
            f = self._choose_browser_file(media_id, m.kind, orig, vdir)
            with self._lock:
                if media_id in self._items:
                    self._browser[media_id] = (m.version, f)
        return f if f is not None and f.is_file() else None

    def source_path(self, media_id: str) -> Optional[Path]:
        """The whole original picture in a form a browser plays (GET /api/media/{id}/source): the
        original itself, or source.mp4 made from it. None for sounds and while there is none yet."""
        with self._lock:
            m = self._items.get(media_id)
            if m is None or m.kind == "audio":
                return None
            known = media_id in self._sources
            p = self._sources.get(media_id)
            orig = self._originals.get(media_id)
        if not known:
            p = self._choose_source(media_id, m.kind, orig)
            if p is not None and p.is_file():
                with self._lock:
                    if media_id in self._items:
                        self._sources[media_id] = p
        return p if p is not None and p.is_file() else None

    def peaks(self, media_id: str) -> Optional[dict]:
        p = self._prepared(media_id)
        if p is None or not p[0].has_audio:
            return None
        try:
            return json.loads((p[1] / "peaks.json").read_text(encoding="utf-8"))
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
                    return m.model_copy(deep=True)
                self._changed.wait(left)

    # ------------------------------------------------------------------ changes
    def add(self, src: Path, name: str, edit: Optional[MediaEdit] = None) -> MediaItem:
        src = Path(src)
        if not src.is_file():
            raise ValueError("The upload is missing")
        kind = self._kind_of(src)
        mid = new_id()
        d = self.root / mid
        d.mkdir(parents=True)
        size = src.stat().st_size
        try:
            orig = self._take_original(src, d)
        except OSError as e:
            shutil.rmtree(d, ignore_errors=True)
            raise ValueError(f"The upload could not be stored: {e}") from e
        item = MediaItem(id=mid, name=(name or "").strip() or src.stem, kind=kind, status="processing",
                         size=size, added=time.time(), edit=edit or MediaEdit(), stage="waiting")
        with self._lock:
            self._items[mid] = item
            self._originals[mid] = orig
            self._jobs[mid] = 0
            self._bump()
        self._save(mid)
        self._queue.put(mid)
        return item.model_copy(deep=True)

    def import_url(self, url: str, *, max_height: int = config.IMPORT_HEIGHT, start: float = 0.0,
                   duration: Optional[float] = None, name: Optional[str] = None) -> MediaItem:
        """A new item fetched from a link on the download thread, then prepared like an upload.
        ValueError for something that is not a web link."""
        url = (url or "").strip()
        u = urlparse(url)
        if u.scheme.lower() not in ("http", "https") or not u.netloc:
            raise ValueError("Paste a web link (starting with http:// or https://)")
        mid = new_id()
        (self.root / mid).mkdir(parents=True)
        label = (name or "").strip()
        item = MediaItem(id=mid, name=label or (u.hostname or url), kind="video", status="processing",
                         added=time.time(), source_url=url, stage="queued")
        job = {"max_height": int(max_height), "start": max(0.0, float(start or 0.0)),
               "duration": None if duration is None else float(duration)}
        with self._lock:
            self._items[mid] = item
            self._jobs[mid] = 0
            self._imports[mid] = job
            if not label:
                self._auto_name.add(mid)
            self._bump()
        self._save(mid)
        self._downloads.put((mid, job))
        return item.model_copy(deep=True)

    def probe(self, url: str):
        """download.Probed: title and length of a link's video, as far as they can be found out without
        downloading it (slow: it asks the site). Unknowns are None."""
        if self._probe is not None:
            return self._probe(url)
        from .download import probe
        return probe(url)

    def update(self, media_id: str, *, name: Optional[str] = None, edit: Optional[MediaEdit] = None) -> MediaItem:
        """Rename and / or change the edit. A changed edit re-makes the prepared files from the original
        (the item goes back to "processing"; the previous files keep playing until the new ones are
        complete). An edit for an item that ended in error, changed or not, tries it again (a failed
        link import is fetched again with its first settings). KeyError if unknown, ValueError for an
        edit that cannot apply to the item."""
        with self._lock:
            m = self._items.get(media_id)
            if m is None:
                raise KeyError(media_id)
            if name is not None and name.strip():
                m.name = name.strip()
                self._auto_name.discard(media_id)
            if edit is not None and (edit != m.edit or m.status == "error"):
                if m.kind == "image" and (edit.start or edit.end is not None):
                    edit = edit.model_copy(update={"start": 0.0, "end": None})
                if m.kind == "audio" and (edit.crop or edit.rotate):
                    raise ValueError("A sound has no picture to turn or crop")
                if m.source_duration > 0 and m.kind != "image" and edit.start >= m.source_duration:
                    raise ValueError(f"The in point ({edit.start:.2f} s) is after the end "
                                     f"({m.source_duration:.2f} s)")
                m.edit = edit
                self._restart(media_id, m)
            self._bump()
            out = m.model_copy(deep=True)
        self._save(media_id)
        return out

    def rename(self, media_id: str, name: str) -> MediaItem:
        return self.update(media_id, name=name)

    def duplicate(self, media_id: str, name: Optional[str] = None) -> MediaItem:
        """A second item from the same original (for another crop of it). The original is hard-linked
        where the file system allows it (no second copy on disk) and copied where not (FAT / exFAT). The
        prepared files come along, so the copy is ready at once. KeyError if unknown, ValueError while
        the original is still being downloaded."""
        with self._lock:
            m = self._items.get(media_id)
            if m is None:
                raise KeyError(media_id)
            orig = self._originals.get(media_id)
            src = m.model_copy(deep=True)
            good = self._good_edit.get(media_id)
        if orig is None or not orig.is_file():
            raise ValueError("The original is not there yet (still downloading?)")
        nid = new_id()
        d = self.root / nid
        d.mkdir(parents=True)
        try:
            _link_or_copy(orig, d / orig.name)
            source = self.root / media_id / "source.mp4"
            if source.is_file():
                _link_or_copy(source, d / "source.mp4")
            version = 0
            if src.version > 0:
                vdir = self.root / media_id / f"v{src.version}"
                files = [f for f in _PREPARED if (vdir / f).is_file()]
                if files:
                    (d / "v1").mkdir()
                    for f in files:
                        _link_or_copy(vdir / f, d / "v1" / f)
                    version = 1
        except OSError as e:
            shutil.rmtree(d, ignore_errors=True)
            raise ValueError(f"The copy could not be stored: {e}") from e
        item = src.model_copy(update={"id": nid, "name": (name or "").strip() or f"{src.name} copy",
                                      "version": version, "added": time.time()})
        redo = item.status != "ready" or version == 0
        if redo:
            item.status, item.error, item.progress, item.stage = "processing", None, 0.0, "waiting"
        with self._lock:
            self._items[nid] = item
            self._originals[nid] = d / orig.name
            self._jobs[nid] = 0
            if good is not None:
                self._good_edit[nid] = good
            elif item.status == "ready":
                self._good_edit[nid] = item.edit
            self._bump()
            out = item.model_copy(deep=True)
        self._save(nid)
        if redo:
            self._queue.put(nid)
        return out

    def delete(self, media_id: str) -> None:
        d = self.root / media_id
        with self._lock:
            if media_id not in self._items:
                raise KeyError(media_id)
            del self._items[media_id]
            for table in (self._originals, self._browser, self._sources, self._good_edit, self._jobs, self._imports):
                table.pop(media_id, None)
            self._auto_name.discard(media_id)
            self._cancel.add(media_id)
            self._bump()
        with self._io:
            try:
                (d / _DELETED).touch()
            except OSError:
                log.exception("cannot mark %s as deleted", d)
        self._discard(d)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._cancel.update(self._items)
        self._queue.put(None)
        self._downloads.put(None)
        self._thread.join(timeout=10)
        self._dthread.join(timeout=10)

    # ------------------------------------------------------------------ internals
    def _bump(self) -> None:
        self._rev += 1
        self._last_bump = time.monotonic()
        self._changed.notify_all()

    def _restart(self, mid: str, m: MediaItem) -> None:
        """(lock held) Throw away work in progress for the item and queue it again."""
        self._jobs[mid] = self._jobs.get(mid, 0) + 1
        m.error = None
        if m.status == "processing" and m.stage in ("queued", "downloading"):
            return                                # the download hands it to the ingest when done
        if mid not in self._originals and mid in self._imports:
            m.status, m.progress, m.stage = "processing", 0.0, "queued"     # a failed import: try again
            self._downloads.put((mid, self._imports[mid]))
            return
        m.status, m.progress, m.stage = "processing", 0.0, "waiting"
        self._queue.put(mid)

    def _kind_of(self, src: Path) -> str:
        try:
            return ingest.probe(src).kind
        except IngestError:
            # Unreadable content with a media extension is kept and ends as an error item, so the
            # person sees why; anything else is refused outright.
            kind = ingest.kind_from_ext(src)
            if kind is None:
                raise ValueError(f"Unsupported file type: {src.suffix or src.name}") from None
            return kind

    @staticmethod
    def _take_original(src: Path, d: Path) -> Path:
        # An odd extension is kept (not replaced by one that would misname the content); the browser
        # then gets the converted file instead of the original.
        ext = src.suffix.lower()
        if not (1 < len(ext) <= 6 and ext[1:].isalnum()):
            ext = ".bin"
        orig = d / f"original{ext}"
        _move(src, orig)
        return orig

    def _save(self, media_id: str) -> None:
        with self._io:
            with self._lock:
                m = self._items.get(media_id)
                data = m.model_dump(mode="json") if m is not None else None
                job = self._imports.get(media_id)
            if data is None:
                return
            if job is not None:
                data["import"] = job        # only meta.json remembers across a restart what is left to fetch
            p = self.root / media_id / "meta.json"
            tmp = p.with_name("meta.json.tmp")
            try:
                tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
                _replace(tmp, p)
            except OSError:
                log.exception("cannot write %s", p)

    def _load(self) -> None:
        for d in sorted(self.root.iterdir()) if self.root.is_dir() else []:
            if not d.is_dir():
                continue
            meta = d / "meta.json"
            if (d / _DELETED).exists() or not meta.is_file():
                self._discard(d)
                continue
            try:
                raw = json.loads(meta.read_text(encoding="utf-8"))
                job = raw.pop("import", None)
                m = MediaItem.model_validate(raw)
            except (OSError, ValueError) as e:
                log.warning("skipping %s: unreadable meta.json (%s)", d, e)
                continue
            if m.id != d.name:
                log.warning("skipping %s: meta.json names id %s", d, m.id)
                continue
            originals = sorted(p for p in d.glob("original.*") if not p.name.endswith(".tmp"))
            if originals:
                self._originals[m.id] = originals[0]
            if m.version == 0 and m.status == "ready":
                self._adopt_flat_layout(d, m)
            self._tidy(d, m.version)
            self._items[m.id] = m
            self._jobs[m.id] = 0
            if not originals and m.source_url and isinstance(job, dict):
                self._imports[m.id] = job
            if m.status == "ready":
                self._good_edit[m.id] = m.edit
            if m.status == "processing":
                # Interrupted by a restart: start again (outputs only count once complete).
                if originals:
                    m.stage, m.progress = "waiting", 0.0
                    self._queue.put(m.id)
                elif m.id in self._imports:
                    m.stage, m.progress = "queued", 0.0
                    self._downloads.put((m.id, self._imports[m.id]))
                else:
                    m.status, m.error = "error", "The upload was lost during a restart"
                self._save(m.id)

    def _adopt_flat_layout(self, d: Path, m: MediaItem) -> None:
        """An item prepared before versions existed keeps its files at the top of its folder: move them
        into v1/ (nothing has them open this early)."""
        files = [f for f in _PREPARED if (d / f).is_file()]
        if not files:
            return
        try:
            (d / "v1").mkdir(exist_ok=True)
            for f in files:
                os.replace(d / f, d / "v1" / f)
        except OSError:
            log.exception("cannot move the prepared files of %s into v1/", d)
            return
        m.version = 1
        m.source_width = m.source_width or m.width
        m.source_height = m.source_height or m.height
        m.source_duration = m.source_duration or m.duration
        self._items[m.id] = m
        self._save(m.id)

    def _tidy(self, d: Path, keep: int) -> None:
        """Leftovers of work a restart interrupted: other versions, half-written files, downloads."""
        for p in d.iterdir():
            v = _VDIR.match(p.name)
            if (v and int(v.group(1)) != keep) or p.name == "download":
                self._discard(p)
            elif p.is_file() and ".tmp" in p.name:
                try:
                    p.unlink()
                except OSError:
                    pass

    def _discard(self, p: Path) -> None:
        """Delete a folder now if possible, else keep retrying from the ingest thread."""
        if not self._remove_dir(p):
            with self._lock:
                self._trash.add(p)
            self._queue.put(None)      # wake the worker so it starts retrying

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
                self._fail(mid, f"Processing failed ({ingest.short_error(e)})", None)

    def _sweep(self) -> None:
        with self._lock:
            pending = list(self._trash)
        for d in pending:
            if self._remove_dir(d):
                with self._lock:
                    self._trash.discard(d)

    def _checker(self, mid: str, job: int) -> Callable[[], None]:
        def check() -> None:
            with self._lock:
                if mid in self._cancel or self._closed or self._jobs.get(mid) != job:
                    raise Cancelled()
        return check

    def _reporter(self, mid: str, job: Optional[int], lo: float, hi: float) -> Callable[[float], None]:
        """Progress of one step, mapped to lo..hi of the item's progress. Clients learn of it through
        rev, bumped at most every _PROGRESS_EVERY seconds."""
        def report(x: float) -> None:
            with self._lock:
                m = self._items.get(mid)
                if m is None or (job is not None and self._jobs.get(mid) != job) or m.status != "processing":
                    return
                m.progress = round(lo + (hi - lo) * min(1.0, max(0.0, x)), 3)
                if time.monotonic() - self._last_bump >= _PROGRESS_EVERY:
                    self._bump()
        return report

    def _stage(self, mid: str, job: int, stage: str, progress: float) -> None:
        with self._lock:
            m = self._items.get(mid)
            if m is None or self._jobs.get(mid) != job:
                return
            m.stage, m.progress = stage, progress
            self._bump()

    # -- downloads
    def _download_worker(self) -> None:
        while True:
            task = self._downloads.get()
            with self._lock:
                closed = self._closed
            if closed or task is None:
                if closed:
                    return
                continue
            mid, job = task
            try:
                self._download(mid, job)
            except Exception as e:
                log.exception("download of %s crashed", mid)
                self._fail(mid, f"The download failed ({ingest.short_error(e)})", None)

    def _download(self, mid: str, job: dict) -> None:
        from .download import DownloadError
        with self._lock:
            m = self._items.get(mid)
            if m is None or mid in self._cancel:
                return
            url = m.source_url
            check = self._checker_any(mid)
            # One link at a time: the others stay "queued" until the worker gets to them.
            m.stage, m.progress = "downloading", 0.0
            self._bump()
        work = self.root / mid / "download"
        fetch = self._fetch or _default_fetcher()
        t0 = time.monotonic()
        try:
            got = fetch(url, work, max_height=job.get("max_height", config.IMPORT_HEIGHT), start=job.get("start", 0.0),
                        duration=job.get("duration"), progress=self._reporter(mid, None, 0.0, 1.0), check=check)
            check()
            kind = self._kind_of(got.path)
            size = got.path.stat().st_size
            orig = self._take_original(got.path, self.root / mid)
        except Cancelled:
            self._discard(work)
            return
        except (DownloadError, IngestError, ValueError, OSError) as e:
            log.info("download of %s (%s) failed: %s", mid, url, e)
            self._discard(work)
            self._fail(mid, str(e) or type(e).__name__, None)
            return
        self._discard(work)
        log.info("downloaded %s (%s) in %.1f s", mid, url, time.monotonic() - t0)
        with self._lock:
            m = self._items.get(mid)
            if m is None:
                return
            self._originals[mid] = orig
            self._imports.pop(mid, None)
            m.kind, m.size = kind, size
            if mid in self._auto_name and got.title:
                m.name = got.title.strip()[:120] or m.name
            if m.edit == MediaEdit() and (got.start > 0 or got.end is not None):
                m.edit = MediaEdit(start=round(got.start, 3), end=None if got.end is None else round(got.end, 3))
            m.stage, m.progress = "waiting", 0.0
            self._bump()
        self._save(mid)
        self._queue.put(mid)

    def _checker_any(self, mid: str) -> Callable[[], None]:
        """A download is cancelled only by a delete or shutdown (a new edit just waits for it)."""
        def check() -> None:
            with self._lock:
                if mid in self._cancel or self._closed:
                    raise Cancelled()
        return check

    # -- preparing
    def _next_version(self, mid: str, current: int) -> int:
        d = self.root / mid
        used = [int(v.group(1)) for p in d.iterdir() if (v := _VDIR.match(p.name))] if d.is_dir() else []
        return max([current, *used]) + 1

    def _ingest(self, mid: str) -> None:
        with self._lock:
            m = self._items.get(mid)
            orig = self._originals.get(mid)
            if m is None or mid in self._cancel or m.status != "processing" or orig is None:
                return                 # gone, done already (queued twice), or still downloading
            job = self._jobs.get(mid, 0)
            kind, edit, old_version = m.kind, m.edit.model_copy(deep=True), m.version
            m.stage, m.progress = "converting", 0.0
            self._bump()
        if not orig.is_file():
            self._fail(mid, "The uploaded file is missing", job)
            return
        check = self._checker(mid, job)
        d = self.root / mid
        version = self._next_version(mid, old_version)
        vdir = d / f"v{version}"
        t0 = time.monotonic()
        try:
            vdir.mkdir()
            pr = ingest.probe(orig)
            kind = pr.kind
            source = d / "source.mp4"
            need_source = kind == "video" and not source.is_file() and \
                (pr.animated or not ingest.browser_playable(orig, "video"))
            if kind == "image":
                res = ingest.ingest_image(orig, vdir, edit)
            elif pr.animated:
                res = ingest.ingest_animation(orig, vdir, check, edit, self._reporter(mid, job, 0.0, 1.0),
                                              source=source if need_source else None)
                need_source = False
            elif kind == "video":
                share = 0.6 if need_source else 1.0
                res = ingest.ingest_video(orig, vdir, check, edit, self._reporter(mid, job, 0.0, share))
                if need_source:
                    self._stage(mid, job, "preparing the crop view", share)
                    ingest.make_video_source(orig, source, check, self._reporter(mid, job, share, 1.0))
            else:
                res = ingest.ingest_audio(orig, vdir, check, edit)
            check()
        except Cancelled:
            self._discard(vdir)
            return
        except IngestError as e:
            log.info("ingest of %s (%s) failed: %s", mid, orig.name, e)
            self._discard(vdir)
            self._fail(mid, str(e), job)
            return
        except BaseException:
            self._discard(vdir)
            raise
        log.info("prepared %s (%s) as v%d in %.1f s", mid, orig.name, version, time.monotonic() - t0)
        with self._lock:
            m = self._items.get(mid)
            if m is None or self._jobs.get(mid) != job:
                stale = True
            else:
                stale = False
                old_version = m.version
                m.kind = kind
                m.status, m.error, m.progress, m.stage = "ready", None, 1.0, ""
                m.duration, m.width, m.height = res.duration, res.width, res.height
                m.fps, m.has_audio = res.fps, res.has_audio
                m.source_duration, m.source_width, m.source_height = \
                    res.source_duration, res.source_width, res.source_height
                m.version = version
                self._good_edit[mid] = edit
                self._browser.pop(mid, None)
                self._sources.pop(mid, None)
                self._bump()
        if stale:
            self._discard(vdir)
            return
        self._save(mid)
        if old_version > 0 and old_version != version:
            self._discard(d / f"v{old_version}")

    def _fail(self, mid: str, error: str, job: Optional[int]) -> None:
        """A step failed. If earlier prepared files exist they stay in use: the item stays ready with
        its previous edit and says what went wrong; else the item ends as an error."""
        with self._lock:
            m = self._items.get(mid)
            if m is None or (job is not None and self._jobs.get(mid) != job):
                return
            good = self._good_edit.get(mid)
            if m.version > 0 and good is not None:
                m.status, m.edit = "ready", good
                m.error = f"The new crop or trim could not be made: {error}"
            else:
                m.status, m.error = "error", error
            m.progress, m.stage = 0.0, ""
            self._bump()
        self._save(mid)

    def _choose_browser_file(self, mid: str, kind: str, orig: Optional[Path], vdir: Path) -> Optional[Path]:
        if orig is not None and orig.is_file() and ingest.browser_playable(orig, kind):
            return orig
        if kind == "video" and (self.root / mid / "source.mp4").is_file():
            return self.root / mid / "source.mp4"
        return {"video": vdir / "proxy.mp4", "image": vdir / "proxy.png", "audio": vdir / "audio.wav"}[kind]

    def _choose_source(self, mid: str, kind: str, orig: Optional[Path]) -> Optional[Path]:
        source = self.root / mid / "source.mp4"
        if source.is_file():
            return source
        if orig is not None and orig.is_file() and ingest.browser_playable(orig, kind):
            return orig
        return None

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


def _link_or_copy(src: Path, dest: Path) -> None:
    """A hard link (no second copy on disk; neither name is ever written to) or, where the file system
    has none (FAT, exFAT) or refuses, a copy."""
    try:
        os.link(src, dest)
    except OSError:
        shutil.copyfile(src, dest)


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
