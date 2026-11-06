// Timeline logic of the editor: placement without overlaps, snapping, trimming,
// splitting and the immutable edits of a set. Pure functions, no React (unit-tested).
//
// Rules (also described in the editor's help):
//  * Clips on one track never overlap. A clip that is moved goes to the free
//    position nearest to where it is dragged: if the spot under the pointer is
//    taken, it butts up against the neighbour (or hops past it when the pointer is
//    nearer the other side). A group moves as one block.
//  * A new clip dropped into a gap shorter than its length is shortened to fill
//    the gap; dropped onto a clip it goes to the nearest free position.
//  * Trimming stops at the neighbouring clip and at a minimum length.
//  * Snapping (when on): an edge within a few pixels of another clip's edge, the
//    playhead, the set's start/end or a loop edge is pulled onto it; otherwise the
//    dragged edge is quantized to the chosen grid: beats or plain time, at a fixed
//    step or at whichever step is at least 10 px apart at the current zoom.
import type { Clip, SetData, Target, Track } from '../api/types';
import { nearestGrid } from './beats';

export const MIN_DURATION = 0.1;
const EPS = 1e-6;

export function newId(): string {
  const b = new Uint8Array(6);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
}

export const clipEnd = (c: { start: number; duration: number }) => c.start + c.duration;
const round = (t: number) => Math.round(t * 10000) / 10000;

/** Can a clip type with these `targets` ("face" | "all" | "audio") go on a track with this target? */
export function accepts(targets: readonly string[], trackTarget: Target): boolean {
  if (trackTarget === 'audio') return targets.includes('audio');
  if (trackTarget === 'all') return targets.includes('all');
  return targets.includes('face');
}

export interface Span {
  start: number;
  end: number;
}

/** The clips of a track that block placement (all but `exclude`), sorted by start. */
export function blockers(track: Track, exclude?: ReadonlySet<string>): Span[] {
  return track.clips
    .filter((c) => !exclude?.has(c.id))
    .map((c) => ({ start: c.start, end: clipEnd(c) }))
    .sort((a, b) => a.start - b.start);
}

export function collides(spans: Span[], start: number, end: number): boolean {
  for (const s of spans) {
    if (s.start >= end - EPS) return false; // sorted: nothing later can collide
    if (s.end > start + EPS) return true;
  }
  return false;
}

/** The free interval containing `t` (end may be Infinity), or null when `t` is inside a clip. */
export function gapAt(spans: Span[], t: number): Span | null {
  let lo = 0;
  for (const s of spans) {
    if (s.end <= t + EPS) {
      lo = Math.max(lo, s.end);
      continue;
    }
    if (s.start <= t + EPS) return null;
    return { start: lo, end: s.start };
  }
  return { start: lo, end: Infinity };
}

// ---------------------------------------------------------------- moving
export interface MoveItem {
  /** Destination track's blockers (the moving clips excluded). */
  spans: Span[];
  start: number;
  end: number;
}

/**
 * The shift closest to `desired` that leaves every item free of overlaps and at t >= 0.
 * `forward` only considers shifts >= desired (paste: "here or later"). Null if none exists.
 */
export function resolveShift(items: MoveItem[], desired: number, forward = false): number | null {
  if (!items.length) return desired;
  const minStart = Math.min(...items.map((i) => i.start));
  const valid = (dt: number) => {
    if (minStart + dt < -EPS) return false;
    for (const i of items) if (collides(i.spans, i.start + dt, i.end + dt)) return false;
    return true;
  };
  if (valid(desired)) return desired;
  const cands = [-minStart];
  for (const i of items) {
    for (const s of i.spans) {
      cands.push(s.end - i.start, s.start - i.end);
    }
  }
  const sorted = cands
    .filter((c) => (forward ? c >= desired - EPS : true))
    .sort((a, b) => Math.abs(a - desired) - Math.abs(b - desired));
  for (const c of sorted) if (valid(c)) return c;
  return null;
}

