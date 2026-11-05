// A tiny external store (no state library): get / set / subscribe + a React hook.
import { useSyncExternalStore } from 'react';

export interface Store<T> {
  get: () => T;
  set: (v: T | ((prev: T) => T)) => void;
  subscribe: (cb: () => void) => () => void;
}

export function createStore<T>(initial: T): Store<T> {
  let value = initial;
  const subs = new Set<() => void>();
  return {
    get: () => value,
    set(v) {
      const next = typeof v === 'function' ? (v as (p: T) => T)(value) : v;
      if (Object.is(next, value)) return;
      value = next;
      subs.forEach((s) => s());
    },
    subscribe(cb) {
      subs.add(cb);
      return () => subs.delete(cb);
    },
  };
}

export function useStore<T>(store: Store<T>): T {
  return useSyncExternalStore(store.subscribe, store.get);
}
