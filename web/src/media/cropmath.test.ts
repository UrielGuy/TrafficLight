import { describe, expect, it } from 'vitest';
import {
  FACE, FACE_RADIUS, SIGN_H, SIGN_W, applyCircles, boxForShape, boxFromEdit, clampBox, coverRegion, cropFromBox, faceMask,
  proxySize, resizeBox, rotatedSize, sameEdit, shapeAspect, switchShape, turnBox, turnMatrix, unturnBox, type Box,
} from './cropmath';
import type { MediaEdit } from '../api/types';

const edit = (p: Partial<MediaEdit> = {}): MediaEdit => ({ rotate: 0, crop: null, start: 0, end: null, shape: 'free', ...p });
const closeBox = (a: Box, b: Box, tol = 1e-6) => {
  for (const k of ['x', 'y', 'w', 'h'] as const) expect(Math.abs(a[k] - b[k])).toBeLessThan(tol);
};

describe('shapes', () => {
  it('locks one light to 1:1 and the sign to 1:3', () => {
    expect(shapeAspect('light')).toBe(1);
    expect(shapeAspect('sign')).toBeCloseTo(1 / 3);
    expect(shapeAspect('free')).toBeNull();
    expect(SIGN_H).toBe(3 * FACE);
  });
  it('starts with the largest box of the shape, centred', () => {
    closeBox(boxForShape('light', 1920, 1080), { x: 420, y: 0, w: 1080, h: 1080 });
    closeBox(boxForShape('sign', 1920, 1080), { x: 780, y: 0, w: 360, h: 1080 });
    closeBox(boxForShape('sign', 300, 2000), { x: 0, y: 550, w: 300, h: 900 });
    closeBox(boxForShape('free', 640, 360), { x: 0, y: 0, w: 640, h: 360 });
  });
  it('switches shape around the same centre, keeping the longer side', () => {
    // a square becomes a tall box as high as the square
    closeBox(switchShape('sign', { x: 100, y: 100, w: 90, h: 90 }, 640, 360), { x: 130, y: 100, w: 30, h: 90 });
    // a tall box becomes a square as wide as it was high, moved back into the picture
    closeBox(switchShape('light', { x: 0, y: 0, w: 100, h: 300 }, 640, 360), { x: 0, y: 0, w: 300, h: 300 });
    // the whole picture becomes its largest square
    closeBox(switchShape('light', { x: 0, y: 0, w: 640, h: 360 }, 640, 360), { x: 140, y: 0, w: 360, h: 360 });
    const free = { x: 1, y: 2, w: 3, h: 4 };
    expect(switchShape('free', free, 640, 360)).toBe(free);
  });
});

describe('resizing', () => {
  const W = 1000;
  const H = 600;
  const b = { x: 100, y: 100, w: 300, h: 300 };
  it('keeps the shape and the opposite corner when locked', () => {
    const r = resizeBox(b, 'se', 100, 20, 1, W, H);
    expect(r.w).toBeCloseTo(400);
    expect(r.h).toBeCloseTo(400);
    expect(r.x).toBe(100);
    expect(r.y).toBe(100);
    const n = resizeBox(b, 'nw', 50, 50, 1, W, H);
    expect(n.x + n.w).toBeCloseTo(400);
    expect(n.y + n.h).toBeCloseTo(400);
    expect(n.w).toBeCloseTo(250);
  });
  it('never leaves the picture', () => {
    const r = resizeBox(b, 'se', 5000, 5000, 1, W, H);
    expect(r.x + r.w).toBeLessThanOrEqual(W + 1e-9);
    expect(r.y + r.h).toBeLessThanOrEqual(H + 1e-9);
    expect(r.w).toBeCloseTo(r.h);
    const s = resizeBox({ x: 100, y: 0, w: 100, h: 300 }, 'se', 5000, 5000, 1 / 3, W, H);
    expect(s.h).toBeCloseTo(600);
    expect(s.w).toBeCloseTo(200);
  });
  it('moves single edges when free', () => {
    const r = resizeBox(b, 'e', 120, 999, null, W, H);
    closeBox(r, { x: 100, y: 100, w: 420, h: 300 });
    const t = resizeBox(b, 'n', 0, -500, null, W, H);
    closeBox(t, { x: 100, y: 0, w: 300, h: 400 });
    const tiny = resizeBox(b, 'w', 1000, 0, null, W, H);
    expect(tiny.w).toBe(8);
  });
});