/** Where a new clip of `duration` dropped with its start at `t` goes on a track: see the rules above. */
export function placeNew(track: Track, t: number, duration: number): { start: number; duration: number } {
  const spans = blockers(track);
  const start = Math.max(0, t);
  const gap = gapAt(spans, start);
  if (gap) {
    const room = gap.end - gap.start;
    if (room >= duration - EPS) return { start: Math.min(start, gap.end - duration), duration };
    if (room >= MIN_DURATION) return { start: gap.start, duration: room };
  }
  const dt = resolveShift([{ spans, start, end: start + duration }], 0);
  return { start: start + (dt ?? 0), duration };
}

// ---------------------------------------------------------------- trimming
/**
 * New start/duration when dragging one edge of `clip` (on `track`) to `to`: stops at the
 * neighbours, at t = 0 and at MIN_DURATION. `minStart` limits a start trim further
 * (a media clip cannot start before the beginning of its file).
 */
export function trim(track: Track, clip: Clip, edge: 'start' | 'end', to: number, minStart = 0): { start: number; duration: number } {
  const end = clipEnd(clip);
  const spans = blockers(track, new Set([clip.id]));
  if (edge === 'end') {
    let limit = Infinity;
    for (const s of spans) if (s.start >= clip.start - EPS && s.start < limit) limit = s.start;
    const e = Math.min(Math.max(to, clip.start + MIN_DURATION), limit);
    return { start: clip.start, duration: round(e - clip.start) };
  }
  let limit = Math.max(0, minStart);
  for (const s of spans) if (s.end <= end + EPS && s.start < clip.start && s.end > limit) limit = s.end;
  const st = Math.max(Math.min(to, end - MIN_DURATION), limit);
  return { start: round(st), duration: round(end - st) };
}

/** Applies a trim result: a start trim of a clip with an offset moves its in-point too. */
export function applyTrim(clip: Clip, start: number, duration: number, withOffset: boolean): Clip {
  if (start === clip.start && duration === clip.duration) return clip;
  const out: Clip = { ...clip, start, duration };
  if (withOffset && start !== clip.start) {
    const off = Number(clip.params.offset ?? 0) || 0;
    out.params = { ...clip.params, offset: round(Math.max(0, off + (start - clip.start))) };
  }
  out.fade_in = Math.min(out.fade_in, duration);
  out.fade_out = Math.min(out.fade_out, duration);
  return out;
}

// ---------------------------------------------------------------- split
/** Two halves of `clip` cut at set time `t`, or null if `t` is not inside it (by MIN_DURATION). */
export function split(clip: Clip, t: number, withOffset: boolean): [Clip, Clip] | null {
  if (t <= clip.start + MIN_DURATION - EPS || t >= clipEnd(clip) - MIN_DURATION + EPS) return null;
  const cut = round(t);
  const a: Clip = { ...clip, duration: round(cut - clip.start), fade_out: 0 };
  a.fade_in = Math.min(a.fade_in, a.duration);
  const b: Clip = { ...clip, id: newId(), start: cut, duration: round(clipEnd(clip) - cut), fade_in: 0, params: { ...clip.params } };
  b.fade_out = Math.min(b.fade_out, b.duration);
  if (withOffset) {
    const off = Number(clip.params.offset ?? 0) || 0;
    b.params.offset = round(off + (cut - clip.start));
  }
  return [a, b];
}

// ---------------------------------------------------------------- snapping
export interface SnapContext {
  /** Sorted snap points in seconds (clip edges, playhead, set bounds, loop edges). */
  points: number[];
  /** Grid to quantize to (beats or time, in seconds), or null for none. */
  grid: { offset: number; step: number } | null;
  /** Max distance in seconds. */
  threshold: number;
}

function nearestPoint(points: number[], t: number): number | null {
  if (!points.length) return null;
  let lo = 0;
  let hi = points.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid] < t) lo = mid + 1;
    else hi = mid;
  }
  let best = points[lo];
  if (lo > 0 && Math.abs(points[lo - 1] - t) < Math.abs(best - t)) best = points[lo - 1];
  return best;
}

