import { describe, expect, it } from 'vitest';
import { PANELS, PANEL_NAMES, PORTS, fromSeen, sameSeen, seenProblems, toSeen, type Seen } from './wiring';

const up = (connector: number): Seen => ({ connector, down: false });
const down = (connector: number): Seen => ({ connector, down: true });

describe('fromSeen', () => {
  it('turns the worked example into settings (same case as tests/test_output_wiring.py)', () => {
    const seen = [up(3), down(8), up(2), down(1), up(5), down(4)];
    expect(fromSeen(seen)).toEqual({ panel_order: [2, 7, 1, 0, 4, 3], panel_flip: [false, true, false, true, false, true] });
  });

  it('is the identity for a sign wired J1..J6 from the top, upright', () => {
    expect(fromSeen([1, 2, 3, 4, 5, 6].map(up))).toEqual({ panel_order: [0, 1, 2, 3, 4, 5], panel_flip: Array(6).fill(false) });
  });

  it('gives dark panels the lowest free connectors so the others can be saved', () => {
    expect(fromSeen([down(2), up(1), null, up(3), null, up(8)])).toEqual({
      panel_order: [1, 0, 3, 2, 4, 7], panel_flip: [true, false, false, false, false, false],
    });
    expect(fromSeen(Array(PANELS).fill(null)).panel_order).toEqual([0, 1, 2, 3, 4, 5]);
  });

  it('refuses duplicates', () => {
    expect(() => fromSeen([up(3), down(3), up(1), up(2), up(4), up(5)])).toThrow(/both show 3/);
  });
});

describe('seenProblems', () => {
  it('names the panels that show the same number', () => {
    const p = seenProblems([up(3), up(1), up(2), up(4), down(3), up(5)]);
    expect(p).toHaveLength(1);
    expect(p[0].panels).toEqual([0, 4]);
    expect(p[0].message).toContain(`${PANEL_NAMES[0]} and ${PANEL_NAMES[4]} both show 3`);
  });

  it('reports three panels at once and out-of-range numbers', () => {
    const p = seenProblems([up(2), up(2), up(2), up(0), up(PORTS + 1), null]);
    expect(p.map((x) => x.panels)).toEqual([[3], [4], [0, 1, 2]]);
    expect(seenProblems([up(1)])[0].message).toContain(`${PANELS} panels`);
  });

  it('accepts dark panels and any six distinct connectors', () => {
    expect(seenProblems([up(8), null, down(7), null, up(1), down(2)])).toEqual([]);
  });
});

describe('toSeen', () => {
  it('is the inverse of fromSeen', () => {
    const seen = [up(3), down(8), up(2), down(1), up(5), down(4)];
    const { panel_order, panel_flip } = fromSeen(seen);
    expect(toSeen(panel_order, panel_flip, false)).toEqual(seen);
  });

  it('folds rotate180 in: the reversed order with every panel flipped', () => {
    expect(toSeen([0, 1, 2, 3, 4, 5], Array(6).fill(false), true)).toEqual([6, 5, 4, 3, 2, 1].map(down));
    expect(toSeen([7, 2, 0, 5, 1, 4], [true, false, false, true, false, false], true))
      .toEqual([down(5), down(2), up(6), down(1), down(3), up(8)]);   // same as wiring.to_seen in Python
  });

  it('round-trips random settings through what the panels would show', () => {
    let seed = 1;
    const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
    for (let k = 0; k < 200; k++) {
      const bands = Array.from({ length: PORTS }, (_, b) => b).sort(() => rand() - 0.5).slice(0, PANELS);
      const flips = bands.map(() => rand() < 0.5);
      expect(fromSeen(toSeen(bands, flips, false))).toEqual({ panel_order: bands, panel_flip: flips });
    }
  });
});

describe('sameSeen', () => {
  it('compares observations', () => {
    expect(sameSeen([up(1), null], [up(1), null])).toBe(true);
    expect(sameSeen([up(1), null], [down(1), null])).toBe(false);
    expect(sameSeen([up(1), null], [up(1), up(2)])).toBe(false);
  });
});
