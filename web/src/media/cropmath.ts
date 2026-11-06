// Geometry of the crop tool. The rules mirror the server so the preview shows what the sign will:
// trafficlight/config.py (sizes), media/ingest.py (rotate -> crop -> proxy size), render/media/common.py
// (`cover` fit) and player.py (the round masks). Boxes here are in pixels of the rotated picture.
import type { MediaEdit, MediaShape } from '../api/types';

// ---- the sign (trafficlight/config.py)
export const FACE = 128;
export const FACES = 3;
export const SIGN_W = FACE;
export const SIGN_H = FACE * FACES;
/** Radius of a light's lit disc inside its FACE x FACE box. */
export const FACE_RADIUS = FACE / 2;

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}
export type Handle = 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw';

/** Width / height (in pixels) the shape locks the crop to; null = free. */
export function shapeAspect(shape: MediaShape): number | null {
  if (shape === 'light') return 1;
  if (shape === 'sign') return SIGN_W / SIGN_H;
  return null;
}

/** Size of the picture after turning it `rotate` degrees clockwise. */
export function rotatedSize(w: number, h: number, rotate: number): [number, number] {
  return (rotate / 90) % 2 ? [h, w] : [w, h];
}

export function clampBox(b: Box, W: number, H: number, min = 1): Box {
  const w = Math.min(W, Math.max(min, b.w));
  const h = Math.min(H, Math.max(min, b.h));
  return { x: Math.min(W - w, Math.max(0, b.x)), y: Math.min(H - h, Math.max(0, b.y)), w, h };
}

/** The largest box of `aspect` inside `b`, centred on it. */
export function fitAspect(b: Box, aspect: number): Box {
  let w = b.w;
  let h = w / aspect;
  if (h > b.h) {
    h = b.h;
    w = h * aspect;
  }
  return { x: b.x + (b.w - w) / 2, y: b.y + (b.h - h) / 2, w, h };
}

/** Box with `aspect` around centre (cx, cy), shrunk until it fits in W x H, then moved inside. */
export function boxAround(cx: number, cy: number, w: number, aspect: number, W: number, H: number): Box {
  let bw = Math.min(w, W, H * aspect);
  bw = Math.max(bw, Math.min(1, W));
  const bh = bw / aspect;
  return clampBox({ x: cx - bw / 2, y: cy - bh / 2, w: bw, h: bh }, W, H);
}

export function moveBox(b: Box, dx: number, dy: number, W: number, H: number): Box {
  return clampBox({ ...b, x: b.x + dx, y: b.y + dy }, W, H);
}

/**
 * Drag handle `h` of `start` by (dx, dy). Free: the dragged edges follow the pointer. Locked to `aspect`:
 * the opposite corner stays put (edge handles keep the other axis centred) and the box keeps its shape.
 */
export function resizeBox(start: Box, h: Handle, dx: number, dy: number, aspect: number | null, W: number, H: number, min = 8): Box {
  const left = start.x;
  const top = start.y;
  const right = start.x + start.w;
  const bottom = start.y + start.h;
  const hasW = h.includes('w');
  const hasE = h.includes('e');
  const hasN = h.includes('n');
  const hasS = h.includes('s');
  if (aspect === null) {
    let l = left;
    let r = right;
    let t = top;
    let b = bottom;
    if (hasW) l = Math.min(r - min, Math.max(0, left + dx));
    if (hasE) r = Math.max(l + min, Math.min(W, right + dx));
    if (hasN) t = Math.min(b - min, Math.max(0, top + dy));
    if (hasS) b = Math.max(t + min, Math.min(H, bottom + dy));
    return { x: l, y: t, w: r - l, h: b - t };
  }
  // proposed width from whichever axis moved the box more
  const fromX = hasW ? start.w - dx : hasE ? start.w + dx : null;
  const fromY = hasN ? (start.h - dy) * aspect : hasS ? (start.h + dy) * aspect : null;
  let w = fromX !== null && fromY !== null ? Math.max(fromX, fromY) : (fromX ?? fromY ?? start.w);
  // room the anchored corner (or centred axis) leaves
  const cx = left + start.w / 2;
  const cy = top + start.h / 2;
  const maxW = hasW ? right : hasE ? W - left : 2 * Math.min(cx, W - cx);
  const maxH = hasN ? bottom : hasS ? H - top : 2 * Math.min(cy, H - cy);
  const minW = Math.max(min, min * aspect);
  w = Math.max(Math.min(w, maxW, maxH * aspect), Math.min(minW, maxW, maxH * aspect));
  const hh = w / aspect;
  const x = hasW ? right - w : hasE ? left : cx - w / 2;
  const y = hasN ? bottom - hh : hasS ? top : cy - hh / 2;
  return { x, y, w, h: hh };
}

