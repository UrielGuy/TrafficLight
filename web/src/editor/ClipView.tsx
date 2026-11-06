import { memo, useEffect, useMemo, type CSSProperties } from 'react';
import { api, mediaThumbUrl } from '../api/client';
import type { Clip, ClipType, MediaItem, Peaks, Target } from '../api/types';
import { createStore, useStore } from '../state/store';
import { categoryHue, clipDetails, clipSummary, colorValue, seeThrough, TRANSPARENT } from './clipinfo';
import { mediaSpeed } from './model';

// ---- waveform peaks, fetched once per media item
const peaks = new Map<string, Peaks | null>();
const loading = new Set<string>();
const peaksRev = createStore(0);

function usePeaks(id: string | null): Peaks | null {
  useStore(peaksRev);
  useEffect(() => {
    if (!id || peaks.has(id) || loading.has(id)) return;
    loading.add(id);
    api.mediaPeaks(id)
      .then((p) => peaks.set(id, p))
      .catch(() => peaks.set(id, null))
      .finally(() => {
        loading.delete(id);
        peaksRev.set((n) => n + 1);
      });
  }, [id]);
  return id ? peaks.get(id) ?? null : null;
}

/** Mirrored waveform for the part of the media the clip plays (offset, loop). */
function Waveform({ mediaId, clip, mediaDuration, loop }: { mediaId: string; clip: Clip; mediaDuration: number; loop: boolean }) {
  const p = usePeaks(mediaId);
  const offset = Number(clip.params.offset ?? 0) || 0;
  const d = useMemo(() => {
    if (!p || !p.peaks.length) return null;
    const n = Math.max(2, Math.min(480, Math.ceil(clip.duration * p.rate)));
    const len = p.peaks.length;
    const total = mediaDuration > 0 ? mediaDuration : len / p.rate;
    const vals: number[] = [];
    for (let i = 0; i < n; i++) {
      const a = offset + (i / n) * clip.duration;
      const b = offset + ((i + 1) / n) * clip.duration;
      if (!loop && a >= total) {
        vals.push(0);
        continue;
      }
      let m = 0;
      const i0 = Math.floor(a * p.rate);
      const i1 = Math.max(i0 + 1, Math.ceil(b * p.rate));
      for (let k = i0; k < i1; k++) m = Math.max(m, p.peaks[loop ? k % len : k] ?? 0);
      vals.push(m);
    }
    let top = '';
    let bottom = '';
    vals.forEach((v, i) => {
      const y = 50 - v * 46;
      top += `${i === 0 ? 'M' : 'L'}${i},${y.toFixed(1)}`;
      bottom = `L${i},${(100 - y).toFixed(1)}` + bottom;
    });
    return { path: `${top}L${n - 1},50${bottom}Z`, n };
  }, [p, offset, clip.duration, mediaDuration, loop]);
  if (!d) return null;
  return (
    <svg className="clip-wave" viewBox={`0 0 ${d.n - 1} 100`} preserveAspectRatio="none" aria-hidden>
      <path d={d.path} />
    </svg>
  );
}

/** A small checkerboard: the usual picture of "nothing here, see through". */
export const CHECKER = 'repeating-conic-gradient(#9aa0aa 0 25%, #3a3e46 0 50%) 0 0 / 6px 6px';

function swatch(clip: Clip, type: ClipType | undefined, trackColor: string): string | null {
  const spec = type?.params.find((p) => p.kind === 'color');
  if (!spec) return null;
  const v = String(colorValue(clip, type, spec) ?? '');
  if (v === 'face') return trackColor;
  if (v === TRANSPARENT) return CHECKER;
  if (v === 'random') return 'conic-gradient(#f44, #fd0, #4f6, #4cf, #a4f, #f44)';
  return /^#[0-9a-f]{6}$/i.test(v) ? v : null;
}

