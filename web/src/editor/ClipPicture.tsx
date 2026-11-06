// A still of a video or picture clip as the traffic light shows it: the item's thumbnail fitted
// into the clip's box (one light, or the whole sign) like the player fits it, seen through the
// round lights on black (the default mask: only the round lights are seen on the sign), or the
// whole box when the clip's mask is off.
import { useEffect, useRef } from 'react';
import { mediaThumbUrl } from '../api/client';
import type { MediaItem } from '../api/types';
import { applyCircles, FACE, SIGN_H, SIGN_W } from '../media/cropmath';
import { fitPicture } from './clipinfo';

export default function ClipPicture({ item, whole, fit, circles }: { item: MediaItem; whole: boolean; fit: string; circles: boolean }) {
  const cv = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    let alive = true;
    const img = new Image();
    img.onload = () => {
      const c = cv.current;
      if (!alive || !c) return;
      const h = whole ? SIGN_H : FACE;
      c.width = SIGN_W;
      c.height = h;
      const ctx = c.getContext('2d', { willReadFrequently: true });
      if (!ctx) return;
      ctx.fillStyle = '#000';
      ctx.fillRect(0, 0, SIGN_W, h);
      ctx.imageSmoothingQuality = 'high';
      const p = fitPicture(img.naturalWidth, img.naturalHeight, SIGN_W, h, fit);
      ctx.drawImage(img, p.sx, p.sy, p.sw, p.sh, p.dx, p.dy, p.dw, p.dh);
      if (circles) {
        const px = ctx.getImageData(0, 0, SIGN_W, h);
        applyCircles(px.data, h);
        ctx.putImageData(px, 0, 0);
      }
      c.dataset.loaded = '1';
    };
    img.src = mediaThumbUrl(item.id, item.version);
    return () => {
      alive = false;
    };
  }, [item.id, item.version, whole, fit, circles]);
  return (
    <canvas ref={cv} key={whole ? 'whole' : 'light'} width={SIGN_W} height={whole ? SIGN_H : FACE} className={`clip-pic ${whole ? 'whole' : 'light'}`} data-testid="clip-picture"
      role="img" aria-label={whole ? 'How the whole sign shows it' : 'How the light shows it'} />
  );
}