/**
 * The box after the picture turns a quarter (`dir` 1 = clockwise) under it: picture size W x H before the
 * turn. Free: the box turns with the picture (same content). Locked: the box keeps its shape and size and
 * stays centred on the same point of the picture.
 */
export function turnBox(b: Box, dir: 1 | -1, W: number, H: number, aspect: number | null): Box {
  const cx = b.x + b.w / 2;
  const cy = b.y + b.h / 2;
  // a point (x, y) of the W x H picture lands at (H - y, x) after a clockwise turn, (y, W - x) after an anticlockwise one
  const [ncx, ncy] = dir === 1 ? [H - cy, cx] : [cy, W - cx];
  const [nW, nH] = [H, W];
  if (aspect === null) {
    const nb = dir === 1 ? { x: H - b.y - b.h, y: b.x, w: b.h, h: b.w } : { x: b.y, y: W - b.x - b.w, w: b.h, h: b.w };
    return clampBox(nb, nW, nH);
  }
  return boxAround(ncx, ncy, b.w, aspect, nW, nH);
}

/** The box a shape starts with: the largest of its aspect in the whole picture, centred. */
export function boxForShape(shape: MediaShape, W: number, H: number): Box {
  const whole = { x: 0, y: 0, w: W, h: H };
  const a = shapeAspect(shape);
  return a === null ? whole : fitAspect(whole, a);
}

/** The box after switching to `shape` from box `from`: same centre and the same longer side (a square
 * becomes a tall box as high as it was), shrunk and moved as needed to stay in the picture. */
export function switchShape(shape: MediaShape, from: Box, W: number, H: number): Box {
  const a = shapeAspect(shape);
  if (a === null) return from;
  const long = Math.max(from.w, from.h);
  const w = a >= 1 ? long : long * a;
  return boxAround(from.x + from.w / 2, from.y + from.h / 2, w, a, W, H);
}

// ---- edit <-> box
export function boxFromEdit(edit: MediaEdit, W: number, H: number): Box {
  if (!edit.crop) return boxForShape(edit.shape, W, H);
  const [x, y, w, h] = edit.crop;
  return clampBox({ x: x * W, y: y * H, w: w * W, h: h * H }, W, H);
}

const round6 = (v: number) => Math.round(v * 1e6) / 1e6;

/** The crop for the edit: null when the box is (within half a pixel) the whole picture. */
export function cropFromBox(b: Box, W: number, H: number): [number, number, number, number] | null {
  if (b.x < 0.5 && b.y < 0.5 && b.w > W - 1 && b.h > H - 1) return null;
  const x = Math.max(0, round6(b.x / W));
  const y = Math.max(0, round6(b.y / H));
  return [x, y, Math.min(1 - x, round6(b.w / W)), Math.min(1 - y, round6(b.h / H))];
}

// ---- what the sign shows (server: ingest.proxy_size, then common.geometry "cover")
export function proxySize(w: number, h: number): [number, number] {
  const s = Math.min(1, Math.max(SIGN_W / w, SIGN_H / h));
  // the same tolerance as the server: an exact 1:3 crop must not tip over into a proxy two pixels larger
  return [Math.max(2, Math.ceil((w * s) / 2 - 1e-3) * 2), Math.max(2, Math.ceil((h * s) / 2 - 1e-3) * 2)];
}