describe('turning', () => {
  it('turns the box with the picture when free, and back', () => {
    const b = { x: 10, y: 20, w: 100, h: 50 };
    const t = turnBox(b, 1, 640, 360, null);
    closeBox(t, { x: 360 - 20 - 50, y: 10, w: 50, h: 100 });
    closeBox(turnBox(t, -1, 360, 640, null), b);
    let c = b;
    let [w, h] = [640, 360];
    for (let i = 0; i < 4; i++) {
      c = turnBox(c, 1, w, h, null);
      [w, h] = [h, w];
    }
    closeBox(c, b);
  });
  it('keeps a locked box upright, centred on the same spot', () => {
    const b = { x: 100, y: 0, w: 120, h: 360 };          // a whole-sign box on a 640 x 360 picture
    const t = turnBox(b, 1, 640, 360, 1 / 3);
    expect(t.w / t.h).toBeCloseTo(1 / 3);
    expect(t.h).toBeLessThanOrEqual(640);
    // the box's centre (160, 180) is at (360 - 180, 160) after a clockwise turn; its size stays
    expect(t.x + t.w / 2).toBeCloseTo(180);
    expect(t.w).toBeCloseTo(120);
    expect(t.y).toBe(0);                                  // centred at y 160 it would stick out: moved inside
  });
  it('maps the unturned picture with matching matrices', () => {
    for (const rot of [0, 90, 180, 270]) {
      const [W, H] = [640, 360];
      const [RW, RH] = rotatedSize(W, H, rot);
      const m = turnMatrix(rot, W, H);
      const map = (x: number, y: number) => [m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]];
      const box = { x: 30, y: 40, w: 50, h: 60 };
      const u = unturnBox(box, rot, W, H);
      // the corners of the unturned box land on the corners of the turned one
      const pts = [map(u.x, u.y), map(u.x + u.w, u.y + u.h)];
      const xs = pts.map((p) => p[0]).sort((a, b) => a - b);
      const ys = pts.map((p) => p[1]).sort((a, b) => a - b);
      expect(xs[0]).toBeCloseTo(box.x);
      expect(xs[1]).toBeCloseTo(box.x + box.w);
      expect(ys[0]).toBeCloseTo(box.y);
      expect(ys[1]).toBeCloseTo(box.y + box.h);
      expect(Math.max(...xs)).toBeLessThanOrEqual(RW);
      expect(Math.max(...ys)).toBeLessThanOrEqual(RH);
    }
  });
});

describe('edit <-> box', () => {
  it('round-trips and gives null for the whole picture', () => {
    const e = edit({ crop: [0.25, 0.1, 0.5, 0.8] });
    const b = boxFromEdit(e, 640, 360);
    closeBox(b, { x: 160, y: 36, w: 320, h: 288 });
    expect(cropFromBox(b, 640, 360)).toEqual([0.25, 0.1, 0.5, 0.8]);
    expect(cropFromBox({ x: 0, y: 0, w: 640, h: 360 }, 640, 360)).toBeNull();
    expect(cropFromBox(clampBox({ x: -5, y: -5, w: 700, h: 400 }, 640, 360), 640, 360)).toBeNull();
    // a shape without a crop starts as the largest box of the shape
    closeBox(boxFromEdit(edit({ shape: 'light' }), 640, 360), { x: 140, y: 0, w: 360, h: 360 });
  });
  it('compares edits with rounding noise', () => {
    expect(sameEdit(edit({ crop: [0.1, 0.2, 0.3, 0.4] }), edit({ crop: [0.10000001, 0.2, 0.3, 0.4] }))).toBe(true);
    expect(sameEdit(edit(), edit({ rotate: 90 }))).toBe(false);
    expect(sameEdit(edit({ end: null }), edit({ end: 3 }))).toBe(false);
  });
});

describe('what the sign shows', () => {
  it('sizes the proxy like the server', () => {
    expect(proxySize(1920, 1080)).toEqual([684, 384]);
    expect(proxySize(1080, 1920)).toEqual([216, 384]);
    expect(proxySize(160, 90)).toEqual([160, 90]);
    // the largest 1:3 box of a 480 x 854 picture, exact and as rounded on its way to the server
    expect(proxySize(854 / 3, 854)).toEqual([128, 384]);
    expect(proxySize(0.593056 * 480, 854)).toEqual([128, 384]);
  });
  it('fills a box like the cover fit', () => {
    // a 1:1 crop fills a light exactly; on the whole sign only its middle third of the width shows
    closeBox(coverRegion({ x: 0, y: 0, w: 900, h: 900 }, SIGN_W, FACE), { x: 0, y: 0, w: 900, h: 900 });
    const r = coverRegion({ x: 0, y: 0, w: 900, h: 900 }, SIGN_W, SIGN_H);
    expect(r.h).toBeCloseTo(900);
    expect(r.w).toBeCloseTo(300);
    expect(r.x).toBeCloseTo(300);
    // a 1:3 crop fills the sign
    closeBox(coverRegion({ x: 50, y: 0, w: 120, h: 360 }, SIGN_W, SIGN_H), { x: 50, y: 0, w: 120, h: 360 }, 0.5);
  });
  it('masks the lights like the player', () => {
    const m = faceMask();
    const c = (FACE - 1) / 2;
    expect(m[64 * FACE + 64]).toBe(1);
    expect(m[0]).toBe(0);
    // anti-aliased edge: the value is the formula's, partly covered along the rim
    const [x, y] = [100, 20];
    expect(m[y * FACE + x]).toBeCloseTo(Math.min(1, Math.max(0, FACE_RADIUS + 0.5 - Math.hypot(x - c, y - c))));
    expect(m.some((v) => v > 0.2 && v < 0.8)).toBe(true);
    const px = new Uint8ClampedArray(SIGN_W * SIGN_H * 4).fill(200);
    applyCircles(px, SIGN_H);
    expect(px[0]).toBe(0);                                         // a corner of the top light
    const mid = ((FACE + 64) * SIGN_W + 64) * 4;
    expect(px[mid]).toBe(200);                                     // the middle of the middle light
  });
});
