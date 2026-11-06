// The inspector: the selected clip's params (form controls generated from the
// catalog's ParamSpecs) and common fields, or the set's properties when nothing
// is selected. Every change applies at once (draft -> preview).
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { mediaThumbUrl } from '../api/client';
import { editNote, openCropTool } from '../media/CropTool';
import type { Clip, ClipType, MediaItem, ParamSpec, SetData, Target } from '../api/types';
import { Toggle } from '../components/ui';
import { useCatalog, useMedia, useSettings } from '../state/app';
import { formatTime } from '../state/hooks';
import { useStore } from '../state/store';
import { accepts, copySelection, deleteSelection, duplicateSelection, fitDurationToClips, paste, setWhole, splitAtPlayhead } from './actions';
import { secondsPerBeat, tapTempo } from './beats';
import { categoryHue, trackColor, typeMap } from './clipinfo';
import ClipPicture from './ClipPicture';
import { edit, getClipboard, sealHistory, select, useDoc } from './doc';
import { clock, problemListStore, problemsStore } from './session';
import { TrackMarker } from './Timeline';
import { applyMoves, findClip, mediaSpeed, placeName, planMove, rowTracks, TARGET_NAMES, trim, updateClip } from './model';
import { timelineApi } from './view';

const fmt = (v: number) => String(Math.round(v * 1000) / 1000);

/** A number field that lets you type freely and commits valid values at once. */
export function NumberInput({ value, onChange, min, max, step, int, label, id }: {
  value: number; onChange: (v: number) => void; min?: number; max?: number; step?: number; int?: boolean; label?: string; id?: string;
}) {
  const [text, setText] = useState(fmt(value));
  const focused = useRef(false);
  useEffect(() => {
    if (!focused.current) setText(fmt(value));
  }, [value]);
  return (
    <input type="number" inputMode="decimal" className="num" id={id} aria-label={label} value={text} step={step ?? 'any'} min={min} max={max}
      onFocus={() => (focused.current = true)}
      onBlur={() => {
        focused.current = false;
        setText(fmt(value));
        sealHistory();
      }}
      onChange={(e) => {
        setText(e.target.value);
        let n = parseFloat(e.target.value);
        if (!isFinite(n)) return;
        if (int) n = Math.round(n);
        if (min !== undefined) n = Math.max(min, n);
        if (max !== undefined) n = Math.min(max, n);
        onChange(n);
      }} />
  );
}

function Row({ label, children, help, htmlFor, wide }: { label: string; children: ReactNode; help?: string; htmlFor?: string; wide?: boolean }) {
  return (
    <div className={`insp-row ${wide ? 'wide' : ''}`}>
      <label className="insp-label" htmlFor={htmlFor}>{label}</label>
      <div className="insp-control">{children}</div>
      {help && <div className="insp-help">{help}</div>}
    </div>
  );
}

function ColorControl({ value, onChange, faceColor, label }: { value: string; onChange: (v: string) => void; faceColor: string; label: string }) {
  const hex = /^#[0-9a-f]{6}$/i.test(value);
  return (
    <div className="colorctl" role="group" aria-label={label}>
      <button type="button" className={value === 'face' ? 'active' : ''} onClick={() => onChange('face')} title="The light's own colour">
        <i style={{ background: faceColor }} /> Light
      </button>
      <button type="button" className={value === 'random' ? 'active' : ''} onClick={() => onChange('random')} title="A new colour every time the clip starts">
        <i className="rainbow" /> Random
      </button>
      <label className={`colorpick ${hex ? 'active' : ''}`} title="Pick a colour">
        <input type="color" value={hex ? value : '#ffffff'} onChange={(e) => onChange(e.target.value)} aria-label={`${label}: pick a colour`} />
        <span>{hex ? value : 'Pick'}</span>
      </label>
    </div>
  );
}

function MediaSelect({ spec, value, onChange, media }: { spec: ParamSpec; value: string; onChange: (v: string) => void; media: MediaItem[] }) {
  const items = media.filter((m) => !spec.media_kind || m.kind === spec.media_kind);
  const cur = items.find((m) => m.id === value);
  return (
    <div className="mediactl">
      {cur && cur.version > 0 && cur.kind === 'audio' && <img src={mediaThumbUrl(cur.id, cur.version)} alt="" />}
      <select value={value} onChange={(e) => onChange(e.target.value)} aria-label={spec.label}>
        <option value="">— choose {spec.media_kind ?? 'media'} —</option>
        {value && !cur && <option value={value}>Missing item ({value})</option>}
        {items.map((m) => (
          <option key={m.id} value={m.id} disabled={m.status !== 'ready'}>
            {m.name}{m.duration > 0 ? ` (${formatTime(m.duration)})` : ''}{m.status !== 'ready' ? ` — ${m.status}` : ''}
          </option>
        ))}
      </select>
    </div>
  );
}

