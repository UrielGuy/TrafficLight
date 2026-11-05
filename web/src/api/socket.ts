// WebSocket with automatic reconnect that understands both message kinds of the
// server: JSON text messages and binary image frames.

export interface SocketHandlers {
  onJson?: (msg: any) => void;
  /** One complete PNG/JPEG file. */
  onFrame?: (data: ArrayBuffer) => void;
  onOpen?: () => void;
  onClose?: () => void;
}

export interface Socket {
  send: (msg: unknown) => boolean;
  close: () => void;
  readonly connected: boolean;
}

export function wsUrl(path: string): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}${path}`;
}

/** Opens `path` (e.g. "/ws/sign?fps=20") and keeps it open, retrying with a growing delay (max 5 s).
 * `path` may be a function: it is asked again at every (re)connect. */
export function connectSocket(path: string | (() => string), h: SocketHandlers): Socket {
  let ws: WebSocket | null = null;
  let stopped = false;
  let delay = 500;
  let timer: number | undefined;
  let connected = false;

  const open = () => {
    ws = new WebSocket(wsUrl(typeof path === 'function' ? path() : path));
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => {
      delay = 500;
      connected = true;
      h.onOpen?.();
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        try {
          h.onJson?.(JSON.parse(ev.data));
        } catch {
          /* ignore malformed text */
        }
      } else {
        h.onFrame?.(ev.data as ArrayBuffer);
      }
    };
    ws.onclose = () => {
      const was = connected;
      connected = false;
      ws = null;
      if (was) h.onClose?.();
      if (!stopped) {
        timer = window.setTimeout(open, delay);
        delay = Math.min(delay * 1.7, 5000);
      }
    };
    ws.onerror = () => ws?.close();
  };
  open();

  return {
    send(msg) {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(typeof msg === 'string' ? msg : JSON.stringify(msg));
        return true;
      }
      return false;
    },
    close() {
      stopped = true;
      window.clearTimeout(timer);
      ws?.close();
    },
    get connected() {
      return connected;
    },
  };
}
