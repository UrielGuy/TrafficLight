// Typed wrappers for every REST route in docs/ARCHITECTURE.md ("HTTP API").
// Failures throw ApiError whose message is the server's `detail`.
import type {
  AudioDevice, Catalog, MediaEdit, MediaImport, MediaItem, NetInterface, NewSet, Peaks, Problem, SetData, SetSummary,
  Settings, SettingsPatch, ShowTarget, State, TestPattern,
} from './types';

export class ApiError extends Error {
  status: number;
  /** Parsed `detail` when it was not a plain string (e.g. the 409 on media delete). */
  detail: unknown;
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

function detailText(detail: unknown, fallback: string): string {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    // FastAPI validation errors: [{loc, msg}]
    return detail.map((d) => (d && typeof d === 'object' && 'msg' in d ? String((d as { msg: unknown }).msg) : String(d))).join('; ');
  }
  if (detail && typeof detail === 'object') return JSON.stringify(detail);
  return fallback;
}

async function request<T>(method: string, url: string, body?: unknown, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {};
  let payload: BodyInit | undefined;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(body);
  }
  let res: Response;
  try {
    res = await fetch(url, { method, headers, body: payload, ...init });
  } catch {
    throw new ApiError(0, 'Cannot reach the server');
  }
  if (!res.ok) {
    let detail: unknown;
    try {
      detail = (await res.json()).detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detailText(detail, `${res.status} ${res.statusText}`), detail);
  }
  return (await res.json()) as T;
}

const enc = encodeURIComponent;
type Ok = { ok: boolean };

export const api = {
  state: () => request<State>('GET', '/api/state'),
  catalog: () => request<Catalog>('GET', '/api/catalog'),

  listSets: () => request<SetSummary[]>('GET', '/api/sets'),
  createSet: (s: NewSet) => request<SetData>('POST', '/api/sets', s),
  getSet: (id: string) => request<SetData>('GET', `/api/sets/${enc(id)}`),
  putSet: (s: SetData) => request<SetData>('PUT', `/api/sets/${enc(s.id)}`, s),
  deleteSet: (id: string) => request<Ok>('DELETE', `/api/sets/${enc(id)}`),
  duplicateSet: (id: string) => request<SetData>('POST', `/api/sets/${enc(id)}/duplicate`),
  orderSets: (ids: string[]) => request<Ok>('POST', '/api/sets/order', { ids }),
  showSet: (id: string) => request<Ok>('POST', `/api/sets/${enc(id)}/show`),
  validate: (s: SetData) => request<{ problems: Problem[] }>('POST', '/api/validate', s),

  pause: () => request<Ok>('POST', '/api/transport/pause'),
  play: () => request<Ok>('POST', '/api/transport/play'),
  next: () => request<Ok>('POST', '/api/transport/next'),

  getSettings: () => request<Settings>('GET', '/api/settings'),
  patchSettings: (p: SettingsPatch) => request<Settings>('PATCH', '/api/settings', p),
  audioDevices: () => request<AudioDevice[]>('GET', '/api/audio/devices'),
  outputInterfaces: () => request<NetInterface[]>('GET', '/api/output/interfaces'),
  outputTest: (pattern: TestPattern) => request<Ok>('POST', '/api/output/test', { pattern }),

  listMedia: () => request<MediaItem[]>('GET', '/api/media'),
  uploadMedia: (file: File, name?: string) => {
    const fd = new FormData();
    fd.append('file', file);
    if (name) fd.append('name', name);
    return request<MediaItem>('POST', '/api/media', fd);
  },
  getMedia: (id: string) => request<MediaItem>('GET', `/api/media/${enc(id)}`),
  renameMedia: (id: string, name: string) => request<MediaItem>('PATCH', `/api/media/${enc(id)}`, { name }),
  /** A changed edit re-makes the prepared files: the item goes back to "processing" (the old result keeps playing). */
  editMedia: (id: string, edit: MediaEdit) => request<MediaItem>('PATCH', `/api/media/${enc(id)}`, { edit }),
  duplicateMedia: (id: string, name?: string) => request<MediaItem>('POST', `/api/media/${enc(id)}/duplicate`, name ? { name } : {}),
  importMedia: (body: MediaImport) => request<MediaItem>('POST', '/api/media/import', body),
  /** Without `force` the server answers 409 (ApiError.status === 409) when sets use the item. */
  deleteMedia: (id: string, force = false) => request<Ok>('DELETE', `/api/media/${enc(id)}${force ? '?force=1' : ''}`),
  mediaPeaks: (id: string) => request<Peaks>('GET', `/api/media/${enc(id)}/peaks`),
  showMedia: (id: string, target: ShowTarget, loop = true) =>
    request<Ok>('POST', `/api/media/${enc(id)}/show`, { target, loop }),
  makeSetFromMedia: (id: string, target: ShowTarget = 'all', loop = true) =>
    request<SetData>('POST', `/api/media/${enc(id)}/make-set`, { target, loop }),
};

// ---- URLs for <img>/<video>/<audio>
/** `rev`: anything that changes when the thumbnail does (the item's `version`, or `media_rev`), so caches let go. */
export const mediaThumbUrl = (id: string, rev?: number) => `/api/media/${enc(id)}/thumb.jpg${rev ? `?v=${rev}` : ''}`;
export const mediaFileUrl = (id: string) => `/api/media/${enc(id)}/file`;
/** The whole original picture for the crop tool (the original, or a browser copy of it). */
export const mediaSourceUrl = (id: string) => `/api/media/${enc(id)}/source`;
export const renderUrl = (setId: string, t = 0, rev?: number) =>
  `/api/render.png?set=${enc(setId)}&t=${t}${rev ? `&v=${rev}` : ''}`;

/** Like api.uploadMedia but reports progress (fetch cannot). */
export function uploadMediaWithProgress(file: File, onProgress: (fraction: number) => void): Promise<MediaItem> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/media');
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onerror = () => reject(new ApiError(0, 'Cannot reach the server'));
    xhr.onload = () => {
      let body: any;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* not JSON */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as MediaItem);
      else reject(new ApiError(xhr.status, detailText(body?.detail, `${xhr.status} ${xhr.statusText}`), body?.detail));
    };
    const fd = new FormData();
    fd.append('file', file);
    xhr.send(fd);
  });
}
