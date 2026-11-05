// Zoom and snapping of the timeline, shared by the transport bar and the timeline.
import { createStore } from '../state/store';

export const MIN_PPS = 1;
export const MAX_PPS = 800;
const SNAP_PREF = 'trafficlight.editor.snap';

function readSnap(): boolean {
  try {
    return localStorage.getItem(SNAP_PREF) !== '0';
  } catch {
    return true;
  }
}

export interface ViewState {
  /** Pixels per second. */
  pps: number;
  snap: boolean;
}
export const viewStore = createStore<ViewState>({ pps: 40, snap: readSnap() });

export function setSnap(on: boolean) {
  viewStore.set((v) => ({ ...v, snap: on }));
  try {
    localStorage.setItem(SNAP_PREF, on ? '1' : '0');
  } catch {
    /* ignore */
  }
}

/** Implemented by the mounted timeline: zoom keeping a point still, fit the set, scroll a time into view. */
export interface TimelineApi {
  zoom(factor: number, clientX?: number): void;
  fit(): void;
  reveal(t: number): void;
}
export const timelineApi: { current: TimelineApi | null } = { current: null };
