import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ApiError, api, mediaFileUrl, mediaThumbUrl, uploadMediaWithProgress } from '../api/client';
import type { MediaItem, ShowTarget } from '../api/types';
import { Dialog, Empty, Toggle } from '../components/ui';
import { mediaRes, useAppState, useMedia } from '../state/app';
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
  const state = useAppState();
  const nav = useNavigate();
  const [uploads, setUploads] = useState<Upload[]>([]);
  const [over, setOver] = useState(false);
  const [preview, setPreview] = useState<MediaItem | null>(null);
  const [renaming, setRenaming] = useState<MediaItem | null>(null);
  const [use, setUse] = useState<{ item: MediaItem; mode: 'show' | 'make' } | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const keyN = useRef(0);

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
        .then(() => {
          patch({ progress: 1, done: true });
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

  return (
    <div className="page">
      <div className="page-head">
        <h1>Media</h1>
        <button className="primary" onClick={() => input.current?.click()}>Upload files</button>
      </div>
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
        {media?.map((m) => (
          <div key={m.id} className="card media-card">
            <div className="media-thumb" onClick={() => m.status === 'ready' && setPreview(m)}>
              {m.status === 'ready' ? <img src={mediaThumbUrl(m.id, state?.media_rev)} alt="" loading="lazy" /> : m.status === 'processing' ? <div className="spinner" /> : <div className="err small pad">Failed</div>}
              <span className="kind-tag">{m.kind}</span>
            </div>
            <div className="name ellipsis" title={m.name}>{m.name}</div>
            <div className="small muted">
              {m.status === 'processing' ? 'Processing…' : m.status === 'error' ? <span className="err">{m.error ?? 'Error'}</span> : (
                [m.kind !== 'image' && formatTime(m.duration), m.width ? `${m.width}×${m.height}` : '', formatSize(m.size)].filter(Boolean).join(' · ')
              )}
            </div>
            <div className="card-actions">
              {m.status === 'ready' && <button onClick={() => setPreview(m)}>Preview</button>}
              {m.status === 'ready' && m.kind !== 'audio' && <button className="primary" onClick={() => setUse({ item: m, mode: 'show' })}>Play now on…</button>}
              {m.status === 'ready' && m.kind !== 'audio' && <button onClick={() => setUse({ item: m, mode: 'make' })}>Make a set</button>}
              <button onClick={() => setRenaming(m)}>Rename</button>
              <button className="danger-text" onClick={() => remove(m)}>Delete</button>
            </div>
          </div>
        ))}
      </div>

      {preview && <PreviewDialog item={preview} onClose={() => setPreview(null)} />}
      {renaming && <NameDialog title="Rename" initial={renaming.name} onClose={() => setRenaming(null)} onSubmit={(n) => { void rename(renaming, n); setRenaming(null); }} />}
      {use && <UseDialog item={use.item} mode={use.mode} onClose={() => setUse(null)} onMade={(id) => nav(`/editor/${id}`)} />}
    </div>
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
