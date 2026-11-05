// Editor commands used by the keyboard, the toolbar, the inspector and the palette.
import { api } from '../api/client';
import type { SetData, Target } from '../api/types';
import { catalogRes, setsRes } from '../state/app';
import { toast } from '../state/toast';
import { acceptsType, itemDuration, makeClip, offsetTypes, typeMap, type PaletteItem } from './clipinfo';
import { clearBackup, docStore, draft, edit, getClipboard, isDirty, markSaved, select, setClipboard, setInsert } from './doc';
import { clock } from './session';
import {
  addClips, allClipIds, clipEnd, copyClips, duplicateClips, findClip, pasteClips, placeNew, planMove, applyMoves,
  removeClips, splitClips,
} from './model';

let typesCache: { src: unknown; map: ReturnType<typeof typeMap> } | null = null;
export const types = () => {
  const src = catalogRes.store.get();
  if (typesCache?.src !== src) typesCache = { src, map: typeMap(src?.clip_types ?? null) };
  return typesCache.map;
};
export const accepts = (type: string, target: Target) => acceptsType(types(), type, target);
const selection = () => docStore.get()?.selection ?? [];

/**
 * Adds a palette item without dragging: on the track of the insertion point if it can take the
 * item (else the first track that can), at the insertion point or the playhead. The insertion
 * point then moves to the new clip's end, so repeated clicks lay clips out one after another.
 */
export function addItem(item: PaletteItem): boolean {
  const s = draft();
  const ins = docStore.get()?.insert ?? null;
  const ok = (target: Target) => accepts(item.type.type, target);
  let track = ins ? s.tracks.find((t) => t.id === ins.trackId && ok(t.target)) : undefined;
  const sel = selection()[0];
  if (!track && sel) {
    const loc = findClip(s, sel);
    if (loc && ok(loc.track.target)) track = loc.track;
  }
  track ??= s.tracks.find((t) => ok(t.target));
  if (!track) {
    toast.info(`No track can take ${item.type.label.toLowerCase()} clips: add one first`);
    return false;
  }
  const at = ins && ins.trackId === track.id ? ins.t : clock.now();
  dropItem(item, track.id, at);
  return true;
}

/** Puts a new clip for `item` on a track with its start near `t` (see placeNew). Returns its id. */
export function dropItem(item: PaletteItem, trackId: string, t: number): string | null {
  const s = draft();
  const track = s.tracks.find((x) => x.id === trackId);
  if (!track || !accepts(item.type.type, track.target)) return null;
  const p = placeNew(track, t, itemDuration(item));
  const clip = makeClip(item, p.start, p.duration);
  edit((x) => addClips(x, trackId, [clip]), { select: [clip.id] });
  setInsert({ trackId, t: clipEnd(clip) });
  return clip.id;
}

export function deleteSelection() {
  const ids = selection();
  if (ids.length) edit((s) => removeClips(s, new Set(ids)), { select: [] });
}

export function copySelection(): number {
  const items = copyClips(draft(), selection());
  if (items.length) setClipboard(items);
  return items.length;
}
export function cutSelection() {
  if (copySelection()) deleteSelection();
}
export function paste() {
  const items = getClipboard();
  if (!items.length) return;
  let ids: string[] = [];
  edit((s) => {
    const r = pasteClips(s, items, clock.now());
    ids = r.ids;
    return r.set;
  });
  select(ids);
}
export function duplicateSelection() {
  let ids: string[] = [];
  edit((s) => {
    const r = duplicateClips(s, selection());
    ids = r.ids;
    return r.set;
  });
  if (ids.length) select(ids);
}

/** Splits the selected clips at the playhead; with nothing selected, every clip under it. */
export function splitAtPlayhead() {
  const t = clock.now();
  const s = draft();
  let ids = selection();
  if (!ids.length) ids = s.tracks.flatMap((tr) => tr.clips.filter((c) => c.start < t && clipEnd(c) > t).map((c) => c.id));
  const r = splitClips(s, ids, t, offsetTypes(types()));
  if (!r.created.length) {
    toast.info('Put the playhead inside a clip to split it');
    return;
  }
  edit(() => r.set);
}

/** Moves the selection by dt seconds and `rows` tracks, to the nearest free position. */
export function nudge(dt: number, rows = 0) {
  const ids = selection();
  if (!ids.length) return;
  const plan = planMove(draft(), ids, dt, rows, accepts);
  if (plan) edit((s) => applyMoves(s, plan), { key: 'nudge' });
}

export function selectAll() {
  select(allClipIds(draft()));
}

let saving: Promise<boolean> | null = null;
/** PUT the draft. Edits made while the request runs stay unsaved. */
export function save(): Promise<boolean> {
  if (saving) return saving;
  const s = draft();
  saving = (async () => {
    try {
      await api.putSet(s);
      markSaved(s);
      if (!isDirty()) clearBackup(s.id);
      void setsRes.refresh();
      return true;
    } catch (e) {
      toast.error(`Could not save: ${e instanceof Error ? e.message : e}`);
      return false;
    } finally {
      saving = null;
    }
  })();
  return saving;
}

/** Sets the set's duration to the end of its last clip. */
export function fitDurationToClips() {
  edit((s: SetData) => {
    const end = Math.max(0, ...s.tracks.flatMap((t) => t.clips.map(clipEnd)));
    return end > 0 && Math.abs(end - s.duration) > 1e-6 ? { ...s, duration: Math.round(end * 1000) / 1000 } : s;
  });
}
