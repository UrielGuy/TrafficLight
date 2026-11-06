import { useEffect, type ReactNode } from 'react';
import { confirmStore } from '../state/confirm';
import { dismiss, toasts } from '../state/toast';
import { useStore } from '../state/store';
import { useConnected } from '../state/app';

export function Toggle({ checked, onChange, label, disabled }: { checked: boolean; onChange: (v: boolean) => void; label?: string; disabled?: boolean }) {
  return (
    <button type="button" role="switch" aria-checked={checked} aria-label={label} disabled={disabled}
      className={`toggle ${checked ? 'on' : ''}`} onClick={() => onChange(!checked)}>
      <span className="knob" />
    </button>
  );
}

export function Slider({ value, min, max, step, onChange, format, label }: {
  value: number; min: number; max: number; step: number; onChange: (v: number) => void; format?: (v: number) => string; label: string;
}) {
  return (
    <div className="slider-row">
      <input type="range" aria-label={label} min={min} max={max} step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} />
      <span className="slider-value">{format ? format(value) : value}</span>
    </div>
  );
}

export function Segmented<T extends string>({ value, options, onChange }: { value: T; options: { value: T; label: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="segmented" role="group">
      {options.map((o) => (
        <button key={o.value} type="button" className={o.value === value ? 'active' : ''} onClick={() => onChange(o.value)}>{o.label}</button>
      ))}
    </div>
  );
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <div className="field">
      <div className="field-label">{label}</div>
      <div className="field-control">{children}</div>
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}

export function Dialog({ title, onClose, children, wide }: { title: string; onClose: () => void; children: ReactNode; wide?: boolean }) {
  useEffect(() => {
    const k = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', k);
    return () => window.removeEventListener('keydown', k);
  }, [onClose]);
  return (
    <div className="backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className={`dialog ${wide ? 'wide' : ''}`} role="dialog" aria-label={title}>
        <h2>{title}</h2>
        {children}
      </div>
    </div>
  );
}

export function ConfirmHost() {
  const req = useStore(confirmStore);
  if (!req) return null;
  const done = (ok: boolean) => {
    confirmStore.set(null);
    req.resolve(ok);
  };
  return (
    <Dialog title={req.title} onClose={() => done(false)}>
      {req.text && <p className="muted">{req.text}</p>}
      <div className="row end">
        <button onClick={() => done(false)}>Cancel</button>
        <button className={req.danger ? 'danger' : 'primary'} onClick={() => done(true)} autoFocus>{req.action}</button>
      </div>
    </Dialog>
  );
}

export function Toasts() {
  const list = useStore(toasts);
  return (
    <div className="toasts" aria-live="polite">
      {list.map((t) => (
        <div key={t.id} className={`toast ${t.kind}`} onClick={() => dismiss(t.id)}>{t.text}</div>
      ))}
    </div>
  );
}

export function ConnectionBanner() {
  const connected = useConnected();
  if (connected) return null;
  return <div className="conn-banner" role="alert">Connection to the sign controller lost. Reconnecting…</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}
