// The /ws/editor session: sends the draft and transport commands, receives status
// and preview frames. The playhead time lives in `clock` (read every animation
// frame, never through React state) so playback and scrubbing re-render nothing.
import { connectSocket, type Socket } from '../api/socket';
import type { EditorClientMessage, EditorStatus, Problem, SetData } from '../api/types';
import type { FrameFeed } from '../state/app';
import { createStore } from '../state/store';
import { toast } from '../state/toast';

export interface LoopRange {
  on: boolean;
  from: number;
  /** null = to the end of the set. */
  to: number | null;
}

export interface SessionInfo {
  connected: boolean;
  playing: boolean;
  live: boolean;
  /** Set when another editor took the session over: we stay disconnected until asked. */
  closed: string | null;
}

export const sessionStore = createStore<SessionInfo>({ connected: false, playing: false, live: true, closed: null });
export const loopStore = createStore<LoopRange>({ on: true, from: 0, to: null });
/** clip id -> problem messages, from the server's newest status. */
export const problemsStore = createStore<Map<string, string[]>>(new Map());
export const problemListStore = createStore<Problem[]>([]);

/** Playhead clock: server time extrapolated locally while playing. */
export const clock = {
  t: 0,
  at: 0,
  playing: false,
  duration: 0,
  loopFrom: 0,
  loopTo: 0,
  loopOn: true,
  /** While performance.now() < hold, status messages do not move the playhead (we just seeked). */
  hold: 0,
  now(): number {
    if (!this.playing) return this.t;
    let t = this.t + (performance.now() - this.at) / 1000;
    if (this.loopOn && this.loopTo > this.loopFrom && t >= this.loopTo) {
      t = this.loopFrom + ((t - this.loopFrom) % (this.loopTo - this.loopFrom));
    } else if (this.duration > 0 && t > this.duration) t = this.duration;
    return t;
  },
};

const frameSubs = new Set<(d: ArrayBuffer) => void>();
export const editorFeed: FrameFeed = {
  subscribe(cb) {
    frameSubs.add(cb);
    return () => frameSubs.delete(cb);
  },
};

const LIVE_PREF = 'trafficlight.editor.live';
const SEND_DELAY = 40;   // ms: coalesce bursts of edits
const SEND_MAX_WAIT = 150;

function readLivePref(): boolean {
  try {
    return localStorage.getItem(LIVE_PREF) !== '0';
  } catch {
    return true;
  }
}

export class EditorSession {
  private sock: Socket | null = null;
  private draft: SetData | null = null;
  private sentDraft: SetData | null = null;
  private timer: number | undefined;
  private firstPending = 0;
  private lastProblems = '';
  private lastErrorToast = -Infinity;
  private lastSeekSent = 0;
  private pendingSeek: number | null = null;
  private seekTimer: number | undefined;

  constructor() {
    sessionStore.set({ connected: false, playing: false, live: readLivePref(), closed: null });
    clock.t = 0;
    clock.playing = false;
    problemsStore.set(new Map());
    problemListStore.set([]);
    this.connect();
  }

  private connect() {
    this.sock = connectSocket('/ws/editor?fps=30', {
      onOpen: () => {
        sessionStore.set((s) => ({ ...s, connected: true }));
        this.sendLive();
        this.sendLoop();
        if (this.draft) this.flush();
        this.send({ type: 'seek', t: clock.now() });
        if (sessionStore.get().playing) this.send({ type: 'play' });
      },
      onClose: () => sessionStore.set((s) => ({ ...s, connected: false })),
      onJson: (m) => this.onJson(m),
      onFrame: (d) => frameSubs.forEach((f) => f(d)),
    });
  }

  private onJson(m: { type: string; [k: string]: unknown }) {
    if (m.type === 'status') this.onStatus(m as unknown as EditorStatus & { loop?: { on: boolean; from: number; to: number } });
    else if (m.type === 'closed') {
      // Another editor window took over. Do not reconnect by ourselves (that would steal it back).
      this.sock?.close();
      this.sock = null;
      sessionStore.set((s) => ({ ...s, connected: false, playing: false, closed: String(m.reason ?? 'Closed by the server') }));
      clock.t = clock.now();
      clock.playing = false;
    } else if (m.type === 'error') {
      // e.g. a draft the server cannot use: the preview stops following, so say so (not on every keystroke)
      const now = performance.now();
      if (now - this.lastErrorToast > 5000) {
        this.lastErrorToast = now;
        toast.error(`The sign controller did not take the last change: ${String(m.message ?? '')}`);
      }
    }
  }

