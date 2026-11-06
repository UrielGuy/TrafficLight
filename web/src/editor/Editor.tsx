// The timeline editor page (/editor/:id): loads the set, opens the /ws/editor
// session, and lays out preview, library, inspector, transport and timeline.
import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api } from '../api/client';
import SignPreview from '../components/SignPreview';
import { Dialog } from '../components/ui';
import { confirmDialog } from '../state/confirm';
import { useStore } from '../state/store';
import {
  copySelection, cutSelection, deleteSelection, duplicateSelection, nudge, paste, save, selectAll, splitAtPlayhead,
} from './actions';
import { BEATS_PER_BAR, gridLines, secondsPerBeat } from './beats';
import {
  clearBackup, docStore, draft, isDirty, loadDoc, readBackup, redo, select, setInsert, undo, useDoc, writeBackup, type Backup,
} from './doc';
import { Icon } from './icons';
import Inspector from './Inspector';
import Palette from './Palette';
import { clock, EditorSession, editorFeed, loopStore, problemListStore, sessionStore } from './session';
import Timeline, { Popover } from './Timeline';
import { findClip, placeName } from './model';
import Transport from './Transport';
import { setSnap, timelineApi, viewStore } from './view';
import './editor.css';

function useMediaQuery(q: string): boolean {
  const [m, setM] = useState(() => window.matchMedia(q).matches);
  useEffect(() => {
    const mq = window.matchMedia(q);
    const on = () => setM(mq.matches);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, [q]);
  return m;
}

/** Keys typed into a field belong to the field. */
function isTyping(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  if (!el || !el.tagName) return false;
  if (el.isContentEditable) return true;
  if (el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') return true;
  if (el.tagName === 'INPUT') {
    const type = (el as HTMLInputElement).type;
    return !['checkbox', 'radio', 'button', 'submit', 'color'].includes(type);
  }
  return false;
}

export default function Editor() {
  const { id = '' } = useParams();
  const [phase, setPhase] = useState<'loading' | 'ready' | 'error'>('loading');
  const [error, setError] = useState('');
  const [recovery, setRecovery] = useState<Backup | null>(null);
  const [session, setSession] = useState<EditorSession | null>(null);

  useEffect(() => {
    let alive = true;
    setPhase('loading');
    api.getSet(id).then(
      (set) => {
        if (!alive) return;
        loadDoc(set);
        loopStore.set({ on: true, from: 0, to: null });
        const b = readBackup(id);
        if (b && JSON.stringify(b.set) !== JSON.stringify(set)) setRecovery(b);
        else if (b) clearBackup(id);
        setPhase('ready');
      },
      (e: Error) => {
        if (!alive) return;
        setError(e.message);
        setPhase('error');
      },
    );
    return () => {
      alive = false;
    };
  }, [id]);

  // The editor session follows the draft; the local backup follows unsaved changes.
  useEffect(() => {
    if (phase !== 'ready') return;
    const s = new EditorSession();
    setSession(s);
    s.setDraft(draft());
    let backupTimer: number | undefined;
    let prev = draft();
    const unsub = docStore.subscribe(() => {
      const d = docStore.get();
      if (!d || d.history.present === prev) return;
      prev = d.history.present;
      s.setDraft(d.history.present);
      window.clearTimeout(backupTimer);
      backupTimer = window.setTimeout(() => {
        const cur = docStore.get();
        if (!cur) return;
        if (isDirty(cur)) writeBackup(cur.history.present, cur.saved);
        else clearBackup(cur.saved.id);
      }, 400);
    });
    return () => {
      unsub();
      window.clearTimeout(backupTimer);
      // flush the backup now: the page may be going away
      const cur = docStore.get();
      if (cur && isDirty(cur)) writeBackup(cur.history.present, cur.saved);
      s.close();
      setSession(null);
    };
  }, [phase]);

  if (phase === 'loading') return <div className="editor-msg muted">Loading…</div>;
  if (phase === 'error' || !session) {
    return (
      <div className="editor-msg">
        {phase === 'error' && <p>Could not open the set: {error}</p>}
        <Link to="/sets" className="btn">Back to sets</Link>
      </div>
    );
  }
  return (
    <>
      <EditorView session={session} />
      {recovery && (
        <Dialog title="Recover unsaved changes?" onClose={() => { clearBackup(id); setRecovery(null); }}>
          <p className="muted">
            This browser kept changes to this set that were never saved (last edit {new Date(recovery.at).toLocaleString()}).
            {recovery.base !== JSON.stringify(docStore.get()?.saved) && ' The saved set has changed since then; recovering replaces it in the editor (nothing is saved until you press Save).'}
          </p>
          <div className="row end">
            <button onClick={() => { clearBackup(id); setRecovery(null); }}>Discard them</button>
            <button className="primary" autoFocus data-testid="recover" onClick={() => {
              const saved = docStore.get()!.saved;
              loadDoc(recovery.set, saved);
              setRecovery(null);
            }}>Recover</button>
          </div>
        </Dialog>
      )}
    </>
  );
}

function PreviewBox() {
  const box = useRef<HTMLDivElement>(null);
  const [h, setH] = useState(300);
  useEffect(() => {
    const el = box.current!;
    const ro = new ResizeObserver(() => {
      // the housing adds ~26 px around the canvas; the canvas is 1:3
      const byH = el.clientHeight - 26;
      const byW = (el.clientWidth - 20) * 3;
      setH(Math.max(90, Math.min(byH, byW)));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return (
    <div className="ed-preview" ref={box}>
      <SignPreview feed={editorFeed} height={h} />
    </div>
  );
}

function ProblemsButton() {
  const problems = useStore(problemListStore);
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  if (!problems.length) return null;
  return (
    <>
      <button className="ed-problems" data-pop-toggle data-testid="problems" onClick={(e) => {
        const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
        setMenu(menu ? null : { x: r.left, y: r.bottom + 4 });
      }}>
        <Icon name="warn" size={16} /> {problems.length}<span className="hide-phone"> problem{problems.length > 1 ? 's' : ''}</span>
      </button>
      {menu && (
        <Popover x={menu.x} y={menu.y} onClose={() => setMenu(null)}>
          {problems.map((p, i) => {
            const loc = findClip(draft(), p.clip_id);
            return (
              <button key={i} role="menuitem" className="problem-item" onClick={() => {
                setMenu(null);
                select([p.clip_id]);
                if (loc) timelineApi.current?.reveal(loc.clip.start);
              }}>
                <strong>{loc ? `${placeName(loc.track)}, ${loc.clip.start.toFixed(2)} s` : 'Clip'}</strong>
                <span>{p.message}</span>
              </button>
            );
          })}
        </Popover>
      )}
    </>
  );
}

function EditorView({ session }: { session: EditorSession }) {
  const nav = useNavigate();
  const name = useDoc((s) => s.history.present.name);
  const dirty = useDoc((s) => s.history.present !== s.saved);
  const canUndo = useDoc((s) => s.history.past.length > 0);
  const canRedo = useDoc((s) => s.history.future.length > 0);
  const info = useStore(sessionStore);
  const narrow = useMediaQuery('(max-width: 899px)');
  const [tab, setTab] = useState<'library' | 'inspector'>('library');
  const [showLib, setShowLib] = useState(true);
  const [showInsp, setShowInsp] = useState(true);
  const [collapsed, setCollapsed] = useState(false);
  const [saving, setSaving] = useState(false);
  const [lostLong, setLostLong] = useState(false);

  // Only complain about the connection when it stays down for a moment.
  useEffect(() => {
    if (info.connected || info.closed) {
      setLostLong(false);
      return;
    }
    const t = window.setTimeout(() => setLostLong(true), 1500);
    return () => window.clearTimeout(t);
  }, [info.connected, info.closed]);

  const doSave = useCallback(async () => {
    setSaving(true);
    await save();
    setSaving(false);
  }, []);

  const leave = useCallback(async (to: string) => {
    if (isDirty() && !(await confirmDialog('Leave without saving?', {
      text: 'Your changes stay in this browser and are offered again when you reopen this set.', action: 'Leave', danger: true,
    }))) return;
    nav(to);
  }, [nav]);

  // Unsaved changes: ask before the page goes away, and before in-app links leave the editor.
  useEffect(() => {
    const beforeUnload = (e: BeforeUnloadEvent) => {
      if (!isDirty()) return;
      e.preventDefault();
      e.returnValue = '';
    };
    const click = (e: MouseEvent) => {
      if (!isDirty() || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      const a = (e.target as HTMLElement).closest?.('a');
      if (!a || a.target === '_blank' || !a.href) return;
      const url = new URL(a.href, location.href);
      if (url.origin !== location.origin || url.pathname === location.pathname) return;
      e.preventDefault();
      e.stopPropagation();
      void leave(url.pathname + url.search);
    };
    window.addEventListener('beforeunload', beforeUnload);
    document.addEventListener('click', click, true);
    return () => {
      window.removeEventListener('beforeunload', beforeUnload);
      document.removeEventListener('click', click, true);
    };
  }, [leave]);

  // Keyboard shortcuts (never while typing in a field, except Ctrl+S).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.ctrlKey || e.metaKey;
      const k = e.key.toLowerCase();
      if (mod && k === 's') {
        e.preventDefault();
        (document.activeElement as HTMLElement | null)?.blur?.();
        void doSave();
        return;
      }
      if (e.defaultPrevented || isTyping(e.target) || document.querySelector('.backdrop')) return;
      const d = docStore.get();
      if (!d) return;
      // Arrows move by one line of the snap grid (Shift: one strong line), a beat when it is too dense to show.
      const { bpm, beat_offset } = d.history.present;
      const view = viewStore.get();
      const g = gridLines(view.grid, view.pps, bpm, beat_offset) ?? { step: secondsPerBeat(bpm), major: BEATS_PER_BAR };
      const step = e.altKey ? 0.05 : e.shiftKey ? g.step * g.major : g.step;
      let handled = true;
      if (mod) {
        if (k === 'z' && !e.shiftKey) undo();
        else if ((k === 'z' && e.shiftKey) || k === 'y') redo();
        else if (k === 'c') copySelection();
        else if (k === 'x') cutSelection();
        else if (k === 'v') paste();
        else if (k === 'd') duplicateSelection();
        else if (k === 'a') selectAll();
        else if (k === 'b') splitAtPlayhead();
        else handled = false;
      } else if (e.key === ' ') session.toggle();
      else if (e.key === 'Delete' || e.key === 'Backspace') deleteSelection();
      else if (e.key === 'Home') session.seek(0);
      else if (e.key === 'End') session.seek(d.history.present.duration);
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
        const dir = e.key === 'ArrowLeft' ? -1 : 1;
        if (d.selection.length) nudge(dir * step);
        else session.seek(clock.now() + dir * step);
      } else if ((e.key === 'ArrowUp' || e.key === 'ArrowDown') && d.selection.length) nudge(0, e.key === 'ArrowUp' ? -1 : 1);
      else if (e.key === 'Escape') {
        select([]);
        setInsert(null);
      } else if (k === 's') splitAtPlayhead();
      else if (k === 'l') session.setLoop({ ...loopStore.get(), on: !loopStore.get().on });
      else if (k === 'n') setSnap(!viewStore.get().snap);
      else if (e.key === '+' || e.key === '=') timelineApi.current?.zoom(1.5);
      else if (e.key === '-') timelineApi.current?.zoom(1 / 1.5);
      else if (e.key === '0') timelineApi.current?.fit();
      else handled = false;
      if (handled) e.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [session, doSave]);

  const openInspector = useCallback(() => {
    setTab('inspector');
    setShowInsp(true);
    setCollapsed(false);
  }, []);

  return (
    <div className={`editor ${narrow ? 'narrow' : 'wide'} ${collapsed ? 'collapsed' : ''}`}>
      <header className="ed-bar">
        <button className="icon-btn" onClick={() => void leave('/sets')} title="Back to sets" aria-label="Back to sets"><Icon name="back" /></button>
        <div className="ed-title">
          <span className="ed-name ellipsis" title={name}>{name}</span>
          {dirty ? <span className="ed-dirty" data-testid="dirty" title="There are unsaved changes">● Unsaved</span> : <span className="ed-saved muted small">Saved</span>}
        </div>
        <div className="ed-actions">
          <ProblemsButton />
          <button className="icon-btn" onClick={undo} disabled={!canUndo} title="Undo (Ctrl+Z)" aria-label="Undo"><Icon name="undo" /></button>
          <button className="icon-btn" onClick={redo} disabled={!canRedo} title="Redo (Ctrl+Shift+Z)" aria-label="Redo"><Icon name="redo" /></button>
          {!narrow && (
            <>
              <button className={`panel-btn ${showLib ? 'on' : ''}`} aria-pressed={showLib} onClick={() => setShowLib(!showLib)}>Library</button>
              <button className={`panel-btn ${showInsp ? 'on' : ''}`} aria-pressed={showInsp} onClick={() => setShowInsp(!showInsp)}>Inspector</button>
            </>
          )}
          <button className="primary ed-save" onClick={() => void doSave()} disabled={!dirty || saving} title="Save (Ctrl+S)" data-testid="save">
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
      </header>
      {info.closed && (
        <div className="ed-banner" role="alert">
          This editor was disconnected: {info.closed}. The preview and the sign no longer follow it.
          <button onClick={() => session.reconnect()}>Take over again</button>
        </div>
      )}
      {!info.closed && lostLong && <div className="ed-banner" role="alert">No connection to the editor session — reconnecting. Your edits are kept.</div>}

      {!collapsed && (
        <div className="ed-top">
          {!narrow && showLib && <aside className="ed-panel ed-lib" aria-label="Library"><Palette /></aside>}
          <PreviewBox />
          {!narrow && showInsp && <aside className="ed-panel ed-insp" aria-label="Inspector"><Inspector /></aside>}
          {narrow && (
            <div className="ed-panel ed-tabs">
              <div className="ed-tabbar" role="tablist">
                <button role="tab" aria-selected={tab === 'library'} className={tab === 'library' ? 'active' : ''} onClick={() => setTab('library')}>Library</button>
                <button role="tab" aria-selected={tab === 'inspector'} className={tab === 'inspector' ? 'active' : ''} onClick={() => setTab('inspector')}>Inspector</button>
              </div>
              <div className="ed-tabbody">{tab === 'library' ? <Palette /> : <Inspector />}</div>
            </div>
          )}
        </div>
      )}
      <Transport session={session} collapsed={collapsed} onCollapse={() => setCollapsed(!collapsed)} />
      <Timeline session={session} narrow={narrow} onInspect={openInspector} />
    </div>
  );
}
