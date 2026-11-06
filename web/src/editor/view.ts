// Zoom and snapping of the timeline, shared by the transport bar and the timeline.
import { createStore } from '../state/store';
import { BEAT_STEPS, TIME_STEPS, type SnapGrid } from './beats';

export const MIN_PPS = 1;
export const MAX_PPS = 800;
const SNAP_PREF = 'trafficlight.editor.snap';
const GRID_PREF = 'trafficlight.editor.snapGrid';

function readSnap(): boolean {
  try {
    return localStorage.getItem(SNAP_PREF) !== '0';
  } catch {
    return true;
  }
}

type Unit = SnapGrid['unit'];
/** The step each unit starts with (0 follows the zoom): beats follow the zoom, time is a quarter second. */
export const DEFAULT_STEPS: Record<Unit, number> = { beats: 0, time: 0.25 };
const STEPS: Record<Unit, number[]> = { beats: BEAT_STEPS, time: TIME_STEPS };

interface GridPref {
  unit: Unit;
  /** The step last chosen for each unit, so switching back and forth keeps both. */
  steps: Record<Unit, number>;
}

/** A stored preference made safe: unknown units and steps fall back to the defaults. */
export function parseGridPref(raw: string | null): GridPref {
  let v: { unit?: unknown; steps?: Record<string, unknown> } = {};
  try {
    v = JSON.parse(raw ?? '{}') ?? {};
  } catch {
    /* defaults */
  }
  const step = (u: Unit) => {
    const n = Number(v.steps?.[u]);
    return n === 0 || STEPS[u].includes(n) ? n : DEFAULT_STEPS[u];
  };
  return { unit: v.unit === 'time' ? 'time' : 'beats', steps: { beats: step('beats'), time: step('time') } };
}

function readGridPref(): GridPref {
  try {
    return parseGridPref(localStorage.getItem(GRID_PREF));
  } catch {
    return parseGridPref(null);
  }
}
let gridPref = readGridPref();

export interface ViewState {
  /** Pixels per second. */
  pps: number;
  snap: boolean;
  /** What dragged edges are quantized to, and the lines drawn behind the tracks. */
  grid: SnapGrid;
}
export const viewStore = createStore<ViewState>({ pps: 40, snap: readSnap(), grid: { unit: gridPref.unit, step: gridPref.steps[gridPref.unit] } });

export function setSnap(on: boolean) {
  viewStore.set((v) => ({ ...v, snap: on }));
  try {
    localStorage.setItem(SNAP_PREF, on ? '1' : '0');
  } catch {
    /* ignore */
  }
}

/** Choosing a grid also turns snapping on: picking one while it is off would do nothing visible. */
function applyGrid(pref: GridPref) {
  gridPref = pref;
  viewStore.set((v) => ({ ...v, grid: { unit: pref.unit, step: pref.steps[pref.unit] } }));
  try {
    localStorage.setItem(GRID_PREF, JSON.stringify(pref));
  } catch {
    /* ignore */
  }
  setSnap(true);
}
export const setSnapUnit = (unit: Unit) => applyGrid({ ...gridPref, unit });
export const setSnapStep = (step: number) => applyGrid({ ...gridPref, steps: { ...gridPref.steps, [gridPref.unit]: step } });

/** Implemented by the mounted timeline: zoom keeping a point still, fit the set, scroll a time into view. */
export interface TimelineApi {
  zoom(factor: number, clientX?: number): void;
  fit(): void;
  reveal(t: number): void;
}
export const timelineApi: { current: TimelineApi | null } = { current: null };
