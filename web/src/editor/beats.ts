// Time, beat grid and tempo helpers for the editor. Pure functions (unit-tested).

export const BEATS_PER_BAR = 4;

export const secondsPerBeat = (bpm: number) => 60 / Math.max(1e-6, bpm);

/** Fractional beats since beat 0 of the set's grid (negative before `offset`). */
export const beatAt = (t: number, bpm: number, offset: number) => ((t - offset) * bpm) / 60;

/** Time of the grid line nearest `t`, the grid being every `stepBeats` beats from `offset`. */
export function nearestGrid(t: number, bpm: number, offset: number, stepBeats: number): number {
  const step = secondsPerBeat(bpm) * stepBeats;
  return offset + Math.round((t - offset) / step) * step;
}

/** "12.35 s" below a minute, "1:02.35" from then on. */
export function formatSeconds(t: number): string {
  if (!isFinite(t)) return '–';
  const neg = t < 0;
  const a = Math.abs(t);
  if (a < 60) return `${neg ? '-' : ''}${a.toFixed(2)} s`;
  const m = Math.floor(a / 60);
  const s = a - m * 60;
  return `${neg ? '-' : ''}${m}:${s.toFixed(2).padStart(5, '0')}`;
}

/** Short label for the ruler: "5s", "1:30". */
export function formatRulerTime(t: number, step: number): string {
  const r = Math.round(t * 1000) / 1000;
  if (step < 1) return `${parseFloat(r.toFixed(2))}s`;
  return r < 60 ? `${Math.round(r)}s` : clock(r);
}
function clock(r: number): string {
  const m = Math.floor(r / 60);
  const s = Math.round(r - m * 60);
  return `${m}:${String(s).padStart(2, '0')}`;
}

/** Bar and beat (both 1-based) at time t: {bar: 3, beat: 2}; null before beat 0. */
export function barBeat(t: number, bpm: number, offset: number): { bar: number; beat: number } | null {
  const b = beatAt(t, bpm, offset) + 1e-6;
  if (b < 0) return null;
  const whole = Math.floor(b);
  return { bar: Math.floor(whole / BEATS_PER_BAR) + 1, beat: (whole % BEATS_PER_BAR) + 1 };
}

const NICE = [0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
/** The smallest "nice" seconds step whose labels are at least `minPx` apart at `pps` pixels per second. */
export function rulerStep(pps: number, minPx = 64): number {
  for (const s of NICE) if (s * pps >= minPx) return s;
  return NICE[NICE.length - 1];
}

/** Beats between snap/grid lines at this zoom: 1 (every beat), 4 (bars), 16 (4 bars) or 0 (too dense). */
export function gridBeats(pps: number, bpm: number, minPx = 10): number {
  const beatPx = secondsPerBeat(bpm) * pps;
  for (const b of [1, BEATS_PER_BAR, BEATS_PER_BAR * 4, BEATS_PER_BAR * 16]) if (beatPx * b >= minPx) return b;
  return 0;
}

/**
 * Tap tempo: `taps` are times in ms (oldest first). Uses the taps after the last pause longer
 * than `resetMs`, at most the last 8; needs two. Returns bpm rounded to 0.1, or null.
 */
export function tapTempo(taps: number[], resetMs = 2000): number | null {
  let from = 0;
  for (let i = 1; i < taps.length; i++) if (taps[i] - taps[i - 1] > resetMs) from = i;
  const use = taps.slice(Math.max(from, taps.length - 8));
  if (use.length < 2) return null;
  const avg = (use[use.length - 1] - use[0]) / (use.length - 1);
  if (avg <= 0) return null;
  const bpm = Math.round((60000 / avg) * 10) / 10;
  return Math.min(400, Math.max(10, bpm));
}
