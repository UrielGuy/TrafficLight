// The editor's document: the draft set with undo history, the selection and the
// insertion point. One module-level store; components read slices of it with
// useDoc(selector) so an edit re-renders only what depends on the changed parts.
import { useSyncExternalStore } from 'react';
import type { SetData } from '../api/types';
import { createStore } from '../state/store';
import { commit, createHistory, redo as redoH, seal, undo as undoH, type History } from './history';
import { allClipIds, type ClipboardItem } from './model';

export interface Insert {
  trackId: string;
  t: number;
}

export interface DocState {
  history: History<SetData>;
  /** The set as last loaded or saved; the draft is dirty while it is a different object. */
  saved: SetData;
  selection: string[];
  /** Where a click on a palette item puts the new clip (set by clicking an empty spot of a track). */
  insert: Insert | null;
  /** Bumped when the draft is replaced from outside (load, recovery) so views can reset. */
  epoch: number;
}

export const docStore = createStore<DocState | null>(null);

export function useDoc<T>(selector: (s: DocState) => T): T {
  return useSyncExternalStore(docStore.subscribe, () => selector(docStore.get()!));
}

const get = () => docStore.get()!;
export const draft = () => get().history.present;
export const isDirty = (s: DocState | null = docStore.get()) => !!s && s.history.present !== s.saved;

/** Starts editing `set`. A recovered draft passes the saved set too: Undo then goes back to it. */
export function loadDoc(set: SetData, saved: SetData = set) {
  const prev = docStore.get();
  const history = createHistory(set);
  if (saved !== set) history.past = [saved];
  docStore.set({ history, saved, selection: [], insert: null, epoch: (prev?.epoch ?? 0) + 1 });
}

/**
 * Applies an edit to the draft. `key` makes consecutive edits of the same thing (typing in a
 * field) one undo step. `select` replaces the selection.
 */
export function edit(fn: (s: SetData) => SetData, opts: { key?: string; select?: string[] } = {}) {
  const s = get();
  const next = fn(s.history.present);
  if (next === s.history.present && !opts.select) return;
  const history = next === s.history.present ? s.history : commit(s.history, next, { key: opts.key });
  docStore.set({ ...s, history, selection: opts.select ?? pruneSelection(s.selection, next) });
}

function pruneSelection(sel: string[], set: SetData): string[] {
  if (!sel.length) return sel;
  const ids = new Set(allClipIds(set));
  const out = sel.filter((id) => ids.has(id));
  return out.length === sel.length ? sel : out;
}

export function undo() {
  const s = get();
  const history = undoH(s.history);
  if (history !== s.history) docStore.set({ ...s, history, selection: pruneSelection(s.selection, history.present) });
}
export function redo() {
  const s = get();
  const history = redoH(s.history);
  if (history !== s.history) docStore.set({ ...s, history, selection: pruneSelection(s.selection, history.present) });
}
/** Ends the current coalescing run (e.g. when a field loses focus). */
export function sealHistory() {
  const s = docStore.get();
  if (s && s.history.key !== null) docStore.set({ ...s, history: seal(s.history) });
}

export function markSaved(set: SetData) {
  const s = get();
  // Keep the draft object if nothing changed since the save was sent, so the draft is clean.
  docStore.set({ ...s, saved: set });
}

export function select(ids: string[]) {
  const s = get();
  if (ids.length === s.selection.length && ids.every((id, i) => id === s.selection[i])) return;
  docStore.set({ ...s, selection: ids });
}
export function toggleSelect(id: string) {
  const s = get();
  select(s.selection.includes(id) ? s.selection.filter((x) => x !== id) : [...s.selection, id]);
}
export function setInsert(insert: Insert | null) {
  const s = get();
  if (insert?.trackId === s.insert?.trackId && insert?.t === s.insert?.t) return;
  docStore.set({ ...s, insert });
}

// ---- clipboard (in memory, shared by every editor page of this tab)
let clipboard: ClipboardItem[] = [];
export const getClipboard = () => clipboard;
export const setClipboard = (items: ClipboardItem[]) => {
  clipboard = items;
};

// ---- local backup of an unsaved draft (survives reloads and crashes)
const backupKey = (id: string) => `trafficlight.draft.${id}`;
export interface Backup {
  set: SetData;
  /** JSON of the saved set the draft was based on. */
  base: string;
  at: number;
}
export function writeBackup(set: SetData, saved: SetData) {
  try {
    localStorage.setItem(backupKey(set.id), JSON.stringify({ set, base: JSON.stringify(saved), at: Date.now() } satisfies Backup));
  } catch {
    /* storage full or unavailable: nothing more we can do */
  }
}
export function readBackup(id: string): Backup | null {
  try {
    const raw = localStorage.getItem(backupKey(id));
    return raw ? (JSON.parse(raw) as Backup) : null;
  } catch {
    return null;
  }
}
export function clearBackup(id: string) {
  try {
    localStorage.removeItem(backupKey(id));
  } catch {
    /* ignore */
  }
}
