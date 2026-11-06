import { describe, expect, it } from 'vitest';
import { commit, createHistory, redo, seal, undo } from './history';
import { barBeat, formatSeconds, gridBeats, gridLines, nearestGrid, rulerStep, stepLabel, tapTempo } from './beats';
import { DEFAULT_STEPS, parseGridPref } from './view';

describe('history', () => {
  it('undoes and redoes', () => {
    let h = createHistory(1);
    h = commit(h, 2, { now: 0 });
    h = commit(h, 3, { now: 10_000 });
    expect(h.present).toBe(3);
    h = undo(h);
    expect(h.present).toBe(2);
    h = undo(h);
    expect(h.present).toBe(1);
    expect(undo(h)).toBe(h);
    h = redo(h);
    expect(h.present).toBe(2);
    h = commit(h, 9, { now: 20_000 });
    expect(h.future).toEqual([]);
    expect(redo(h)).toBe(h);
  });
  it('coalesces commits with the same key', () => {
    let h = createHistory('');
    h = commit(h, 'a', { key: 'k', now: 0 });
    h = commit(h, 'ab', { key: 'k', now: 300 });
    h = commit(h, 'abc', { key: 'k', now: 600 });
    expect(h.past).toEqual(['']);
    h = commit(h, 'abcd', { key: 'k', now: 5000 });
    expect(h.past).toEqual(['', 'abc']);
    h = commit(seal(h), 'abcde', { key: 'k', now: 5100 });
    expect(h.past).toEqual(['', 'abc', 'abcd']);
  });
  it('ignores a commit that changes nothing', () => {
    const h = createHistory({});
    expect(commit(h, h.present)).toBe(h);
  });
});

describe('beats', () => {
  it('finds grid lines', () => {
    expect(nearestGrid(1.2, 0, 0.5)).toBe(1);
    expect(nearestGrid(1.3, 0.1, 0.5)).toBeCloseTo(1.1);
    expect(nearestGrid(2.9, 0, 2)).toBe(2);
  });
  it('turns a snap grid into lines', () => {
    // beats: from the set's beat offset, strong lines on bars
    expect(gridLines({ unit: 'beats', step: 1 }, 100, 120, 0.1)).toEqual({ offset: 0.1, step: 0.5, major: 4 });
    expect(gridLines({ unit: 'beats', step: 0.5 }, 100, 120, 0)).toEqual({ offset: 0, step: 0.25, major: 8 });
    expect(gridLines({ unit: 'beats', step: 4 }, 100, 120, 0)).toEqual({ offset: 0, step: 2, major: 4 });
    // time: from zero whatever the beat offset and tempo, strong lines on whole seconds
    expect(gridLines({ unit: 'time', step: 0.25 }, 100, 93, 0.1)).toEqual({ offset: 0, step: 0.25, major: 4 });
    expect(gridLines({ unit: 'time', step: 5 }, 100, 120, 0)).toEqual({ offset: 0, step: 5, major: 2 });
    // step 0 follows the zoom; a fixed step does not
    expect(gridLines({ unit: 'beats', step: 0 }, 10, 120, 0)!.step).toBe(2);
    expect(gridLines({ unit: 'beats', step: 0 }, 0.01, 120, 0)).toBeNull();
    expect(gridLines({ unit: 'time', step: 0 }, 100, 120, 0)!.step).toBe(0.1);
    expect(gridLines({ unit: 'time', step: 0 }, 2, 120, 0)!.step).toBe(5);
    expect(gridLines({ unit: 'beats', step: 1 }, 0.01, 120, 0)!.step).toBe(0.5);
  });
  it('starts time snapping at a quarter second and beats on auto', () => {
    expect(DEFAULT_STEPS).toEqual({ beats: 0, time: 0.25 });
    expect(parseGridPref(null)).toEqual({ unit: 'beats', steps: { beats: 0, time: 0.25 } });
    expect(parseGridPref('{"unit":"time","steps":{"beats":4,"time":0}}')).toEqual({ unit: 'time', steps: { beats: 4, time: 0 } });
    expect(parseGridPref('{"unit":"weeks","steps":{"beats":3,"time":7}}')).toEqual({ unit: 'beats', steps: { beats: 0, time: 0.25 } });
    expect(parseGridPref('not json')).toEqual({ unit: 'beats', steps: { beats: 0, time: 0.25 } });
  });
  it('names snap steps', () => {
    expect(stepLabel({ unit: 'beats', step: 0 })).toBe('Auto');
    expect(stepLabel({ unit: 'beats', step: 0.5 })).toBe('½ beat');
    expect(stepLabel({ unit: 'beats', step: 4 })).toBe('1 bar');
    expect(stepLabel({ unit: 'beats', step: 16 })).toBe('4 bars');
    expect(stepLabel({ unit: 'time', step: 0.25 })).toBe('0.25 s');
    expect(stepLabel({ unit: 'time', step: 60 })).toBe('1 min');
  });
  it('names bars and beats', () => {
    expect(barBeat(0, 120, 0)).toEqual({ bar: 1, beat: 1 });
    expect(barBeat(2.6, 120, 0)).toEqual({ bar: 2, beat: 2 });
    expect(barBeat(0.1, 120, 0.5)).toBeNull();
  });
  it('formats seconds', () => {
    expect(formatSeconds(3.456)).toBe('3.46 s');
    expect(formatSeconds(62.5)).toBe('1:02.50');
  });
  it('chooses ruler and grid steps by zoom', () => {
    expect(rulerStep(100)).toBe(1);
    expect(rulerStep(10)).toBe(10);
    expect(gridBeats(100, 120)).toBe(1);
    expect(gridBeats(10, 120)).toBe(4);
    expect(gridBeats(0.01, 120)).toBe(0);
  });
  it('taps a tempo', () => {
    expect(tapTempo([0])).toBeNull();
    expect(tapTempo([0, 500, 1000, 1500])).toBe(120);
    expect(tapTempo([0, 400, 5000, 5600, 6200])).toBe(100);
  });
});
