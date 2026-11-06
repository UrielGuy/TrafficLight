import { describe, expect, it } from 'vitest';
import type { Clip, ClipType, MediaItem, MediaShape, ParamSpec } from '../api/types';
import { clipDetails, colorValue, fitPicture, itemLabel, itemWhole, makeClip, seeThrough, typeItems, withColor, type PaletteItem } from './clipinfo';

const type = (targets: string[]): ClipType => ({ type: 'video', label: 'Video', category: 'media', targets, default_duration: 10, description: '', params: [] });
const media = (shape: MediaShape, width: number, height: number) =>
  ({ id: 'm', name: 'm', kind: 'video', width, height, edit: { rotate: 0, crop: null, start: 0, end: null, shape } }) as unknown as MediaItem;
const item = (shape: MediaShape, w: number, h: number): PaletteItem => ({ kind: 'media', media: media(shape, w, h), type: type(['face', 'all']) });

describe('itemWhole', () => {
  it('follows the crop shape', () => {
    expect(itemWhole(item('sign', 1000, 1000))).toBe(true);
    expect(itemWhole(item('light', 300, 900))).toBe(false);
  });
  it('a free crop is whole-sign when nearer 1:3 than 1:1', () => {
    expect(itemWhole(item('free', 100, 180))).toBe(true);
    expect(itemWhole(item('free', 100, 170))).toBe(false);
    expect(itemWhole(item('free', 1920, 1080))).toBe(false);
  });
  it('types: only "all" is whole-sign, "face" or both is a light clip', () => {
    expect(itemWhole({ kind: 'type', type: type(['all']) })).toBe(true);
    expect(itemWhole({ kind: 'type', type: type(['face']) })).toBe(false);
    expect(itemWhole({ kind: 'type', type: type(['face', 'all']) })).toBe(false);
    expect(itemWhole({ kind: 'media', media: media('sign', 1, 3), type: type(['audio']) })).toBe(false);
  });
});

describe('typeItems', () => {
  const figure: ClipType = {
    type: 'figure', label: 'Figure', category: 'figure', targets: ['face'], default_duration: 8, description: '',
    params: [
      { key: 'figure', label: 'Figure', kind: 'select', default: 'standing', options: [{ value: 'standing', label: 'Person standing' }, { value: 'walking', label: 'Walking' }] },
      { key: 'color', label: 'Colour', kind: 'color', default: 'face' },
    ],
  } as unknown as ClipType;
  it('gives a figure type one item per figure, each making a clip of that figure', () => {
    const items = typeItems(figure);
    expect(items.map(itemLabel)).toEqual(['Person standing', 'Walking']);
    const clip = makeClip(items[1], 2, 8);
    expect(clip.type).toBe('figure');
    expect(clip.params).toEqual({ figure: 'walking', color: 'face' });
  });
  it('leaves other types as one item', () => {
    expect(typeItems({ ...figure, category: 'beat' })).toEqual([{ kind: 'type', type: { ...figure, category: 'beat' } }]);
    expect(typeItems(type(['face']))).toHaveLength(1);
  });
});

describe('fitPicture (as the server places pictures)', () => {
  it('cover cuts the middle out', () => {
    // scaled to 228 x 128 (rounded, as the server does), then 50 px cut off on the left
    const p = fitPicture(256, 144, 128, 128, 'cover');
    expect(p.sx).toBeCloseTo((50 * 256) / 228);
    expect(p.sw).toBeCloseTo((128 * 256) / 228);
    expect([p.sy, p.sh, p.dx, p.dy, p.dw, p.dh]).toEqual([0, 144, 0, 0, 128, 128]);
  });
  it('contain leaves bars', () => {
    expect(fitPicture(256, 128, 128, 128, 'contain')).toEqual({ sx: 0, sy: 0, sw: 256, sh: 128, dx: 0, dy: 32, dw: 128, dh: 64 });
  });
  it('stretch fills the box', () => {
    expect(fitPicture(256, 128, 128, 384, 'stretch')).toEqual({ sx: 0, sy: 0, sw: 256, sh: 128, dx: 0, dy: 0, dw: 128, dh: 384 });
  });
});