export interface Snap {
  t: number;
  /** True when pulled to a point (an edge, the playhead), false when quantized to the grid. */
  magnet: boolean;
}

function magnet(t: number, ctx: SnapContext): number | null {
  const p = nearestPoint(ctx.points, t);
  return p !== null && Math.abs(p - t) <= ctx.threshold ? p : null;
}

/**
 * Where `t` snaps to: a snap point within the threshold, else the nearest grid line
 * (quantize), else nowhere (null).
 */
export function snapTarget(t: number, ctx: SnapContext): Snap | null {
  const p = magnet(t, ctx);
  if (p !== null) return { t: p, magnet: true };
  if (ctx.grid && ctx.grid.step > 0) return { t: nearestGrid(t, ctx.grid.offset, ctx.grid.step), magnet: false };
  return null;
}

/**
 * Snaps a block [start, start+duration): if either edge is near a snap point the nearer one
 * moves onto it; otherwise the start is quantized to the grid. `at` is the point it was
 * pulled to (for drawing a snap line), null for grid quantizing or no snap.
 */
export function snapBlock(start: number, duration: number, ctx: SnapContext): { start: number; at: number | null } {
  const a = magnet(start, ctx);
  const b = magnet(start + duration, ctx);
  const da = a === null ? Infinity : Math.abs(a - start);
  const db = b === null ? Infinity : Math.abs(b - (start + duration));
  if (da !== Infinity || db !== Infinity) return da <= db ? { start: a!, at: a } : { start: b! - duration, at: b };
  if (ctx.grid && ctx.grid.step > 0) return { start: nearestGrid(start, ctx.grid.offset, ctx.grid.step), at: null };
  return { start, at: null };
}

/** Sorted, de-duplicated snap points: edges of all clips except `exclude`, plus `extra`. */
export function snapPoints(set: SetData, exclude: ReadonlySet<string>, extra: number[]): number[] {
  const pts = [...extra];
  for (const tr of set.tracks) for (const c of tr.clips) if (!exclude.has(c.id)) pts.push(c.start, clipEnd(c));
  pts.sort((a, b) => a - b);
  return pts.filter((p, i) => i === 0 || p - pts[i - 1] > EPS);
}

// ---------------------------------------------------------------- set edits (immutable)
export interface Located {
  track: Track;
  trackIndex: number;
  clip: Clip;
}

export function findClip(set: SetData, id: string): Located | null {
  for (let i = 0; i < set.tracks.length; i++) {
    const c = set.tracks[i].clips.find((x) => x.id === id);
    if (c) return { track: set.tracks[i], trackIndex: i, clip: c };
  }
  return null;
}

const sortClips = (clips: Clip[]) => clips.slice().sort((a, b) => a.start - b.start);

function mapTrack(set: SetData, trackId: string, fn: (t: Track) => Track): SetData {
  let changed = false;
  const tracks = set.tracks.map((t) => {
    if (t.id !== trackId) return t;
    const n = fn(t);
    if (n !== t) changed = true;
    return n;
  });
  return changed ? { ...set, tracks } : set;
}

export function updateClip(set: SetData, id: string, fn: (c: Clip) => Clip): SetData {
  const loc = findClip(set, id);
  if (!loc) return set;
  return mapTrack(set, loc.track.id, (t) => {
    const n = fn(loc.clip);
    if (n === loc.clip) return t;
    const clips = t.clips.map((c) => (c.id === id ? n : c));
    return { ...t, clips: n.start !== loc.clip.start ? sortClips(clips) : clips };
  });
}

export function addClips(set: SetData, trackId: string, clips: Clip[]): SetData {
  return mapTrack(set, trackId, (t) => ({ ...t, clips: sortClips([...t.clips, ...clips]) }));
}

export function removeClips(set: SetData, ids: ReadonlySet<string>): SetData {
  let changed = false;
  const tracks = set.tracks.map((t) => {
    if (!t.clips.some((c) => ids.has(c.id))) return t;
    changed = true;
    return { ...t, clips: t.clips.filter((c) => !ids.has(c.id)) };
  });
  return changed ? { ...set, tracks } : set;
}

