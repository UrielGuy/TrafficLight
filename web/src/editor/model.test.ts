import { describe, expect, it } from 'vitest';
import type { Clip, SetData, Target, Track } from '../api/types';
import {
  accepts, addTrack, applyMoves, applyTrim, blockers, duplicateClips, gapAt, moveTrack, pasteClips, copyClips, placeNew,
  planMove, removeClips, resolveShift, snapBlock, snapPoints, snapTarget, split, splitClips, trim, MIN_DURATION,
} from './model';

const clip = (id: string, start: number, duration: number, type = 'text', params: Record<string, unknown> = {}): Clip => ({
  id, type, start, duration, params, mask: null, opacity: 1, fade_in: 0, fade_out: 0,
});
const track = (id: string, target: Target, clips: Clip[] = []): Track => ({ id, name: id, target, muted: false, clips });
const mkSet = (tracks: Track[]): SetData => ({ id: 's', name: 'S', bpm: 120, beat_offset: 0, duration: 30, in_rotation: true, tracks });
const TYPES: Record<string, string[]> = { text: ['face'], light: ['face', 'all'], audio: ['audio'], video: ['face', 'all'] };
const acc = (type: string, target: Target) => accepts(TYPES[type], target);

describe('accepts', () => {
  it('maps targets to track kinds', () => {
    expect(accepts(['face'], 'face1')).toBe(true);
    expect(accepts(['face'], 'all')).toBe(false);
    expect(accepts(['face', 'all'], 'all')).toBe(true);
    expect(accepts(['audio'], 'audio')).toBe(true);
    expect(accepts(['face', 'all'], 'audio')).toBe(false);
  });
});

describe('gaps and placement', () => {
  const t = track('t', 'face0', [clip('a', 2, 2), clip('b', 6, 4)]);
  it('finds the gap under a time', () => {
    const s = blockers(t);
    expect(gapAt(s, 1)).toEqual({ start: 0, end: 2 });
    expect(gapAt(s, 5)).toEqual({ start: 4, end: 6 });
    expect(gapAt(s, 3)).toBeNull();
    expect(gapAt(s, 11)).toEqual({ start: 10, end: Infinity });
  });
  it('drops a new clip where it fits', () => {
    expect(placeNew(t, 11, 4)).toEqual({ start: 11, duration: 4 });
  });
  it('clamps a new clip into the gap under the pointer', () => {
    expect(placeNew(t, 0.5, 1.5)).toEqual({ start: 0.5, duration: 1.5 });
    expect(placeNew(t, 1, 1.5)).toEqual({ start: 0.5, duration: 1.5 });
  });
  it('shortens a new clip to fill a smaller gap', () => {
    expect(placeNew(t, 4.5, 4)).toEqual({ start: 4, duration: 2 });
  });
  it('moves a new clip dropped onto a clip to the nearest free place', () => {
    expect(placeNew(t, 7, 1)).toEqual({ start: 5, duration: 1 });
    expect(placeNew(t, 9, 1)).toEqual({ start: 10, duration: 1 });
    expect(placeNew(t, 2.2, 1)).toEqual({ start: 1, duration: 1 });
  });
  it('never places before 0', () => {
    expect(placeNew(t, -3, 1)).toEqual({ start: 0, duration: 1 });
  });
});

describe('resolveShift', () => {
  const spans = [{ start: 4, end: 8 }];
  it('keeps a free shift', () => {
    expect(resolveShift([{ spans, start: 0, end: 2 }], 1)).toBe(1);
  });
  it('butts up against a neighbour', () => {
    expect(resolveShift([{ spans, start: 0, end: 2 }], 3)).toBe(2);
  });
  it('hops past a neighbour when that is nearer', () => {
    expect(resolveShift([{ spans, start: 0, end: 2 }], 6.5)).toBe(8);
  });
  it('stops at 0', () => {
    expect(resolveShift([{ spans: [], start: 2, end: 3 }], -5)).toBe(-2);
  });
  it('forward only looks later', () => {
    expect(resolveShift([{ spans, start: 0, end: 2 }], 3, true)).toBe(8);
  });
  it('moves a group as one block', () => {
    const items = [
      { spans: [{ start: 10, end: 12 }], start: 0, end: 2 },
      { spans: [], start: 1, end: 5 },
    ];
    expect(resolveShift(items, 9)).toBe(8);
  });
});

