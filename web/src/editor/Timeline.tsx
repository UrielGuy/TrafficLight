// The timeline: ruler, track rows with their clips, playhead, and every pointer
// interaction on them (move, trim, marquee, scrub, loop range, palette drops,
// zoom). Drags draw into the drag layer only; the document changes once, on drop.
import { memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ClipType, MediaItem, Settings, Target, Track } from '../api/types';
import { useCatalog, useMedia, useSettings } from '../state/app';
import { confirmDialog } from '../state/confirm';
import { useStore } from '../state/store';
import { accepts, dropItem } from './actions';
import { gridBeats } from './beats';
import { itemDuration, makeClip, offsetTypes, trackColor, typeMap, type PaletteItem, type TypeMap } from './clipinfo';
import { ClipView } from './ClipView';
import { docStore, draft, edit, select, setInsert, toggleSelect, useDoc } from './doc';
import { dragItemStore, dragView, dropTarget, EMPTY_DRAG, type Ghost } from './dnd';
import { drawGrid, drawRuler } from './draw';
import { clock, loopStore, problemsStore, sessionStore, type EditorSession } from './session';
import {
  addTrack, applyMoves, applyTrim, clipEnd, findClip, moveTrack, placeNew, planMove, removeTrack, snapBlock, snapPoints,
  snapTarget, TARGET_NAMES, trim, updateClip, updateTrack, type Located, type Move, type SnapContext,
} from './model';
import { MAX_PPS, MIN_PPS, timelineApi, viewStore } from './view';

const RULER_H = 36;
/** Track names that already say what the track is (no second line needed). */
const DEFAULT_NAMES = ['Whole sign', 'Top', 'Middle', 'Bottom', 'Audio', ...Object.values(TARGET_NAMES)];
const DRAG_START_PX = 4;

type Gesture =
  | { kind: 'pending-clip'; id: string; edge: 'start' | 'end' | null; x0: number; y0: number; wasSelected: boolean; mod: boolean; touch: boolean }
  | { kind: 'pending-empty'; x0: number; y0: number; trackId: string | null; t: number; additive: boolean }
  | { kind: 'move'; ids: string[]; start: number; dur: number; row: number; t0: number; ctx: SnapContext; plan: Move[] | null }
  | { kind: 'trim'; loc: Located; edge: 'start' | 'end'; t0: number; minStart: number; withOffset: boolean; ctx: SnapContext; result: { start: number; duration: number } | null }
  | { kind: 'marquee'; cx0: number; cy0: number; additive: boolean; base: string[] }
  | { kind: 'track'; id: string; to: number };

type RulerGesture = { kind: 'scrub' } | { kind: 'loop-edge'; which: 'from' | 'to' } | { kind: 'loop-make'; from: number; x0: number };

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));

