import { useState } from 'react';
import { bundleUrl, uploadBundle } from '../api/client';
import type { BundleResult } from '../api/types';
import { mediaRes, setsRes, settingsRes } from '../state/app';
import { toast } from '../state/toast';
import { Dialog, Segmented } from './ui';

/** Saves one set, with the media it uses, as a zip. */
export function DownloadSet({ id }: { id: string }) {
  return <a className="btn" href={bundleUrl({ kind: 'sets', ids: [id] })} download title="Save this set and its media as a zip, to load on another TrafficLight">Download</a>;
}

/** Download all / everything, and upload: to move a show to another TrafficLight. */
export function BundleBar() {
  const [open, setOpen] = useState(false);
  return (
    <div className="row bundle-bar">
      <a className="btn" href={bundleUrl({ kind: 'all' })} download title="Every set and the media they use">Download all sets</a>
      <a className="btn" href={bundleUrl({ kind: 'everything' })} download
        title="Every set, every media item (used or not) and the schedule and light colours: to move the whole show to another computer">Download everything</a>
      <button onClick={() => setOpen(true)}>Upload…</button>
      {open && <UploadDialog onClose={() => setOpen(false)} />}
    </div>
  );
}

function UploadDialog({ onClose }: { onClose: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<'replace' | 'keep'>('replace');
  const [progress, setProgress] = useState<number | null>(null);
  const [result, setResult] = useState<BundleResult | null>(null);
  const busy = progress !== null && !result;

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!file) return;
    setProgress(0);
    try {
      const r = await uploadBundle(file, mode === 'replace', setProgress);
      setResult(r);
      void setsRes.refresh();
      void mediaRes.refresh();
      if (r.settings) void settingsRes.refresh();
    } catch (err) {
      toast.error(err);
      setProgress(null);
    }
  };

  return (
    <Dialog title="Upload sets" onClose={busy ? () => undefined : onClose}>
      {result ? (
        <>
          <ResultList r={result} />
          <div className="row end"><button className="primary" onClick={onClose} autoFocus>Done</button></div>
        </>
      ) : (
        <form className="form" onSubmit={submit}>
          <p className="muted small">A zip saved with Download on this or another TrafficLight. Its media come along, ready to play.</p>
          <input type="file" accept=".zip,application/zip" disabled={busy} onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
          <label>Sets and media that are here already
            <Segmented value={mode} onChange={setMode} options={[
              { value: 'replace', label: 'Replace with the upload' },
              { value: 'keep', label: 'Keep both' },
            ]} />
          </label>
          {progress !== null && (
            <>
              <div className="progress"><div style={{ width: `${Math.round(progress * 100)}%` }} /></div>
              <div className="small muted">{progress < 1 ? `Uploading… ${Math.round(progress * 100)}%` : 'Unpacking…'}</div>
            </>
          )}
          <div className="row end">
            <button type="button" onClick={onClose} disabled={busy}>Cancel</button>
            <button className="primary" disabled={!file || busy}>Upload</button>
          </div>
        </form>
      )}
    </Dialog>
  );
}

function ResultList({ r }: { r: BundleResult }) {
  const lines: [string, string[]][] = [
    ['New sets', r.sets_added],
    ['Replaced', r.sets_replaced],
    ['Added next to the set here', r.sets_copied],
    ['Sets already up to date', r.sets_unchanged],
    ['New media', r.media_added],
    ['Media updated', r.media_updated],
    ['Media already here', r.media_unchanged],
    ['Media that failed', r.media_failed],
  ];
  const shown = lines.filter(([, l]) => l.length > 0);
  return (
    <div className="bundle-result">
      {shown.length === 0 && <p className="muted">The zip had no sets or media in it.</p>}
      {shown.map(([label, list]) => (
        <div key={label} className={label === 'Media that failed' ? 'err' : ''}>
          <strong>{label} ({list.length}):</strong> <span className="muted">{list.join(', ')}</span>
        </div>
      ))}
      {r.settings && <div><strong>Schedule and light colours</strong> <span className="muted">taken from the upload</span></div>}
    </div>
  );
}
