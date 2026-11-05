// Transport and timeline tools: play / pause, go to start, time, loop, live,
// split / delete, snapping, zoom.
import { useEffect, useRef } from 'react';
import { Toggle } from '../components/ui';
import { useStore } from '../state/store';
import { deleteSelection, splitAtPlayhead } from './actions';
import { barBeat, formatSeconds } from './beats';
import { docStore, draft, useDoc } from './doc';
import { Icon } from './icons';
import { clock, loopStore, sessionStore, type EditorSession } from './session';
import { spanOf } from './model';
import { setSnap, timelineApi, viewStore } from './view';

/** Time and bar/beat of the playhead, updated every animation frame without React. */
function TimeDisplay() {
  const sec = useRef<HTMLSpanElement>(null);
  const bb = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    let raf = 0;
    let lastS = '';
    let lastB = '';
    const tick = () => {
      const d = docStore.get();
      if (d && sec.current && bb.current) {
        const t = clock.now();
        const s = formatSeconds(t);
        const b = barBeat(t, d.history.present.bpm, d.history.present.beat_offset);
        const bs = b ? `${b.bar}.${b.beat}` : '–';
        if (s !== lastS) sec.current.textContent = lastS = s;
        if (bs !== lastB) bb.current.textContent = lastB = bs;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);
  return (
    <div className="tp-time" title="Playhead: seconds, and bar.beat on the set's beat grid">
      <span ref={sec} className="tp-sec" data-testid="time-seconds">0.00 s</span>
      <span ref={bb} className="tp-bb" data-testid="time-bars">1.1</span>
    </div>
  );
}

export default function Transport({ session, collapsed, onCollapse }: { session: EditorSession; collapsed: boolean; onCollapse: () => void }) {
  const info = useStore(sessionStore);
  const loop = useStore(loopStore);
  const { snap } = useStore(viewStore);
  const hasSel = useDoc((s) => s.selection.length > 0);

  const loopSelection = () => {
    const span = spanOf(draft(), docStore.get()!.selection);
    if (span) session.setLoop({ on: true, from: span.start, to: span.end });
  };

  return (
    <div className="transport-bar" role="toolbar" aria-label="Transport">
      <div className="tp-group">
        <button className="icon-btn" onClick={() => session.seek(loop.on && loop.to !== null ? loop.from : 0)} title="Go to start (Home)" aria-label="Go to start"><Icon name="start" /></button>
        <button className="icon-btn tp-play" onClick={() => session.toggle()} title="Play / pause (Space)" aria-label={info.playing ? 'Pause' : 'Play'} data-testid="play">
          <Icon name={info.playing ? 'pause' : 'play'} size={22} />
        </button>
        <TimeDisplay />
      </div>
      <div className="tp-group">
        <button className={`icon-btn ${loop.on ? 'on' : ''}`} aria-pressed={loop.on} onClick={() => session.setLoop({ ...loop, on: !loop.on })}
          title="Loop (L). Shift-drag on the ruler to loop a range." aria-label="Loop" data-testid="loop"><Icon name="loop" /></button>
        {loop.on && loop.to !== null && (
          <span className="tp-range">
            {loop.from.toFixed(2)}–{loop.to.toFixed(2)} s
            <button className="link-btn" onClick={() => session.setLoop({ on: true, from: 0, to: null })} title="Loop the whole set" aria-label="Clear loop range">✕</button>
          </span>
        )}
        {hasSel && <button className="small-btn tp-loopsel" onClick={loopSelection} title="Loop the time span of the selected clips">Loop selection</button>}
        <label className="tp-live" title="Show the draft on the real sign and its speakers. Off: the sign carries on with its schedule.">
          <Toggle checked={info.live} onChange={(v) => session.setLive(v)} label="Live on sign" />
          <span className="long">Live on sign</span><span className="short">Live</span>
        </label>
      </div>
      <div className="tp-group tp-edit">
        <button className="icon-btn" onClick={splitAtPlayhead} title="Split at the playhead (S)" aria-label="Split at playhead"><Icon name="split" /></button>
        <button className="icon-btn" onClick={deleteSelection} disabled={!hasSel} title="Delete (Del)" aria-label="Delete selected clips"><Icon name="trash" /></button>
      </div>
      <div className="tp-group tp-view">
        <button className={`icon-btn ${snap ? 'on' : ''}`} aria-pressed={snap} onClick={() => setSnap(!snap)}
          title="Snap to beats, clip edges and the playhead (N). Hold Shift while dragging to suspend it." aria-label="Snapping" data-testid="snap"><Icon name="magnet" /></button>
        <button className="icon-btn" onClick={() => timelineApi.current?.zoom(1 / 1.5)} title="Zoom out (-)" aria-label="Zoom out"><Icon name="zoomOut" /></button>
        <button className="icon-btn" onClick={() => timelineApi.current?.zoom(1.5)} title="Zoom in (+)" aria-label="Zoom in"><Icon name="zoomIn" /></button>
        <button className="icon-btn" onClick={() => timelineApi.current?.fit()} title="Fit the set (0)" aria-label="Zoom to fit"><Icon name="fit" /></button>
        <button className="icon-btn" onClick={onCollapse} title={collapsed ? 'Show preview and panels' : 'Hide preview and panels (more room for tracks)'} aria-label="Toggle panels">
          <Icon name={collapsed ? 'chevronDown' : 'chevronUp'} />
        </button>
      </div>
    </div>
  );
}