export default function Timeline({ session, narrow, onInspect }: { session: EditorSession; narrow: boolean; onInspect: () => void }) {
  const HEAD_W = narrow ? 120 : 190;
  const ROW_H = narrow ? 50 : 56;
  const { pps, snap } = useStore(viewStore);
  const tracks = useDoc((s) => s.history.present.tracks);
  const bpm = useDoc((s) => s.history.present.bpm);
  const offset = useDoc((s) => s.history.present.beat_offset);
  const duration = useDoc((s) => s.history.present.duration);
  const selection = useDoc((s) => s.selection);
  const insert = useDoc((s) => s.insert);
  const epoch = useDoc((s) => s.epoch);
  const problems = useStore(problemsStore);
  const loop = useStore(loopStore);
  const playing = useStore(sessionStore).playing;
  const catalog = useCatalog();
  const types = useMemo(() => typeMap(catalog), [catalog]);
  const mediaList = useMedia();
  const media = useMemo(() => new Map((mediaList ?? []).map((m) => [m.id, m])), [mediaList]);
  const settings = useSettings();
  const dragItem = useStore(dragItemStore);

  const scroller = useRef<HTMLDivElement>(null);
  const rulerCv = useRef<HTMLCanvasElement>(null);
  const gridCv = useRef<HTMLCanvasElement>(null);
  const playhead = useRef<HTMLDivElement>(null);
  const [viewW, setViewW] = useState(800);
  const [addMenu, setAddMenu] = useState<{ x: number; y: number } | null>(null);

  const end = useMemo(() => Math.max(duration, ...tracks.flatMap((t) => t.clips.map(clipEnd))), [tracks, duration]);
  const contentW = Math.max(viewW - HEAD_W, (end + 8) * pps);

  // Latest values for event handlers and the animation loop (they are created once).
  const L = useRef({ pps, snap, tracks, bpm, offset, duration, loop, playing, HEAD_W, ROW_H, types, end, viewW });
  L.current = { pps, snap, tracks, bpm, offset, duration, loop, playing, HEAD_W, ROW_H, types, end, viewW };

  // ---------------------------------------------------------------- geometry
  const xToT = useCallback((cx: number) => {
    const sc = scroller.current!;
    const r = sc.getBoundingClientRect();
    return (cx - r.left - L.current.HEAD_W + sc.scrollLeft) / L.current.pps;
  }, []);
  const rowAt = useCallback((cy: number) => {
    const sc = scroller.current!;
    const r = sc.getBoundingClientRect();
    return Math.floor((cy - r.top + sc.scrollTop) / L.current.ROW_H);
  }, []);

  const redraw = useCallback(() => {
    const sc = scroller.current;
    if (!sc) return;
    const v = L.current;
    const t0 = sc.scrollLeft / v.pps;
    const base = { pps: v.pps, t0, bpm: v.bpm, offset: v.offset, duration: v.duration };
    if (rulerCv.current) drawRuler(rulerCv.current, { ...base, width: rulerCv.current.clientWidth, height: rulerCv.current.clientHeight }, v.loop, v.playing);
    if (gridCv.current) drawGrid(gridCv.current, { ...base, width: gridCv.current.clientWidth, height: gridCv.current.clientHeight }, v.loop);
  }, []);
  useEffect(redraw, [pps, bpm, offset, duration, loop, viewW, playing, tracks.length, redraw]);

  useEffect(() => {
    const sc = scroller.current!;
    const ro = new ResizeObserver(() => {
      setViewW(sc.clientWidth);
      redraw();
    });
    ro.observe(sc);
    return () => ro.disconnect();
  }, [redraw]);

  // ---------------------------------------------------------------- zoom
  const anchor = useRef<{ t: number; x: number } | null>(null);
  const zoomTo = useCallback((next: number, clientX?: number) => {
    const sc = scroller.current!;
    const r = sc.getBoundingClientRect();
    const v = L.current;
    let x: number;
    if (clientX !== undefined) x = clientX - r.left - v.HEAD_W;
    else {
      // keep the playhead still if it is in view, else the middle
      const px = clock.now() * v.pps - sc.scrollLeft;
      x = px >= 0 && px <= sc.clientWidth - v.HEAD_W ? px : (sc.clientWidth - v.HEAD_W) / 2;
    }
    anchor.current = { t: (x + sc.scrollLeft) / v.pps, x };
    viewStore.set((s) => ({ ...s, pps: clamp(next, MIN_PPS, MAX_PPS) }));
  }, []);
  useLayoutEffect(() => {
    const a = anchor.current;
    const sc = scroller.current;
    if (a && sc) sc.scrollLeft = Math.max(0, a.t * pps - a.x);
    anchor.current = null;
    redraw();
  }, [pps, redraw]);

  const fit = useCallback(() => {
    const sc = scroller.current!;
    const v = L.current;
    anchor.current = { t: 0, x: 0 };
    viewStore.set((s) => ({ ...s, pps: clamp((sc.clientWidth - v.HEAD_W - 24) / Math.max(1, v.end), MIN_PPS, MAX_PPS) }));
  }, []);
  // A new set: fit it once the layout is known.
  useEffect(() => {
    const id = requestAnimationFrame(fit);
    return () => cancelAnimationFrame(id);
  }, [epoch, fit]);

  useEffect(() => {
    timelineApi.current = {
      zoom: (f, cx) => zoomTo(L.current.pps * f, cx),
      fit,
      reveal: (t) => {
        const sc = scroller.current!;
        const x = t * L.current.pps - sc.scrollLeft;
        const w = sc.clientWidth - L.current.HEAD_W;
        if (x < 0 || x > w - 20) sc.scrollLeft = Math.max(0, t * L.current.pps - w / 3);
      },
    };
    return () => {
      timelineApi.current = null;
    };
  }, [zoomTo, fit]);

  // ctrl+wheel / trackpad pinch zoom, wheel scrolls sideways when there is nothing to scroll down, touch pinch.
  useEffect(() => {
    const sc = scroller.current!;
    const onWheel = (e: WheelEvent) => {
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        zoomTo(L.current.pps * Math.exp(-e.deltaY * 0.0025), e.clientX);
      } else if (!e.shiftKey && Math.abs(e.deltaY) > Math.abs(e.deltaX) && sc.scrollHeight <= sc.clientHeight + 1) {
        e.preventDefault();
        sc.scrollLeft += e.deltaY;
      }
    };
    let pinch: { d0: number; pps0: number } | null = null;
    const dist = (t: TouchList) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
    const onTouchStart = (e: TouchEvent) => {
      if (e.touches.length === 2) {
        pinch = { d0: dist(e.touches) || 1, pps0: L.current.pps };
        cancelGesture();
      }
    };
    const onTouchMove = (e: TouchEvent) => {
      if (!pinch || e.touches.length !== 2) return;
      e.preventDefault();
      zoomTo(pinch.pps0 * (dist(e.touches) / pinch.d0), (e.touches[0].clientX + e.touches[1].clientX) / 2);
    };
    const onTouchEnd = (e: TouchEvent) => {
      if (e.touches.length < 2) pinch = null;
    };
    sc.addEventListener('wheel', onWheel, { passive: false });
    sc.addEventListener('touchstart', onTouchStart, { passive: true });
    sc.addEventListener('touchmove', onTouchMove, { passive: false });
    sc.addEventListener('touchend', onTouchEnd);
    sc.addEventListener('touchcancel', onTouchEnd);
    return () => {
      sc.removeEventListener('wheel', onWheel);
      sc.removeEventListener('touchstart', onTouchStart);
      sc.removeEventListener('touchmove', onTouchMove);
      sc.removeEventListener('touchend', onTouchEnd);
      sc.removeEventListener('touchcancel', onTouchEnd);
    };
  }, [zoomTo]);

  // ---------------------------------------------------------------- playhead (animation frame, no React)
  useEffect(() => {
    let raf = 0;
    let prevT = 0;
    const tick = () => {
      const sc = scroller.current;
      const ph = playhead.current;
      if (sc && ph) {
        const v = L.current;
        const t = clock.now();
        const w = sc.clientWidth - v.HEAD_W;
        let x = t * v.pps - sc.scrollLeft;
        if (clock.playing && !gesture.current && !rulerG.current) {
          // follow playback a page at a time; jump back when the loop wraps
          if (x > w - 16 && x < w + 80) {
            sc.scrollLeft += w * 0.8;
            redraw();
          } else if (t < prevT - 0.05 && (x < 0 || x > w)) {
            sc.scrollLeft = Math.max(0, t * v.pps - 16);
            redraw();
          }
          x = t * v.pps - sc.scrollLeft;
        }
        prevT = t;
        ph.style.transform = `translateX(${Math.round(x)}px)`;
        ph.style.display = x < -8 || x > w + 8 ? 'none' : '';
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [redraw]);

  // ---------------------------------------------------------------- clip gestures
  const gesture = useRef<Gesture | null>(null);
  const last = useRef({ x: 0, y: 0, shift: false });
  const autoRaf = useRef(0);
  const paletteHover = useRef<{ item: PaletteItem; x: number; y: number } | null>(null);

  const snapCtx = useCallback((exclude: Set<string>): SnapContext => {
    const s = draft();
    const v = L.current;
    const lp = loopStore.get();
    const extra = [0, s.duration, clock.now()];
    if (lp.to !== null) extra.push(lp.from, lp.to);
    return { points: snapPoints(s, exclude, extra), grid: { bpm: s.bpm, offset: s.beat_offset, stepBeats: gridBeats(v.pps, s.bpm, 10) }, threshold: 8 / v.pps };
  }, []);

  const markMoving = (ids: string[], on: boolean) => {
    const sc = scroller.current;
    if (!sc) return;
    for (const id of ids) sc.querySelector(`[data-clip="${id}"]`)?.classList.toggle('moving', on);
  };

  const update = useCallback(() => {
    const g = gesture.current;
    if (!g) return;
    const p = last.current;
    const v = L.current;
    const s = draft();
    const useSnap = v.snap && !p.shift;
    if (g.kind === 'move') {
      let dt = xToT(p.x) - g.t0;
      let snapAt: number | null = null;
      if (useSnap) {
        const r = snapBlock(g.start + dt, g.dur, g.ctx);
        dt = r.start - g.start;
        snapAt = r.at;
      }
      const rows = clamp(rowAt(p.y), 0, s.tracks.length - 1) - g.row;
      const plan = planMove(s, g.ids, dt, rows, accepts);
      g.plan = plan;
      const ghosts: Ghost[] = [];
      for (const id of g.ids) {
        const loc = findClip(s, id);
        if (!loc) continue;
        const m = plan?.find((x) => x.id === id);
        const row = m ? s.tracks.findIndex((t) => t.id === m.toTrack) : loc.trackIndex;
        ghosts.push({ clip: { ...loc.clip, start: m ? m.start : loc.clip.start + dt }, row });
      }
      const landed = plan ? Math.abs((plan.find((m) => m.id === g.ids[0])?.start ?? NaN) - (findClip(s, g.ids[0])!.clip.start + dt)) < 1e-6 : false;
      dragView.set({ ...EMPTY_DRAG, ghosts, snapAt: landed ? snapAt : null, invalid: !plan });
    } else if (g.kind === 'trim') {
      const edge0 = g.edge === 'start' ? g.loc.clip.start : clipEnd(g.loc.clip);
      let to = edge0 + xToT(p.x) - g.t0;
      let snapAt: number | null = null;
      if (useSnap) {
        const st = snapTarget(to, g.ctx);
        if (st) {
          to = st.t;
          if (st.magnet) snapAt = st.t;
        }
      }
      const tr = s.tracks[g.loc.trackIndex];
      const r = trim(tr, g.loc.clip, g.edge, to, g.minStart);
      g.result = r;
      const edgeAt = g.edge === 'start' ? r.start : r.start + r.duration;
      const clip = applyTrim(g.loc.clip, r.start, r.duration, g.withOffset);
      dragView.set({ ...EMPTY_DRAG, ghosts: [{ clip, row: g.loc.trackIndex }], snapAt: snapAt !== null && Math.abs(edgeAt - snapAt) < 1e-6 ? snapAt : null });
    } else if (g.kind === 'marquee') {
      const sc = scroller.current!;
      const r = sc.getBoundingClientRect();
      const cx1 = p.x - r.left + sc.scrollLeft;
      const cy1 = p.y - r.top + sc.scrollTop;
      dragView.set({ ...EMPTY_DRAG, marquee: { x0: g.cx0, y0: g.cy0, x1: cx1, y1: cy1 } });
      // live selection
      const ta = (Math.min(g.cx0, cx1) - v.HEAD_W) / v.pps;
      const tb = (Math.max(g.cx0, cx1) - v.HEAD_W) / v.pps;
      const ra = Math.floor(Math.min(g.cy0, cy1) / v.ROW_H);
      const rb = Math.floor(Math.max(g.cy0, cy1) / v.ROW_H);
      const ids = new Set(g.base);
      s.tracks.forEach((t, i) => {
        if (i < ra || i > rb) return;
        for (const c of t.clips) if (c.start < tb && clipEnd(c) > ta) ids.add(c.id);
      });
      select([...ids]);
    } else if (g.kind === 'track') {
      g.to = clamp(rowAt(p.y), 0, s.tracks.length - 1);
      dragView.set({ ...EMPTY_DRAG, trackTo: g.to });
    }
  }, [xToT, rowAt]);

  // Scrolls while a drag is near the left or right edge.
  const autoscroll = useCallback(() => {
    cancelAnimationFrame(autoRaf.current);
    const step = () => {
      const sc = scroller.current;
      const g = gesture.current;
      const ph = paletteHover.current;
      if (!sc || (!g && !ph) || (g && g.kind !== 'move' && g.kind !== 'trim' && g.kind !== 'marquee')) return;
      const r = sc.getBoundingClientRect();
      const p = last.current;
      const left = r.left + L.current.HEAD_W;
      let v = 0;
      if (p.x < left + 24 && p.x > r.left - 60) v = -(left + 24 - p.x) / 3;
      else if (p.x > r.right - 24 && p.x < r.right + 60) v = (p.x - (r.right - 24)) / 3;
      if (v) {
        sc.scrollLeft += clamp(v, -30, 30);
        redraw();
        if (g) update();
        else if (ph) showDrop(ph.item, ph.x, ph.y);
      }
      autoRaf.current = requestAnimationFrame(step);
    };
    autoRaf.current = requestAnimationFrame(step);
  }, [redraw, update]);

  const cancelGesture = () => {
    const g = gesture.current;
    if (g?.kind === 'move') markMoving(g.ids, false);
    if (g?.kind === 'trim') markMoving([g.loc.clip.id], false);
    gesture.current = null;
    cancelAnimationFrame(autoRaf.current);
    dragView.set(EMPTY_DRAG);
  };

  const onPointerDown = (e: React.PointerEvent) => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    const target = e.target as HTMLElement;
    if (target.closest('.tl-head')) return;
    (document.activeElement as HTMLElement | null)?.blur?.();
    const sc = scroller.current!;
    last.current = { x: e.clientX, y: e.clientY, shift: e.shiftKey };
    const mod = e.shiftKey || e.ctrlKey || e.metaKey;
    const clipEl = target.closest<HTMLElement>('[data-clip]');
    if (clipEl) {
      const id = clipEl.dataset.clip!;
      const edge = (target.closest<HTMLElement>('[data-edge]')?.dataset.edge ?? null) as 'start' | 'end' | null;
      const wasSelected = docStore.get()!.selection.includes(id);
      if (mod) toggleSelect(id);
      else if (!wasSelected) select([id]);
      // On touch a first tap only selects (so a swipe over clips still scrolls); a selected clip drags.
      if (e.pointerType === 'touch' && !wasSelected) return;
      if (mod && wasSelected) return;
      gesture.current = { kind: 'pending-clip', id, edge, x0: e.clientX, y0: e.clientY, wasSelected, mod, touch: e.pointerType === 'touch' };
      sc.setPointerCapture(e.pointerId);
      e.preventDefault();
    } else if (target.closest('.tl-lane')) {
      const track = L.current.tracks[rowAt(e.clientY)];
      gesture.current = { kind: 'pending-empty', x0: e.clientX, y0: e.clientY, trackId: track?.id ?? null, t: xToT(e.clientX), additive: mod };
      if (e.pointerType !== 'touch') {
        sc.setPointerCapture(e.pointerId);
        e.preventDefault();
      }
    }
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const g = gesture.current;
    if (!g) return;
    last.current = { x: e.clientX, y: e.clientY, shift: e.shiftKey };
    if (g.kind === 'pending-clip') {
      if (Math.hypot(e.clientX - g.x0, e.clientY - g.y0) < DRAG_START_PX) return;
      const s = draft();
      const loc = findClip(s, g.id);
      if (!loc) return cancelGesture();
      if (g.edge) {
        select([g.id]);
        const withOffset = offsetTypes(L.current.types).has(loc.clip.type);
        const off = withOffset ? Number(loc.clip.params.offset ?? 0) || 0 : 0;
        gesture.current = { kind: 'trim', loc, edge: g.edge, t0: xToT(g.x0), minStart: withOffset ? loc.clip.start - off : 0, withOffset, ctx: snapCtx(new Set([g.id])), result: null };
        markMoving([g.id], true);
      } else {
        const sel = docStore.get()!.selection;
        const ids = sel.includes(g.id) ? [g.id, ...sel.filter((x) => x !== g.id)] : [g.id];
        gesture.current = { kind: 'move', ids, start: loc.clip.start, dur: loc.clip.duration, row: loc.trackIndex, t0: xToT(g.x0), ctx: snapCtx(new Set(ids)), plan: null };
        markMoving(ids, true);
      }
      autoscroll();
    } else if (g.kind === 'pending-empty') {
      if (Math.hypot(e.clientX - g.x0, e.clientY - g.y0) < DRAG_START_PX || e.pointerType === 'touch') return;
      const sc = scroller.current!;
      const r = sc.getBoundingClientRect();
      gesture.current = { kind: 'marquee', cx0: g.x0 - r.left + sc.scrollLeft, cy0: g.y0 - r.top + sc.scrollTop, additive: g.additive, base: g.additive ? docStore.get()!.selection : [] };
      autoscroll();
    }
    update();
  };

  const onPointerUp = (e: React.PointerEvent) => {
    const g = gesture.current;
    if (!g) return;
    last.current = { x: e.clientX, y: e.clientY, shift: e.shiftKey };
    if (g.kind === 'move' || g.kind === 'trim') update();
    cancelGesture();
    if (g.kind === 'pending-clip') {
      if (!g.mod && g.wasSelected) {
        if (docStore.get()!.selection.length === 1) onInspect();
        select([g.id]);
      }
    } else if (g.kind === 'pending-empty') {
      if (!g.additive) select([]);
      if (g.trackId) {
        const t = Math.max(0, g.t);
        const st = L.current.snap && !e.shiftKey ? snapTarget(t, snapCtx(new Set())) : null;
        setInsert({ trackId: g.trackId, t: Math.max(0, st?.t ?? Math.round(t * 1000) / 1000) });
      }
    } else if (g.kind === 'move') {
      if (g.plan && g.plan.some((m) => {
        const loc = findClip(draft(), m.id);
        return !loc || loc.clip.start !== m.start || loc.track.id !== m.toTrack;
      })) edit((s) => applyMoves(s, g.plan!));
    } else if (g.kind === 'trim') {
      const r = g.result;
      if (r) edit((s) => updateClip(s, g.loc.clip.id, (c) => applyTrim(c, r.start, r.duration, g.withOffset)));
    } else if (g.kind === 'track') {
      edit((s) => moveTrack(s, g.id, g.to));
    }
  };

  // Shift pressed or released mid-drag: re-evaluate (it suspends snapping).
  useEffect(() => {
    const k = (e: KeyboardEvent) => {
      if (e.key !== 'Shift' || !gesture.current) return;
      last.current = { ...last.current, shift: e.type === 'keydown' };
      update();
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && gesture.current) {
        e.stopPropagation();
        cancelGesture();
      }
    };
    window.addEventListener('keydown', k);
    window.addEventListener('keyup', k);
    window.addEventListener('keydown', esc, true);
    return () => {
      window.removeEventListener('keydown', k);
      window.removeEventListener('keyup', k);
      window.removeEventListener('keydown', esc, true);
    };
  }, [update]);

  const startTrackDrag = useCallback((id: string, e: React.PointerEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const idx = L.current.tracks.findIndex((t) => t.id === id);
    gesture.current = { kind: 'track', id, to: idx };
    last.current = { x: e.clientX, y: e.clientY, shift: false };
    scroller.current!.setPointerCapture(e.pointerId);
    dragView.set({ ...EMPTY_DRAG, trackTo: idx });
  }, []);

  // ---------------------------------------------------------------- palette drops
  const computeDrop = useCallback((item: PaletteItem, x: number, y: number) => {
    const sc = scroller.current;
    if (!sc) return null;
    const r = sc.getBoundingClientRect();
    if (x < r.left + L.current.HEAD_W || x > r.right || y < r.top || y > r.bottom) return null;
    const row = rowAt(y);
    const track = draft().tracks[row];
    if (!track || !accepts(item.type.type, track.target)) return null;
    const dur = itemDuration(item);
    let t = Math.max(0, xToT(x));
    if (L.current.snap) t = snapBlock(t, dur, snapCtx(new Set())).start;
    t = Math.max(0, t);
    return { track, row, t, place: placeNew(track, t, dur) };
  }, [rowAt, xToT, snapCtx]);

  const showDrop = useCallback((item: PaletteItem, x: number, y: number): boolean => {
    const d = computeDrop(item, x, y);
    if (!d) {
      if (dragView.get().ghosts.length) dragView.set(EMPTY_DRAG);
      return false;
    }
    const clip = makeClip(item, d.place.start, d.place.duration);
    clip.id = 'drop';
    dragView.set({ ...EMPTY_DRAG, ghosts: [{ clip, row: d.row }] });
    return true;
  }, [computeDrop]);

  useEffect(() => {
    dropTarget.current = {
      hover(item, x, y) {
        const first = !paletteHover.current;
        paletteHover.current = { item, x, y };
        last.current = { x, y, shift: false };
        if (first) autoscroll();
        return showDrop(item, x, y);
      },
      drop(item, x, y) {
        const d = computeDrop(item, x, y);
        this.leave();
        if (!d) return false;
        return dropItem(item, d.track.id, d.t) !== null;
      },
      leave() {
        paletteHover.current = null;
        cancelAnimationFrame(autoRaf.current);
        dragView.set(EMPTY_DRAG);
      },
    };
    return () => {
      dropTarget.current = null;
    };
  }, [showDrop, computeDrop, autoscroll]);

  // ---------------------------------------------------------------- ruler: scrub, loop range
  const rulerG = useRef<RulerGesture | null>(null);
  const rulerT = (cx: number) => {
    const c = rulerCv.current!;
    const r = c.getBoundingClientRect();
    return Math.max(0, (cx - r.left + scroller.current!.scrollLeft) / L.current.pps);
  };
  const snapLoop = (t: number, shift: boolean) => Math.max(0, L.current.snap && !shift ? snapTarget(t, snapCtx(new Set()))?.t ?? t : t);
  const onRulerDown = (e: React.PointerEvent) => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    (document.activeElement as HTMLElement | null)?.blur?.();
    const t = rulerT(e.clientX);
    const lp = loopStore.get();
    const near = (x: number) => Math.abs((x - t) * L.current.pps) < (e.pointerType === 'touch' ? 14 : 7);
    rulerCv.current!.setPointerCapture(e.pointerId);
    e.preventDefault();
    if (lp.on && lp.to !== null && near(lp.from)) rulerG.current = { kind: 'loop-edge', which: 'from' };
    else if (lp.on && lp.to !== null && near(lp.to)) rulerG.current = { kind: 'loop-edge', which: 'to' };
    else if (e.shiftKey) rulerG.current = { kind: 'loop-make', from: t, x0: e.clientX };
    else {
      rulerG.current = { kind: 'scrub' };
      session.seek(t);
    }
  };
  const onRulerMove = (e: React.PointerEvent) => {
    const g = rulerG.current;
    const t = rulerT(e.clientX);
    if (!g) {
      // cursor hint over loop edges
      const lp = loopStore.get();
      const near = lp.on && lp.to !== null && [lp.from, lp.to].some((x) => Math.abs((x - t) * L.current.pps) < 7);
      rulerCv.current!.style.cursor = near ? 'ew-resize' : '';
      return;
    }
    if (g.kind === 'scrub') session.seek(t);
    else if (g.kind === 'loop-edge') {
      const lp = loopStore.get();
      const v = snapLoop(t, e.shiftKey);
      const from = g.which === 'from' ? v : lp.from;
      const to = g.which === 'to' ? v : lp.to!;
      if (to - from >= 0.1) session.setLoop({ on: true, from: Math.min(from, to), to: Math.max(from, to) });
    } else if (g.kind === 'loop-make' && Math.abs(e.clientX - g.x0) > 4) {
      const a = snapLoop(g.from, false);
      const b = snapLoop(t, false);
      if (Math.abs(b - a) >= 0.1) session.setLoop({ on: true, from: Math.min(a, b), to: Math.max(a, b) });
    }
  };
  const onRulerUp = () => {
    rulerG.current = null;
  };

  // ---------------------------------------------------------------- render
  const selSet = useMemo(() => new Set(selection), [selection]);
  const color = (t: Target) => trackColor(t, settings);

  return (
    <div className="tl" style={{ ['--row-h' as string]: `${ROW_H}px`, ['--head-w' as string]: `${HEAD_W}px` }}>
      <div className="tl-top" style={{ height: RULER_H }}>
        <div className="tl-corner" style={{ width: HEAD_W }}>
          <button className="tl-add" data-pop-toggle onClick={(e) => {
            const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
            setAddMenu(addMenu ? null : { x: r.left, y: r.bottom + 4 });
          }} aria-haspopup="menu">+ Track</button>
        </div>
        <canvas ref={rulerCv} className="tl-ruler" onPointerDown={onRulerDown} onPointerMove={onRulerMove}
          onPointerUp={onRulerUp} onPointerCancel={onRulerUp} aria-label="Time ruler: click or drag to move the playhead, shift-drag to set a loop range" />
      </div>
      <div className="tl-body">
        <canvas ref={gridCv} className="tl-grid" style={{ left: HEAD_W }} />
        <div ref={scroller} className={`tl-scroll ${dragItem ? 'palette-drag' : ''}`} onScroll={redraw}
          onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp} onPointerCancel={cancelGesture}
          onDoubleClick={(e) => (e.target as HTMLElement).closest('[data-clip]') && onInspect()}>
          <div className="tl-inner" data-pps={pps} style={{ width: HEAD_W + contentW, height: Math.max(tracks.length, 1) * ROW_H }}>
            {tracks.map((t, i) => {
              const sel = t.clips.filter((c) => selSet.has(c.id)).map((c) => c.id).join(',');
              const drop = dragItem ? (accepts(dragItem.type.type, t.target) ? 'drop-ok' : 'drop-no') : '';
              return (
                <TrackRow key={t.id} track={t} index={i} count={tracks.length} pps={pps} headW={HEAD_W} rowH={ROW_H} contentW={contentW}
                  selKey={sel} problems={problems} types={types} media={media} color={color(t.target)} settings={settings}
                  drop={drop} insertT={insert?.trackId === t.id ? insert.t : null} onGrip={startTrackDrag} />
              );
            })}
            {!tracks.length && <div className="tl-empty" style={{ left: HEAD_W }}>No tracks. Use “+ Track” to add one.</div>}
            <DragLayer headW={HEAD_W} rowH={ROW_H} pps={pps} types={types} media={media} settings={settings} />
          </div>
        </div>
      </div>
      <div className="tl-ph-clip" style={{ left: HEAD_W }}>
        <div ref={playhead} className="tl-playhead"><i /></div>
      </div>
      {addMenu && (
        <Popover x={addMenu.x} y={addMenu.y} onClose={() => setAddMenu(null)}>
          {(['face0', 'face1', 'face2', 'all', 'audio'] as Target[]).map((tg) => (
            <button key={tg} role="menuitem" onClick={() => {
              setAddMenu(null);
              edit((s) => addTrack(s, tg).set);
            }}><TrackMarker target={tg} settings={settings} /> {TARGET_NAMES[tg]}</button>
          ))}
        </Popover>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- pieces
function DragLayer({ headW, rowH, pps, types, media, settings }: { headW: number; rowH: number; pps: number; types: TypeMap; media: Map<string, MediaItem>; settings: Settings | null }) {
  const v = useStore(dragView);
  const tracks = draft().tracks;
  return (
    <>
      {v.ghosts.map((g) => {
        const m = typeof g.clip.params.media === 'string' ? media.get(g.clip.params.media) : undefined;
        return (
          <ClipView key={`ghost-${g.clip.id}`} clip={g.clip} pps={pps} selected={false} type={types.get(g.clip.type)} media={m}
            trackColor={trackColor(tracks[g.row]?.target ?? 'all', settings)} ghost invalid={v.invalid} top={g.row * rowH + 4} offsetX={headW} />
        );
      })}
      {v.snapAt !== null && <div className="tl-snapline" style={{ left: headW + v.snapAt * pps }} />}
      {v.marquee && (
        <div className="tl-marquee" style={{
          left: Math.min(v.marquee.x0, v.marquee.x1), top: Math.min(v.marquee.y0, v.marquee.y1),
          width: Math.abs(v.marquee.x1 - v.marquee.x0), height: Math.abs(v.marquee.y1 - v.marquee.y0),
        }} />
      )}
      {v.trackTo !== null && <div className="tl-track-drop" style={{ top: v.trackTo * rowH, height: rowH }} />}
    </>
  );
}

export function TrackMarker({ target, settings }: { target: Target; settings: Settings | null }) {
  if (target === 'all') {
    return (
      <span className="tmark all" aria-hidden>
        {[0, 1, 2].map((i) => <i key={i} style={{ background: trackColor(`face${i}` as Target, settings) }} />)}
      </span>
    );
  }
  if (target === 'audio') return <span className="tmark audio" aria-hidden>♪</span>;
  return <span className="tmark face" aria-hidden style={{ background: trackColor(target, settings), boxShadow: `0 0 8px ${trackColor(target, settings)}66` }} />;
}

interface RowProps {
  track: Track;
  index: number;
  count: number;
  pps: number;
  headW: number;
  rowH: number;
  contentW: number;
  selKey: string;
  problems: Map<string, string[]>;
  types: Map<string, ClipType>;
  media: Map<string, MediaItem>;
  color: string;
  settings: Settings | null;
  drop: string;
  insertT: number | null;
  onGrip: (id: string, e: React.PointerEvent) => void;
}

const TrackRow = memo(function TrackRow({ track, index, count, pps, headW, rowH, contentW, selKey, problems, types, media, color, settings, drop, insertT, onGrip }: RowProps) {
  const sel = useMemo(() => new Set(selKey ? selKey.split(',') : []), [selKey]);
  return (
    <div className={`tl-row ${track.muted ? 'muted' : ''} ${drop} ${track.target === 'audio' ? 'audio' : ''}`} style={{ height: rowH, ['--track-color' as string]: color }}>
      <TrackHead track={track} index={index} count={count} width={headW} settings={settings} onGrip={onGrip} />
      <div className="tl-lane" style={{ width: contentW }}>
        {track.clips.map((c) => (
          <ClipView key={c.id} clip={c} pps={pps} selected={sel.has(c.id)} problems={problems.get(c.id)} type={types.get(c.type)}
            media={typeof c.params.media === 'string' ? media.get(c.params.media) : undefined} trackColor={color} />
        ))}
        {insertT !== null && <div className="tl-insert" style={{ left: insertT * pps }} title="New clips from the library go here" />}
      </div>
    </div>
  );
});

function TrackHead({ track, index, count, width, settings, onGrip }: { track: Track; index: number; count: number; width: number; settings: Settings | null; onGrip: (id: string, e: React.PointerEvent) => void }) {
  const [renaming, setRenaming] = useState(false);
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const [name, setName] = useState(track.name);
  const finish = (ok: boolean) => {
    setRenaming(false);
    if (ok && name.trim() && name.trim() !== track.name) edit((s) => updateTrack(s, track.id, { name: name.trim() }));
  };
  const remove = async () => {
    setMenu(null);
    if (track.clips.length && !(await confirmDialog(`Delete the track "${track.name}"?`, {
      text: `Its ${track.clips.length} clip${track.clips.length > 1 ? 's go' : ' goes'} with it (Undo brings them back).`, action: 'Delete', danger: true,
    }))) return;
    edit((s) => removeTrack(s, track.id));
  };
  return (
    <div className="tl-head" style={{ width }}>
      <span className="tl-grip" onPointerDown={(e) => onGrip(track.id, e)} title="Drag to reorder (top row = top layer)">⠿</span>
      <TrackMarker target={track.target} settings={settings} />
      <div className="tl-head-text">
        {renaming ? (
          <input className="tl-rename" value={name} autoFocus onChange={(e) => setName(e.target.value)} onBlur={() => finish(true)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') finish(true);
              if (e.key === 'Escape') finish(false);
            }} />
        ) : (
          <div className="tl-name ellipsis" onDoubleClick={() => { setName(track.name); setRenaming(true); }} title={track.name}>{track.name || TARGET_NAMES[track.target]}</div>
        )}
        {!DEFAULT_NAMES.includes(track.name) && <div className="tl-target ellipsis">{TARGET_NAMES[track.target]}</div>}
      </div>
      <button className={`tl-mute ${track.muted ? 'on' : ''}`} aria-pressed={track.muted} title={track.muted ? 'Unmute track' : 'Mute track'}
        onClick={() => edit((s) => updateTrack(s, track.id, { muted: !track.muted }))}>M</button>
      <button className="tl-more" data-pop-toggle aria-label="Track menu" aria-haspopup="menu" onClick={(e) => {
        const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
        setMenu(menu ? null : { x: r.left, y: r.bottom + 2 });
      }}>⋯</button>
      {menu && (
        <Popover x={menu.x} y={menu.y} onClose={() => setMenu(null)}>
          <button role="menuitem" onClick={() => { setMenu(null); setName(track.name); setRenaming(true); }}>Rename</button>
          <button role="menuitem" disabled={index === 0} onClick={() => { setMenu(null); edit((s) => moveTrack(s, track.id, index - 1)); }}>Move up</button>
          <button role="menuitem" disabled={index === count - 1} onClick={() => { setMenu(null); edit((s) => moveTrack(s, track.id, index + 1)); }}>Move down</button>
          <button role="menuitem" onClick={() => { setMenu(null); edit((s) => addTrack(s, track.target, index + 1).set); }}>Add a {TARGET_NAMES[track.target].toLowerCase()} track below</button>
          <button role="menuitem" className="danger-text" onClick={remove}>Delete track</button>
        </Popover>
      )}
    </div>
  );
}

/** A small menu at a fixed position (outside the scrolling timeline so it is never clipped). */
export function Popover({ x, y, onClose, children }: { x: number; y: number; onClose: () => void; children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ x, y });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    setPos({ x: Math.max(4, Math.min(x, window.innerWidth - r.width - 4)), y: r.bottom > window.innerHeight - 4 ? Math.max(4, y - r.height - 40) : y });
  }, [x, y]);
  useEffect(() => {
    const down = (e: PointerEvent) => {
      const t = e.target as HTMLElement;
      // the button that opened the menu toggles it itself
      if (ref.current && !ref.current.contains(t) && !t.closest?.('[data-pop-toggle]')) onClose();
    };
    const key = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('pointerdown', down, true);
    window.addEventListener('keydown', key);
    return () => {
      window.removeEventListener('pointerdown', down, true);
      window.removeEventListener('keydown', key);
    };
  }, [onClose]);
  return <div ref={ref} className="popover" role="menu" style={{ left: pos.x, top: pos.y }}>{children}</div>;
}
