// Canvas drawing of the time ruler and the beat grid behind the tracks. Both draw
// only the visible window, so the cost does not depend on the set's length.
import { BEATS_PER_BAR, formatRulerTime, gridBeats, rulerStep, secondsPerBeat, type GridLines } from './beats';
import type { LoopRange } from './session';

export interface View {
  pps: number;
  /** Seconds at the left edge of the drawing. */
  t0: number;
  width: number;
  height: number;
  bpm: number;
  offset: number;
  duration: number;
}

const css = (name: string, fallback: string) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

/** Prepares a canvas for drawing in CSS pixels at the device's resolution. */
export function prepare(cv: HTMLCanvasElement): CanvasRenderingContext2D | null {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth;
  const h = cv.clientHeight;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
  }
  const ctx = cv.getContext('2d');
  if (!ctx) return null;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return ctx;
}

/** Beat lines in the visible window: [x, isBar, barNumber]. */
function beatLines(v: View, every: number): [number, boolean, number][] {
  if (every <= 0) return [];
  const spb = secondsPerBeat(v.bpm);
  const t1 = v.t0 + v.width / v.pps;
  const first = Math.ceil((v.t0 - v.offset) / spb / every) * every;
  const out: [number, boolean, number][] = [];
  for (let b = first; v.offset + b * spb <= t1 && out.length < 4000; b += every) {
    const t = v.offset + b * spb;
    if (t < 0) continue;
    out.push([(t - v.t0) * v.pps, b % BEATS_PER_BAR === 0, Math.floor(b / BEATS_PER_BAR) + 1]);
  }
  return out;
}

export function drawRuler(cv: HTMLCanvasElement, v: View, loop: LoopRange, playing: boolean) {
  const ctx = prepare(cv);
  if (!ctx) return;
  const { width: w, height: h, pps, t0 } = v;
  const muted = css('--muted', '#8f99a8');
  const accent = css('--accent', '#ffb000');
  const x = (t: number) => (t - t0) * pps;

  // beyond the set's end
  const endX = x(v.duration);
  if (endX < w) {
    ctx.fillStyle = 'rgba(0,0,0,.35)';
    ctx.fillRect(Math.max(0, endX), 0, w - Math.max(0, endX), h);
  }

  // loop band
  if (loop.on) {
    const lo = Math.max(0, Math.min(loop.from, v.duration));
    const hi = loop.to === null ? v.duration : Math.min(loop.to, v.duration);
    const a = x(lo);
    const b = x(hi);
    ctx.fillStyle = loop.to === null ? 'rgba(255,176,0,.18)' : 'rgba(255,176,0,.35)';
    ctx.fillRect(a, h - 6, b - a, 6);
    if (loop.to !== null) {
      ctx.fillStyle = accent;
      ctx.fillRect(a, h - 12, 2, 12);
      ctx.fillRect(b - 2, h - 12, 2, 12);
    }
  }

  // seconds
  const step = rulerStep(pps);
  const minor = step / (step === 15 || step === 30 ? 3 : 5);
  ctx.font = '11px system-ui, sans-serif';
  ctx.textBaseline = 'top';
  const tEnd = t0 + w / pps;
  ctx.fillStyle = 'rgba(143,153,168,.45)';
  for (let i = Math.max(0, Math.floor(t0 / minor)); i * minor <= tEnd; i++) {
    ctx.fillRect(Math.round(x(i * minor)), 0, 1, 4);
  }
  for (let i = Math.max(0, Math.floor(t0 / step)); i * step <= tEnd + step; i++) {
    const t = i * step;
    const px = Math.round(x(t));
    ctx.fillStyle = muted;
    ctx.fillRect(px, 0, 1, 9);
    ctx.fillText(formatRulerTime(t, step), px + 3, 2);
  }

  // bars and beats
  const every = gridBeats(pps, v.bpm, 6);
  const lines = beatLines(v, every);
  const barPx = secondsPerBeat(v.bpm) * BEATS_PER_BAR * pps;
  const labelEvery = barPx >= 28 ? 1 : barPx >= 7 ? 4 : 16;
  ctx.textBaseline = 'alphabetic';
  for (const [px, isBar, bar] of lines) {
    if (isBar) {
      ctx.fillStyle = 'rgba(232,235,240,.35)';
      ctx.fillRect(Math.round(px), h - 14, 1, 8);
      if ((bar - 1) % labelEvery === 0) {
        ctx.fillStyle = 'rgba(232,235,240,.55)';
        ctx.fillText(String(bar), Math.round(px) + 3, h - 7);
      }
    } else {
      ctx.fillStyle = 'rgba(232,235,240,.2)';
      ctx.fillRect(Math.round(px), h - 10, 1, 4);
    }
  }

  if (endX >= 0 && endX <= w) {
    ctx.fillStyle = playing ? muted : 'rgba(232,235,240,.7)';
    ctx.fillRect(Math.round(endX) - 1, 0, 2, h);
  }
}

/** Thinnest spacing at which grid lines are still drawn. */
const MIN_LINE_PX = 5;

/** The snap grid behind the tracks: what is drawn is what an edge is quantized to. */
export function drawGrid(cv: HTMLCanvasElement, v: View, loop: LoopRange, grid: GridLines | null) {
  const ctx = prepare(cv);
  if (!ctx) return;
  const { width: w, height: h, pps, t0 } = v;
  if (grid) {
    // A fixed step can be finer than the zoom shows: draw the strong lines only, or none.
    const fine = grid.step * pps >= MIN_LINE_PX;
    const every = fine ? 1 : grid.major;
    if (grid.step * every * pps >= MIN_LINE_PX) {
      const first = Math.ceil(Math.max(0, t0 - grid.offset) / grid.step / every) * every;
      const last = (t0 + w / pps - grid.offset) / grid.step;
      for (let n = first; n <= last; n += every) {
        ctx.fillStyle = fine && n % grid.major === 0 ? 'rgba(255,255,255,.085)' : 'rgba(255,255,255,.035)';
        ctx.fillRect(Math.round((grid.offset + n * grid.step - t0) * pps), 0, 1, h);
      }
    }
  }
  if (loop.on && loop.to !== null) {
    ctx.fillStyle = 'rgba(255,176,0,.25)';
    ctx.fillRect(Math.round((loop.from - t0) * pps), 0, 1, h);
    ctx.fillRect(Math.round((loop.to - t0) * pps) - 1, 0, 1, h);
  }
  const endX = (v.duration - t0) * pps;
  if (endX < w) {
    ctx.fillStyle = 'rgba(0,0,0,.4)';
    ctx.fillRect(Math.max(0, endX), 0, w - Math.max(0, endX), h);
    if (endX >= 0) {
      ctx.fillStyle = 'rgba(232,235,240,.35)';
      ctx.fillRect(Math.round(endX), 0, 1, h);
    }
  }
}
