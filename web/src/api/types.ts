// Mirrors trafficlight/model.py, trafficlight/render/base.py (ParamSpec) and the
// HTTP/WebSocket sections of docs/ARCHITECTURE.md. Keep in step with them.

export type Target = 'face0' | 'face1' | 'face2' | 'all' | 'audio';
export const FACE_TARGETS: Target[] = ['face0', 'face1', 'face2'];

export interface Clip {
  id: string;
  type: string;
  start: number;
  duration: number;
  params: Record<string, unknown>;
  mask: boolean | null;
  opacity: number;
  fade_in: number;
  fade_out: number;
}

export interface Track {
  id: string;
  name: string;
  target: Target;
  muted: boolean;
  clips: Clip[];
}

export interface SetData {
  id: string;
  name: string;
  bpm: number;
  beat_offset: number;
  duration: number;
  in_rotation: boolean;
  tracks: Track[];
}
/** `Set` in the document; renamed to avoid clashing with the JS built-in. */
export type { SetData as Set };

export interface SetSummary {
  id: string;
  name: string;
  bpm: number;
  duration: number;
  in_rotation: boolean;
}

/** Body for POST /api/sets: everything optional except what you want to set. */
export type NewSet = Partial<SetData>;

export type OutputDriver = 'colorlight' | 'none';
export type ColorOrder = 'rgb' | 'rbg' | 'grb' | 'gbr' | 'brg' | 'bgr';

export interface OutputSettings {
  driver: OutputDriver;
  interface: string | null;
  brightness: number;
  gamma: number;
  rotate180: boolean;
  panel_order: number[];
  panel_flip: boolean[];
  color_order: ColorOrder;
}
export interface AudioSettings {
  device: string | null;
  volume: number;
}
export interface ScheduleSettings {
  default_set: string | null;
  random_minutes: number;
  random_enabled: boolean;
}
export interface Settings {
  output: OutputSettings;
  audio: AudioSettings;
  schedule: ScheduleSettings;
  face_colors: string[];
  off_style: 'ring' | 'dark';
  fps: number;
}
export type DeepPartial<T> = { [K in keyof T]?: T[K] extends object ? (T[K] extends unknown[] ? T[K] : DeepPartial<T[K]>) : T[K] };
export type SettingsPatch = DeepPartial<Settings>;

export type MediaKind = 'video' | 'image' | 'audio';
export interface MediaItem {
  id: string;
  name: string;
  kind: MediaKind;
  status: 'processing' | 'ready' | 'error';
  error: string | null;
  duration: number;
  width: number;
  height: number;
  fps: number;
  has_audio: boolean;
  size: number;
  added: number;
}

export type ParamKind = 'string' | 'text' | 'number' | 'int' | 'bool' | 'select' | 'color' | 'media' | 'font';
export interface ParamOption {
  value: string | number;
  label: string;
}
export interface ParamSpec {
  key: string;
  label: string;
  kind: ParamKind;
  default: unknown;
  min?: number;
  max?: number;
  step?: number;
  options?: ParamOption[];
  media_kind?: MediaKind;
  help?: string;
}
export interface ClipType {
  type: string;
  label: string;
  category: string;
  targets: string[];
  default_duration: number;
  description: string;
  params: ParamSpec[];
}
export interface Catalog {
  clip_types: ClipType[];
}

export interface Problem {
  clip_id: string;
  track_id: string;
  message: string;
}

export type NowSource = 'schedule' | 'show' | 'editor' | 'test' | 'idle';
export interface State {
  now: {
    source: NowSource;
    set_id: string | null;
    set_name: string | null;
    t: number;
    duration: number;
    paused: boolean;
  };
  schedule: {
    default_set: string | null;
    random_minutes: number;
    random_enabled: boolean;
    random_in: number | null;
  };
  output: { driver: string; ok: boolean; error: string | null; interface: string | null; fps: number };
  audio: { ok: boolean; error: string | null; device: string | null; device_name: string | null; volume: number; voices: number };
  sets_rev: number;
  media_rev: number;
  version: string;
}

export interface AudioDevice {
  id: string;
  name: string;
  default: boolean;
  channels: number;
}
export interface NetInterface {
  id: string;
  name: string;
  description: string;
  mac: string;
  up: boolean;
}

export type TestPattern = 'off' | 'panels' | 'white' | 'red' | 'green' | 'blue' | 'gradient';
export type ShowTarget = 'face0' | 'face1' | 'face2' | 'all' | 'faces';
export interface Peaks {
  rate: number;
  peaks: number[];
}

// ---- WebSocket messages
export type SignMessage = ({ type: 'state' } & State) | { type: string; [k: string]: unknown };
export interface EditorStatus {
  type: 'status';
  t: number;
  playing: boolean;
  live: boolean;
  duration: number;
  problems: Problem[];
}
export type EditorClientMessage =
  | { type: 'set'; set: SetData }
  | { type: 'play' }
  | { type: 'pause' }
  | { type: 'seek'; t: number }
  | { type: 'loop'; on: boolean; from?: number; to?: number }
  | { type: 'live'; on: boolean };
