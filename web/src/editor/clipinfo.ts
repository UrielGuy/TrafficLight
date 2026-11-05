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