/** Quick choices for a media clip's speed: the slider alone makes 0.5 or 2 hard to hit. */
const SPEEDS = [0.25, 0.5, 1, 2, 4];

function ParamControl({ spec, value, onChange, faceColor, media, help }: {
  spec: ParamSpec; value: unknown; onChange: (v: unknown) => void; faceColor: string; media: MediaItem[]; help?: string;
}) {
  const id = `p-${spec.key}`;
  const v = value ?? spec.default;
  let control: ReactNode;
  switch (spec.kind) {
    case 'text':
      control = <textarea id={id} rows={3} value={String(v ?? '')} onChange={(e) => onChange(e.target.value)} onBlur={sealHistory} dir="auto" />;
      break;
    case 'string':
      control = <input id={id} type="text" value={String(v ?? '')} onChange={(e) => onChange(e.target.value)} onBlur={sealHistory} dir="auto" />;
      break;
    case 'number':
    case 'int': {
      const n = Number(v) || 0;
      const int = spec.kind === 'int';
      control = (
        <div className="numctl">
          {spec.min !== undefined && spec.max !== undefined && (
            <input type="range" min={spec.min} max={spec.max} step={spec.step ?? (int ? 1 : 0.01)} value={n} aria-label={spec.label}
              onChange={(e) => onChange(int ? Math.round(Number(e.target.value)) : Number(e.target.value))} onPointerUp={sealHistory} />
          )}
          <NumberInput id={id} value={n} min={spec.min} max={spec.max} step={spec.step} int={int} onChange={onChange} label={spec.label} />
        </div>
      );
      if (spec.key === 'speed' && !int) {
        control = (
          <>
            {control}
            <div className="segmented small speedctl" role="group" aria-label="Speed presets" data-testid="speed-presets">
              {SPEEDS.filter((x) => (spec.min ?? 0) <= x && x <= (spec.max ?? Infinity)).map((x) => (
                <button key={x} type="button" className={Math.abs(n - x) < 1e-6 ? 'active' : ''} onClick={() => { onChange(x); sealHistory(); }}>{x}×</button>
              ))}
            </div>
          </>
        );
      }
      break;
    }
    case 'bool':
      control = <Toggle checked={!!v} onChange={(b) => onChange(b)} label={spec.label} />;
      break;
    case 'select':
    case 'font': {
      const opts = spec.options ?? [];
      if (!opts.length) {
        control = <input id={id} type="text" value={String(v ?? '')} onChange={(e) => onChange(e.target.value)} />;
        break;
      }
      const idx = opts.findIndex((o) => o.value === v);
      control = (
        <select id={id} value={idx < 0 ? '' : String(idx)} onChange={(e) => onChange(opts[Number(e.target.value)].value)}>
          {idx < 0 && <option value="">{String(v ?? '')}</option>}
          {opts.map((o, i) => <option key={i} value={i}>{o.label}</option>)}
        </select>
      );
      break;
    }
    case 'color':
      control = <ColorControl value={String(v ?? '')} onChange={onChange} faceColor={faceColor} label={spec.label} />;
      break;
    case 'media':
      control = <MediaSelect spec={spec} value={String(v ?? '')} onChange={onChange} media={media} />;
      break;
    default:
      control = <input id={id} type="text" value={String(v ?? '')} onChange={(e) => onChange(e.target.value)} />;
  }
  // colour choices, media pickers and long text get the whole width, label above
  const wide = spec.kind === 'color' || spec.kind === 'media' || spec.kind === 'text';
  return <Row label={spec.label} help={help ?? spec.help} htmlFor={id} wide={wide}>{control}</Row>;
}

