// A few inline icons (no icon library in the project).
const P = {
  play: 'M7 4.5v15l12-7.5z',
  pause: 'M6.5 4.5h4v15h-4zM13.5 4.5h4v15h-4z',
  start: 'M5 4.5h2.5v15H5zM19 4.5v15L8.5 12z',
  loop: 'M7 7h10l-2-2.2 1.4-1.3L20.6 8l-4.2 4.5-1.4-1.3L17 9H7a3 3 0 0 0-3 3H2a5 5 0 0 1 5-5zm10 10H7l2 2.2-1.4 1.3L3.4 16l4.2-4.5 1.4 1.3L7 15h10a3 3 0 0 0 3-3h2a5 5 0 0 1-5 5z',
  magnet: 'M5 3h4v8a3 3 0 0 0 6 0V3h4v8a7 7 0 0 1-14 0zm0 2v2h4V5zm10 0v2h4V5z',
  split: 'M11 2h2v20h-2zM4 7h5v10H4zM15 7h5v10h-5z',
  trash: 'M9 3h6l1 2h4v2H4V5h4zM6 9h12l-1 12H7z',
  undo: 'M8 6V2L2 8l6 6v-4h6a4 4 0 0 1 0 8h-3v2h3a6 6 0 0 0 0-12z',
  redo: 'M16 6V2l6 6-6 6v-4h-6a4 4 0 0 0 0 8h3v2h-3a6 6 0 0 1 0-12z',
  zoomIn: 'M10 3a7 7 0 0 1 5.6 11.2l5.1 5.1-1.4 1.4-5.1-5.1A7 7 0 1 1 10 3zm0 2a5 5 0 1 0 0 10 5 5 0 0 0 0-10zM9 7h2v2h2v2h-2v2H9v-2H7V9h2z',
  zoomOut: 'M10 3a7 7 0 0 1 5.6 11.2l5.1 5.1-1.4 1.4-5.1-5.1A7 7 0 1 1 10 3zm0 2a5 5 0 1 0 0 10 5 5 0 0 0 0-10zM7 9h6v2H7z',
  fit: 'M3 5h2v14H3zM19 5h2v14h-2zM7 11h10v2H7zM9 8l-3 4 3 4zM15 8l3 4-3 4z',
  copy: 'M8 3h11v13h-2V5H8zM4 7h11v14H4z',
  back: 'M10 5l-7 7 7 7 1.4-1.4L6.8 13H21v-2H6.8l4.6-4.6z',
  chevronDown: 'M6 9l6 6 6-6-1.4-1.4L12 12.2 7.4 7.6z',
  chevronUp: 'M6 15l6-6 6 6-1.4 1.4L12 11.8l-4.6 4.6z',
  warn: 'M12 2L1 21h22zm-1 7h2v6h-2zm0 8h2v2h-2z',
} as const;

export type IconName = keyof typeof P;

export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden fill="currentColor" className="icon">
      <path d={P[name]} fillRule="evenodd" />
    </svg>
  );
}