describe('planMove', () => {
  const s = mkSet([
    track('all', 'all', [clip('v', 0, 4, 'video')]),
    track('top', 'face0', [clip('t1', 0, 2), clip('t2', 4, 2)]),
    track('mid', 'face1', [clip('m1', 1, 2)]),
    track('aud', 'audio', [clip('a1', 0, 5, 'audio')]),
  ]);
  it('moves along a track', () => {
    expect(planMove(s, ['t1'], 1, 0, acc)).toEqual([{ id: 't1', toTrack: 'top', start: 1 }]);
  });
  it('does not overlap the neighbour', () => {
    expect(planMove(s, ['t1'], 3, 0, acc)).toEqual([{ id: 't1', toTrack: 'top', start: 2 }]);
  });
  it('moves to another compatible track, avoiding its clips', () => {
    expect(planMove(s, ['t1'], 0, 1, acc)).toEqual([{ id: 't1', toTrack: 'mid', start: 3 }]);
  });
  it('stays on the last compatible track', () => {
    expect(planMove(s, ['t1'], 0, -1, acc)).toEqual([{ id: 't1', toTrack: 'top', start: 0 }]);
    expect(planMove(s, ['m1'], 0, 5, acc)![0].toTrack).toBe('mid');
  });
  it('applies the moves', () => {
    const out = applyMoves(s, planMove(s, ['t1'], 8, 1, acc)!);
    expect(out.tracks[1].clips.map((c) => c.id)).toEqual(['t2']);
    expect(out.tracks[2].clips.map((c) => [c.id, c.start])).toEqual([['m1', 1], ['t1', 8]]);
    expect(s.tracks[1].clips.length).toBe(2); // immutable
  });
});

describe('trim', () => {
  const t = track('t', 'face0', [clip('a', 2, 2), clip('b', 6, 4), clip('c', 12, 1)]);
  const b = t.clips[1];
  it('trims the end up to the next clip', () => {
    expect(trim(t, b, 'end', 11)).toEqual({ start: 6, duration: 4 + 1 });
    expect(trim(t, b, 'end', 20)).toEqual({ start: 6, duration: 6 });
  });
  it('trims the start back to the previous clip', () => {
    expect(trim(t, b, 'start', 5)).toEqual({ start: 5, duration: 5 });
    expect(trim(t, b, 'start', 0)).toEqual({ start: 4, duration: 6 });
  });
  it('keeps a minimum length', () => {
    expect(trim(t, b, 'start', 50).duration).toBeCloseTo(MIN_DURATION);
    expect(trim(t, b, 'end', 0).duration).toBeCloseTo(MIN_DURATION);
  });
  it('respects minStart (start of the media)', () => {
    expect(trim(t, b, 'start', 4.5, 5.5)).toEqual({ start: 5.5, duration: 4.5 });
  });
  it('moves the media in-point with a start trim', () => {
    const v = clip('v', 6, 4, 'video', { offset: 1 });
    expect(applyTrim(v, 7, 3, true).params.offset).toBe(2);
    expect(applyTrim(v, 5, 5, true).params.offset).toBe(0);
    expect(applyTrim(v, 7, 3, false).params.offset).toBe(1);
  });
});

describe('split', () => {
  it('cuts a clip in two', () => {
    const c = { ...clip('a', 2, 4, 'video', { offset: 1 }), fade_in: 0.5, fade_out: 1 };
    const [a, b] = split(c, 3, true)!;
    expect([a.start, a.duration, a.fade_in, a.fade_out]).toEqual([2, 1, 0.5, 0]);
    expect([b.start, b.duration, b.fade_in, b.fade_out, b.params.offset]).toEqual([3, 3, 0, 1, 2]);
    expect(b.id).not.toBe(a.id);
    expect(a.id).toBe('a');
  });
  it('does nothing outside the clip', () => {
    expect(split(clip('a', 2, 4), 2, false)).toBeNull();
    expect(split(clip('a', 2, 4), 7, false)).toBeNull();
  });
  it('splits selected clips in a set', () => {
    const s = mkSet([track('t', 'face0', [clip('a', 0, 4), clip('b', 5, 2)])]);
    const r = splitClips(s, ['a', 'b'], 2, new Set());
    expect(r.created.length).toBe(1);
    expect(r.set.tracks[0].clips.map((c) => [c.start, c.duration])).toEqual([[0, 2], [2, 2], [5, 2]]);
  });
});