// ---------------------------------------------------------------- clip
function ClipInspector({ clip, target, trackName, type }: { clip: Clip; target: Target; trackName: string; type: ClipType | undefined }) {
  const settings = useSettings();
  const media = useMedia() ?? [];
  const problems = useStore(problemsStore).get(clip.id);
  const bpm = useDoc((s) => s.history.present.bpm);
  const audio = target === 'audio';
  const faceColor = trackColor(target === 'audio' ? 'all' : target, settings);
  const set = (fn: (c: Clip) => Clip, key: string) => edit((s) => updateClip(s, clip.id, fn), { key: `${clip.id}:${key}` });
  const setParam = (k: string, v: unknown) => set((c) => ({ ...c, params: { ...c.params, [k]: v } }), `p.${k}`);

  const setStart = (v: number) => edit((s) => {
    const plan = planMove(s, [clip.id], v - clip.start, 0, accepts);
    return plan ? applyMoves(s, plan) : s;
  }, { key: `${clip.id}:start` });
  const setDuration = (v: number) => edit((s) => {
    const loc = findClip(s, clip.id);
    if (!loc) return s;
    const r = trim(s, loc.track, loc.clip, 'end', loc.clip.start + v);
    return updateClip(s, clip.id, (c) => ({ ...c, duration: r.duration, fade_in: Math.min(c.fade_in, r.duration), fade_out: Math.min(c.fade_out, r.duration) }));
  }, { key: `${clip.id}:duration` });
  const beats = clip.duration / secondsPerBeat(bpm);

  // the crop tool for the clip's video or picture; when the new crop is ready, a clip that ran for the
  // whole of the old one follows the new length
  const mediaItem = (clip.type === 'video' || clip.type === 'image') && typeof clip.params.media === 'string'
    ? media.find((m) => m.id === clip.params.media) : undefined;
  const canCrop = !!mediaItem && mediaItem.kind === clip.type && mediaItem.version > 0;
  const whole = target === 'all';
  // say it where the speed is set: a video's own sound stops at any other speed
  const speed = mediaSpeed(clip);
  const silenced = Math.abs(speed - 1) > 1e-3 && !!mediaItem?.has_audio && clip.params.sound !== false;
  const speedHelp = clip.type !== 'video' ? undefined
    : speed === 0 ? 'Shows one still frame, without sound'
    : silenced ? 'Its own sound is silent at this speed: it plays only at 1×' : 'Its own sound plays only at 1×';
  // types that may be on one light or on the whole sign can be switched between the two
  const canSwitch = !!type && type.targets.includes('face') && type.targets.includes('all') && target !== 'audio';
  const cropMedia = () => {
    if (!mediaItem) return;
    const speed = Number(clip.params.speed ?? 1) || 1;
    const offset = Number(clip.params.offset ?? 0) || 0;
    const followed = clip.type === 'video' && Math.abs(clip.duration - Math.max(0, mediaItem.duration - offset) / speed) < 0.05;
    openCropTool(mediaItem, (m) => {
      if (followed && m.duration > 0) setDuration(Math.max(0.1, Math.max(0, m.duration - offset) / speed));
    });
  };

  return (
    <div className="insp">
      <div className="insp-head" style={{ ['--hue' as string]: categoryHue(type?.category ?? '') }}>
        <span className="insp-cat" />
        <div className="grow">
          <div className="insp-title">{type?.label ?? clip.type}</div>
          <div className="muted small insp-track">
            <TrackMarker target={target} settings={settings} /> {whole ? 'Whole sign: all three lights' : `${trackName} · ${TARGET_NAMES[target]}`}
          </div>
        </div>
      </div>
      {type?.description && <p className="muted small insp-desc">{type.description}</p>}
      {!type && <div className="insp-problems" role="alert">Unknown clip type “{clip.type}”: it cannot be edited here.</div>}
      {!!problems?.length && (
        <div className="insp-problems" role="alert" data-testid="clip-problems">
          {problems.map((p, i) => <div key={i}>{p}</div>)}
        </div>
      )}
      {(canSwitch || canCrop) && (
        <section className="insp-section insp-place">
          {canCrop && <ClipPicture item={mediaItem!} whole={whole} fit={String(clip.params.fit ?? 'cover')} circles={clip.mask !== false} />}
          <div className="insp-place-side">
            {canSwitch && (
              <div className="insp-shows">
                <span className="insp-label">Shows on</span>
                <div className="segmented small" role="group" aria-label="Shows on" data-testid="shows-on">
                  <button type="button" className={whole ? '' : 'active'} aria-pressed={!whole} onClick={() => whole && setWhole(clip.id, false)}>One light</button>
                  <button type="button" className={whole ? 'active' : ''} aria-pressed={whole} onClick={() => !whole && setWhole(clip.id, true)}>Whole sign</button>
                </div>
              </div>
            )}
            {canCrop && (
              <div className="insp-crop">
                <button onClick={cropMedia} data-testid="insp-crop">Crop &amp; trim…</button>
                <span className="muted small">{mediaItem!.status === 'processing' ? 'Making the new crop…' : editNote(mediaItem!) ?? 'Whole picture'}</span>
              </div>
            )}
          </div>
        </section>
      )}
      {type && type.params.length > 0 && (
        <section className="insp-section">
          {type.params.map((spec) => (
            <ParamControl key={spec.key} spec={spec} value={clip.params[spec.key]} onChange={(v) => setParam(spec.key, v)} faceColor={faceColor} media={media}
              help={spec.key === 'speed' ? speedHelp : undefined} />
          ))}
        </section>
      )}
      <section className="insp-section">
        <h4>Timing</h4>
        <Row label="Start (s)" htmlFor="c-start"><NumberInput id="c-start" value={clip.start} min={0} step={0.1} onChange={setStart} /></Row>
        <Row label="Length (s)" htmlFor="c-dur" help={`${fmt(Math.round(beats * 100) / 100)} beats`}>
          <NumberInput id="c-dur" value={clip.duration} min={0.1} step={0.1} onChange={setDuration} />
        </Row>
        <Row label="Fade in (s)" htmlFor="c-fi"><NumberInput id="c-fi" value={clip.fade_in} min={0} max={clip.duration} step={0.1} onChange={(v) => set((c) => ({ ...c, fade_in: v }), 'fade_in')} /></Row>
        <Row label="Fade out (s)" htmlFor="c-fo"><NumberInput id="c-fo" value={clip.fade_out} min={0} max={clip.duration} step={0.1} onChange={(v) => set((c) => ({ ...c, fade_out: v }), 'fade_out')} /></Row>
      </section>
      <section className="insp-section">
        <h4>{audio ? 'Level' : 'Look'}</h4>
        <Row label={audio ? 'Volume' : 'Opacity'}>
          <div className="numctl">
            <input type="range" min={0} max={1} step={0.01} value={clip.opacity} aria-label={audio ? 'Volume' : 'Opacity'}
              onChange={(e) => set((c) => ({ ...c, opacity: Number(e.target.value) }), 'opacity')} onPointerUp={sealHistory} />
            <span className="numval">{Math.round(clip.opacity * 100)}%</span>
          </div>
        </Row>
        {!audio && (
          <Row label="Mask" help={`Only the round lights are seen on the sign (the default). None fills the whole ${target === 'all' ? 'sign' : 'square'}.`}>
            <div className="segmented small" data-testid="mask">
              {/* the default (null) is the circles: stored as null so a later change of the default reaches it */}
              {([[null, target === 'all' ? 'Circles' : 'Circle'], [false, 'None']] as const).map(([v, l]) => (
                <button key={String(v)} type="button" className={(clip.mask !== false) === (v === null) ? 'active' : ''}
                  onClick={() => set((c) => ({ ...c, mask: v }), 'mask')}>{l}</button>
              ))}
            </div>
          </Row>
        )}
      </section>
      <div className="insp-actions">
        <button onClick={splitAtPlayhead}>Split at playhead</button>
        <button onClick={duplicateSelection}>Duplicate</button>
        <button onClick={copySelection}>Copy</button>
        <button className="danger-text" onClick={deleteSelection}>Delete</button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- set
function SetInspector() {
  const s = useDoc((d) => d.history.present);
  const problems = useStore(problemListStore);
  const catalog = useCatalog();
  const types = typeMap(catalog);
  const taps = useRef<number[]>([]);
  const [tapBpm, setTapBpm] = useState<number | null>(null);
  const patch = (p: Partial<SetData>, key: string) => edit((x) => ({ ...x, ...p }), { key: `set:${key}` });
  const tap = () => {
    const now = performance.now();
    taps.current = [...taps.current.slice(-15), now];
    const bpm = tapTempo(taps.current);
    setTapBpm(bpm);
    if (bpm) patch({ bpm }, 'bpm');
  };
  const clips = s.tracks.reduce((n, t) => n + t.clips.length, 0);
  const rows = rowTracks(s.tracks).length;
  return (
    <div className="insp">
      <div className="insp-head"><div className="grow"><div className="insp-title">Set</div><div className="muted small">{rows} tracks · {clips} clips</div></div></div>
      <section className="insp-section">
        <Row label="Name" htmlFor="s-name"><input id="s-name" type="text" value={s.name} onChange={(e) => patch({ name: e.target.value }, 'name')} onBlur={sealHistory} /></Row>
        <Row label="Tempo (bpm)" htmlFor="s-bpm" help={tapBpm ? `Tapped ${tapBpm} bpm — keep tapping to refine` : 'Tap along to the music to set it'}>
          <div className="numctl">
            <NumberInput id="s-bpm" value={s.bpm} min={10} max={400} step={0.1} onChange={(v) => patch({ bpm: v }, 'bpm')} />
            <button type="button" className="tap-btn" onPointerDown={(e) => { e.preventDefault(); tap(); }} onKeyDown={(e) => e.key === 'Enter' && tap()}>Tap</button>
          </div>
        </Row>
        <Row label="Beat offset (s)" htmlFor="s-off" help="Where beat 1 falls: the start of the grid">
          <div className="numctl">
            <NumberInput id="s-off" value={s.beat_offset} min={0} step={0.01} onChange={(v) => patch({ beat_offset: v }, 'offset')} />
            <button type="button" onClick={() => patch({ beat_offset: Math.round(clock.now() * 1000) / 1000 }, 'offset')} title="Put beat 1 at the playhead">At playhead</button>
          </div>
        </Row>
        <Row label="Duration (s)" htmlFor="s-dur">
          <div className="numctl">
            <NumberInput id="s-dur" value={s.duration} min={1} step={1} onChange={(v) => patch({ duration: v }, 'duration')} />
            <button type="button" onClick={fitDurationToClips} title="End the set where its last clip ends">Fit to clips</button>
          </div>
        </Row>
        <Row label="In rotation" help="May be picked as the random set">
          <Toggle checked={s.in_rotation} onChange={(v) => patch({ in_rotation: v }, 'rot')} label="In rotation" />
        </Row>
      </section>
      {getClipboard().length > 0 && <div className="insp-actions"><button onClick={paste}>Paste at playhead</button></div>}
      {problems.length > 0 && (
        <section className="insp-section">
          <h4>Problems</h4>
          <ul className="insp-plist">
            {problems.map((p, i) => {
              const loc = findClip(s, p.clip_id);
              return (
                <li key={i}>
                  <button className="link-btn" onClick={() => {
                    select([p.clip_id]);
                    if (loc) timelineApi.current?.reveal(loc.clip.start);
                  }}>{loc ? `${types.get(loc.clip.type)?.label ?? loc.clip.type} on ${placeName(loc.track)}` : 'Clip'}</button>: {p.message}
                </li>
              );
            })}
          </ul>
        </section>
      )}
      <p className="muted small insp-tips">
        Click an empty spot on a track, then a library item, to add it there. Drag clips to move them, their edges to trim.
        Shift-click or drag a box to select several. Hold Shift while dragging to turn snapping off.
        A whole-sign clip is one block over the three lights: while it plays, nothing else can be on them.
        Videos and pictures cropped for the whole sign become one; “Shows on” switches a clip between one light and the whole sign.
      </p>
    </div>
  );
}

function MultiInspector({ count }: { count: number }) {
  return (
    <div className="insp">
      <div className="insp-head"><div className="grow"><div className="insp-title">{count} clips selected</div></div></div>
      <div className="insp-actions">
        <button onClick={duplicateSelection}>Duplicate</button>
        <button onClick={copySelection}>Copy</button>
        <button onClick={splitAtPlayhead}>Split at playhead</button>
        <button className="danger-text" onClick={deleteSelection}>Delete</button>
        <button onClick={() => select([])}>Clear selection</button>
      </div>
    </div>
  );
}

export default function Inspector() {
  const count = useDoc((s) => s.selection.length);
  // two selectors that each return objects of the set itself, so they are stable between edits
  const clip = useDoc((s) => (s.selection.length === 1 ? findClip(s.history.present, s.selection[0])?.clip ?? null : null));
  const track = useDoc((s) => (s.selection.length === 1 ? findClip(s.history.present, s.selection[0])?.track ?? null : null));
  const catalog = useCatalog();
  if (count > 1) return <MultiInspector count={count} />;
  if (clip && track) {
    const type = catalog?.find((t) => t.type === clip.type);
    return <ClipInspector key={clip.id} clip={clip} target={track.target} trackName={track.name} type={type} />;
  }
  return <SetInspector />;
}
