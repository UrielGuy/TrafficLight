import { useCallback, useEffect, useRef, useState } from 'react';

/** Local value that follows `external` until the user edits; `commit` is called debounced. */
export function useDebouncedValue<T>(external: T, commit: (v: T) => void, delay = 300): [T, (v: T) => void] {
  const [local, setLocal] = useState(external);
  const editing = useRef(false);
  const timer = useRef<number | undefined>(undefined);
  const commitRef = useRef(commit);
  commitRef.current = commit;

  useEffect(() => {
    if (!editing.current) setLocal(external);
  }, [external]);

  const set = useCallback(
    (v: T) => {
      editing.current = true;
      setLocal(v);
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => {
        commitRef.current(v);
        // keep following the server only after it had a moment to echo the change
        window.setTimeout(() => (editing.current = false), 800);
      }, delay);
    },
    [delay],
  );
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return [local, set];
}

export function formatTime(s: number | null | undefined): string {
  if (s == null || !isFinite(s)) return '–';
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}