export interface Move {
  id: string;
  toTrack: string;
  start: number;
}
/** Moves clips (possibly to other tracks) in one edit. */
export function applyMoves(set: SetData, moves: Move[]): SetData {
  if (!moves.length) return set;
  const byId = new Map(moves.map((m) => [m.id, m]));
  const moved: Clip[] = [];
  const dest = new Map<string, Clip[]>();
  let tracks = set.tracks.map((t) => {
    if (!t.clips.some((c) => byId.has(c.id))) return t;
    const keep: Clip[] = [];
    for (const c of t.clips) {
      const m = byId.get(c.id);
      if (!m) keep.push(c);
      else {
        const n = { ...c, start: round(m.start) };
        moved.push(n);
        dest.set(m.toTrack, [...(dest.get(m.toTrack) ?? []), n]);
      }
    }
    return { ...t, clips: keep };
  });
  tracks = tracks.map((t) => (dest.has(t.id) ? { ...t, clips: sortClips([...t.clips, ...dest.get(t.id)!]) } : t));
  return { ...set, tracks };
}

/** Splits every clip in `ids` that the time `t` cuts; returns the new set and the ids of the right halves. */
export function splitClips(set: SetData, ids: string[], t: number, offsetTypes: ReadonlySet<string>): { set: SetData; created: string[] } {
  let out = set;
  const created: string[] = [];
  for (const id of ids) {
    const loc = findClip(out, id);
    if (!loc) continue;
    const parts = split(loc.clip, t, offsetTypes.has(loc.clip.type));
    if (!parts) continue;
    out = mapTrack(out, loc.track.id, (tr) => ({ ...tr, clips: sortClips([...tr.clips.filter((c) => c.id !== id), ...parts]) }));
    created.push(parts[1].id);
  }
  return { set: out, created };
}

// ---------------------------------------------------------------- tracks
export const TARGET_NAMES: Record<Target, string> = {
  face0: 'Top light', face1: 'Middle light', face2: 'Bottom light', all: 'Whole sign', audio: 'Audio',
};

export function addTrack(set: SetData, target: Target, index?: number): { set: SetData; id: string } {
  const id = newId();
  const n = set.tracks.filter((t) => t.target === target).length;
  const base = { face0: 'Top', face1: 'Middle', face2: 'Bottom', all: 'Whole sign', audio: 'Audio' }[target];
  const track: Track = { id, name: n ? `${base} ${n + 1}` : base, target, muted: false, clips: [] };
  const tracks = set.tracks.slice();
  // By default a new track goes after the last track of the same target (or at the end).
  let at = index;
  if (at === undefined) {
    let last = -1;
    tracks.forEach((t, i) => t.target === target && (last = i));
    at = last >= 0 ? last + 1 : target === 'audio' ? tracks.length : tracks.findIndex((t) => t.target === 'audio');
    if (at < 0) at = tracks.length;
  }
  tracks.splice(at, 0, track);
  return { set: { ...set, tracks }, id };
}

export function updateTrack(set: SetData, id: string, patch: Partial<Pick<Track, 'name' | 'muted'>>): SetData {
  return mapTrack(set, id, (t) => ({ ...t, ...patch }));
}

export function removeTrack(set: SetData, id: string): SetData {
  return { ...set, tracks: set.tracks.filter((t) => t.id !== id) };
}

export function moveTrack(set: SetData, id: string, to: number): SetData {
  const from = set.tracks.findIndex((t) => t.id === id);
  if (from < 0) return set;
  const dest = Math.max(0, Math.min(set.tracks.length - 1, to));
  if (dest === from) return set;
  const tracks = set.tracks.slice();
  const [t] = tracks.splice(from, 1);
  tracks.splice(dest, 0, t);
  return { ...set, tracks };
}

// ---------------------------------------------------------------- groups: move, nudge, paste, duplicate
/**
 * Plans moving the clips `ids` by `dt` seconds and `rows` tracks. A row shift that would put
 * a clip on a missing or incompatible track is reduced toward 0 until it fits; the time shift
 * is resolved with resolveShift. Returns null when there is no free position at all.
 */
