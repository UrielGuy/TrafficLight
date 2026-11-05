// The crop tool: the whole original (GET /api/media/{id}/source) with a crop box over it, in / out points,
// quarter turns, and next to it the result as the sign will show it — at the sign's own resolution, through
// the round lights if asked. Opened with openCropTool() from anywhere (CropHost is mounted in the app shell).
import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as RPointerEvent } from 'react';
import { api, mediaSourceUrl } from '../api/client';
import type { MediaEdit, MediaItem, MediaShape } from '../api/types';
import { Segmented, Toggle } from '../components/ui';
import { mediaRes } from '../state/app';
import { createStore, useStore } from '../state/store';
import { toast } from '../state/toast';
import {
  FACE, FACE_RADIUS, SIGN_H, SIGN_W, applyCircles, boxAround, boxForShape, boxFromEdit, clampBox, coverRegion, cropFromBox,
  moveBox, resizeBox, rotatedSize, sameEdit, shapeAspect, switchShape, turnBox, turnMatrix, unturnBox, type Box, type Handle,
} from './cropmath';
import './crop.css';

// ---------------------------------------------------------------- host
interface CropRequest {
  item: MediaItem;
  onDone?: (item: MediaItem) => void;
}
const cropStore = createStore<CropRequest | null>(null);

/** Open the crop tool for a video or picture. `onDone` gets the item once its new crop is ready. */
export function openCropTool(item: MediaItem, onDone?: (item: MediaItem) => void) {
  cropStore.set({ item, onDone });
}

export function CropHost() {
  const req = useStore(cropStore);
  if (!req) return null;
  return <CropTool key={req.item.id} item={req.item} onDone={req.onDone} onClose={() => cropStore.set(null)} />;
}

// ---------------------------------------------------------------- helpers
const SHAPES: { value: MediaShape; label: string }[] = [
  { value: 'light', label: 'One light' },
  { value: 'sign', label: 'Whole sign' },
  { value: 'free', label: 'Free' },
];
const SHAPE_NOTE: Record<MediaShape, string> = { light: 'one light', sign: 'whole sign', free: 'free crop' };
const CIRCLES_PREF = 'trafficlight.crop.circles';
const HANDLES: Handle[] = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
const MIN_BOX_CSS = 40;          // smallest crop box on screen, in CSS pixels
const PHONE = '(max-width: 760px)';

function readCirclesPref(): boolean {
  try {
    return localStorage.getItem(CIRCLES_PREF) !== '0';
  } catch {
    return true;
  }
}

const fmtS = (v: number) => (Math.round(v * 100) / 100).toFixed(2);

type Source = HTMLVideoElement | HTMLImageElement;
function naturalSize(el: Source | null): [number, number] {
  if (!el) return [0, 0];
  return el instanceof HTMLVideoElement ? [el.videoWidth, el.videoHeight] : [el.naturalWidth, el.naturalHeight];
}
function sourceReady(el: Source | null): boolean {
  if (!el) return false;
  return el instanceof HTMLVideoElement ? el.readyState >= 2 : el.complete && el.naturalWidth > 0;
}

interface Saving {
  id: string;
  progress: number;
  stage: string;
  copy: boolean;
}

