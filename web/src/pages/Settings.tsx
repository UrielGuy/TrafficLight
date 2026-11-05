import { useEffect, useState } from 'react';
import { api } from '../api/client';
import type { AudioDevice, ColorOrder, NetInterface, Settings, TestPattern } from '../api/types';
import { Field, Segmented, Slider, Toggle } from '../components/ui';
import { patchSettings, settingsRes, useSettings } from '../state/app';
import { useDebouncedValue } from '../state/hooks';
import { attempt } from '../state/toast';

const PATTERNS: TestPattern[] = ['panels', 'white', 'red', 'green', 'blue', 'gradient', 'off'];
const COLOR_ORDERS: ColorOrder[] = ['rgb', 'rbg', 'grb', 'gbr', 'brg', 'bgr'];
const LIGHT_NAMES = ['Top', 'Middle', 'Bottom'];

export default function SettingsPage() {
  const s = useSettings();
  const [devices, setDevices] = useState<AudioDevice[] | null>(null);
  const [ifaces, setIfaces] = useState<NetInterface[] | null>(null);

  useEffect(() => {
    void settingsRes.refresh();
    attempt(api.audioDevices).then((d) => d && setDevices(d));
    attempt(api.outputInterfaces).then((d) => d && setIfaces(d));
  }, []);

  if (!s) return <div className="page"><h1>Settings</h1><p className="muted">Loading…</p></div>;
  return (
    <div className="page">
      <h1>Settings</h1>
      <AudioCard s={s} devices={devices} />
      <OutputCard s={s} ifaces={ifaces} />
      <PanelsCard s={s} />
      <LightsCard s={s} />
    </div>
  );
}

function AudioCard({ s, devices }: { s: Settings; devices: AudioDevice[] | null }) {
  const [volume, setVolume] = useDebouncedValue(s.audio.volume, (v) => void patchSettings({ audio: { volume: v } }));
  const known = devices?.some((d) => d.id === s.audio.device) ?? true;
  return (
    <section className="card">
      <h2>Audio</h2>
      <Field label="Output device">
        <select value={s.audio.device ?? ''} onChange={(e) => void patchSettings({ audio: { device: e.target.value || null } })}>
          <option value="">System default</option>
          {!known && s.audio.device && <option value={s.audio.device}>{s.audio.device} (not found)</option>}
          {devices?.map((d) => <option key={d.id} value={d.id}>{d.name}{d.default ? ' (default)' : ''}</option>)}
        </select>
      </Field>
      <Field label="Volume"><Slider label="Volume" value={volume} min={0} max={1} step={0.01} onChange={setVolume} format={(v) => `${Math.round(v * 100)}%`} /></Field>
    </section>
  );
}

function OutputCard({ s, ifaces }: { s: Settings; ifaces: NetInterface[] | null }) {
  const o = s.output;
  const [brightness, setBrightness] = useDebouncedValue(o.brightness, (v) => void patchSettings({ output: { brightness: v } }));
  const [gamma, setGamma] = useDebouncedValue(o.gamma, (v) => void patchSettings({ output: { gamma: v } }));
  const known = ifaces?.some((i) => i.id === o.interface) ?? true;
  return (
    <section className="card">
      <h2>LED output</h2>
      <Field label="Driver">
        <Segmented value={o.driver} options={[{ value: 'colorlight', label: 'Colorlight' }, { value: 'none', label: 'None' }]} onChange={(v) => void patchSettings({ output: { driver: v } })} />
      </Field>
      <Field label="Network interface" hint="The Ethernet port the Colorlight card is plugged into.">
        <select value={o.interface ?? ''} onChange={(e) => void patchSettings({ output: { interface: e.target.value || null } })}>
          <option value="">Choose…</option>
          {!known && o.interface && <option value={o.interface}>{o.interface} (not found)</option>}
          {ifaces?.map((i) => <option key={i.id} value={i.id}>{i.name}{i.description ? ` – ${i.description}` : ''}{i.up ? '' : ' (down)'}</option>)}
        </select>
      </Field>
      <Field label="Brightness"><Slider label="Brightness" value={brightness} min={0} max={1} step={0.01} onChange={setBrightness} format={(v) => `${Math.round(v * 100)}%`} /></Field>
      <Field label="Gamma"><Slider label="Gamma" value={gamma} min={0.5} max={3} step={0.05} onChange={setGamma} format={(v) => v.toFixed(2)} /></Field>
      <Field label="Rotate 180°"><Toggle label="Rotate 180 degrees" checked={o.rotate180} onChange={(v) => void patchSettings({ output: { rotate180: v } })} /></Field>
      <Field label="Colour order" hint="If red looks green or blue, change this.">
        <select value={o.color_order} onChange={(e) => void patchSettings({ output: { color_order: e.target.value as ColorOrder } })}>
          {COLOR_ORDERS.map((c) => <option key={c} value={c}>{c.toUpperCase()}</option>)}
        </select>
      </Field>
      <Field label="Test pattern" hint="Replaces the show on the LEDs until you press Off.">
        <div className="chips">
          {PATTERNS.map((p) => <button key={p} onClick={() => attempt(() => api.outputTest(p))}>{p === 'off' ? 'Off (back to show)' : p[0].toUpperCase() + p.slice(1)}</button>)}
        </div>
      </Field>
    </section>
  );
}