describe('snapping', () => {
  const ctx = { points: [0, 3.3, 10], grid: { bpm: 120, offset: 0, stepBeats: 1 }, threshold: 0.2 };
  it('pulls to a nearby point', () => {
    expect(snapTarget(3.25, ctx)).toEqual({ t: 3.3, magnet: true });
  });
  it('quantizes to the grid otherwise', () => {
    expect(snapTarget(1.9, ctx)).toEqual({ t: 2, magnet: false });
    expect(snapTarget(1.7, { ...ctx, threshold: 0.1 })).toEqual({ t: 1.5, magnet: false });
    expect(snapTarget(1.7, { ...ctx, grid: null })).toBeNull();
  });
  it('prefers a point over the grid', () => {
    expect(snapTarget(0.1, { ...ctx, points: [0.2] })).toEqual({ t: 0.2, magnet: true });
  });
  it('snaps whichever edge of a block is nearer to a point', () => {
    expect(snapBlock(3.2, 2, ctx)).toEqual({ start: 3.3, at: 3.3 });
    expect(snapBlock(8.05, 2, ctx)).toEqual({ start: 8, at: 10 });
  });
  it('quantizes the start of a block when no point is near', () => {
    expect(snapBlock(5.2, 1, ctx)).toEqual({ start: 5, at: null });
    expect(snapBlock(5.2, 1, { ...ctx, grid: { bpm: 120, offset: 0.1, stepBeats: 4 } })).toEqual({ start: 6.1, at: null });
    expect(snapBlock(5.2, 1, { ...ctx, grid: null })).toEqual({ start: 5.2, at: null });
  });
  it('collects edges except the moving clips', () => {
    const s = mkSet([track('t', 'face0', [clip('a', 0, 2), clip('b', 2, 3)])]);
    expect(snapPoints(s, new Set(['b']), [7])).toEqual([0, 2, 7]);
  });
});

describe('set edits', () => {
  const s = mkSet([track('top', 'face0', [clip('a', 0, 2), clip('b', 2, 2)]), track('aud', 'audio')]);
  it('removes clips', () => {
    expect(removeClips(s, new Set(['a'])).tracks[0].clips.map((c) => c.id)).toEqual(['b']);
    expect(removeClips(s, new Set(['zz']))).toBe(s);
  });
  it('duplicates after the selection', () => {
    const r = duplicateClips(s, ['a', 'b']);
    expect(r.ids.length).toBe(2);
    expect(r.set.tracks[0].clips.map((c) => c.start)).toEqual([0, 2, 4, 6]);
  });
  it('pastes at a time, later if taken', () => {
    const items = copyClips(s, ['a']);
    expect(pasteClips(s, items, 10).set.tracks[0].clips.map((c) => c.start)).toEqual([0, 2, 10]);
    expect(pasteClips(s, items, 1).set.tracks[0].clips.map((c) => c.start)).toEqual([0, 2, 4]);
  });
  it('adds a track after the last of its kind', () => {
    const r = addTrack(s, 'face0');
    expect(r.set.tracks.map((t) => t.target)).toEqual(['face0', 'face0', 'audio']);
    expect(r.set.tracks[1].name).toBe('Top 2');
    const r2 = addTrack(s, 'all');
    expect(r2.set.tracks.map((t) => t.target)).toEqual(['face0', 'all', 'audio']);
  });
  it('reorders tracks', () => {
    expect(moveTrack(s, 'aud', 0).tracks.map((t) => t.id)).toEqual(['aud', 'top']);
  });
});