// ---------------------------------------------------------------- the tool
export default function CropTool({ item, onClose, onDone }: { item: MediaItem; onClose: () => void; onDone?: (m: MediaItem) => void }) {
  const isVideo = item.kind === 'video';
  // the original's size as the server measures it (display-oriented); the box lives in its turned pixels
  const [W, setW] = useState(item.source_width || item.width || 0);
  const [H, setH] = useState(item.source_height || item.height || 0);
  const [rotate, setRotate] = useState<MediaEdit['rotate']>(item.edit.rotate);
  const [shape, setShape] = useState<MediaShape>(item.edit.shape);
  const [RW, RH] = rotatedSize(W, H, rotate);
  const [box, setBox] = useState<Box | null>(() => (W && H ? boxFromEdit(item.edit, ...rotatedSize(W, H, item.edit.rotate)) : null));
  const [inPt, setIn] = useState(item.edit.start);
  const [outPt, setOut] = useState<number | null>(item.edit.end);
  const [duration, setDuration] = useState(item.source_duration || 0);
  const [circles, setCircles] = useState(readCirclesPref);
  const [freeTarget, setFreeTarget] = useState<'light' | 'sign'>(() => {
    if (!item.edit.crop || !W || !H) return 'sign';
    const [, , w, h] = item.edit.crop;
    const [rw, rh] = rotatedSize(W, H, item.edit.rotate);
    return (w * rw) / (h * rh) > 0.6 ? 'light' : 'sign';
  });
  const [loaded, setLoaded] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(true);
  const [saving, setSaving] = useState<Saving | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [stageSize, setStageSize] = useState<[number, number]>([0, 0]);

  const target: 'light' | 'sign' = shape === 'free' ? freeTarget : shape;
  const previewH = target === 'light' ? FACE : SIGN_H;
  const end = outPt ?? duration;

  const videoRef = useRef<HTMLVideoElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const stageCanvas = useRef<HTMLCanvasElement>(null);
  const previewCanvas = useRef<HTMLCanvasElement>(null);
  const playheadRef = useRef<HTMLDivElement>(null);
  const timeRef = useRef<HTMLSpanElement>(null);
  const work = useRef<HTMLCanvasElement[]>([]);
  const dirty = useRef(true);
  const source = (): Source | null => (isVideo ? videoRef.current : imgRef.current);

  // everything the animation loop reads, current on every render
  const live = useRef({ box, rotate, W, H, RW, RH, inPt, end, circles, previewH, playing });
  live.current = { box, rotate, W, H, RW, RH, inPt, end, circles, previewH, playing };
  useEffect(() => {
    dirty.current = true;
  }, [box, rotate, circles, previewH, loaded, stageSize]);

  const edit = useMemo<MediaEdit>(() => ({
    rotate,
    crop: box && RW && RH ? cropFromBox(box, RW, RH) : null,
    start: isVideo ? Math.round(inPt * 1000) / 1000 : 0,
    end: isVideo && outPt !== null && outPt < duration - 0.005 ? Math.round(outPt * 1000) / 1000 : null,
    shape,
  }), [rotate, box, RW, RH, inPt, outPt, duration, shape, isVideo]);
  const changed = !sameEdit(edit, item.edit);

  // ---- the source
  const onLoaded = () => {
    const el = source();
    const [nw, nh] = naturalSize(el);
    if (!nw || !nh) return;
    if (!W || !H) {
      // no size from the server (should not happen): take the browser's
      setW(nw);
      setH(nh);
      const [rw, rh] = rotatedSize(nw, nh, rotate);
      setBox(boxFromEdit(item.edit, rw, rh));
    }
    if (el instanceof HTMLVideoElement) {
      if (isFinite(el.duration) && el.duration > 0) setDuration(el.duration);
      el.currentTime = inPt;
    }
    setLoaded(true);
  };

  // ---- stage size: the turned picture fitted into the space there is
  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setStageSize([el.clientWidth, el.clientHeight]));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const [phone, setPhone] = useState(() => window.matchMedia(PHONE).matches);
  useEffect(() => {
    const mq = window.matchMedia(PHONE);
    const on = () => setPhone(mq.matches);
    mq.addEventListener('change', on);
    return () => mq.removeEventListener('change', on);
  }, []);
  // on a phone the row is only as high as the picture needs (no wide black bars), but high enough for the preview
  const viewHeight = phone && RW && RH && stageSize[0]
    ? Math.round(Math.max(target === 'light' ? 230 : 300, Math.min(window.innerHeight * 0.5, (stageSize[0] * RH) / RW)))
    : undefined;
  const [dispW, dispH] = useMemo(() => {
    const [cw, ch] = stageSize;
    if (!RW || !RH || !cw || !ch) return [0, 0];
    const s = Math.min(cw / RW, ch / RH);
    return [Math.max(1, Math.floor(RW * s)), Math.max(1, Math.floor(RH * s))];
  }, [stageSize, RW, RH]);

  // ---- drawing
  const draw = useCallback(() => {
    const el = source();
    const s = live.current;
    if (!el || !sourceReady(el) || !s.box || !s.RW) return;
    const [nw, nh] = naturalSize(el);
    const turn = turnMatrix(s.rotate, nw, nh);
    const [nrw, nrh] = rotatedSize(nw, nh, s.rotate);

    const sc = stageCanvas.current;
    if (sc && sc.width > 0) {
      const ctx = sc.getContext('2d');
      if (ctx) {
        const k = sc.width / nrw;
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.clearRect(0, 0, sc.width, sc.height);
        ctx.imageSmoothingEnabled = true;
        ctx.imageSmoothingQuality = 'high';
        ctx.setTransform(k * turn[0], k * turn[1], k * turn[2], k * turn[3], k * turn[4], k * turn[5]);
        ctx.drawImage(el, 0, 0, nw, nh);
      }
    }

    const pc = previewCanvas.current;
    if (!pc) return;
    const ph = s.previewH;
    // the part of the crop the box shows (cover fit), in turned pixels of the server's size -> of the browser's
    const reg = coverRegion(s.box, SIGN_W, ph);
    const fx = nrw / s.RW;
    const fy = nrh / s.RH;
    const regN = { x: reg.x * fx, y: reg.y * fy, w: reg.w * fx, h: reg.h * fy };
    // Draw at (at least) the source's own resolution, then halve: each halving averages exactly 2x2 pixels, so
    // the whole reduction is close to the server's area averaging (a direct bilinear reduction would treat
    // one-pixel details differently). Bounded so a phone's GPU is not asked for more than about a megapixel.
    const maxSteps = Math.floor(Math.log2(Math.sqrt(1.2e6 / (SIGN_W * ph))));
    const steps = Math.max(0, Math.min(maxSteps, Math.ceil(Math.log2(regN.w / SIGN_W))));
    const bufs = work.current;
    while (bufs.length < steps + 1) bufs.push(document.createElement('canvas'));
    let cw = SIGN_W << steps;
    let chh = ph << steps;
    const b0 = bufs[0];
    b0.width = cw;
    b0.height = chh;
    const c0 = b0.getContext('2d')!;
    c0.imageSmoothingEnabled = true;
    c0.imageSmoothingQuality = 'high';
    const k = cw / regN.w;
    c0.setTransform(k * turn[0], k * turn[1], k * turn[2], k * turn[3], k * turn[4] - k * regN.x, k * turn[5] - k * regN.y);
    const src = unturnBox(regN, s.rotate, nw, nh);
    c0.fillStyle = '#000';
    c0.fillRect(src.x - 1, src.y - 1, src.w + 2, src.h + 2);
    c0.drawImage(el, src.x, src.y, src.w, src.h, src.x, src.y, src.w, src.h);
    let cur = b0;
    for (let i = 1; i <= steps; i++) {
      const nb = bufs[i];
      cw >>= 1;
      chh >>= 1;
      nb.width = cw;
      nb.height = chh;
      const nc = nb.getContext('2d')!;
      nc.imageSmoothingEnabled = true;
      nc.drawImage(cur, 0, 0, cw, chh);
      cur = nb;
    }
    const pctx = pc.getContext('2d', { willReadFrequently: true });
    if (!pctx) return;
    pctx.setTransform(1, 0, 0, 1, 0, 0);
    pctx.imageSmoothingEnabled = true;
    pctx.drawImage(cur, 0, 0, SIGN_W, ph);
    if (s.circles) {
      const img = pctx.getImageData(0, 0, SIGN_W, ph);
      applyCircles(img.data, ph);
      pctx.putImageData(img, 0, 0);
    }
  }, [isVideo]);

  useEffect(() => {
    let raf = 0;
    let lastUi = 0;
    const tick = (now: number) => {
      raf = requestAnimationFrame(tick);
      const v = videoRef.current;
      const s = live.current;
      if (isVideo && v) {
        if (s.playing && v.currentTime >= s.end - 0.02) {
          v.currentTime = s.inPt;       // loop between the in and out points
        }
        if (playheadRef.current && duration > 0) playheadRef.current.style.left = `${(v.currentTime / duration) * 100}%`;
        if (timeRef.current && now - lastUi > 80) {
          lastUi = now;
          timeRef.current.textContent = fmtS(v.currentTime);
        }
        if (!v.paused || dirty.current) {
          dirty.current = false;
          draw();
        }
      } else if (dirty.current) {
        dirty.current = false;
        draw();
      }
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [draw, isVideo, duration]);

  // ---- keys
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT');
      if (e.key === 'Escape' && !saving) onClose();
      if (typing) return;
      if (e.key === ' ' && isVideo) {
        e.preventDefault();
        togglePlay();
      } else if ((e.key === 'i' || e.key === 'I') && isVideo) markIn();
      else if ((e.key === 'o' || e.key === 'O') && isVideo) markOut();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  // ---- playback
  const togglePlay = () => {
    const v = videoRef.current;
    if (!v) return;
    if (v.paused) {
      if (v.currentTime < inPt || v.currentTime >= end - 0.02) v.currentTime = inPt;
      void v.play().catch(() => undefined);
      setPlaying(true);
    } else {
      v.pause();
      setPlaying(false);
    }
  };
  const seek = (t: number) => {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = Math.max(0, Math.min(duration || t, t));
    dirty.current = true;
  };
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    const onSeeked = () => (dirty.current = true);
    v.addEventListener('seeked', onSeeked);
    return () => v.removeEventListener('seeked', onSeeked);
  }, []);
  const now = () => videoRef.current?.currentTime ?? 0;
  const setInSafe = (t: number) => {
    const v = Math.max(0, Math.min(t, end - 0.05));
    setIn(v);
    return v;
  };
  const setOutSafe = (t: number) => {
    const v = Math.min(duration || t, Math.max(t, inPt + 0.05));
    setOut(v >= duration - 0.005 ? null : v);
    return v;
  };
  const markIn = () => setInSafe(now());
  const markOut = () => setOutSafe(now());

  // ---- shape, turning, reset
  const chooseShape = (s: MediaShape) => {
    setShape(s);
    if (!RW || !RH) return;
    if (!box) return;
    let b = switchShape(s, box, RW, RH);
    const a = shapeAspect(s);
    const minPx = dispW ? MIN_BOX_CSS * (RW / dispW) : 0;
    if (a !== null && Math.min(b.w, b.h) < minPx) {
      // a thin box would hide under its own handles: make its short side a finger wide
      b = boxAround(b.x + b.w / 2, b.y + b.h / 2, a >= 1 ? minPx * a : minPx, a, RW, RH);
    }
    setBox(clampBox(b, RW, RH));
    if (s !== 'free') setFreeTarget(s);
  };
  const turn = (dir: 1 | -1) => {
    if (!box || !RW) return;
    setBox(turnBox(box, dir, RW, RH, shapeAspect(shape)));
    setRotate((((rotate + dir * 90) % 360 + 360) % 360) as MediaEdit['rotate']);
  };
  const reset = () => {
    setRotate(0);
    const [rw, rh] = rotatedSize(W, H, 0);
    setBox(boxForShape(shape, rw, rh));
    setIn(0);
    setOut(null);
    seek(0);
  };

  // ---- box gestures (pointer events: mouse, pen and touch alike)
  const gesture = useRef<{ kind: 'move' | Handle; x: number; y: number; start: Box; id: number } | null>(null);
  const scale = dispW ? RW / dispW : 1;
  const onBoxDown = (kind: 'move' | Handle) => (e: RPointerEvent) => {
    if (!box || saving) return;
    e.preventDefault();
    e.stopPropagation();
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    gesture.current = { kind, x: e.clientX, y: e.clientY, start: box, id: e.pointerId };
  };
  const onBoxMove = (e: RPointerEvent) => {
    const g = gesture.current;
    if (!g || g.id !== e.pointerId) return;
    const dx = (e.clientX - g.x) * scale;
    const dy = (e.clientY - g.y) * scale;
    // never smaller than a finger can still take hold of
    const minPx = Math.min(RW, RH, Math.max(4, MIN_BOX_CSS * scale));
    setBox(g.kind === 'move' ? moveBox(g.start, dx, dy, RW, RH) : resizeBox(g.start, g.kind, dx, dy, shapeAspect(shape), RW, RH, minPx));
  };
  const onBoxUp = (e: RPointerEvent) => {
    if (gesture.current?.id === e.pointerId) gesture.current = null;
  };
  const onBoxKey = (e: React.KeyboardEvent) => {
    if (!box) return;
    const step = (e.shiftKey ? 10 : 1) * scale;
    const d: Record<string, [number, number]> = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (d[e.key]) {
      e.preventDefault();
      setBox(moveBox(box, d[e.key][0], d[e.key][1], RW, RH));
    }
  };

  // ---- saving
  const pollUntilDone = async (id: string, copy: boolean) => {
    for (;;) {
      await new Promise((r) => window.setTimeout(r, 350));
      let m: MediaItem;
      try {
        m = await api.getMedia(id);
      } catch (e) {
        setSaveError(e instanceof Error ? e.message : String(e));
        return;
      }
      if (m.status === 'processing') {
        setSaving({ id, progress: m.progress, stage: m.stage, copy });
        continue;
      }
      void mediaRes.refresh();
      if (m.status === 'ready' && !m.error) {
        toast.info(copy ? `Saved as "${m.name}"` : `Saved "${m.name}"`);
        onDone?.(m);
        onClose();
      } else {
        setSaving(null);
        setSaveError(m.error ?? 'The new crop could not be made');
      }
      return;
    }
  };
  const save = async (copy: boolean) => {
    if (saving) return;
    setSaveError(null);
    if (!copy && !changed) {
      onClose();
      return;
    }
    try {
      let id = item.id;
      if (copy) {
        const name = `${item.name} · ${shape === 'free' ? 'copy' : SHAPE_NOTE[shape]}`;
        id = (await api.duplicateMedia(item.id, name)).id;
      }
      videoRef.current?.pause();
      setPlaying(false);
      const r = await api.editMedia(id, edit);
      void mediaRes.refresh();
      setSaving({ id, progress: r.progress, stage: r.stage || 'waiting', copy });
      void pollUntilDone(id, copy);
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : String(e));
    }
  };

  const setCirclesPref = (on: boolean) => {
    setCircles(on);
    try {
      localStorage.setItem(CIRCLES_PREF, on ? '1' : '0');
    } catch {
      /* ignore */
    }
  };

  // ---- the circles drawn over the box on the source (where each light is)
  const overlay = useMemo(() => {
    if (!box) return null;
    const reg = coverRegion(box, SIGN_W, previewH);
    const n = previewH / FACE;
    const r = (reg.w * FACE_RADIUS) / FACE;
    const discs = Array.from({ length: n }, (_, i) => ({ cx: reg.x - box.x + reg.w / 2, cy: reg.y - box.y + (reg.h * (i + 0.5)) / n, r }));
    const cut = Math.abs(reg.w - box.w) > 0.5 || Math.abs(reg.h - box.h) > 0.5;
    return { reg: { ...reg, x: reg.x - box.x, y: reg.y - box.y }, discs, cut };
  }, [box, previewH]);

  const canvasPx = (v: number) => Math.round(v * Math.min(2, window.devicePixelRatio || 1));
  const inPct = duration ? (inPt / duration) * 100 : 0;
  const outPct = duration ? (end / duration) * 100 : 100;
  const busy = !!saving;
  const cropW = box ? Math.round(box.w) : 0;
  const cropH = box ? Math.round(box.h) : 0;

  return (
    <div className="crop-tool" role="dialog" aria-label={`Crop and trim ${item.name}`} data-testid="crop-tool"
      data-edit={JSON.stringify(edit)} data-loaded={loaded ? '1' : '0'}>
      <header className="crop-head">
        <div className="grow">
          <h2>Crop &amp; trim</h2>
          <div className="muted small ellipsis" title={item.name}>{item.name}</div>
        </div>
        <div className="crop-actions">
          <button onClick={onClose} disabled={busy}>Cancel</button>
          <button onClick={() => save(true)} disabled={busy || !loaded} data-testid="crop-save-copy"><span>Save<span className="wide-only"> as a</span> copy</span></button>
          <button className="primary" onClick={() => save(false)} disabled={busy || !loaded} data-testid="crop-save">Save</button>
        </div>
      </header>

      {(saving || saveError) && (
        <div className={`crop-status ${saveError ? 'bad' : ''}`} role="status" data-testid="crop-status">
          {saving && (
            <>
              <div className="grow">
                <div>{saving.copy ? 'Making the copy' : 'Making the new crop'}: {saving.stage || 'converting'}… {Math.round(saving.progress * 100)}%</div>
                <div className="progress"><div style={{ width: `${saving.progress * 100}%` }} /></div>
                <div className="muted small">The sign keeps playing the previous crop until this one is ready.</div>
              </div>
              <button onClick={onClose}>Close, finish in the background</button>
            </>
          )}
          {saveError && <div className="err">{saveError}</div>}
        </div>
      )}

      <div className="crop-body">
        <div className="crop-view" style={viewHeight ? { height: viewHeight } : undefined}>
          <div className="crop-stage" ref={stageRef} data-testid="crop-stage">
            {!loaded && !loadError && <div className="spinner" />}
            {loadError && <div className="err pad">{loadError}</div>}
            {dispW > 0 && (
              <div className="crop-frame" style={{ width: dispW, height: dispH, visibility: loaded ? 'visible' : 'hidden' }}
                onPointerMove={onBoxMove} onPointerUp={onBoxUp} onPointerCancel={onBoxUp}>
                <canvas ref={stageCanvas} width={canvasPx(dispW)} height={canvasPx(dispH)} style={{ width: dispW, height: dispH }} />
                {box && overlay && (
                  <div className={`crop-box ${circles ? 'circles' : ''}`} data-testid="crop-box" tabIndex={0} aria-label="Crop box: drag to move, arrow keys nudge"
                    onKeyDown={onBoxKey}
                    style={{ left: `${(box.x / RW) * 100}%`, top: `${(box.y / RH) * 100}%`, width: `${(box.w / RW) * 100}%`, height: `${(box.h / RH) * 100}%` }}
                    onPointerDown={onBoxDown('move')}>
                    <svg className="crop-svg" viewBox={`0 0 ${box.w} ${box.h}`} preserveAspectRatio="none" aria-hidden="true">
                      {circles ? (
                        <>
                          <path fillRule="evenodd" className="veil"
                            d={`M0 0H${box.w}V${box.h}H0Z ${overlay.discs.map((c) => `M${c.cx - c.r} ${c.cy}a${c.r} ${c.r} 0 1 0 ${2 * c.r} 0a${c.r} ${c.r} 0 1 0 ${-2 * c.r} 0Z`).join(' ')}`} />
                          {overlay.discs.map((c, i) => <ellipse key={i} cx={c.cx} cy={c.cy} rx={c.r} ry={c.r} className="disc" vectorEffect="non-scaling-stroke" />)}
                        </>
                      ) : (
                        overlay.discs.length > 1 && overlay.discs.slice(1).map((c, i) => (
                          <line key={i} x1={overlay.reg.x} x2={overlay.reg.x + overlay.reg.w} y1={c.cy - overlay.reg.h / overlay.discs.length / 2}
                            y2={c.cy - overlay.reg.h / overlay.discs.length / 2} className="split" vectorEffect="non-scaling-stroke" />
                        ))
                      )}
                      {overlay.cut && (
                        <rect x={overlay.reg.x} y={overlay.reg.y} width={overlay.reg.w} height={overlay.reg.h} className="cover" vectorEffect="non-scaling-stroke" />
                      )}
                    </svg>
                    {HANDLES.filter((h) => shape === 'free' || h.length === 2).map((h) => (
                      <span key={h} className={`crop-handle h-${h}`} data-testid={`crop-handle-${h}`} onPointerDown={onBoxDown(h)} />
                    ))}
                  </div>
                )}
              </div>
            )}
            {isVideo ? (
              <video ref={videoRef} className="crop-source" src={mediaSourceUrl(item.id)} muted={muted} playsInline preload="auto"
                onLoadedData={onLoaded} onError={() => setLoadError('The original cannot be shown in this browser.')}
                onPause={() => setPlaying(false)} onPlay={() => setPlaying(true)} />
            ) : (
              <img ref={imgRef} className="crop-source" src={mediaSourceUrl(item.id)} alt="" onLoad={onLoaded}
                onError={() => setLoadError('The original cannot be shown in this browser.')} />
            )}
          </div>

          <div className={`crop-preview-col ${target}`}>
            <div className="crop-label">On the sign</div>
            <div className={`crop-preview ${target}`} data-testid="crop-preview-box">
              <canvas ref={previewCanvas} width={SIGN_W} height={previewH} data-testid="crop-preview" />
            </div>
            <label className="crop-circles">
              <Toggle checked={circles} onChange={setCirclesPref} label="Show circles" />
              <span>Show circles</span>
            </label>
            <p className="muted small crop-explain">
              {circles
                ? `Only the round lights glow: what falls outside the ${target === 'light' ? 'circle' : 'three circles'} is not seen.`
                : `The full ${SIGN_W} × ${previewH} picture, before the round lights cut it.`}
            </p>
          </div>
        </div>

        {isVideo && (
          <div className="crop-time">
            <div className="row crop-transport">
              <button onClick={togglePlay} aria-label={playing ? 'Pause' : 'Play'} data-testid="crop-play" className="crop-play">{playing ? '❚❚' : '▶'}</button>
              <span className="crop-clock"><span ref={timeRef} data-testid="crop-time">{fmtS(inPt)}</span> / {fmtS(duration)} s</span>
              <button className="small-btn" onClick={() => setMuted(!muted)} aria-pressed={!muted}>{muted ? 'Sound off' : 'Sound on'}</button>
            </div>
            <Scrub duration={duration} inPct={inPct} outPct={outPct} playheadRef={playheadRef}
              onSeek={(f) => seek(f * duration)}
              onIn={(f) => seek(setInSafe(f * duration))}
              onOut={(f) => seek(Math.max(0, setOutSafe(f * duration) - 0.04))} />
            <div className="crop-trim">
              <label>In <TimeInput value={inPt} testId="crop-in" onChange={(v) => seek(setInSafe(v))} /></label>
              <button className="small-btn" onClick={markIn} title="Set the in point at the playhead (I)">Set in here</button>
              <label>Out <TimeInput value={end} testId="crop-out" onChange={(v) => seek(Math.max(0, setOutSafe(v) - 0.04))} /></label>
              <button className="small-btn" onClick={markOut} title="Set the out point at the playhead (O)">Set out here</button>
              <span className="muted small">Length {fmtS(Math.max(0, end - inPt))} s</span>
            </div>
          </div>
        )}

        <aside className="crop-side">
          <div className="crop-group">
            <div className="crop-label">Shape</div>
            <Segmented value={shape} options={SHAPES} onChange={chooseShape} />
            {shape === 'free' && (
              <div className="crop-sub">
                <span className="muted small">Preview on</span>
                <Segmented value={freeTarget} options={[{ value: 'light', label: 'A light' }, { value: 'sign', label: 'The sign' }]} onChange={setFreeTarget} />
              </div>
            )}
            <p className="muted small crop-target-note">
              {SIGN_W} × {previewH} LEDs: {target === 'light' ? 'one light' : 'the whole sign'}{shape === 'free' ? ', filled as a clip with Fit: Cover (the dashed line)' : ''}.
            </p>
          </div>

          <div className="crop-group">
            <div className="crop-label">Turn</div>
            <div className="row">
              <button onClick={() => turn(-1)} aria-label="Turn left" data-testid="crop-turn-left">↺ Left</button>
              <button onClick={() => turn(1)} aria-label="Turn right" data-testid="crop-turn-right">↻ Right</button>
              <span className="muted small" data-testid="crop-rotation">{rotate}°</span>
            </div>
          </div>

          <div className="crop-group">
            <div className="muted small" data-testid="crop-info">
              Crop {cropW} × {cropH} px of {RW} × {RH}
              {isVideo && <> · {fmtS(Math.max(0, end - inPt))} s</>}
            </div>
            {overlay && overlay.reg.w < SIGN_W - 0.5 && (
              <div className="small crop-warn">Fewer pixels across than the {SIGN_W} LEDs of a light: the sign shows them enlarged (blocky).</div>
            )}
            <button onClick={reset} className="small-btn" data-testid="crop-reset">Reset</button>
          </div>
        </aside>
      </div>
    </div>
  );
}

