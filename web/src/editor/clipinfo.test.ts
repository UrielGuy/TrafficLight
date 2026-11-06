import { describe, expect, it } from 'vitest';
import type { ClipType, MediaItem, MediaShape } from '../api/types';
import { fitPicture, itemLabel, itemWhole, makeClip, typeItems, type PaletteItem } from './clipinfo';

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
