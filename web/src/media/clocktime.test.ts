import { describe, expect, it } from 'vitest';
import { parseClock } from './clocktime';

describe('parseClock', () => {
  it('reads seconds, minutes and hours', () => {
    expect(parseClock('90')).toBe(90);
    expect(parseClock('2.5')).toBe(2.5);
    expect(parseClock('3:00')).toBe(180);
    expect(parseClock(' 1:30.5 ')).toBe(90.5);
    expect(parseClock('1:02:03')).toBe(3723);
  });
  it('tells an empty field from a wrong one', () => {
    expect(parseClock('')).toBeNull();
    expect(parseClock('   ')).toBeNull();
    for (const bad of ['abc', '1:90', '1.5:00', '1:2:3:4', '-5', '3m', '1:75:00']) expect(parseClock(bad)).toBeNaN();
  });
});