/** Seconds, typed freely: commits when the text is a number, tidies up on leaving the field. */
function TimeInput({ value, onChange, testId }: { value: number; onChange: (v: number) => void; testId: string }) {
  const [text, setText] = useState(fmtS(value));
  const focused = useRef(false);
  useEffect(() => {
    if (!focused.current) setText(fmtS(value));
  }, [value]);
  return (
    <input type="number" inputMode="decimal" step={0.01} min={0} value={text} data-testid={testId}
      onFocus={() => (focused.current = true)}
      onBlur={() => {
        focused.current = false;
        setText(fmtS(value));
      }}
      onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
      onChange={(e) => {
        setText(e.target.value);
        const v = parseFloat(e.target.value);
        if (isFinite(v) && v >= 0) onChange(v);
      }} />
  );
}

/** The scrub bar: the whole original, the kept part between the in and out handles, the playhead. */
function Scrub({ duration, inPct, outPct, playheadRef, onSeek, onIn, onOut }: {
  duration: number; inPct: number; outPct: number; playheadRef: React.RefObject<HTMLDivElement>;
  onSeek: (f: number) => void; onIn: (f: number) => void; onOut: (f: number) => void;
}) {
  const bar = useRef<HTMLDivElement>(null);
  const drag = useRef<{ what: 'seek' | 'in' | 'out'; id: number } | null>(null);
  const frac = (x: number) => {
    const r = bar.current!.getBoundingClientRect();
    return Math.max(0, Math.min(1, (x - r.left) / r.width));
  };
  const down = (what: 'seek' | 'in' | 'out') => (e: RPointerEvent) => {
    e.preventDefault();
    e.stopPropagation();
    bar.current?.setPointerCapture(e.pointerId);
    drag.current = { what, id: e.pointerId };
    move(e);
  };
  const move = (e: RPointerEvent) => {
    const d = drag.current;
    if (!d || d.id !== e.pointerId || !duration) return;
    const f = frac(e.clientX);
    if (d.what === 'seek') onSeek(f);
    else if (d.what === 'in') onIn(f);
    else onOut(f);
  };
  const up = (e: RPointerEvent) => {
    if (drag.current?.id === e.pointerId) drag.current = null;
  };
  return (
    <div className="crop-scrub" ref={bar} data-testid="crop-scrub" onPointerDown={down('seek')} onPointerMove={move} onPointerUp={up} onPointerCancel={up}>
      <div className="crop-scrub-track" />
      <div className="crop-scrub-kept" style={{ left: `${inPct}%`, width: `${Math.max(0, outPct - inPct)}%` }} />
      <div className="crop-scrub-head" ref={playheadRef} />
      <div className="crop-scrub-handle in" style={{ left: `${inPct}%` }} onPointerDown={down('in')} data-testid="crop-in-handle" aria-label="In point" />
      <div className="crop-scrub-handle out" style={{ left: `${outPct}%` }} onPointerDown={down('out')} data-testid="crop-out-handle" aria-label="Out point" />
    </div>
  );
}

/** A short note of what was done to an item, for its card. */
export function editNote(item: MediaItem): string | null {
  const e = item.edit;
  if (!e) return null;
  const parts: string[] = [];
  if (e.crop || e.shape !== 'free') parts.push(e.shape === 'free' ? 'Cropped' : e.shape === 'light' ? 'One light' : 'Whole sign');
  if (e.rotate) parts.push(`turned ${e.rotate}°`);
  if (item.kind !== 'image' && (e.start > 0 || e.end !== null)) parts.push(`${fmtS(e.start)}–${e.end === null ? 'end' : fmtS(e.end)} s`);
  if (!parts.length) return null;
  const s = parts.join(', ');
  return s[0].toUpperCase() + s.slice(1);
}

