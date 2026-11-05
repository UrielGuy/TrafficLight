import { createStore } from './store';

export interface ConfirmRequest {
  title: string;
  text?: string;
  action: string;
  danger?: boolean;
  resolve: (ok: boolean) => void;
}
export const confirmStore = createStore<ConfirmRequest | null>(null);

/** Shows a modal confirmation (rendered by <ConfirmHost/>); resolves true if accepted. */
export function confirmDialog(title: string, opts: { text?: string; action?: string; danger?: boolean } = {}): Promise<boolean> {
  return new Promise((resolve) => {
    confirmStore.set({ title, text: opts.text, action: opts.action ?? 'OK', danger: opts.danger, resolve });
  });
}
