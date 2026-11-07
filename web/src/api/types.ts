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

export type OutputDriver = 'colorlight' | 'fpga' | 'none';
export type ColorOrder = 'rgb' | 'rbg' | 'grb' | 'gbr' | 'brg' | 'bgr';

export interface OutputSettings {
  driver: OutputDriver;
  interface: string | null;
  /** fpga: '' = automatic (the card announces itself), else the card's address (pinned). */
  card_address: string;
  card_port: number;
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
  /** false: the sign dark and silent until turned back on (the show runs on unseen; test patterns still show). */
  sign_on: boolean;
}
export type DeepPartial<T> = { [K in keyof T]?: T[K] extends object ? (T[K] extends unknown[] ? T[K] : DeepPartial<T[K]>) : T[K] };
export type SettingsPatch = DeepPartial<Settings>;

export type MediaKind = 'video' | 'image' | 'audio';
/** What the crop was drawn for: one light (1:1), the whole sign (1:3), or no fixed shape. */
export type MediaShape = 'free' | 'light' | 'sign';
/** How an item's original is prepared for the sign (never destructive). */
export interface MediaEdit {
  /** Degrees clockwise, applied first. */
  rotate: 0 | 90 | 180 | 270;
  /** [x, y, w, h] as fractions (0..1) of the rotated picture; null = all of it. */
  crop: [number, number, number, number] | null;
  /** Trim: seconds from the original's first picture. */
  start: number;
  /** null = to the end. */
  end: number | null;
  shape: MediaShape;
}
export interface MediaItem {
  id: string;
  name: string;
  kind: MediaKind;
  status: 'processing' | 'ready' | 'error';
  /** With status "error": why. With "ready": the last change of the edit failed (the previous result still plays). */
  error: string | null;
  /** duration / width / height / fps / has_audio describe the prepared result (after rotate, crop, trim; size in
   * pixels of the original). While a new edit is applied they still describe the previous result. */
  duration: number;
  width: number;
  height: number;
  fps: number;
  has_audio: boolean;
  size: number;
  added: number;
  edit: MediaEdit;
  /** Number of the prepared files in use; grows each time they are re-made (0 = none yet). */
  version: number;
  source_duration: number;
  source_width: number;
  source_height: number;
  source_url: string | null;
  /** 0..1 while processing. */
  progress: number;
  /** While processing: "queued" (a link waiting for the download before it), "downloading", "waiting", "converting",
   * "preparing the crop view". */
  stage: string;
}
/** Answer of POST /api/media/probe: what is known about a link's video before downloading it. */
export interface MediaProbe {
  title: string | null;
  /** Seconds; null when it cannot be found out beforehand (direct file links, unknown sites). */
  duration: number | null;
  /** config.IMPORT_WARN_SECONDS: ask before downloading more than this. */
  warn_over: number;
}
/** Body of POST /api/media/import. */
export interface MediaImport {
  url: string;
  /** The "480" of 480p (shorter side); default 480 (config.IMPORT_HEIGHT). */
  max_height?: number;
  start?: number;
  duration?: number | null;
  name?: string | null;
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
  /** kind "color": the value may also be "transparent" (nothing drawn in it; what is below shows). */
  transparent?: boolean;
}
export interface ClipType {
  type: string;
  label: string;
  category: string;
  targets: string[];
  default_duration: number;
  description: string;
  params: ParamSpec[];
  /** False: the type always fills its whole box, whatever Clip.mask says (Off), so there is no mask to choose. */
  masked?: boolean;
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

/** One setup check (trafficlight/checks.py). error = the sign or the sound cannot work; warning = works, but
 * something is off; info = a neutral note or a hint; ok = fine. Ids are stable: output.driver, output.interface,
 * output.access, output.settings, output.link, output.send, output.receiver, output.card_size, output.wiring,
 * audio.backend, audio.device, server.web_ui; the custom firmware adds output.cards, output.firmware,
 * output.schedule, output.packets, output.timing. */
export type CheckLevel = 'error' | 'warning' | 'info' | 'ok';
export interface SetupCheck {
  id: string;
  level: CheckLevel;
  title: string;
  detail: string;
  fix: string | null;
}
/** A custom-firmware card that announced itself (docs/FIRMWARE.md 6.7). */
export interface FoundCard {
  ip: string;
  port: number;
  mac: string;
  uid: string;            // the flash's unique id, hex
  uid_valid: boolean;
  fixed_address: boolean; // built with a fixed address instead of the one from the id
  fw: string;
}
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
  output: {
    driver: string; ok: boolean; error: string | null; interface: string | null; fps: number;
    // fpga only: where the card is driven ("ip:port", null until found), whether it answers, the card in use
    // (auto: found by its announcements rather than set in the settings) and other cards heard.
    address?: string | null;
    answering?: boolean;
    found?: (FoundCard & { auto: boolean }) | null;
    others?: FoundCard[];
  };
  audio: { ok: boolean; error: string | null; device: string | null; device_name: string | null; volume: number; voices: number };
  checks: SetupCheck[];   // in a fixed order, most basic first
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

export type TestPattern = 'off' | 'panels' | 'white' | 'red' | 'green' | 'blue' | 'gradient' | 'wiring';
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
  /** The loop in effect (a range shorter than the minimum falls back to the whole set). */
  loop: { on: boolean; from: number; to: number };
  problems: Problem[];
}
export type EditorClientMessage =
  | { type: 'set'; set: SetData }
  | { type: 'play' }
  | { type: 'pause' }
  | { type: 'seek'; t: number }
  | { type: 'loop'; on: boolean; from?: number; to?: number }
  | { type: 'live'; on: boolean };

// ---- bundles (GET / POST /api/bundle)
/** sets = the chosen ones, all = every set, both with the media they use; everything = every set, all the
 * media and the show settings (schedule, light colours, off style; not this computer's output or sound). */
export type BundleKind = { kind: 'sets'; ids: string[] } | { kind: 'all' } | { kind: 'everything' };

/** What an upload did, by name. */
export interface BundleResult {
  sets_added: string[];
  sets_replaced: string[];
  /** Kept both: the bundle's set under a new id, named "… (imported)". */
  sets_copied: string[];
  sets_unchanged: string[];
  media_added: string[];
  media_updated: string[];
  media_unchanged: string[];
  /** "name: why" */
  media_failed: string[];
  settings: boolean;
}