export function planMove(
  set: SetData, ids: string[], dt: number, rows: number, acceptsType: (type: string, target: Target) => boolean, forward = false,
): Move[] | null {
  const locs = ids.map((id) => findClip(set, id)).filter((l): l is Located => !!l);
  if (!locs.length) return [];
  const rowOk = (r: number) => locs.every((l) => {
    const t = set.tracks[l.trackIndex + r];
    return !!t && acceptsType(l.clip.type, t.target);
  });
  let r = rows;
  while (r !== 0 && !rowOk(r)) r += r > 0 ? -1 : 1;
  const moving = new Set(ids);
  const spanCache = new Map<string, Span[]>();
  const spansOf = (t: Track) => {
    let s = spanCache.get(t.id);
    if (!s) spanCache.set(t.id, (s = blockers(t, moving)));
    return s;
  };
  const items = locs.map((l) => ({ spans: spansOf(set.tracks[l.trackIndex + r]), start: l.clip.start, end: clipEnd(l.clip) }));
  const shift = resolveShift(items, dt, forward);
  if (shift === null) return null;
  return locs.map((l) => ({ id: l.clip.id, toTrack: set.tracks[l.trackIndex + r].id, start: Math.max(0, l.clip.start + shift) }));
}

export interface ClipboardItem {
  trackId: string;
  target: Target;
  clip: Clip;
}

export function copyClips(set: SetData, ids: string[]): ClipboardItem[] {
  return ids
    .map((id) => findClip(set, id))
    .filter((l): l is Located => !!l)
    .map((l) => ({ trackId: l.track.id, target: l.track.target, clip: l.clip }));
}

/**
 * Inserts copies of `items` with their earliest start at `at` (or later if that is taken), each on
 * its original track if it still exists, else on the first track with the same target.
 */
export function pasteClips(set: SetData, items: ClipboardItem[], at: number): { set: SetData; ids: string[] } {
  if (!items.length) return { set, ids: [] };
  const first = Math.min(...items.map((i) => i.clip.start));
  const placed: { trackId: string; clip: Clip }[] = [];
  for (const it of items) {
    const track = set.tracks.find((t) => t.id === it.trackId) ?? set.tracks.find((t) => t.target === it.target);
    if (!track) continue;
    placed.push({ trackId: track.id, clip: { ...it.clip, id: newId(), params: { ...it.clip.params }, start: it.clip.start - first + at } });
  }
  if (!placed.length) return { set, ids: [] };
  const items2 = placed.map((p) => ({ spans: blockers(set.tracks.find((t) => t.id === p.trackId)!), start: p.clip.start, end: clipEnd(p.clip) }));
  const shift = resolveShift(items2, 0, true) ?? 0;
  let out = set;
  for (const p of placed) out = addClips(out, p.trackId, [{ ...p.clip, start: round(Math.max(0, p.clip.start + shift)) }]);
  return { set: out, ids: placed.map((p) => p.clip.id) };
}

/** Copies of the clips right after the selection's end (or the next free place). */
export function duplicateClips(set: SetData, ids: string[]): { set: SetData; ids: string[] } {
  const items = copyClips(set, ids);
  if (!items.length) return { set, ids: [] };
  const end = Math.max(...items.map((i) => clipEnd(i.clip)));
  return pasteClips(set, items, end);
}

/** All clip ids of the set in track order. */
export const allClipIds = (set: SetData) => set.tracks.flatMap((t) => t.clips.map((c) => c.id));

/** The time span covered by the clips `ids`, or null. */
export function spanOf(set: SetData, ids: string[]): Span | null {
  let s = Infinity;
  let e = -Infinity;
  for (const id of ids) {
    const l = findClip(set, id);
    if (!l) continue;
    s = Math.min(s, l.clip.start);
    e = Math.max(e, clipEnd(l.clip));
  }
  return s < e ? { start: s, end: e } : null;
}