function PanelsCard({ s }: { s: Settings }) {
  const { panel_order: order, panel_flip: flip } = s.output;
  const setOrder = (panel: number, pos: number) => {
    // keep it a permutation: whoever had `pos` gets this panel's old position
    const next = order.slice();
    const other = next.indexOf(pos);
    if (other >= 0) next[other] = next[panel];
    next[panel] = pos;
    void patchSettings({ output: { panel_order: next } });
  };
  const setFlip = (panel: number, v: boolean) => {
    const next = flip.slice();
    next[panel] = v;
    void patchSettings({ output: { panel_flip: next } });
  };
  return (
    <section className="card">
      <h2>Panels</h2>
      <p className="muted small">The six panels as you see the sign from the front, top to bottom. Use the test pattern "Panels" to see which is which on the real sign. "Position" is where the panel sits in the receiver card's image.</p>
      <div className="panel-stack">
        {order.map((pos, i) => {
          const light = Math.floor(i / 2);
          return (
            <div key={i} className="panel-row">
              <div className={`panel-box ${flip[i] ? 'flipped' : ''}`} style={{ borderColor: s.face_colors[light] }}>
                <span className="panel-num">{i + 1}</span>
                <span className="panel-arrow" title={flip[i] ? 'Mounted upside down' : 'Mounted upright'}>▲</span>
              </div>
              <div className="panel-ctl">
                <div className="small muted">{LIGHT_NAMES[light]} light, {i % 2 === 0 ? 'upper' : 'lower'} half</div>
                <div className="row">
                  <label className="inline-field">Position
                    <select value={pos} onChange={(e) => setOrder(i, Number(e.target.value))}>
                      {order.map((_, p) => <option key={p} value={p}>{p + 1}</option>)}
                    </select>
                  </label>
                  <label className="inline-field"><Toggle label={`Flip panel ${i + 1}`} checked={flip[i]} onChange={(v) => setFlip(i, v)} /> Flip 180°</label>
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function LightsCard({ s }: { s: Settings }) {
  const [fps, setFps] = useDebouncedValue(s.fps, (v) => void patchSettings({ fps: v }));
  const setColor = (i: number, c: string) => {
    const next = s.face_colors.slice();
    next[i] = c;
    void patchSettings({ face_colors: next });
  };
  return (
    <section className="card">
      <h2>Lights</h2>
      {LIGHT_NAMES.map((n, i) => (
        <Field key={n} label={`${n} light colour`}>
          <ColorInput value={s.face_colors[i] ?? '#ffffff'} onChange={(c) => setColor(i, c)} />
        </Field>
      ))}
      <Field label="A light with nothing on it">
        <Segmented value={s.off_style} options={[{ value: 'ring', label: 'Dim ring' }, { value: 'dark', label: 'Dark' }]} onChange={(v) => void patchSettings({ off_style: v })} />
      </Field>
      <Field label="Frame rate"><Slider label="Frame rate" value={fps} min={10} max={60} step={1} onChange={setFps} format={(v) => `${v} fps`} /></Field>
    </section>
  );
}

function ColorInput({ value, onChange }: { value: string; onChange: (c: string) => void }) {
  // The native picker fires continuously while dragging: debounce.
  const [v, setV] = useDebouncedValue(value, onChange, 250);
  return (
    <div className="row">
      <input type="color" className="color" value={v} onChange={(e) => setV(e.target.value)} aria-label="Colour" />
      <code>{v}</code>
    </div>
  );
}
