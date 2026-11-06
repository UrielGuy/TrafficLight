// The library: clip types from /api/catalog by category and the media library,
// searchable. Drag an item onto a track, or click it to add it at the insertion
// point (the touch-friendly way).
import { useMemo, useState } from 'react';
import { mediaThumbUrl } from '../api/client';
import type { ClipType, MediaItem } from '../api/types';
import { useCatalog, useMedia } from '../state/app';
import { formatTime } from '../state/hooks';
import { createStore, useStore } from '../state/store';
import { addItem } from './actions';
import { CATEGORY_LABELS, CATEGORY_ORDER, categoryHue, itemLabel, itemWhole, MEDIA_CLIP_TYPE, typeItems, type PaletteItem } from './clipinfo';
import { dragItemStore, dropTarget } from './dnd';

const ghostStore = createStore<{ item: PaletteItem; x: number; y: number; over: boolean } | null>(null);
const DRAG_PX = 6;

function whereLabel(t: ClipType): string {
  const w = [];
  if (t.targets.includes('face')) w.push('light');
  if (t.targets.includes('all')) w.push('whole sign');
  if (t.targets.includes('audio')) w.push('audio');
  return w.join(' · ');
}

/** Where the dragged item can be dropped, for the hint next to the pointer. */
function dropHint(item: PaletteItem): string {
  if (itemWhole(item)) return 'drop on the lights (it covers all three)';
  return item.type.targets.includes('audio') ? 'drop on an audio track' : 'drop on a light';
}

/** Pointer handling shared by all items: a short move starts a drag, otherwise it is a click. */
function onItemPointerDown(e: React.PointerEvent, item: PaletteItem) {
  if (e.pointerType === 'mouse' && e.button !== 0) return;
  const x0 = e.clientX;
  const y0 = e.clientY;
  let dragging = false;
  let done = false;
  const move = (ev: PointerEvent) => {
    if (!dragging) {
      if (Math.hypot(ev.clientX - x0, ev.clientY - y0) < DRAG_PX) return;
      dragging = true;
      dragItemStore.set(item);
    }
    const over = dropTarget.current?.hover(item, ev.clientX, ev.clientY) ?? false;
    ghostStore.set({ item, x: ev.clientX, y: ev.clientY, over });
  };
  const finish = (ev: PointerEvent, cancelled: boolean) => {
    if (done) return;
    done = true;
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', up);
    window.removeEventListener('pointercancel', cancel);
    if (dragging) {
      if (cancelled) dropTarget.current?.leave();
      else dropTarget.current?.drop(item, ev.clientX, ev.clientY);
      dragItemStore.set(null);
      ghostStore.set(null);
      // swallow the click that follows the drag
      const stop = (c: Event) => {
        c.stopPropagation();
        c.preventDefault();
      };
      window.addEventListener('click', stop, { capture: true, once: true });
      window.setTimeout(() => window.removeEventListener('click', stop, { capture: true }), 0);
    }
  };
  const up = (ev: PointerEvent) => finish(ev, false);
  const cancel = (ev: PointerEvent) => finish(ev, true);
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
  window.addEventListener('pointercancel', cancel);
}

function DragGhost() {
  const g = useStore(ghostStore);
  if (!g) return null;
  return (
    <div className={`pal-ghost ${g.over ? 'over' : ''}`} style={{ left: g.x + 12, top: g.y + 12, ['--hue' as string]: categoryHue(g.item.type.category) }}>
      {itemLabel(g.item)}
      {!g.over && <span className="muted"> — {dropHint(g.item)}</span>}
    </div>
  );
}