describe('clipDetails (the clip tooltip)', () => {
  const p = (key: string, label: string, kind: ParamSpec['kind'], dflt: unknown, extra: Partial<ParamSpec> = {}): ParamSpec => ({ key, label, kind, default: dflt, ...extra });
  const videoType: ClipType = {
    type: 'video', label: 'Video', category: 'media', targets: ['face', 'all'], default_duration: 10, description: '',
    params: [p('media', 'Video', 'media', ''), p('offset', 'Start at', 'number', 0), p('loop', 'Loop', 'bool', true),
      p('speed', 'Speed', 'number', 1), p('sound', 'Sound', 'bool', true), p('volume', 'Volume', 'number', 1)],
  };
  const figureType: ClipType = {
    type: 'figure', label: 'Figure', category: 'figure', targets: ['face'], default_duration: 4, description: '',
    params: [p('figure', 'Figure', 'select', 'walk', { options: [{ value: 'walk', label: 'Walking' }, { value: 'run', label: 'Running' }] }),
      p('color', 'Colour', 'color', 'face')],
  };
  const lib = new Map([['m1', { id: 'm1', name: 'Beach' } as MediaItem]]);
  const clip = (type: string, params: Record<string, unknown>, more: Partial<Clip> = {}): Clip => ({
    id: 'c', type, start: 2, duration: 4, params, mask: null, opacity: 1, fade_in: 0, fade_out: 0, ...more,
  });

  it('a video at speed 2: what, where, when, and the settings that differ', () => {
    const d = clipDetails(clip('video', { media: 'm1', offset: 1.5, loop: false, speed: 2, sound: true, volume: 1 }, { fade_in: 0.5 }), videoType, lib, 'all');
    expect(d).toEqual(['Video: Beach', 'Whole sign · 2–6 s (4 s)', 'Start at: 1.5', 'Loop: off', 'Speed: 2', 'Fade in: 0.5 s']);
  });
  it('a figure: its choice is the summary, a picked colour is listed', () => {
    expect(clipDetails(clip('figure', { figure: 'run', color: 'face' }), figureType, lib, 'face1')).toEqual(['Figure: Running', 'Middle light · 2–6 s (4 s)']);
    expect(clipDetails(clip('figure', { figure: 'run', color: '#00ff00' }, { opacity: 0.5, mask: false }), figureType, lib, 'face0', true)).toEqual([
      'Figure: Running', 'Top light · 2–6 s (4 s)', 'Shown instead of the whole-sign clip in this light', 'Colour: #00ff00', 'Opacity: 50%', 'Mask: none (fills the whole box)',
    ]);
  });
  it('a missing media item, sound off, an unknown type', () => {
    expect(clipDetails(clip('video', { media: 'gone', sound: false }), videoType, lib, 'face2')).toEqual(['Video: Missing media', 'Bottom light · 2–6 s (4 s)', 'Sound: off']);
    expect(clipDetails(clip('zap', {}), undefined, lib, 'audio')[0]).toBe('Unknown clip type “zap”');
  });
});

describe('transparent colours', () => {
  const bg: ParamSpec = { key: 'bg', label: 'Background', kind: 'color', default: 'face', transparent: true };
  const ink: ParamSpec = { key: 'ink', label: 'Ink', kind: 'color', default: '#000000' };
  const text: ClipType = { type: 'text', label: 'Text', category: 'basic', targets: ['face'], default_duration: 4, description: '', params: [bg, ink] };
  const clip = (params: Record<string, unknown>): Clip => ({ id: 'c', type: 'text', start: 0, duration: 4, params, mask: null, opacity: 1, fade_in: 0, fade_out: 0 });

  it('the old see-through switch shows as a transparent background', () => {
    const old = clip({ bg: 'face', transparent: true });
    expect(colorValue(old, text, bg)).toBe('transparent');
    expect(colorValue(old, text, ink)).toBe('#000000');
    expect(seeThrough(old, text)).toBe(true);
    expect(colorValue(clip({ bg: '#ff0000' }), text, bg)).toBe('#ff0000');
    // a type that still lists the switch keeps its own meaning for it
    expect(colorValue(old, { ...text, params: [bg, { key: 'transparent', label: 'No background', kind: 'bool', default: false }] }, bg)).toBe('face');
  });
  it('choosing a background drops the old switch; another colour keeps it', () => {
    const old = clip({ bg: 'face', transparent: true });
    expect(withColor(old, text, 'bg', '#00ff00')).toEqual({ bg: '#00ff00' });
    expect(withColor(old, text, 'ink', '#00ff00')).toEqual({ bg: 'face', transparent: true, ink: '#00ff00' });
    expect(withColor(clip({ bg: 'face' }), text, 'bg', 'transparent')).toEqual({ bg: 'transparent' });
  });
  it('reads as "transparent" in the tooltip', () => {
    expect(clipDetails(clip({ bg: 'transparent' }), text, new Map(), 'face1')).toContain('Background: transparent');
    expect(clipDetails(clip({ bg: 'face', transparent: true }), text, new Map(), 'face1')).toContain('Background: transparent');
  });
});
