// Undo / redo over immutable snapshots. Pure functions (unit-tested).
//
// Consecutive commits with the same `key` inside `coalesceMs` replace the newest
// entry instead of adding one, so typing a word in a field or dragging a slider
// is one undo step.

export interface History<T> {
  past: T[];
  present: T;
  future: T[];
  key: string | null;
  at: number;
}

export const HISTORY_LIMIT = 300;
export const COALESCE_MS = 1200;

export function createHistory<T>(present: T): History<T> {
  return { past: [], present, future: [], key: null, at: 0 };
}

export function commit<T>(h: History<T>, next: T, opts: { key?: string; now?: number; coalesceMs?: number } = {}): History<T> {
  if (next === h.present) return h;
  const now = opts.now ?? Date.now();
  const key = opts.key ?? null;
  if (key !== null && key === h.key && now - h.at < (opts.coalesceMs ?? COALESCE_MS) && h.past.length > 0) {
    return { ...h, present: next, future: [], at: now };
  }
  const past = h.past.length >= HISTORY_LIMIT ? h.past.slice(h.past.length - HISTORY_LIMIT + 1) : h.past.slice();
  past.push(h.present);
  return { past, present: next, future: [], key, at: now };
}

export function undo<T>(h: History<T>): History<T> {
  if (!h.past.length) return h;
  const past = h.past.slice(0, -1);
  return { past, present: h.past[h.past.length - 1], future: [h.present, ...h.future], key: null, at: 0 };
}

export function redo<T>(h: History<T>): History<T> {
  if (!h.future.length) return h;
  const [next, ...future] = h.future;
  return { past: [...h.past, h.present], present: next, future, key: null, at: 0 };
}

/** Ends coalescing: the next commit starts a new undo step whatever its key. */
export function seal<T>(h: History<T>): History<T> {
  return h.key === null ? h : { ...h, key: null };
}
