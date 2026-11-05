import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ApiError, api, mediaFileUrl, mediaThumbUrl, uploadMediaWithProgress } from '../api/client';
import type { MediaItem, ShowTarget } from '../api/types';
import { Dialog, Empty, Toggle } from '../components/ui';
import { editNote, openCropTool } from '../media/CropTool';
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
              {m.status === 'processing' ? (
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

function stageText(m: MediaItem): string {
  const s = m.stage || 'waiting';
  const words: Record<string, string> = {
    waiting: 'Waiting', downloading: 'Downloading', converting: m.version > 0 ? 'Making the new crop' : 'Converting',
    'preparing the crop view': 'Preparing the crop view',
  };
  return `${words[s] ?? s[0].toUpperCase() + s.slice(1)}…`;
}

const QUALITIES = [360, 480, 720, 1080, 1440, 2160];

/** Fetch a video from a link (YouTube and the like, or a direct file link). */
function ImportForm({ onImported }: { onImported: (m: MediaItem) => void }) {
  const [url, setUrl] = useState('');
  const [more, setMore] = useState(false);
  const [quality, setQuality] = useState(1080);
  const [start, setStart] = useState('');
  const [length, setLength] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const u = url.trim();
    if (!u) return;
    setBusy(true);
    const s = parseFloat(start);
    const l = parseFloat(length);
    const m = await attempt(() => api.importMedia({
      url: u, max_height: quality, start: isFinite(s) && s > 0 ? s : 0, duration: isFinite(l) && l > 0 ? l : null,
    }));
    setBusy(false);
    if (m) {
      setUrl('');
      onImported(m);
    }
  };
  return (
    <form className="card import-form" onSubmit={submit}>
      <div className="import-row">
        <input type="url" inputMode="url" placeholder="Paste a video link (YouTube, a file…)" value={url} onChange={(e) => setUrl(e.target.value)}
          aria-label="Video link" data-testid="import-url" />
        <button className="primary" type="submit" disabled={busy || !url.trim()} data-testid="import-go">{busy ? 'Starting…' : 'Import'}</button>
      </div>
      <button type="button" className="link-btn small" onClick={() => setMore(!more)} aria-expanded={more}>{more ? 'Fewer options' : 'Options: quality, part of the video'}</button>
      {more && (
        <div className="import-opts">
          <label>Up to
            <select value={quality} onChange={(e) => setQuality(Number(e.target.value))}>
              {QUALITIES.map((q) => <option key={q} value={q}>{q}p</option>)}
            </select>
          </label>
          <label>From (s) <input type="number" min={0} step={0.1} value={start} onChange={(e) => setStart(e.target.value)} placeholder="0" /></label>
          <label>Length (s) <input type="number" min={0} step={0.1} value={length} onChange={(e) => setLength(e.target.value)} placeholder="all" /></label>
          <span className="muted small">Only that part is downloaded; it becomes the trim, which the crop tool can change within it.</span>
        </div>
      )}
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
