import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ApiError, api, mediaFileUrl, mediaThumbUrl, uploadMediaWithProgress } from '../api/client';
import type { MediaItem, ShowTarget } from '../api/types';
import { Dialog, Empty, Toggle } from '../components/ui';
import { editNote, openCropTool } from '../media/CropTool';
import { parseClock } from '../media/clocktime';
import { mediaRes, useMedia } from '../state/app';
import { confirmDialog } from '../state/confirm';
import { formatSize, formatTime } from '../state/hooks';
import { attempt, toast } from '../state/toast';
import { NameDialog } from './Sets';

interface Upload {
  key: number;
  name: string;
  progress: number;
  error?: string;
  done?: boolean;
}

const TARGETS: { value: ShowTarget; label: string }[] = [
  { value: 'face0', label: 'Top light' },
  { value: 'face1', label: 'Middle light' },
  { value: 'face2', label: 'Bottom light' },
  { value: 'all', label: 'Whole sign' },
  { value: 'faces', label: 'All three lights' },
];

export default function MediaPage() {
  const media = useMedia();
  const nav = useNavigate();
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [over, setOver] = useState(false);
  const [preview, setPreview] = useState<MediaItem | null>(null);
  const [renaming, setRenaming] = useState<MediaItem | null>(null);
  const [use, setUse] = useState<{ item: MediaItem; mode: 'show' | 'make' } | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const keyN = useRef(0);
  /** Items uploaded or imported here: once ready, their card offers the crop tool. */
  const [fresh, setFresh] = useState<Set<string>>(new Set());
  const addFresh = (id: string) => setFresh((f) => new Set(f).add(id));
  const dropFresh = (id: string) => setFresh((f) => {
    const n = new Set(f);
    n.delete(id);
    return n;
  });

  useEffect(() => void mediaRes.refresh(), []);

  // Poll while something is processing (media_rev also moves, this is a safety net).
  const processing = media?.some((m) => m.status === 'processing') ?? false;
  useEffect(() => {
    if (!processing) return;
    const t = window.setInterval(() => void mediaRes.refresh(), 1500);
    return () => window.clearInterval(t);
  }, [processing]);

  const upload = (files: FileList | File[]) => {
    for (const file of Array.from(files)) {
      const key = ++keyN.current;
      const patch = (p: Partial<Upload>) => setUploads((u) => u.map((x) => (x.key === key ? { ...x, ...p } : x)));
      setUploads((u) => [...u, { key, name: file.name, progress: 0 }]);
      uploadMediaWithProgress(file, (f) => patch({ progress: f }))
        .then((m) => {
          patch({ progress: 1, done: true });
          if (m?.id) addFresh(m.id);
          void mediaRes.refresh();
          window.setTimeout(() => setUploads((u) => u.filter((x) => x.key !== key)), 2500);
        })
        .catch((e: Error) => patch({ error: e.message }));
    }
  };

  const remove = async (m: MediaItem) => {
    if (!(await confirmDialog(`Delete "${m.name}"?`, { action: 'Delete', danger: true }))) return;
    try {
      await api.deleteMedia(m.id);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        const names = describeUsers(e.detail) ?? e.message;
        if (await confirmDialog('This item is used by sets', { text: `${names}. Delete it anyway? Those clips will stop working.`, action: 'Delete anyway', danger: true })) {
          await attempt(() => api.deleteMedia(m.id, true));
        }
      } else toast.error(e);
    }
    void mediaRes.refresh();
  };

  const rename = async (m: MediaItem, name: string) => {
    await attempt(() => api.renameMedia(m.id, name));
    void mediaRes.refresh();
  };

  /** A failed item (a link that could not be fetched): the server tries again, with the same settings. */
  const retry = async (m: MediaItem) => {
    if (await attempt(() => api.editMedia(m.id, m.edit))) addFresh(m.id);
    void mediaRes.refresh();
  };
  const crop = (m: MediaItem) => {
    dropFresh(m.id);
    openCropTool(m, () => void mediaRes.refresh());
  };

  return (
    <div className="page">
      <div className="page-head">
        <h1>Media</h1>
        <button className="primary" onClick={() => input.current?.click()}>Upload files</button>
      </div>
      <ImportForm onImported={(m) => { addFresh(m.id); void mediaRes.refresh(); }} />
      <input ref={input} type="file" multiple hidden accept="video/*,image/*,audio/*,.gif" onChange={(e) => { if (e.target.files) upload(e.target.files); e.target.value = ''; }} />
      <div className={`dropzone ${over ? 'over' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); if (e.dataTransfer.files.length) upload(e.dataTransfer.files); }}
        onClick={() => input.current?.click()}>
        Drop videos, images or music here, or tap to choose files
      </div>

      {uploads.length > 0 && (
        <ul className="plain-list uploads">
          {uploads.map((u) => (
            <li key={u.key} className="card upload">
              <div className="grow">
                <div className="name">{u.name}</div>
                <div className="progress"><div className={u.error ? 'bad' : ''} style={{ width: `${u.progress * 100}%` }} /></div>
                <div className={`small ${u.error ? 'err' : 'muted'}`}>{u.error ?? (u.done ? 'Uploaded' : `Uploading ${Math.round(u.progress * 100)}%`)}</div>
              </div>
              {u.error && <button onClick={() => setUploads((x) => x.filter((y) => y.key !== u.key))}>Dismiss</button>}
            </li>
          ))}
        </ul>
      )}

      {media && media.length === 0 && uploads.length === 0 && <Empty>The library is empty.</Empty>}
      <div className="media-grid">
        {media?.map((m) => {
          const ahead = queuedAhead(media, m);
          const visual = m.kind !== 'audio';
          const playable = m.status === 'ready' || (m.status === 'processing' && m.version > 0);
          const note = editNote(m);
          return (
            <div key={m.id} className={`card media-card ${fresh.has(m.id) && m.status === 'ready' && visual ? 'fresh' : ''}`} data-media-card={m.id}>
              <div className="media-thumb" onClick={() => playable && setPreview(m)}>
                {m.version > 0 ? <img src={mediaThumbUrl(m.id, m.version)} alt="" loading="lazy" /> : m.status === 'processing' ? <div className="spinner" /> : <div className="err small pad">Failed</div>}
                <span className="kind-tag">{m.kind}</span>
                {m.status === 'processing' && m.version > 0 && <span className="thumb-busy"><span className="spinner small" /></span>}
              </div>
              <div className="name ellipsis" title={m.name}>{m.name}</div>
              {note && <div className="edit-note" data-testid="edit-note">{note}</div>}
              {m.status === 'processing' && ahead !== null ? (
                <div className="media-progress" data-testid="media-queued">
                  <div className="small muted">{ahead === 0 ? 'Next to download' : `Queued: ${ahead} ${ahead === 1 ? 'download' : 'downloads'} before it`}</div>
                </div>
              ) : m.status === 'processing' ? (
                <div className="media-progress" data-testid="media-progress">
                  <div className="progress"><div style={{ width: `${Math.round(m.progress * 100)}%` }} /></div>
                  <div className="small muted">{stageText(m)} {Math.round(m.progress * 100)}%</div>
                </div>
              ) : (
                <div className="small muted">
                  {m.status === 'error' ? <span className="err">{m.error ?? 'Error'}</span> : (
                    [m.kind !== 'image' && formatTime(m.duration), m.width ? `${m.width}×${m.height}` : '', formatSize(m.size)].filter(Boolean).join(' · ')
                  )}
                </div>
              )}
              {m.status === 'ready' && m.error && <div className="small err">{m.error}</div>}
              {fresh.has(m.id) && m.status === 'ready' && visual && (
                <div className="fresh-offer" role="status">
                  <span className="small">Ready. Crop it for one light or the whole sign?</span>
                  <div className="row">
                    <button className="primary" onClick={() => crop(m)}>Crop &amp; trim</button>
                    <button onClick={() => dropFresh(m.id)}>Not now</button>
                  </div>
                </div>
              )}
              <div className="card-actions">
                {playable && visual && !(fresh.has(m.id) && m.status === 'ready') && <button onClick={() => crop(m)} data-testid="crop-open">Crop &amp; trim</button>}
                {playable && <button onClick={() => setPreview(m)}>Preview</button>}
                {playable && visual && <button className="primary" onClick={() => setUse({ item: m, mode: 'show' })}>Play now on…</button>}
                {playable && visual && <button onClick={() => setUse({ item: m, mode: 'make' })}>Make a set</button>}
                {m.status === 'error' && <button onClick={() => retry(m)}>Try again</button>}
                <button onClick={() => setRenaming(m)}>Rename</button>
                <button className="danger-text" onClick={() => remove(m)}>Delete</button>
              </div>
            </div>
          );
        })}
      </div>

      {preview && <PreviewDialog item={preview} onClose={() => setPreview(null)} />}
      {renaming && <NameDialog title="Rename" initial={renaming.name} onClose={() => setRenaming(null)} onSubmit={(n) => { void rename(renaming, n); setRenaming(null); }} />}
      {use && <UseDialog item={use.item} mode={use.mode} onClose={() => setUse(null)} onMade={(id) => nav(`/editor/${id}`)} />}
    </div>
  );
}

/** For a link waiting its turn: how many downloads come before it (the server fetches one link at a time, in the
 * order they were added); null for anything else. */
function queuedAhead(all: MediaItem[], m: MediaItem): number | null {
  if (m.status !== 'processing' || m.stage !== 'queued') return null;
  const busy = all.some((x) => x.status === 'processing' && x.stage === 'downloading') ? 1 : 0;
  return busy + all.filter((x) => x.status === 'processing' && x.stage === 'queued' && (x.added < m.added || (x.added === m.added && x.id < m.id))).length;
}

function stageText(m: MediaItem): string {
  const s = m.stage || 'waiting';
  const words: Record<string, string> = {
    waiting: 'Waiting', downloading: 'Downloading', converting: m.version > 0 ? 'Making the new crop' : 'Converting',
    'preparing the crop view': 'Preparing the crop view',
  };
  return `${words[s] ?? s[0].toUpperCase() + s.slice(1)}…`;
}

const QUALITIES = [360, 480, 720, 1080, 1440, 2160];

/** config.IMPORT_HEIGHT: 480 lines cover even a whole-sign crop of the full picture height. */
const DEFAULT_QUALITY = 480;

// v2: the default dropped from 1080p to 480p; a quality remembered under the old key was most likely just the old
// default, so only the length limit is carried over from it.
const IMPORT_PREF = 'trafficlight.import.v2';
const OLD_IMPORT_PREF = 'trafficlight.import';
/** Quality and length limit of the last import: someone who wants 3 minutes of a 3 hour video wants that every time. */
function readImportPref(): { quality: number; length: string } {
  try {
    const v = JSON.parse(localStorage.getItem(IMPORT_PREF) ?? 'null');
    if (v) return { quality: QUALITIES.includes(v.quality) ? v.quality : DEFAULT_QUALITY, length: typeof v.length === 'string' ? v.length : '' };
    const old = JSON.parse(localStorage.getItem(OLD_IMPORT_PREF) ?? '{}') ?? {};
    return { quality: DEFAULT_QUALITY, length: typeof old.length === 'string' ? old.length : '' };
  } catch {
    return { quality: DEFAULT_QUALITY, length: '' };
  }
}

/** Asks "are you sure?" when the part of the video that would be downloaded is longer than the server's limit
 * (config.IMPORT_WARN_SECONDS). True to go ahead; also when the length cannot be found out beforehand. */
async function longVideoOk(url: string, start: number, maxLength: number | null): Promise<boolean> {
  let p;
  try {
    p = await api.probeMedia(url);
  } catch {
    return true;                        // the import itself says what is wrong with the link
  }
  if (p.duration === null) return true;
  const rest = Math.max(0, p.duration - start);
  const get = maxLength === null ? rest : Math.min(rest, maxLength);
  if (get <= p.warn_over) return true;
  const part = get < p.duration - 0.5 ? `${formatTime(get)} of it` : 'all of it';
  return confirmDialog(`This video is ${formatTime(p.duration)} long`, {
    text: `${p.title ? `"${p.title}": ` : ''}that downloads ${part}, which takes a while and fills the disk. `
      + 'To take only part, cancel and set a Max length (and Start at). Download it anyway?',
    action: 'Download anyway',
  });
}

/** Fetch a video from a link (YouTube and the like, or a direct file link). */
function ImportForm({ onImported }: { onImported: (m: MediaItem) => void }) {
  const [url, setUrl] = useState('');
  const [quality, setQuality] = useState(() => readImportPref().quality);
  const [start, setStart] = useState('');
  const [length, setLength] = useState(() => readImportPref().length);
  const [busy, setBusy] = useState<false | 'checking' | 'starting'>(false);
  const s = parseClock(start);
  const l = parseClock(length);
  const badStart = s !== null && isNaN(s);
  const badLength = l !== null && (isNaN(l) || l <= 0);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const u = url.trim();
    if (!u || badStart || badLength) return;
    setBusy('checking');
    if (!(await longVideoOk(u, s ?? 0, l))) {
      setBusy(false);
      return;
    }
    setBusy('starting');
    try {
      localStorage.setItem(IMPORT_PREF, JSON.stringify({ quality, length: length.trim() }));
    } catch {
      /* ignore */
    }
    const m = await attempt(() => api.importMedia({ url: u, max_height: quality, start: s ?? 0, duration: l }));
    setBusy(false);
    if (m) {
      // A link typed in while this one was being checked stays for the next import.
      setUrl((cur) => (cur.trim() === u ? '' : cur));
      setStart('');
      onImported(m);
    }
  };
  return (
    <form className="card import-form" onSubmit={submit}>
      <div className="import-row">
        <input type="url" inputMode="url" placeholder="Paste a video link (YouTube, a file…)" value={url} onChange={(e) => setUrl(e.target.value)}
          aria-label="Video link" data-testid="import-url" />
        <button className="primary" type="submit" disabled={Boolean(busy) || !url.trim() || badStart || badLength} data-testid="import-go">{busy === 'checking' ? 'Checking…' : busy ? 'Starting…' : 'Import'}</button>
      </div>
      <div className="import-opts">
        <label>Max length
          <input type="text" inputMode="decimal" value={length} onChange={(e) => setLength(e.target.value)} placeholder="whole video"
            aria-invalid={badLength} title="Download no more than this: 3:00, 90 or 1:30:00. Empty: the whole video." data-testid="import-length" />
        </label>
        <label>Start at
          <input type="text" inputMode="decimal" value={start} onChange={(e) => setStart(e.target.value)} placeholder="0:00"
            aria-invalid={badStart} title="Where in the video to start: 1:30 or 90" data-testid="import-start" />
        </label>
        <label>Up to
          <select value={quality} onChange={(e) => setQuality(Number(e.target.value))} data-testid="import-quality"
            title="480p is plenty for the sign (128×384). Pick more only to crop into a small part of the picture.">
            {QUALITIES.map((q) => <option key={q} value={q}>{q}p</option>)}
          </select>
        </label>
        {badStart || badLength
          ? <span className="error small" role="alert">Write times as 3:00, 90 or 1:30:00</span>
          : <span className="muted small">{l ? `Only ${formatTime(l)} from ${formatTime(s ?? 0)} is downloaded.` : 'Set a max length to download only part of a long video.'}</span>}
      </div>
    </form>
  );
}

/** The 409 detail is specified only as "the names of the sets that use it": accept a string, a list or {sets:[...]}. */
function describeUsers(detail: unknown): string | null {
  const names = (v: unknown): string[] =>
    Array.isArray(v) ? v.map((x) => (typeof x === 'string' ? x : (x as { name?: string })?.name ?? '')).filter(Boolean) : [];
  if (typeof detail === 'string') return detail;
  const list = names(detail).length ? names(detail) : names((detail as { sets?: unknown })?.sets);
  return list.length ? `Used by: ${list.join(', ')}` : null;
}

function PreviewDialog({ item, onClose }: { item: MediaItem; onClose: () => void }) {
  // the original as it was uploaded; the card's thumbnail shows the crop
  const url = mediaFileUrl(item.id);
  return (
    <Dialog title={item.name} onClose={onClose} wide>
      <div className="preview-box">
        {item.kind === 'video' && <video src={url} controls autoPlay loop playsInline />}
        {item.kind === 'audio' && <audio src={url} controls autoPlay />}
        {item.kind === 'image' && <img src={url} alt={item.name} />}
      </div>
      <div className="row end"><button onClick={onClose}>Close</button></div>
    </Dialog>
  );
}

function UseDialog({ item, mode, onClose, onMade }: { item: MediaItem; mode: 'show' | 'make'; onClose: () => void; onMade: (setId: string) => void }) {
  const [loop, setLoop] = useState(true);
  const go = async (target: ShowTarget) => {
    if (mode === 'show') {
      if (await attempt(() => api.showMedia(item.id, target, loop))) {
        toast.info('Playing on the sign');
        onClose();
      }
    } else {
      const s = await attempt(() => api.makeSetFromMedia(item.id, target, loop));
      if (s) {
        onClose();
        onMade(s.id);
      }
    }
  };
  return (
    <Dialog title={mode === 'show' ? `Play "${item.name}" now on` : `Make a set from "${item.name}" on`} onClose={onClose}>
      <div className="target-grid">
        {TARGETS.map((t) => <button key={t.value} onClick={() => go(t.value)}>{t.label}</button>)}
      </div>
      <label className="inline-field"><Toggle label="Loop" checked={loop} onChange={setLoop} /> Loop</label>
      <div className="row end"><button onClick={onClose}>Cancel</button></div>
    </Dialog>
  );
}