  private onStatus(st: EditorStatus & { loop?: { on: boolean; from: number; to: number } }) {
    const now = performance.now();
    if (now >= clock.hold) {
      clock.t = st.t;
      clock.at = now;
      clock.playing = st.playing;
    }
    clock.duration = st.duration;
    if (st.loop) {
      clock.loopOn = st.loop.on;
      clock.loopFrom = st.loop.from;
      clock.loopTo = st.loop.to;
    }
    const cur = sessionStore.get();
    if (cur.playing !== st.playing || cur.live !== st.live) sessionStore.set({ ...cur, playing: st.playing, live: st.live });
    const pj = JSON.stringify(st.problems);
    if (pj !== this.lastProblems) {
      this.lastProblems = pj;
      const map = new Map<string, string[]>();
      for (const p of st.problems) map.set(p.clip_id, [...(map.get(p.clip_id) ?? []), p.message]);
      problemsStore.set(map);
      problemListStore.set(st.problems);
    }
  }

  private send(msg: EditorClientMessage) {
    return this.sock?.send(msg) ?? false;
  }

  /** The draft changed: send it soon (bursts coalesce, but never wait longer than SEND_MAX_WAIT). */
  setDraft(set: SetData) {
    this.draft = set;
    if (set === this.sentDraft) return;
    const now = performance.now();
    if (!this.firstPending) this.firstPending = now;
    window.clearTimeout(this.timer);
    const wait = Math.max(0, Math.min(SEND_DELAY, this.firstPending + SEND_MAX_WAIT - now));
    this.timer = window.setTimeout(() => this.flush(), wait);
  }

  private flush() {
    window.clearTimeout(this.timer);
    this.firstPending = 0;
    if (!this.draft) return;
    if (this.send({ type: 'set', set: this.draft })) this.sentDraft = this.draft;
  }

  play() {
    clock.t = clock.now();
    clock.at = performance.now();
    clock.playing = true;
    sessionStore.set((s) => ({ ...s, playing: true }));
    this.send({ type: 'play' });
  }
  pause() {
    clock.t = clock.now();
    clock.playing = false;
    sessionStore.set((s) => ({ ...s, playing: false }));
    this.send({ type: 'pause' });
  }
  toggle() {
    if (clock.playing) this.pause();
    else this.play();
  }

  /**
   * Moves the playhead. The local clock moves at once; the server gets at most one seek per
   * ~30 ms while scrubbing (the last position always goes out).
   */
  seek(t: number) {
    const d = clock.duration || this.draft?.duration || Infinity;
    t = Math.max(0, Math.min(t, d));
    clock.t = t;
    clock.at = performance.now();
    clock.hold = performance.now() + 400;
    this.pendingSeek = t;
    const since = performance.now() - this.lastSeekSent;
    if (since >= 30) this.sendSeek();
    else if (this.seekTimer === undefined) this.seekTimer = window.setTimeout(() => this.sendSeek(), 30 - since);
  }
  private sendSeek() {
    window.clearTimeout(this.seekTimer);
    this.seekTimer = undefined;
    if (this.pendingSeek === null) return;
    this.lastSeekSent = performance.now();
    clock.hold = performance.now() + 400;
    this.send({ type: 'seek', t: this.pendingSeek });
    this.pendingSeek = null;
  }

  setLoop(l: LoopRange) {
    loopStore.set(l);
    this.sendLoop();
  }
  private sendLoop() {
    const l = loopStore.get();
    this.send(l.to === null ? { type: 'loop', on: l.on, from: l.from } : { type: 'loop', on: l.on, from: l.from, to: l.to });
  }

  setLive(on: boolean) {
    try {
      localStorage.setItem(LIVE_PREF, on ? '1' : '0');
    } catch {
      /* ignore */
    }
    sessionStore.set((s) => ({ ...s, live: on }));
    this.sendLive();
  }
  private sendLive() {
    this.send({ type: 'live', on: sessionStore.get().live });
  }

  /** After another editor took over: take the session back. */
  reconnect() {
    sessionStore.set((s) => ({ ...s, closed: null }));
    this.sentDraft = null;
    if (!this.sock) this.connect();
  }

  close() {
    window.clearTimeout(this.timer);
    window.clearTimeout(this.seekTimer);
    this.sock?.close();
    this.sock = null;
  }
}
