import { describe, expect, it } from 'vitest';
import { commit, createHistory, redo, seal, undo } from './history';
import { barBeat, formatSeconds, gridBeats, nearestGrid, rulerStep, tapTempo } from './beats';

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
    expect(nearestGrid(1.2, 120, 0, 1)).toBe(1);
    expect(nearestGrid(1.3, 120, 0.1, 1)).toBeCloseTo(1.1);
    expect(nearestGrid(2.9, 120, 0, 4)).toBe(2);
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
