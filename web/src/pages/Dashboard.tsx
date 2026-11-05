import { useEffect } from 'react';
import { api } from '../api/client';
import SignPreview from '../components/SignPreview';
import { Empty, Field, Slider, Toggle, Warning } from '../components/ui';
import { patchSettings, setsRes, settingsRes, signFeed, useAppState, useSets, useSettings } from '../state/app';
import { formatTime, useDebouncedValue } from '../state/hooks';
import { attempt } from '../state/toast';
import type { NowSource } from '../api/types';

const SOURCE: Record<NowSource, string> = {
  schedule: 'Schedule',
  show: 'Shown by hand',
  editor: 'Editor (live)',
  test: 'Test pattern',
  idle: 'Idle',
};

export default function Dashboard() {
  const state = useAppState();
  const sets = useSets();
  const settings = useSettings();

  useEffect(() => {
    void settingsRes.refresh();
    void setsRes.refresh();
  }, []);

  const [brightness, setBrightness] = useDebouncedValue(settings?.output.brightness ?? 0.5, (v) => void patchSettings({ output: { brightness: v } }));
  const [volume, setVolume] = useDebouncedValue(settings?.audio.volume ?? state?.audio.volume ?? 0.8, (v) => void patchSettings({ audio: { volume: v } }));
  const [minutes, setMinutes] = useDebouncedValue(settings?.schedule.random_minutes ?? 6, (v) => void patchSettings({ schedule: { random_minutes: v } }), 600);

  const now = state?.now;
  const progress = now && now.duration > 0 ? Math.min(1, now.t / now.duration) : 0;

  return (
    <div className="page">
      <h1>Dashboard</h1>
      {state && !state.output.ok && (
        <Warning title="The LED output is not working">{state.output.error ?? 'Unknown error'}{state.output.interface ? ` (interface: ${state.output.interface})` : ''}</Warning>
      )}
      {state && !state.audio.ok && (
        <Warning title="The audio output is not working">{state.audio.error ?? 'Unknown error'}{state.audio.device_name ? ` (device: ${state.audio.device_name})` : ''}</Warning>
      )}

      <div className="dash-top">
        <div className="preview-col">
          <SignPreview feed={signFeed} className="dash-preview" />
          {state && <div className="muted small center">{state.output.driver === 'none' ? 'No output driver' : `${state.output.fps.toFixed(0)} fps`}</div>}
        </div>

        <div className="card now">
          <div className="small muted">{now ? SOURCE[now.source] : 'Waiting for the controller…'}{now?.paused ? ' · paused' : ''}</div>
          <div className="now-name">{now?.set_name ?? (now?.source === 'test' ? 'Test pattern' : '—')}</div>
          <div className="progress"><div style={{ width: `${progress * 100}%` }} /></div>
          <div className="small muted">{formatTime(now?.t)} / {formatTime(now?.duration)}</div>
          <div className="row transport">
            <button className="primary" onClick={() => attempt(now?.paused ? api.play : api.pause)}>{now?.paused ? 'Play' : 'Pause'}</button>
            <button onClick={() => attempt(api.next)}>Next</button>
          </div>
          <Field label="Brightness"><Slider label="Brightness" value={brightness} min={0} max={1} step={0.01} onChange={setBrightness} format={(v) => `${Math.round(v * 100)}%`} /></Field>
          <Field label="Volume"><Slider label="Volume" value={volume} min={0} max={1} step={0.01} onChange={setVolume} format={(v) => `${Math.round(v * 100)}%`} /></Field>
        </div>
      </div>

      <section className="card">
        <h2>Schedule</h2>
        <Field label="Default set" hint="The set the sign loops. Without one, all sets play in order.">
          <select value={settings?.schedule.default_set ?? ''} onChange={(e) => void patchSettings({ schedule: { default_set: e.target.value || null } })}>
            <option value="">None (play all in order)</option>
            {sets?.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </Field>
        <Field label="Random set">
          <Toggle label="Random set" checked={settings?.schedule.random_enabled ?? false} onChange={(v) => void patchSettings({ schedule: { random_enabled: v } })} />
        </Field>
        <Field label="Every (minutes)">
          <input type="number" min={0.5} step={0.5} value={minutes} onChange={(e) => e.target.value && setMinutes(Number(e.target.value))} />
        </Field>
        <Field label="Next random set in">
          <span className="big-num">{state?.schedule.random_enabled && state.schedule.random_in != null ? formatTime(state.schedule.random_in) : '–'}</span>
        </Field>
      </section>

      <section className="card">
        <h2>Sets</h2>
        {sets && sets.length === 0 && <Empty>No sets yet. Create one on the Sets page.</Empty>}
        <ul className="plain-list">
          {sets?.map((s) => (
            <li key={s.id} className="list-row">
              <div className="grow">
                <div className="name">{s.name}{state?.now.set_id === s.id && <span className="badge">playing</span>}{settings?.schedule.default_set === s.id && <span className="badge">default</span>}</div>
                <div className="small muted">{formatTime(s.duration)} · {s.bpm} bpm{s.in_rotation ? '' : ' · not in rotation'}</div>
              </div>
              <button onClick={() => attempt(() => api.showSet(s.id))}>Show now</button>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