export interface ClipViewProps {
  clip: Clip;
  pps: number;
  selected: boolean;
  problems?: string[];
  type?: ClipType;
  media?: MediaItem;
  trackColor: string;
  /** Rendered in the drag layer (a preview of where the clip goes). */
  ghost?: boolean;
  invalid?: boolean;
  /** A whole-sign clip: one tall block over the light rows. */
  whole?: boolean;
  /** Drawn faded: its (hidden) track is muted. */
  dim?: boolean;
  /** Where it plays (for the tooltip). */
  target?: Target;
  /** A light's clip lying over a whole-sign clip: drawn on top of the block, set off from it. */
  over?: boolean;
  top?: number;
  height?: number;
  offsetX?: number;
}

export const ClipView = memo(function ClipView({
  clip, pps, selected, problems, type, media, trackColor, ghost, invalid, whole, dim, target, over, top, height, offsetX = 0,
}: ClipViewProps) {
  const w = Math.max(3, clip.duration * pps);
  const hue = categoryHue(type?.category ?? 'unknown');
  const mediaId = typeof clip.params.media === 'string' && clip.params.media ? clip.params.media : null;
  const showThumb = !!media && media.version > 0 && (media.kind === 'video' || media.kind === 'image');
  // a video's own sound plays only at speed 1 (as the server does it)
  const sounding = !!media && media.status === 'ready' && media.has_audio
    && (media.kind === 'audio' || (clip.params.sound !== false && Math.abs(mediaSpeed(clip) - 1) < 1e-3));
  const lib = media ? new Map([[media.id, media]]) : new Map<string, MediaItem>();
  const summary = clipSummary(clip, type, lib);
  const sw = swatch(clip, type, trackColor);
  const narrow = w < 26;
  const style: CSSProperties & Record<string, string | number> = {
    left: offsetX + clip.start * pps,
    width: w,
    '--hue': hue,
  };
  if (top !== undefined) style.top = top;
  if (height !== undefined) style.height = height;
  const cls = ['clip', selected && 'sel', problems?.length && 'bad', ghost && 'ghost', invalid && 'invalid', !type && 'unknown', whole && 'whole', dim && 'dim', over && 'over', seeThrough(clip, type) && 'see']
    .filter(Boolean).join(' ');
  // a native tooltip: it never takes the pointer from a drag; screen readers get the name and the details
  const details = ghost ? null : clipDetails(clip, type, lib, target ?? (whole ? 'all' : 'face0'), over);
  const title = details && [...details, ...(problems ?? []), ...(dim ? ['Muted in this set: any edit plays it again'] : [])].join('\n');
  return (
    <div className={cls} data-clip={ghost ? undefined : clip.id} data-type={clip.type} data-whole={whole ? '' : undefined} style={style}
      title={title ?? undefined} aria-label={details?.[0]}>
      {showThumb && <div className="clip-thumb" style={{ backgroundImage: `url(${mediaThumbUrl(media!.id, media!.version)})` }} />}
      {sounding && mediaId && <Waveform mediaId={mediaId} clip={clip} mediaDuration={media!.duration} loop={clip.params.loop === true} />}
      {clip.fade_in > 0 && <div className="clip-fade in" style={{ width: Math.min(w, clip.fade_in * pps) }} />}
      {clip.fade_out > 0 && <div className="clip-fade out" style={{ width: Math.min(w, clip.fade_out * pps) }} />}
      {!narrow && (
        <div className="clip-label">
          <div className="clip-line1">
            {whole && <span className="clip-whole"><i /><i /><i /></span>}
            {sw && <i className="clip-swatch" style={{ background: sw }} />}
            <span className="clip-type">{type?.label ?? clip.type}</span>
          </div>
          {summary && <div className={`clip-sum ${typeof clip.params.text === 'string' ? 'text' : ''}`}>{summary}</div>}
        </div>
      )}
      {!!problems?.length && <span className="clip-warn" aria-label={problems.join('; ')}>!</span>}
      {!ghost && !narrow && <div className="clip-edge start" data-edge="start" />}
      {!ghost && <div className="clip-edge end" data-edge="end" />}
    </div>
  );
});
