import { useEffect, useMemo, useState } from 'react';
import { api } from '../api/client';
import type { AudioDevice, ColorOrder, NetInterface, OutputSettings, Settings, TestPattern } from '../api/types';
import { Field, Segmented, Slider, Toggle } from '../components/ui';
import { SetupList } from '../components/SetupChecks';
import { patchSettings, settingsRes, useAppState, useSettings } from '../state/app';
import { useDebouncedValue } from '../state/hooks';
import { attempt } from '../state/toast';
import { PANEL_NAMES, PORTS, fromSeen, sameSeen, seenProblems, toSeen, type Seen } from './wiring';

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
      <SetupCard />
      <AudioCard s={s} devices={devices} />
      <OutputCard s={s} ifaces={ifaces} />
      <WiringCard s={s} />
      <LightsCard s={s} />
    </div>
  );
}

function SetupCard() {
  const checks = useAppState()?.checks;
  return (
    <section className="card setup">
      <h2>Setup</h2>
      {checks ? <SetupList checks={checks} /> : <p className="muted small">Waiting for the controller…</p>}
    </section>
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
        <Segmented value={o.driver} options={[{ value: 'colorlight', label: 'Colorlight' }, { value: 'fpga', label: 'Custom firmware' }, { value: 'none', label: 'None' }]}
          // The custom card is linear, so its gamma setting is the gamma; the stock card applies its own (1.0).
          onChange={(v) => void patchSettings({ output: v === 'fpga' && o.gamma === 1 ? { driver: v, gamma: 2.2 } : { driver: v } })} />
      </Field>
      {o.driver === 'colorlight' && (
        <Field label="Network interface" hint="The Ethernet port the Colorlight card is plugged into.">
          <select value={o.interface ?? ''} onChange={(e) => void patchSettings({ output: { interface: e.target.value || null } })}>
            <option value="">Choose…</option>
            {!known && o.interface && <option value={o.interface}>{o.interface} (not found)</option>}
            {ifaces?.map((i) => <option key={i.id} value={i.id}>{i.name}{i.description ? ` – ${i.description}` : ''}{i.up ? '' : ' (down)'}</option>)}
          </select>
        </Field>
      )}
      {o.driver === 'fpga' && <CardAddress o={o} />}
      <Field label="Brightness"><Slider label="Brightness" value={brightness} min={0} max={1} step={0.01} onChange={setBrightness} format={(v) => `${Math.round(v * 100)}%`} /></Field>
      <Field label="Gamma"><Slider label="Gamma" value={gamma} min={0.5} max={3} step={0.05} onChange={setGamma} format={(v) => v.toFixed(2)} /></Field>
      <Field label="Rotate 180°" hint="Turns the whole picture over. Panel wiring (below) switches it off and handles upside-down panels itself."><Toggle label="Rotate 180 degrees" checked={o.rotate180} onChange={(v) => void patchSettings({ output: { rotate180: v } })} /></Field>
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

function CardAddress({ o }: { o: OutputSettings }) {
  const out = useAppState()?.output;
  const found = out?.found ?? null;
  const others = out?.others ?? [];
  const [address, setAddress] = useDebouncedValue(o.card_address, (v) => void patchSettings({ output: { card_address: v.trim() } }), 600);
  // "Fixed" shows the field before anything is typed in it; an address that stays empty is still automatic.
  const [fixed, setFixed] = useState(o.card_address !== '');
  useEffect(() => {
    if (o.card_address) setFixed(true);
  }, [o.card_address]);
  const choose = (mode: 'auto' | 'fixed') => {
    setFixed(mode === 'fixed');
    if (mode === 'auto') setAddress('');
    else if (!address && found) setAddress(found.ip);   // pin the card in use
  };
  const card = (c: { ip: string; mac: string; fw: string }) => `${c.ip} · MAC ${c.mac} · firmware ${c.fw}`;
  return (
    <Field label="Card address" hint="Automatic finds the card by its announcements. This computer's port for the card needs the address 10.75.0.1 with netmask 255.255.0.0.">
      <div>
        <div className="row">
          <Segmented value={fixed ? 'fixed' : 'auto'} options={[{ value: 'auto', label: 'Automatic' }, { value: 'fixed', label: 'Fixed address' }]} onChange={choose} />
          {fixed && <input type="text" value={address} onChange={(e) => setAddress(e.target.value)} placeholder="10.75.c.d" spellCheck={false} aria-label="Card address" />}
        </div>
        <div className="muted small">
          {found ? `${found.auto ? 'Found' : 'Card'}: ${card(found)}` : out?.answering ? 'The card answers; it has not said who it is.' : fixed && address ? 'No answer yet.' : 'Looking for the card…'}
        </div>
        {others.length > 0 && <div className="muted small">Also on the network: {others.map(card).join('; ')}</div>}
      </div>
    </Field>
  );
}

function WiringCard({ s }: { s: Settings }) {
  const o = s.output;
  const now = useAppState()?.now;
  const showing = now?.source === 'test' ? now.set_name : null;
  const saved = useMemo(() => toSeen(o.panel_order, o.panel_flip, o.rotate180), [o.panel_order, o.panel_flip, o.rotate180]);
  // null = follow the saved settings; the first edit takes a copy.
  const [draft, setDraft] = useState<Seen[] | null>(null);
  const [applied, setApplied] = useState<number[] | null>(null);   // after Apply: the panels that were dark
  const seen = draft ?? saved;
  const problems = seenProblems(seen);
  const bad = new Set(problems.flatMap((p) => p.panels));
  const changed = !sameSeen(seen, saved) || o.rotate180;
  const result = problems.length ? null : fromSeen(seen);

  const edit = (i: number, v: Seen) => {
    const next = seen.slice();
    next[i] = v;
    setDraft(next);
    setApplied(null);
  };
  const apply = async () => {
    if (!result) return;
    const ok = await patchSettings({ output: { ...result, rotate180: false } });
    if (ok) {
      setApplied(seen.flatMap((x, i) => (x ? [] : [i])));
      setDraft(null);
    }
  };
  const test = (p: TestPattern) => attempt(() => api.outputTest(p));

  return (
    <section className="card wiring">
      <h2>Panel wiring</h2>
      <ol className="wiring-steps muted small">
        <li>Press <b>Show connector numbers</b>. Each panel shows the number of the card connector (J1 to J{PORTS}) its cable is plugged into, and an arrow.</li>
        <li>Standing in front of the sign, enter for each panel below the number it shows and which way its arrow points. If the arrow points down, the panel hangs upside down and so does its number: the bar belongs under the number (a "9" with the bar on top is a 6).</li>
        <li>Press <b>Apply</b>. This sets the panel order and flips, and switches "Rotate 180°" off: the arrows already say which panels are upside down.</li>
      </ol>
      <div className="row wiring-buttons">
        <button className={showing === 'wiring' ? 'primary' : ''} onClick={() => test('wiring')}>Show connector numbers</button>
        <button onClick={() => test('off')} disabled={!showing}>Stop test picture</button>
      </div>

      <div className="wiring-sign" role="group" aria-label="The sign from the front">
        {LIGHT_NAMES.map((light, f) => (
          <div key={light} className="wiring-light" style={{ borderColor: s.face_colors[f] }}>
            {[2 * f, 2 * f + 1].map((i) => (
              <WiringRow key={i} index={i} seen={seen[i]} bad={bad.has(i)} onChange={(v) => edit(i, v)} />
            ))}
          </div>
        ))}
      </div>

      {problems.map((p) => <p key={p.message} className="wiring-problem" role="alert">{p.message}</p>)}
      {result && seen.some((x) => x === null) && (
        <p className="muted small">
          {seen.flatMap((x, i) => (x ? [] : [`${PANEL_NAMES[i]} (given the free connector J${result.panel_order[i] + 1} for now)`])).join('; ')}:
          {' '}a panel that shows nothing has a loose or broken cable, no power, or sits on a dead connector. The other panels are saved as entered; fix it and run this again.
        </p>
      )}
      <div className="row wiring-buttons">
        <button className="primary" disabled={!result} onClick={() => void apply()}>Apply</button>
        {draft && <button onClick={() => { setDraft(null); setApplied(null); }}>Back to saved</button>}
        {!changed && !applied && <span className="muted small">Matches the saved settings.</span>}
      </div>

      {applied && (
        <div className="wiring-done" role="status">
          <p><b>Saved.</b> Check it: press <b>Show panels 1–6</b>. The sign should read 1 to 6 from the top, all upright, each with its white triangle in the top-left corner.
            {applied.length > 0 && ' Panels that showed nothing stay dark until they are fixed.'}</p>
          <div className="row">
            <button className="primary" onClick={() => test('panels')}>Show panels 1–6</button>
            <button onClick={() => test('off')}>Back to the show</button>
          </div>
        </div>
      )}
      <p className="muted small">A number that looks mirrored (not upside down), stripes, or half a panel dark cannot be fixed here: that is the receiver setup in LEDVision (docs/HARDWARE.md, section 4).</p>
    </section>
  );
}

function WiringRow({ index, seen, bad, onChange }: { index: number; seen: Seen; bad: boolean; onChange: (v: Seen) => void }) {
  const name = PANEL_NAMES[index];
  return (
    <div className={`wiring-row ${bad ? 'bad' : ''}`}>
      <div className={`wiring-panel ${seen?.down ? 'down' : ''} ${seen ? '' : 'dark'}`} aria-hidden="true">
        {seen ? <><span className="wiring-num">{seen.connector}</span><span className="wiring-arrow">↑</span></> : <span className="wiring-none">dark</span>}
      </div>
      <div className="wiring-ctl">
        <div className="wiring-name">{name}</div>
        <div className="row">
          <select aria-label={`Number on ${name}`} value={seen ? String(seen.connector) : ''}
            onChange={(e) => onChange(e.target.value ? { connector: Number(e.target.value), down: seen?.down ?? false } : null)}>
            {Array.from({ length: PORTS }, (_, b) => <option key={b} value={b + 1}>{b + 1}</option>)}
            <option value="">Nothing (dark)</option>
          </select>
          {seen && (
            <Segmented value={seen.down ? 'down' : 'up'}
              options={[{ value: 'up', label: '↑ Up' }, { value: 'down', label: '↓ Down' }]}
              onChange={(v) => onChange({ connector: seen.connector, down: v === 'down' })} />
          )}
        </div>
      </div>
    </div>
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