/**
 * The part of the crop `b` that fills a boxW x boxH box with the clip's default fit (cover): scaled to cover,
 * centred, the overflow cut off. In pixels of the rotated picture.
 */
export function coverRegion(b: Box, boxW: number, boxH: number): Box {
  const [pw, ph] = proxySize(b.w, b.h);
  const s = Math.max(boxW / pw, boxH / ph);
  const sw = Math.max(boxW, Math.round(pw * s));
  const sh = Math.max(boxH, Math.round(ph * s));
  const ox = Math.floor((sw - boxW) / 2);
  const oy = Math.floor((sh - boxH) / 2);
  return { x: b.x + (ox / sw) * b.w, y: b.y + (oy / sh) * b.h, w: (boxW / sw) * b.w, h: (boxH / sh) * b.h };
}

let maskCache: Float32Array | null = null;
/** FACE x FACE coverage of a light's disc, exactly as player.face_mask(): clip(r + 0.5 - d, 0, 1). */
export function faceMask(): Float32Array {
  if (maskCache) return maskCache;
  const m = new Float32Array(FACE * FACE);
  const c = (FACE - 1) / 2;
  for (let y = 0; y < FACE; y++) {
    for (let x = 0; x < FACE; x++) {
      const d = Math.hypot(x - c, y - c);
      m[y * FACE + x] = Math.min(1, Math.max(0, FACE_RADIUS + 0.5 - d));
    }
  }
  maskCache = m;
  return m;
}

/** In place: RGBA pixels of a SIGN_W wide picture (FACE or SIGN_H tall) seen through the round lights, on black. */
export function applyCircles(px: Uint8ClampedArray, height: number): void {
  const m = faceMask();
  for (let y = 0; y < height; y++) {
    const row = (y % FACE) * FACE;
    for (let x = 0; x < SIGN_W; x++) {
      const k = m[row + x];
      if (k >= 1) continue;
      const i = (y * SIGN_W + x) * 4;
      px[i] = px[i] * k;
      px[i + 1] = px[i + 1] * k;
      px[i + 2] = px[i + 2] * k;
    }
  }
}

/** Canvas transform (a, b, c, d, e, f) that maps the unturned W x H picture onto the turned one. */
export function turnMatrix(rotate: number, W: number, H: number): [number, number, number, number, number, number] {
  switch (((rotate / 90) % 4 + 4) % 4) {
    case 1: return [0, 1, -1, 0, H, 0];        // (x, y) -> (H - y, x)
    case 2: return [-1, 0, 0, -1, W, H];
    case 3: return [0, -1, 1, 0, 0, W];        // (x, y) -> (y, W - x)
    default: return [1, 0, 0, 1, 0, 0];
  }
}

/** The rectangle of the unturned W x H picture that holds box `b` of the turned picture. */
export function unturnBox(b: Box, rotate: number, W: number, H: number): Box {
  switch (((rotate / 90) % 4 + 4) % 4) {
    case 1: return { x: b.y, y: H - b.x - b.w, w: b.h, h: b.w };
    case 2: return { x: W - b.x - b.w, y: H - b.y - b.h, w: b.w, h: b.h };
    case 3: return { x: W - b.y - b.h, y: b.x, w: b.h, h: b.w };
    default: return b;
  }
}

export function sameEdit(a: MediaEdit, b: MediaEdit): boolean {
  const near = (p: number | null, q: number | null) => (p === null || q === null ? p === q : Math.abs(p - q) < 1e-4);
  const crops = a.crop === null || b.crop === null ? a.crop === b.crop : a.crop.every((v, i) => near(v, b.crop![i]));
  return a.rotate === b.rotate && a.shape === b.shape && crops && near(a.start, b.start) && near(a.end, b.end);
}
