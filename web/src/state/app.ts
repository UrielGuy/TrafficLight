// Shared application state. Everything here is a module-level store so any page
// (and the timeline editor) can use it through the hooks at the bottom.
import { useEffect } from 'react';
import { api } from '../api/client';
import { connectSocket, type Socket } from '../api/socket';
import type { Catalog, MediaItem, SetSummary, Settings, State } from '../api/types';
import { createStore, useStore, type Store } from './store';
import { toast } from './toast';

// ---- live state + frames from /ws/sign
export const stateStore = createStore<State | null>(null);
export const connectedStore = createStore(false);

export interface FrameFeed {
  subscribe: (cb: (data: ArrayBuffer) => void) => () => void;
}
const frameSubs = new Set<(d: ArrayBuffer) => void>();
/** Binary frames of /ws/sign. Hand it to <SignPreview feed={signFeed}/>. */
export const signFeed: FrameFeed = {
  subscribe(cb) {
    frameSubs.add(cb);
    return () => frameSubs.delete(cb);
  },
};

let sock: Socket | null = null;
export function startSignConnection() {
  if (sock) return;
  sock = connectSocket('/ws/sign?fps=20', {
    onJson: (m) => {
      if (m.type === 'state') stateStore.set(m as State);
    },
    onFrame: (d) => frameSubs.forEach((f) => f(d)),
    onOpen: () => connectedStore.set(true),
    onClose: () => connectedStore.set(false),
  });
}

// ---- fetched resources
export interface Resource<T> {
  store: Store<T | null>;
  refresh: () => Promise<void>;
}
function resource<T>(fetcher: () => Promise<T>, label: string): Resource<T> {
  const store = createStore<T | null>(null);
  let inflight: Promise<void> | null = null;
  let again = false;
  const run = async (): Promise<void> => {
    if (inflight) {
      again = true;
      return inflight;
    }
    inflight = (async () => {
      do {
        again = false;
        try {
          store.set(await fetcher());
        } catch (e) {
          if (store.get() === null) toast.error(`Could not load ${label}: ${e instanceof Error ? e.message : e}`);
        }
      } while (again);
      inflight = null;
    })();
    return inflight;
  };
  return { store, refresh: run };
}

export const catalogRes = resource<Catalog>(api.catalog, 'the clip catalog');
export const setsRes = resource<SetSummary[]>(api.listSets, 'sets');
export const mediaRes = resource<MediaItem[]>(api.listMedia, 'media');
export const settingsRes = resource<Settings>(api.getSettings, 'settings');

/** Mount once (App shell): connects the socket and refetches lists when the revisions move. */
export function useAppBootstrap() {
  const state = useStore(stateStore);
  useEffect(() => {
    startSignConnection();
    void catalogRes.refresh();
    void settingsRes.refresh();
  }, []);
  const setsRev = state?.sets_rev;
  const mediaRev = state?.media_rev;
  useEffect(() => {
    void setsRes.refresh();
  }, [setsRev]);
  useEffect(() => {
    void mediaRes.refresh();
  }, [mediaRev]);
}

export const useAppState = () => useStore(stateStore);
export const useConnected = () => useStore(connectedStore);
export const useCatalog = () => useStore(catalogRes.store)?.clip_types ?? null;
export const useSets = () => useStore(setsRes.store);
export const useMedia = () => useStore(mediaRes.store);
export const useSettings = () => useStore(settingsRes.store);

/** PATCH /api/settings, publish the result to every page, toast on failure. */
export async function patchSettings(p: import('../api/types').SettingsPatch): Promise<boolean> {
  try {
    settingsRes.store.set(await api.patchSettings(p));
    return true;
  } catch (e) {
    toast.error(e);
    void settingsRes.refresh();
    return false;
  }
}
