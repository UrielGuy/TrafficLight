// What the editor knows about clip types and media: colours, labels, compatibility,
// and how a palette item becomes a clip.
import type { Clip, ClipType, MediaItem, MediaKind, ParamSpec, Settings, Target } from '../api/types';
import { accepts, clipEnd, newId, TARGET_NAMES } from './model';

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
const CATEGORY_HUES: Record<string, number> = { basic: 215, figure: 165, beat: 275, media: 135, clock: 30 };
export function categoryHue(category: string): number {
  if (category in CATEGORY_HUES) return CATEGORY_HUES[category];
  let h = 0;
  for (const ch of category) h = (h * 31 + ch.charCodeAt(0)) % 360;
  return h;
}
export const CATEGORY_LABELS: Record<string, string> = {
  basic: 'Basic', figure: 'Figures', beat: 'Beat animations', media: 'Media clips',
};
export const CATEGORY_ORDER = ['basic', 'figure', 'beat', 'media'];

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

export const TRANSPARENT = 'transparent';

/**
 * Sets made before "transparent" was a colour say so with a separate `transparent: true` param;
 * the server still honours it for `bg`. It counts while the clip's type no longer lists it.
 */
function legacySeeThrough(clip: Clip, type: ClipType | undefined): boolean {
  return clip.params.transparent === true && !!type && !type.params.some((p) => p.key === 'transparent');
}

/** The value a colour param really has: "transparent" for a background made see-through the old way. */
export function colorValue(clip: Clip, type: ClipType | undefined, spec: ParamSpec): unknown {
  if (spec.key === 'bg' && spec.transparent && legacySeeThrough(clip, type)) return TRANSPARENT;
  return spec.key in clip.params ? clip.params[spec.key] : spec.default;
}

/** The clip's params with a colour param set; choosing a background drops the old see-through switch. */
export function withColor(clip: Clip, type: ClipType | undefined, key: string, value: string): Record<string, unknown> {
  const params: Record<string, unknown> = { ...clip.params, [key]: value };
  if (key === 'bg' && legacySeeThrough(clip, type)) delete params.transparent;
  return params;
}

/** Does any colour of the clip let what is below show through (its background, most often)? */
export function seeThrough(clip: Clip, type: ClipType | undefined): boolean {
  return !!type?.params.some((p) => p.kind === 'color' && p.transparent && colorValue(clip, type, p) === TRANSPARENT);
}

const num = (v: number) => String(Math.round(v * 1000) / 1000);
const short = (t: string, n = 40) => (t.length > n ? `${t.slice(0, n - 1)}…` : t);

/** A param's value as words: an option's label, a media item's name, on / off, a colour. */
function paramText(spec: ParamSpec, v: unknown, media: Map<string, MediaItem>): string {
  switch (spec.kind) {
    case 'bool':
      return v ? 'on' : 'off';
    case 'color':
      return v === 'face' ? "the light's colour" : v === 'random' ? 'random' : v === TRANSPARENT ? 'transparent' : String(v);
    case 'media':
      // the same words as clipSummary, so the summary line is not repeated
      return typeof v === 'string' && v ? media.get(v)?.name ?? 'Missing media' : 'No media chosen';
    case 'select':
    case 'font':
      return spec.options?.find((o) => o.value === v)?.label ?? String(v);
    case 'number':
    case 'int':
      return num(Number(v) || 0);
    default:
      return String(v ?? '').replace(/\s+/g, ' ').trim();
  }
}

/**
 * Everything worth knowing about a clip that its block may not have room for, one line each: what
 * it is and shows, where and when it plays, and every setting that differs from the type's default
 * (built from the type's ParamSpecs) plus opacity, fades and mask. `over`: it is a light's clip
 * lying over a whole-sign clip.
 */
export function clipDetails(clip: Clip, type: ClipType | undefined, media: Map<string, MediaItem>, target: Target, over = false): string[] {
  const summary = clipSummary(clip, type, media);
  const out = [type ? (summary ? `${type.label}: ${summary}` : type.label) : `Unknown clip type “${clip.type}”`];
  const where = target === 'audio' ? 'Audio track' : TARGET_NAMES[target];
  out.push(`${where} · ${num(clip.start)}–${num(clipEnd(clip))} s (${num(clip.duration)} s)`);
  if (over) out.push('Shown instead of the whole-sign clip in this light');
  for (const spec of type?.params ?? []) {
    const v = spec.kind === 'color' ? colorValue(clip, type, spec) : spec.key in clip.params ? clip.params[spec.key] : spec.default;
    if (v === spec.default || (typeof v === 'number' && Number(spec.default) === v)) continue;
    const text = paramText(spec, v, media);
    // already said on the first line (the text, the media's name, the figure)
    if (text === summary) continue;
    out.push(`${spec.label}: ${short(text)}`);
  }
  if (clip.opacity !== 1) out.push(`${target === 'audio' ? 'Volume' : 'Opacity'}: ${Math.round(clip.opacity * 100)}%`);
  if (clip.fade_in > 0) out.push(`Fade in: ${num(clip.fade_in)} s`);
  if (clip.fade_out > 0) out.push(`Fade out: ${num(clip.fade_out)} s`);
  if (clip.mask === false && target !== 'audio' && type?.masked !== false) out.push('Mask: none (fills the whole box)');
  return out;
}

/** One choice of a clip type offered as its own palette item: the figure "Walking" of the type "Figure". */
export interface Preset {
  key: string;
  value: unknown;
  label: string;
}

/** What can be dragged from the palette. */
export type PaletteItem = { kind: 'type'; type: ClipType; preset?: Preset } | { kind: 'media'; media: MediaItem; type: ClipType };

/** Categories whose types are a list of things to pick from: each choice gets its own palette item. */
const PRESET_CATEGORIES = new Set(['figure']);

/**
 * The palette items of a clip type: one, or one per option of its leading choice for the
 * categories above (so a figure is dragged in directly instead of chosen in the inspector).
 */
export function typeItems(type: ClipType): PaletteItem[] {
  const first = type.params[0];
  if (!PRESET_CATEGORIES.has(type.category) || first?.kind !== 'select' || !first.options?.length) return [{ kind: 'type', type }];
  return first.options.map((o) => ({ kind: 'type', type, preset: { key: first.key, value: o.value, label: o.label } }));
}

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
  return item.kind === 'media' ? item.media.name : item.preset?.label ?? item.type.label;
}

/** A fresh clip for a palette item: the type's defaults filled in, media chosen. */
export function makeClip(item: PaletteItem, start: number, duration: number): Clip {
  const params: Record<string, unknown> = {};
  for (const p of item.type.params) params[p.key] = p.default;
  if (item.kind === 'media') params.media = item.media.id;
  else if (item.preset) params[item.preset.key] = item.preset.value;
  return { id: newId(), type: item.type.type, start, duration, params, mask: null, opacity: 1, fade_in: 0, fade_out: 0 };
}