export default function Palette() {
  const catalog = useCatalog();
  const media = useMedia();
  const [q, setQ] = useState('');
  const query = q.trim().toLowerCase();

  const groups = useMemo(() => {
    // A search finds a type by its own words (then all its items show) or single items by their name.
    const hit = (words: string[]) => !query || words.some((s) => s.toLowerCase().includes(query));
    const items = (catalog ?? []).flatMap((t) => {
      const all = typeItems(t);
      return hit([t.label, t.type, t.category, t.description]) ? all : all.filter((i) => hit([itemLabel(i)]));
    });
    const cats = [...new Set([...CATEGORY_ORDER, ...items.map((i) => i.type.category)])];
    return cats.map((c) => ({ cat: c, items: items.filter((i) => i.type.category === c) })).filter((g) => g.items.length);
  }, [catalog, query]);

  const byType = useMemo(() => new Map((catalog ?? []).map((t) => [t.type, t])), [catalog]);
  const mediaItems = useMemo(
    () => (media ?? []).filter((m) => !query || m.name.toLowerCase().includes(query) || m.kind.includes(query)),
    [media, query],
  );

  return (
    <div className="palette">
      <input type="search" className="pal-search" placeholder="Search clips and media" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search the library" />
      <div className="pal-scroll">
        <p className="pal-hint muted small">Drag onto a track, or click a track then an item. Whole-sign items cover all three lights.</p>
        {groups.map((g) => (
          <section key={g.cat} className="pal-group">
            <h3>{CATEGORY_LABELS[g.cat] ?? g.cat}</h3>
            <div className="pal-chips">
              {g.items.map((item) => {
                const t = item.type;
                const preset = item.kind === 'type' ? item.preset : undefined;
                return (
                  <button key={preset ? `${t.type}:${String(preset.value)}` : t.type} className="pal-chip" title={preset ? `${t.label}: ${preset.label}` : t.description || t.label}
                    style={{ ['--hue' as string]: categoryHue(t.category) }}
                    onPointerDown={(e) => onItemPointerDown(e, item)} onClick={() => addItem(item)} data-type={t.type} data-preset={preset ? String(preset.value) : undefined}>
                    <span className="pal-chip-label">{itemLabel(item)}</span>
                    <span className="pal-where">{whereLabel(t)}</span>
                  </button>
                );
              })}
            </div>
          </section>
        ))}
        <section className="pal-group">
          <h3>Media library</h3>
          {media && !media.length && <p className="muted small">No media yet: upload videos, pictures and sounds on the Media page.</p>}
          {media && media.length > 0 && !mediaItems.length && <p className="muted small">Nothing matches.</p>}
          <div className="pal-media">
            {mediaItems.map((m) => <MediaRow key={m.id} m={m} type={byType.get(MEDIA_CLIP_TYPE[m.kind])} />)}
          </div>
        </section>
        {!groups.length && query && !mediaItems.length && <p className="muted small">Nothing matches “{q}”.</p>}
      </div>
      <DragGhost />
    </div>
  );
}

function MediaRow({ m, type }: { m: MediaItem; type: ClipType | undefined }) {
  const ready = m.status === 'ready' && !!type;
  const item: PaletteItem | null = ready ? { kind: 'media', media: m, type: type! } : null;
  return (
    <button className="pal-media-item" disabled={!ready} data-media={m.id}
      title={m.status === 'error' ? `Could not be processed: ${m.error ?? ''}` : m.status === 'processing' ? 'Still processing' : m.name}
      onPointerDown={(e) => item && onItemPointerDown(e, item)} onClick={() => item && addItem(item)}
      style={{ ['--hue' as string]: categoryHue('media') }}>
      <span className="pal-thumb">
        {m.status === 'ready' ? <img src={mediaThumbUrl(m.id, m.version)} alt="" loading="lazy" draggable={false} /> : m.status === 'processing' ? <span className="spinner small" /> : <span className="err">!</span>}
      </span>
      <span className="pal-media-text">
        <span className="ellipsis">{m.name}</span>
        <span className="muted small">{m.kind}{m.duration > 0 ? ` · ${formatTime(m.duration)}` : ''}{item && itemWhole(item) ? ' · whole sign' : ''}{m.status === 'processing' ? ' · processing' : m.status === 'error' ? ' · error' : ''}</span>
      </span>
    </button>
  );
}
