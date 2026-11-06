// State shared between the palette (drag source) and the timeline (drop target),
// and the transient state of drags inside the timeline. Kept out of the document
// store so a pointer move re-renders only the drag layer.
import type { Clip } from '../api/types';
import { createStore } from '../state/store';
import type { PaletteItem } from './clipinfo';

/** The palette item being dragged (set at drag start and end only, not per move). */
export const dragItemStore = createStore<PaletteItem | null>(null);

/** The timeline registers itself here so the palette can ask it about drops. */
export interface DropTarget {
  /** Pointer at (x, y) client coords while dragging `item`; returns true when over a track that takes it. */
  hover(item: PaletteItem, x: number, y: number): boolean;
  /** Returns true if the item was dropped onto a track. */
  drop(item: PaletteItem, x: number, y: number): boolean;
  leave(): void;
}
export const dropTarget: { current: DropTarget | null } = { current: null };

/** A clip drawn where it would land: from row `row`, `span` rows tall (a whole-sign clip covers the lights). */
export interface Ghost {
  clip: Clip;
  row: number;
  span: number;
  whole: boolean;
}

export interface DragView {
  ghosts: Ghost[];
  /** Time of the snap line, or null. */
  snapAt: number | null;
  /** The move has no valid position: the drop does nothing. */
  invalid: boolean;
  marquee: { x0: number; y0: number; x1: number; y1: number } | null;
  /** Track reorder: row index the dragged track goes to. */
  trackTo: number | null;
}

export const EMPTY_DRAG: DragView = { ghosts: [], snapAt: null, invalid: false, marquee: null, trackTo: null };
export const dragView = createStore<DragView>(EMPTY_DRAG);
