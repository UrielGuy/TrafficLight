import { useEffect, useRef } from 'react';
import type { FrameFeed } from '../state/app';

export const SIGN_W = 128;
export const SIGN_H = 384;

interface Props {
  /** Subscribe to a stream of frames (e.g. `signFeed`, or one the editor builds from its own socket). */
  feed?: FrameFeed;
  /** Or hand over a single frame (one complete PNG/JPEG file) or an image URL. */
  frame?: ArrayBuffer | Blob | string | null;
  /** Height in CSS px; width follows (1:3). Default fills the container width. */
  height?: number;
  className?: string;
}

/**
 * Draws 128x384 frames on a canvas, nearest-neighbour scaled, laid out like the
 * sign: three round lights in a housing. Frames are decoded newest-wins: if the
 * browser is busy older frames are dropped instead of queued.
 */
export default function SignPreview({ feed, frame, height, className }: Props) {
  const canvas = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    let alive = true;
    let busy = false;
    let pending: ArrayBuffer | Blob | null = null;

    const draw = async (data: ArrayBuffer | Blob) => {
      if (busy) {
        pending = data;
        return;
      }
      busy = true;
      try {
        const bmp = await createImageBitmap(data instanceof Blob ? data : new Blob([data]));
        const c = canvas.current;
        if (alive && c) {
          const ctx = c.getContext('2d');
          if (ctx) {
            ctx.imageSmoothingEnabled = false;
            ctx.drawImage(bmp, 0, 0, SIGN_W, SIGN_H);
          }
        }
        bmp.close();
      } catch {
        /* a broken frame: keep the previous picture */
      }
      busy = false;
      if (pending && alive) {
        const p = pending;
        pending = null;
        void draw(p);
      }
    };

    const unsub = feed?.subscribe((d) => void draw(d));
    if (frame instanceof ArrayBuffer || frame instanceof Blob) void draw(frame);
    else if (typeof frame === 'string') {
      fetch(frame)
        .then((r) => r.blob())
        .then((b) => { if (alive) void draw(b); })
        .catch(() => undefined);
    }
    return () => {
      alive = false;
      unsub?.();
    };
  }, [feed, frame]);

  // The canvas has the native 128x384 resolution; CSS scales it with `pixelated`.
  return (
    <div className={`sign-housing ${className ?? ''}`} style={height ? { height, width: height / 3 + 16 } : undefined}>
      <canvas ref={canvas} width={SIGN_W} height={SIGN_H} className="sign-canvas" />
    </div>
  );
}
