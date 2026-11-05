import { createStore } from './store';

export interface Toast {
  id: number;
  kind: 'error' | 'info';
  text: string;
}

export const toasts = createStore<Toast[]>([]);
let nextId = 1;

function push(kind: Toast['kind'], text: string) {
  const id = nextId++;
  toasts.set((t) => [...t.slice(-3), { id, kind, text }]);
  window.setTimeout(() => dismiss(id), kind === 'error' ? 7000 : 3000);
}
export const dismiss = (id: number) => toasts.set((t) => t.filter((x) => x.id !== id));

export const toast = {
  info: (text: string) => push('info', text),
  error: (e: unknown) => push('error', e instanceof Error ? e.message : String(e)),
};

/** Runs an async action and shows a toast if it fails. Returns the result or undefined. */
export async function attempt<T>(fn: () => Promise<T>): Promise<T | undefined> {
  try {
    return await fn();
  } catch (e) {
    toast.error(e);
    return undefined;
  }
}
