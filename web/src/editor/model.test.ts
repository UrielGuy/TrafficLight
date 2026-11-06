import { describe, expect, it } from 'vitest';
import type { Clip, SetData, Target, Track } from '../api/types';
import {
  accepts, addTrack, applyMoves, applyTrim, blockers, duplicateClips, gapAt, moveTrack, pasteClips, copyClips, placeNew,
  planMove, removeClips, resolveShift, rowTracks, snapBlock, snapPoints, snapTarget, split, splitClips, toLight, toWhole, trim,
  unmuteHidden, wholeTrack, mediaSpeed, trimFloor, MIN_DURATION,
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
  const set = mkSet([t]);
  const placeNewT = (at: number, d: number) => placeNew(set, t, at, d);
  it('finds the gap under a time', () => {
    const s = blockers(set, t);
    expect(gapAt(s, 1)).toEqual({ start: 0, end: 2 });
    expect(gapAt(s, 5)).toEqual({ start: 4, end: 6 });
    expect(gapAt(s, 3)).toBeNull();
    expect(gapAt(s, 11)).toEqual({ start: 10, end: Infinity });
  });
  it('drops a new clip where it fits', () => {
    expect(placeNewT(11, 4)).toEqual({ start: 11, duration: 4 });
  });
  it('clamps a new clip into the gap under the pointer', () => {
    expect(placeNewT(0.5, 1.5)).toEqual({ start: 0.5, duration: 1.5 });
    expect(placeNewT(1, 1.5)).toEqual({ start: 0.5, duration: 1.5 });
  });
  it('shortens a new clip to fill a smaller gap', () => {
    expect(placeNewT(4.5, 4)).toEqual({ start: 4, duration: 2 });
  });
  it('moves a new clip dropped onto a clip to the nearest free place', () => {
    expect(placeNewT(7, 1)).toEqual({ start: 5, duration: 1 });
    expect(placeNewT(9, 1)).toEqual({ start: 10, duration: 1 });
    expect(placeNewT(2.2, 1)).toEqual({ start: 1, duration: 1 });
  });
  it('never places before 0', () => {
    expect(placeNewT(-3, 1)).toEqual({ start: 0, duration: 1 });
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
    track('all', 'all', [clip('v', 10, 4, 'video')]),
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
  const t0 = track('t', 'face0', [clip('a', 2, 2), clip('b', 6, 4), clip('c', 12, 1)]);
  const set = mkSet([t0]);
  const b = t0.clips[1];
  const t = { trim: (tr: Track, c: Clip, e: 'start' | 'end', to: number, min?: number) => trim(set, tr, c, e, to, min) };
  it('trims the end up to the next clip', () => {
    expect(t.trim(t0, b, 'end', 11)).toEqual({ start: 6, duration: 4 + 1 });
    expect(t.trim(t0, b, 'end', 20)).toEqual({ start: 6, duration: 6 });
  });
  it('trims the start back to the previous clip', () => {
    expect(t.trim(t0, b, 'start', 5)).toEqual({ start: 5, duration: 5 });
    expect(t.trim(t0, b, 'start', 0)).toEqual({ start: 4, duration: 6 });
  });
  it('keeps a minimum length', () => {
    expect(t.trim(t0, b, 'start', 50).duration).toBeCloseTo(MIN_DURATION);
    expect(t.trim(t0, b, 'end', 0).duration).toBeCloseTo(MIN_DURATION);
  });
  it('respects minStart (start of the media)', () => {
    expect(t.trim(t0, b, 'start', 4.5, 5.5)).toEqual({ start: 5.5, duration: 4.5 });
  });
  it('moves the media in-point with a start trim', () => {
    const v = clip('v', 6, 4, 'video', { offset: 1 });
    expect(applyTrim(v, 7, 3, true).params.offset).toBe(2);
    expect(applyTrim(v, 5, 5, true).params.offset).toBe(0);
    expect(applyTrim(v, 7, 3, false).params.offset).toBe(1);
  });
});

describe('speed of media clips', () => {
  it('a start trim moves the in-point by the media time it covers', () => {
    const v = clip('v', 6, 4, 'video', { offset: 1, speed: 2 });
    expect(applyTrim(v, 7, 3, true).params.offset).toBe(3);
    expect(applyTrim({ ...v, params: { offset: 1, speed: 0.5 } }, 7, 3, true).params.offset).toBe(1.5);
    // a still frame stays the same frame
    expect(applyTrim({ ...v, params: { offset: 1, speed: 0 } }, 8, 2, true).params.offset).toBe(1);
  });
  it('a split keeps the picture continuous', () => {
    const [, b2] = split(clip('v', 2, 4, 'video', { offset: 1, speed: 2 }), 3, true)!;
    expect(b2.params.offset).toBe(3);
    const [, h] = split(clip('v', 2, 4, 'video', { offset: 1, speed: 0.5 }), 3, true)!;
    expect(h.params.offset).toBe(1.5);
    const [, z] = split(clip('v', 2, 4, 'video', { offset: 1, speed: 0 }), 3, true)!;
    expect(z.params.offset).toBe(1);
  });
  it('a start trim stops where the file begins, in clip time', () => {
    expect(trimFloor(clip('v', 6, 4, 'video', { offset: 2, speed: 2 }))).toBe(5);
    expect(trimFloor(clip('v', 6, 4, 'video', { offset: 2, speed: 0.5 }))).toBe(2);
    expect(trimFloor(clip('v', 6, 4, 'video', { offset: 2, speed: 0 }))).toBe(0);
    expect(trimFloor(clip('v', 6, 4, 'video', { offset: 2 }))).toBe(4);
  });
  it('reads the speed as the server does', () => {
    expect(mediaSpeed(clip('v', 0, 1, 'video', { speed: 'x' }))).toBe(1);
    expect(mediaSpeed(clip('v', 0, 1, 'video', { speed: 20 }))).toBe(8);
    expect(mediaSpeed(clip('a', 0, 1, 'audio'))).toBe(1);
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
  const ctx = { points: [0, 3.3, 10], grid: { offset: 0, step: 0.5 }, threshold: 0.2 };
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
    expect(snapBlock(5.2, 1, { ...ctx, grid: { offset: 0.1, step: 2 } })).toEqual({ start: 6.1, at: null });
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
    const r2 = addTrack(s, 'face2');
    expect(r2.set.tracks.map((t) => t.target)).toEqual(['face0', 'face2', 'audio']);
    expect(addTrack(s, 'audio', 0).set.tracks.map((t) => t.target)).toEqual(['face0', 'audio', 'audio']);
  });
  it('keeps the lights above the audio rows when reordering', () => {
    expect(moveTrack(s, 'aud', 0)).toBe(s);
    const s3 = mkSet([track('w', 'all'), track('top', 'face0'), track('mid', 'face1'), track('aud', 'audio')]);
    expect(moveTrack(s3, 'mid', 0).tracks.map((t) => t.id)).toEqual(['w', 'mid', 'top', 'aud']);
    expect(moveTrack(s3, 'top', 5).tracks.map((t) => t.id)).toEqual(['w', 'mid', 'top', 'aud']);
  });
});

describe('whole sign', () => {
  // whole-sign clip W 4..6 (and W2 12..14 on a second whole-sign track); lights: top 0..2, mid 7..9; audio 0..10
  const base = () => mkSet([
    track('w', 'all', [clip('W', 4, 2, 'video')]),
    track('top', 'face0', [clip('T', 0, 2)]),
    track('mid', 'face1', [clip('M', 7, 2, 'video')]),
    track('bot', 'face2'),
    track('aud', 'audio', [clip('A', 0, 10, 'audio')]),
    track('w2', 'all', [clip('W2', 12, 2, 'video')]),
  ]);
  const tr = (s: SetData, id: string) => s.tracks.find((t) => t.id === id)!;

  it('has no row for whole-sign tracks; lights come before audio whatever the stored order', () => {
    const mixed = [track('aud', 'audio'), track('w', 'all'), track('top', 'face0'), track('mid', 'face1')];
    expect(rowTracks(mixed).map((t) => t.id)).toEqual(['top', 'mid', 'aud']);
  });
  it('a light lane is blocked by its own clips only: it may lie over a whole-sign clip', () => {
    const s = base();
    expect(blockers(s, tr(s, 'bot'))).toEqual([]);
    expect(blockers(s, tr(s, 'top'))).toEqual([{ start: 0, end: 2 }]);
  });
  it('the whole-sign lane is blocked by the whole-sign clips of every whole-sign track', () => {
    const s = base();
    expect(blockers(s, tr(s, 'w'))).toEqual([{ start: 4, end: 6 }, { start: 12, end: 14 }]);
    expect(blockers(s, tr(s, 'w2'), new Set(['W2']))).toEqual([{ start: 4, end: 6 }]);
  });
  it('audio is not affected', () => {
    const s = base();
    expect(blockers(s, tr(s, 'aud'))).toEqual([{ start: 0, end: 10 }]);
  });
  it('a new light clip may be dropped over a whole-sign clip', () => {
    const s = base();
    expect(placeNew(s, tr(s, 'bot'), 4.2, 1)).toEqual({ start: 4.2, duration: 1 });
    expect(placeNew(s, tr(s, 'mid'), 4.5, 1)).toEqual({ start: 4.5, duration: 1 });
  });
  it('a new whole-sign clip may go behind light clips, not over another whole-sign clip', () => {
    const s = base();
    expect(placeNew(s, tr(s, 'w'), 0.5, 1)).toEqual({ start: 0.5, duration: 1 });
    expect(placeNew(s, tr(s, 'w'), 4.5, 1)).toEqual({ start: 6, duration: 1 });
    expect(placeNew(s, tr(s, 'w'), 7, 6)).toEqual({ start: 6, duration: 6 }); // the gap 6..12 holds it
    expect(placeNew(s, tr(s, 'w'), 13.5, 1)).toEqual({ start: 14, duration: 1 }); // W2 is on another whole-sign track
  });
  it('a light clip may be moved and trimmed over a whole-sign clip', () => {
    const s = base();
    expect(planMove(s, ['T'], 3, 0, acc)).toEqual([{ id: 'T', toTrack: 'top', start: 3 }]);
    expect(planMove(s, ['T'], 4.2, 2, acc)).toEqual([{ id: 'T', toTrack: 'bot', start: 4.2 }]);
    expect(trim(s, tr(s, 'top'), s.tracks[1].clips[0], 'end', 5)).toEqual({ start: 0, duration: 5 });
    expect(trim(s, tr(s, 'mid'), s.tracks[2].clips[0], 'start', 5)).toEqual({ start: 5, duration: 4 });
    // still not over a clip of its own row
    expect(planMove(s, ['M'], 0, -1, acc)).toEqual([{ id: 'M', toTrack: 'top', start: 7 }]);
    expect(planMove(s, ['M'], -6, -1, acc)).toEqual([{ id: 'M', toTrack: 'top', start: 2 }]);
  });
  it('a whole-sign clip may be moved and trimmed behind light clips, not over another whole-sign clip', () => {
    const s = base();
    expect(planMove(s, ['W'], 3, 0, acc)).toEqual([{ id: 'W', toTrack: 'w', start: 7 }]);
    expect(planMove(s, ['W'], -4, 0, acc)).toEqual([{ id: 'W', toTrack: 'w', start: 0 }]);
    expect(planMove(s, ['W'], 7.5, 0, acc)).toEqual([{ id: 'W', toTrack: 'w', start: 10 }]); // butts up against W2
    expect(trim(s, tr(s, 'w'), s.tracks[0].clips[0], 'end', 20)).toEqual({ start: 4, duration: 8 });
    expect(trim(s, tr(s, 'w'), s.tracks[0].clips[0], 'start', 0)).toEqual({ start: 0, duration: 6 });
  });
  it('a whole-sign clip keeps its track when the rows change', () => {
    const s = base();
    expect(planMove(s, ['W'], 0, 1, acc)).toEqual([{ id: 'W', toTrack: 'w', start: 4 }]);
    // with a light clip in the group, that one changes row and the whole-sign one stays
    expect(planMove(s, ['W', 'T'], 0, 1, acc)).toEqual([{ id: 'W', toTrack: 'w', start: 4 }, { id: 'T', toTrack: 'mid', start: 0 }]);
  });
  it('moves a whole-sign clip and light clips as one block', () => {
    const s = base();
    // W 4..6 and T 0..2 by +7: W would run into W2 (12..14), so the block stops where W touches it
    expect(planMove(s, ['W', 'T'], 7, 0, acc)).toEqual([{ id: 'W', toTrack: 'w', start: 10 }, { id: 'T', toTrack: 'top', start: 6 }]);
  });
  it('rows count without the whole-sign track', () => {
    const s = base();
    expect(planMove(s, ['M'], 0, 2, acc)![0].toTrack).toBe('bot'); // not onto audio
  });
  it('pastes and duplicates under the same rule', () => {
    const s = base();
    expect(pasteClips(s, copyClips(s, ['W']), 5).set.tracks[0].clips.map((c) => c.start)).toEqual([4, 6]);
    expect(pasteClips(s, copyClips(s, ['T']), 3.5).set.tracks[1].clips.map((c) => c.start)).toEqual([0, 3.5]);
    expect(duplicateClips(s, ['W']).set.tracks[0].clips.map((c) => c.start)).toEqual([4, 6]);
  });
  it('pasting a whole-sign clip into a set without a whole-sign track makes one', () => {
    const s = base();
    const items = copyClips(s, ['W']);
    const bare = mkSet(s.tracks.slice(1, 5).map((t) => ({ ...t, clips: [] })));
    const r = pasteClips(bare, items, 1);
    expect(r.set.tracks[0].target).toBe('all');
    expect(r.set.tracks[0].clips.map((c) => [c.start, c.duration])).toEqual([[1, 2]]);
  });
  it('splitting a whole-sign clip keeps it whole', () => {
    const r = splitClips(base(), ['W'], 5, new Set());
    expect(r.set.tracks[0].clips.map((c) => [c.start, c.duration])).toEqual([[4, 1], [5, 1]]);
  });
  it('makes the whole-sign track when there is none', () => {
    const s = mkSet([track('top', 'face0')]);
    const r = wholeTrack(s);
    expect(r.set.tracks.map((t) => t.target)).toEqual(['all', 'face0']);
    expect(wholeTrack(r.set).track).toBe(r.track);
    expect(wholeTrack(base()).set).toEqual(base());
  });
  it('turns a light clip into a whole-sign clip and back, over other light clips too', () => {
    const s = mkSet([track('top', 'face0', [clip('T', 0, 2)]), track('mid', 'face1', [clip('M', 1, 1)])]);
    const r = toWhole(s, 'T');
    if (!('set' in r)) throw new Error(r.error);
    expect(r.set.tracks[0].target).toBe('all');
    expect(r.set.tracks[0].clips.map((c) => [c.id, c.start, c.duration])).toEqual([['T', 0, 2]]);
    // back: the middle light is taken then, so the preferred row is skipped
    const back = toLight(r.set, 'T', 'mid');
    if (!('set' in back)) throw new Error(back.error);
    expect(back.set.tracks.find((t) => t.id === 'top')!.clips.map((c) => c.id)).toEqual(['T']);
    const back2 = toLight({ ...r.set, tracks: r.set.tracks.map((t) => (t.id === 'mid' ? { ...t, clips: [] } : t)) }, 'T', 'mid');
    expect('set' in back2 && back2.set.tracks.find((t) => t.id === 'mid')!.clips.map((c) => c.id)).toEqual(['T']);
  });
  it('refuses to make it whole-sign over another whole-sign clip', () => {
    const s = mkSet([track('w', 'all', [clip('W', 1, 3)]), track('top', 'face0', [clip('T', 0, 2)])]);
    expect(toWhole(s, 'T')).toHaveProperty('error');
  });
  it('refuses to put it on a light when every light row is taken then', () => {
    const s = mkSet([
      track('w', 'all', [clip('W', 0, 4, 'video')]),
      track('top', 'face0', [clip('T', 1, 1)]),
      track('mid', 'face1', [clip('M', 3, 2)]),
    ]);
    expect(toLight(s, 'W')).toHaveProperty('error');
  });
  it('older data that breaks the rule: what is moved next must fit, the rest stays', () => {
    const s = mkSet([
      track('w', 'all', [clip('W', 0, 4, 'video'), clip('V', 2, 4, 'video')]),
      track('top', 'face0', [clip('T', 1, 1)]),
    ]);
    const out = applyMoves(s, planMove(s, ['V'], 0.5, 0, acc)!);
    expect(out.tracks[0].clips.map((c) => [c.id, c.start])).toEqual([['W', 0], ['V', 4]]);
    expect(out.tracks[1].clips[0].start).toBe(1);
  });
  it('drops a mute stored on a whole-sign track', () => {
    const s = mkSet([{ ...track('w', 'all'), muted: true }, { ...track('top', 'face0'), muted: true }]);
    const u = unmuteHidden(s);
    expect(u.tracks.map((t) => t.muted)).toEqual([false, true]);
    expect(unmuteHidden(u)).toBe(u);
  });
});
