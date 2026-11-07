import { useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import { BundleBar, DownloadSet } from '../components/Bundles';
import SignPreview from '../components/SignPreview';
import { Empty, Field, Slider, Toggle } from '../components/ui';
import { AttentionList } from '../components/SetupChecks';
import { patchSettings, setsRes, settingsRes, signFeed, useAppState, useSets, useSettings } from '../state/app';
import { formatTime, useDebouncedValue } from '../state/hooks';
import { attempt } from '../state/toast';
import type { NowSource, State } from '../api/types';

const SOURCE: Record<NowSource, string> = {
  schedule: 'Schedule',
  show: 'Shown by hand',
  editor: 'Editor (live)',
  test: 'Test pattern',
  idle: 'Idle',
};

/**
 * Position in the playing set. The server reports it a few times a second; between reports the
 * bar runs on by the browser's clock, drawn every animation frame without re-rendering the page.
 */
function NowProgress({ now }: { now: State['now'] | undefined }) {
  const bar = useRef<HTMLDivElement>(null);
  const text = useRef<HTMLDivElement>(null);
  const at = useRef({ now, t: 0, duration: 0, running: false, stamp: 0 });
  // Only a new report restarts the clock: the page also re-renders for unrelated reasons.
  if (at.current.now !== now || at.current.stamp === 0) {
    at.current = { now, t: now?.t ?? 0, duration: now?.duration ?? 0, running: !!now && !now.paused && now.duration > 0, stamp: performance.now() };
  }
  const known = !!now;
  useEffect(() => {
    let raf = 0;
    let shown = '';
    const tick = () => {
      const a = at.current;
      const t = a.running ? Math.min(a.duration, a.t + (performance.now() - a.stamp) / 1000) : a.t;
      if (bar.current) bar.current.style.transform = `scaleX(${a.duration > 0 ? Math.min(1, t / a.duration) : 0})`;
      const label = known ? `${formatTime(t)} / ${formatTime(a.duration)}` : `${formatTime(null)} / ${formatTime(null)}`;
      if (text.current && label !== shown) text.current.textContent = shown = label;
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [known]);
  return (
    <>
      <div className="progress smooth"><div ref={bar} /></div>
      <div className="small muted" ref={text} data-testid="now-time" />
    </>
  );
}

export default function Dashboard() {
  const state = useAppState();
  const sets = useSets();
  const settings = useSettings();
  const nav = useNavigate();

  useEffect(() => {
    void settingsRes.refresh();
    void setsRes.refresh();
  }, []);

  const [brightness, setBrightness] = useDebouncedValue(settings?.output.brightness ?? 0.5, (v) => void patchSettings({ output: { brightness: v } }));
  const [volume, setVolume] = useDebouncedValue(settings?.audio.volume ?? state?.audio.volume ?? 0.8, (v) => void patchSettings({ audio: { volume: v } }));
  const [minutes, setMinutes] = useDebouncedValue(settings?.schedule.random_minutes ?? 6, (v) => void patchSettings({ schedule: { random_minutes: v } }), 600);

  const now = state?.now;

  return (
    <div className="page">
      <h1>Dashboard</h1>
      {state && <AttentionList checks={state.checks ?? []} />}   {/* a server older than the checks sends none */}

      <div className="dash-top">
        <div className="preview-col">
          <SignPreview feed={signFeed} className="dash-preview" />
          {state && <div className="muted small center">{state.output.driver === 'none' ? 'No output driver' : `${state.output.fps.toFixed(0)} fps`}</div>}
        </div>

        <div className="card now">
          <div className="small muted">{now ? SOURCE[now.source] : 'Waiting for the controller…'}{now?.paused ? ' · paused' : ''}</div>
          <div className="now-name">{now?.set_name ?? (now?.source === 'test' ? 'Test pattern' : '—')}</div>
          <NowProgress now={now} />
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
        <div className="section-head">
          <h2>Sets</h2>
          <BundleBar />
        </div>
        {sets && sets.length === 0 && <Empty>No sets yet. Create one on the Sets page, or upload a zip from another TrafficLight.</Empty>}
        <ul className="plain-list">
          {sets?.map((s) => (
            <li key={s.id} className="list-row">
              <div className="grow">
                <div className="name">{s.name}{state?.now.set_id === s.id && <span className="badge">playing</span>}{settings?.schedule.default_set === s.id && <span className="badge">default</span>}</div>
                <div className="small muted">{formatTime(s.duration)} · {s.bpm} bpm{s.in_rotation ? '' : ' · not in rotation'}</div>
              </div>
              <div className="row">
                <button onClick={() => nav(`/editor/${s.id}`)}>Edit</button>
                <button onClick={() => attempt(() => api.showSet(s.id))}>Show now</button>
                <DownloadSet id={s.id} />
              </div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
