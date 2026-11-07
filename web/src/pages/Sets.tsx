import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, renderUrl } from '../api/client';
import type { SetSummary } from '../api/types';
import { BundleBar, DownloadSet } from '../components/Bundles';
import { Dialog, Empty, Toggle } from '../components/ui';
import { patchSettings, setsRes, useAppState, useSets, useSettings } from '../state/app';
import { confirmDialog } from '../state/confirm';
import { formatTime } from '../state/hooks';
import { attempt } from '../state/toast';

export default function SetsPage() {
  const sets = useSets();
  const settings = useSettings();
  const state = useAppState();
  const nav = useNavigate();
  const [order, setOrder] = useState<SetSummary[]>([]);
  const [dragging, setDragging] = useState<string | null>(null);
  const [dialog, setDialog] = useState<null | { kind: 'create' } | { kind: 'rename'; set: SetSummary }>(null);
  const rows = useRef(new Map<string, HTMLElement>());
  const orderRef = useRef(order);
  orderRef.current = order;

  useEffect(() => {
    if (!dragging && sets) setOrder(sets);
  }, [sets, dragging]);
  useEffect(() => void setsRes.refresh(), []);

  const defaultId = settings?.schedule.default_set ?? null;
  const rev = state?.sets_rev;

  // Pointer-based drag so it works with touch as well as the mouse.
  const startDrag = (id: string, ev: React.PointerEvent) => {
    ev.preventDefault();
    (ev.target as HTMLElement).setPointerCapture(ev.pointerId);
    setDragging(id);
  };
  const moveDrag = (ev: React.PointerEvent) => {
    if (!dragging) return;
    const cur = orderRef.current;
    const from = cur.findIndex((s) => s.id === dragging);
    for (let i = 0; i < cur.length; i++) {
      const el = rows.current.get(cur[i].id);
      if (!el) continue;
      const r = el.getBoundingClientRect();
      if (ev.clientY >= r.top && ev.clientY <= r.bottom) {
        if (i !== from) {
          const next = cur.slice();
          const [m] = next.splice(from, 1);
          next.splice(i, 0, m);
          setOrder(next);
        }
        break;
      }
    }
  };
  const endDrag = async () => {
    if (!dragging) return;
    setDragging(null);
    const ids = orderRef.current.map((s) => s.id);
    if (sets && ids.join() !== sets.map((s) => s.id).join()) {
      await attempt(() => api.orderSets(ids));
      void setsRes.refresh();
    }
  };

  const doRename = async (s: SetSummary, name: string) => {
    await attempt(async () => {
      const full = await api.getSet(s.id);
      await api.putSet({ ...full, name });
    });
    void setsRes.refresh();
  };
  const toggleRotation = async (s: SetSummary, on: boolean) => {
    await attempt(async () => {
      const full = await api.getSet(s.id);
      await api.putSet({ ...full, in_rotation: on });
    });
    void setsRes.refresh();
  };
  const remove = async (s: SetSummary) => {
    if (!(await confirmDialog(`Delete "${s.name}"?`, { text: 'The set and its timeline are removed for good.', action: 'Delete', danger: true }))) return;
    await attempt(() => api.deleteSet(s.id));
    void setsRes.refresh();
  };
  const duplicate = async (s: SetSummary) => {
    await attempt(() => api.duplicateSet(s.id));
    void setsRes.refresh();
  };

  return (
    <div className="page">
      <div className="page-head">
        <h1>Sets</h1>
        <button className="primary" onClick={() => setDialog({ kind: 'create' })}>New set</button>
      </div>
      <p className="muted small">Sets play in this order. Drag the handle to reorder.</p>
      <BundleBar />
      {sets && sets.length === 0 && <Empty>No sets yet.</Empty>}
      <ul className="plain-list set-list">
        {order.map((s) => (
          <li key={s.id} ref={(el) => { if (el) rows.current.set(s.id, el); else rows.current.delete(s.id); }}
            className={`card set-row ${dragging === s.id ? 'dragging' : ''}`}>
            <div className="handle" title="Drag to reorder" onPointerDown={(e) => startDrag(s.id, e)} onPointerMove={moveDrag} onPointerUp={endDrag} onPointerCancel={endDrag}>⠿</div>
            <img className="thumb-set" src={renderUrl(s.id, 0, rev)} alt="" loading="lazy" />
            <div className="set-main">
              <div className="name">{s.name}{defaultId === s.id && <span className="badge">default</span>}</div>
              <div className="small muted">{formatTime(s.duration)} · {s.bpm} bpm</div>
              <div className="set-actions">
                <button className="primary" onClick={() => nav(`/editor/${s.id}`)}>Edit</button>
                <button onClick={() => attempt(() => api.showSet(s.id))}>Show now</button>
                <button onClick={() => patchSettings({ schedule: { default_set: defaultId === s.id ? null : s.id } })}>{defaultId === s.id ? 'Clear default' : 'Make default'}</button>
                <button onClick={() => setDialog({ kind: 'rename', set: s })}>Rename</button>
                <button onClick={() => duplicate(s)}>Duplicate</button>
                <DownloadSet id={s.id} />
                <button className="danger-text" onClick={() => remove(s)}>Delete</button>
              </div>
              <label className="inline-field"><Toggle label="In rotation" checked={s.in_rotation} onChange={(v) => toggleRotation(s, v)} /> In rotation</label>
            </div>
          </li>
        ))}
      </ul>

      {dialog?.kind === 'create' && <CreateDialog onClose={() => setDialog(null)} onCreated={() => void setsRes.refresh()} />}
      {dialog?.kind === 'rename' && (
        <NameDialog title="Rename set" initial={dialog.set.name} onClose={() => setDialog(null)} onSubmit={(n) => { setDialog(null); void doRename(dialog.set, n); }} />
      )}
    </div>
  );
}

function CreateDialog({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState('New set');
  const [bpm, setBpm] = useState(120);
  const [duration, setDuration] = useState(30);
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    const s = await attempt(() => api.createSet({ name: name.trim() || 'Untitled', bpm, duration }));
    setBusy(false);
    if (s) {
      onCreated();
      onClose();
    }
  };
  return (
    <Dialog title="New set" onClose={onClose}>
      <form onSubmit={submit} className="form">
        <label>Name<input value={name} onChange={(e) => setName(e.target.value)} autoFocus /></label>
        <label>Tempo (bpm)<input type="number" min={10} max={400} value={bpm} onChange={(e) => setBpm(Number(e.target.value))} /></label>
        <label>Duration (seconds)<input type="number" min={1} step={1} value={duration} onChange={(e) => setDuration(Number(e.target.value))} /></label>
        <div className="row end"><button type="button" onClick={onClose}>Cancel</button><button className="primary" disabled={busy}>Create</button></div>
      </form>
    </Dialog>
  );
}

export function NameDialog({ title, initial, onClose, onSubmit }: { title: string; initial: string; onClose: () => void; onSubmit: (name: string) => void }) {
  const [name, setName] = useState(initial);
  return (
    <Dialog title={title} onClose={onClose}>
      <form className="form" onSubmit={(e) => { e.preventDefault(); if (name.trim()) onSubmit(name.trim()); }}>
        <label>Name<input value={name} onChange={(e) => setName(e.target.value)} autoFocus onFocus={(e) => e.target.select()} /></label>
        <div className="row end"><button type="button" onClick={onClose}>Cancel</button><button className="primary">Save</button></div>
      </form>
    </Dialog>
  );
}
