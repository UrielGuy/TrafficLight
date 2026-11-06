// What the editor knows about clip types and media: colours, labels, compatibility,
// and how a palette item becomes a clip.
import type { Clip, ClipType, MediaItem, MediaKind, Settings, Target } from '../api/types';
import { accepts, newId } from './model';

export type TypeMap = Map<string, ClipType>;

export const typeMap = (types: ClipType[] | null): TypeMap => new Map((types ?? []).map((t) => [t.type, t]));

/** Clip types whose `offset` param is the in-point in their media (trims and splits move it). */
export const offsetTypes = (types: TypeMap): Set<string> =>
  new Set([...types.values()].filter((t) => t.params.some((p) => p.key === 'offset' && p.kind === 'number')).map((t) => t.type));

export function acceptsType(types: TypeMap, type: string, target: Target): boolean {
  const t = types.get(type);
  // Unknown types (a renderer that went away) may stay where they are but go nowhere new.
  return !!t && accepts(t.targets, target);
}

// Muted hues per palette category; the accent (amber) stays reserved for selection.
const CATEGORY_HUES: Record<string, number> = { basic: 215, text: 200, figure: 165, beat: 275, media: 135, clock: 30 };
export function categoryHue(category: string): number {
  if (category in CATEGORY_HUES) return CATEGORY_HUES[category];
  let h = 0;
  for (const ch of category) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return h;
}
export const CATEGORY_LABELS: Record<string, string> = {
  basic: 'Basic', text: 'Text', figure: 'Figures', beat: 'Beat animations', media: 'Media clips',
};
export const CATEGORY_ORDER = ['basic', 'text', 'figure', 'beat', 'media'];

export const MEDIA_CLIP_TYPE: Record<MediaKind, string> = { video: 'video', image: 'image', audio: 'audio' };

/** Light colour of a track ("all" = white, audio = grey). */
export function trackColor(target: Target, settings: Settings | null): string {
  const fc = settings?.face_colors ?? ['#ff0000', '#ffb000', '#00ff40'];
  if (target === 'face0') return fc[0];
  if (target === 'face1') return fc[1];
  if (target === 'face2') return fc[2];
  if (target === 'all') return '#ffffff';
  return '#8f99a8';
}

/** One line saying what the clip shows: its text, its media's name, its figure... */
export function clipSummary(clip: Clip, type: ClipType | undefined, media: Map<string, MediaItem>): string {
  const p = clip.params;
  if (typeof p.text === 'string') return p.text.replace(/\s+/g, ' ').trim();
  if (typeof p.media === 'string') {
    if (!p.media) return 'No media chosen';
    return media.get(p.media)?.name ?? 'Missing media';
  }
  // A type led by a choice (a figure) is best described by that choice.
  const first = type?.params[0];
  if (first?.kind === 'select' && first.options) {
    const v = first.key in p ? p[first.key] : first.default;
    return first.options.find((o) => o.value === v)?.label ?? String(v ?? '');
  }
  return '';
}

/** What can be dragged from the palette. */
export type PaletteItem = { kind: 'type'; type: ClipType } | { kind: 'media'; media: MediaItem; type: ClipType };

/** Where a picture goes in a box, as the server's media clips place it (render/media/common.py geometry). */
export interface Placement {
  /** Part of the picture used. */
  sx: number; sy: number; sw: number; sh: number;
  /** Where it goes in the box. */
  dx: number; dy: number; dw: number; dh: number;
}
export function fitPicture(srcW: number, srcH: number, boxW: number, boxH: number, fit: string): Placement {
  const all = { sx: 0, sy: 0, sw: srcW, sh: srcH };
  if (fit === 'stretch' || srcW <= 0 || srcH <= 0) return { ...all, dx: 0, dy: 0, dw: boxW, dh: boxH };
  if (fit === 'contain') {
    const s = Math.min(boxW / srcW, boxH / srcH);
    const w = Math.min(boxW, Math.max(1, Math.round(srcW * s)));
    const h = Math.min(boxH, Math.max(1, Math.round(srcH * s)));
    return { ...all, dx: Math.floor((boxW - w) / 2), dy: Math.floor((boxH - h) / 2), dw: w, dh: h };
  }
  // cover: scaled to at least the box, the middle cut out
  const s = Math.max(boxW / srcW, boxH / srcH);
  const w = Math.max(boxW, Math.round(srcW * s));
  const h = Math.max(boxH, Math.round(srcH * s));
  const x = Math.floor((w - boxW) / 2);
  const y = Math.floor((h - boxH) / 2);
  return { sx: (x * srcW) / w, sy: (y * srcH) / h, sw: (boxW * srcW) / w, sh: (boxH * srcH) / h, dx: 0, dy: 0, dw: boxW, dh: boxH };
}

/** Height / width above which a free crop counts as made for the whole sign: nearer 1:3 than 1:1. */
const SIGN_RATIO = Math.sqrt(3);

/**
 * Does a palette item become a whole-sign clip (one block over the three lights) rather than a
 * clip on one light? Types made only for the whole sign always do; a video or picture does when
 * its crop was drawn for the whole sign, or (free crop) when it is tall.
 */
export function itemWhole(item: PaletteItem): boolean {
  const tg = item.type.targets;
  if (!tg.includes('all')) return false;
  if (!tg.includes('face')) return true;
  if (item.kind !== 'media') return false;
  const shape = item.media.edit.shape;
  if (shape !== 'free') return shape === 'sign';
  return item.media.width > 0 && item.media.height / item.media.width > SIGN_RATIO;
}

export function itemDuration(item: PaletteItem): number {
  if (item.kind === 'media' && item.media.duration > 0 && item.media.kind !== 'image') return Math.round(item.media.duration * 1000) / 1000;
  return item.type.default_duration;
}

export function itemLabel(item: PaletteItem): string {
  return item.kind === 'media' ? item.media.name : item.type.label;
}

/** A fresh clip for a palette item: the type's defaults filled in, media chosen. */
export function makeClip(item: PaletteItem, start: number, duration: number): Clip {
  const params: Record<string, unknown> = {};
  for (const p of item.type.params) params[p.key] = p.default;
  if (item.kind === 'media') params.media = item.media.id;
  return { id: newId(), type: item.type.type, start, duration, params, mask: null, opacity: 1, fade_in: 0, fade_out: 0 };
}
