// HTTP client for the control plane. Spec: docs/contracts/rest-api.md
//
// Every endpoint except GET /api/health carries `Authorization: Bearer <token>`.
// The token is never placed in a URL, never rendered into the DOM, and never logged.

const TOKEN_KEY = 'ambient.api.token';

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || '';
  } catch {
    return '';
  }
}

export function setToken(value) {
  try {
    localStorage.setItem(TOKEN_KEY, value);
  } catch {
    /* private mode: the token simply does not persist */
  }
}

export function clearToken() {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* nothing to do */
  }
}

export function hasToken() {
  return getToken().length > 0;
}

/** Headers for anything that is not routed through request(): SSE and hls.js. */
export function authHeaders(extra = {}) {
  const headers = { ...extra };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  return headers;
}

export class ApiError extends Error {
  constructor(status, error, detail) {
    super(detail || error || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.error = error;
    this.detail = detail;
  }
}

async function request(method, path, body, opts = {}) {
  const headers = { Accept: opts.accept || 'application/json' };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  const init = { method, headers, cache: 'no-store' };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }

  let res;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError(0, 'unreachable', 'Cannot reach the control plane.');
  }

  const text = res.status === 204 ? '' : await res.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { raw: text };
    }
  }

  if (!res.ok) {
    const error = (data && data.error) || `http_${res.status}`;
    const detail = (data && data.detail) || (data && data.raw) || res.statusText;
    throw new ApiError(res.status, error, String(detail || '').slice(0, 400));
  }
  // Endpoints that may answer in plain text hand back the body untouched; a log
  // tail that happens to parse as JSON must not be mistaken for an envelope.
  if (opts.raw) return { text, path: res.headers.get('X-Log-Path') || '' };
  return data;
}

async function requestBlob(path) {
  const headers = {};
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let res;
  try {
    res = await fetch(path, { headers, cache: 'no-store' });
  } catch {
    throw new ApiError(0, 'unreachable', 'Cannot reach the control plane.');
  }
  if (!res.ok) {
    let data = null;
    try { data = await res.json(); } catch { /* not a JSON response */ }
    throw new ApiError(
      res.status,
      (data && data.error) || `http_${res.status}`,
      (data && data.detail) || res.statusText,
    );
  }
  return res.blob();
}

const enc = encodeURIComponent;

function query(params) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : '';
}

export const api = {
  health: () => request('GET', '/api/health'),
  system: () => request('GET', '/api/system'),
  capacity: () => request('GET', '/api/capacity'),
  logs: ({ channel, service, lines }) =>
    request('GET', `/api/logs${query({ channel, service, lines })}`, undefined, {
      accept: 'text/plain, application/json;q=0.9',
      raw: true,
    }),

  channels: () => request('GET', '/api/channels'),
  channel: (name) => request('GET', `/api/channels/${enc(name)}`),
  createChannel: (body) => request('POST', '/api/channels', body),
  patchChannel: (name, body) => request('PATCH', `/api/channels/${enc(name)}`, body),
  deleteChannel: (name) => request('DELETE', `/api/channels/${enc(name)}`),
  start: (name) => request('POST', `/api/channels/${enc(name)}/start`),
  stop: (name) => request('POST', `/api/channels/${enc(name)}/stop`),
  restart: (name) => request('POST', `/api/channels/${enc(name)}/restart`),
  // Audio only, and free: the compositor reads a live Icecast mount and never
  // learns a track changed.
  skip: (name) => request('POST', `/api/channels/${enc(name)}/skip`),
  // Not live: the filtergraph is fixed at launch, so this restarts the channel.
  setResolution: (name, resolution) =>
    request('PUT', `/api/channels/${enc(name)}/resolution`, { resolution }),
  // Refused with 409 while the channel runs. `stream_key` is write-only: it travels
  // in the body only when the operator typed one, and is never read back.
  setDelivery: (name, body) => request('PUT', `/api/channels/${enc(name)}/delivery`, body),

  mediaAudio: () => request('GET', '/api/media/audio'),
  mediaImages: () => request('GET', '/api/media/images'),
  soundboard: (name) => request('GET', `/api/channels/${enc(name)}/soundboard`),
  previewSound: (name, clip) =>
    requestBlob(`/api/channels/${enc(name)}/soundboard/preview${query({ clip })}`),
  playSound: (name, clip) =>
    request('POST', `/api/channels/${enc(name)}/soundboard/play`, { clip }),
  stopSoundboard: (name) => request('POST', `/api/channels/${enc(name)}/soundboard/stop`),
  playlist: (name) => request('GET', `/api/channels/${enc(name)}/playlist`),
  play: (name, track) => request('POST', `/api/channels/${enc(name)}/play`, { track }),
  setPlaylist: (name, tracks) => request('PUT', `/api/channels/${enc(name)}/playlist`, { tracks }),
  images: (name) => request('GET', `/api/channels/${enc(name)}/images`),
  setImages: (name, slides) => request('PUT', `/api/channels/${enc(name)}/images`, { slides }),

  plugins: () => request('GET', '/api/plugins'),
  setVisualization: (name, active, allowRestart = false) =>
    request('PUT', `/api/channels/${enc(name)}/visualization${query({ allow_restart: allowRestart })}`, { active }),
  // Not live: the filtergraph is fixed at launch, so this lands on the next start.
  // `active` and `hot_set` are deliberately untouched, so the look comes back intact.
  setVisualizationEnabled: (name, enabled) =>
    request('PATCH', `/api/channels/${enc(name)}`, { visualization: { enabled } }),

  setHotSet: (name, body) =>
    request('PUT', `/api/channels/${enc(name)}/hot-set`, body),

  setVisible: (name, visible) =>
    request('PUT', `/api/channels/${enc(name)}/visualization/visible`, { visible }),

  setPluginParameters: (name, plugin, values) =>
    request('PUT', `/api/channels/${enc(name)}/visualization/parameters`, { plugin, values }),
  presets: () => request('GET', '/api/presets'),
  applyPreset: (name, preset) => request('POST', `/api/channels/${enc(name)}/preset`, { preset }),
  setColor: (name, color) => request('PUT', `/api/channels/${enc(name)}/color`, color),
};

// --------------------------------------------------------------------------
// Upload
//
// One request per file. fetch() cannot report how much of a request body has
// gone out, and per-file progress is the whole point here, so this is the one
// call that uses XMLHttpRequest.
//
// The contract does not yet name an upload route, so the first file probes the
// candidates below and the winner is reused for the rest of the session. A 404
// or 405 means "not this shape"; anything else is a real answer.
// --------------------------------------------------------------------------

const UPLOAD_ROUTES = [
  (kind) => `/api/media/${kind}`,
  (kind) => `/api/media/${kind}/upload`,
  () => '/api/media/upload',
];

let uploadRoute = null;

function post(path, form, { onProgress, onOpen } = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', path);
    xhr.setRequestHeader('Accept', 'application/json');
    const token = getToken();
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);

    if (onProgress) {
      xhr.upload.addEventListener('progress', (event) => {
        if (event.lengthComputable && event.total > 0) onProgress(event.loaded / event.total);
      });
    }
    xhr.addEventListener('load', () => {
      let data = null;
      if (xhr.responseText) {
        try {
          data = JSON.parse(xhr.responseText);
        } catch {
          data = { raw: xhr.responseText };
        }
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(data || {});
        return;
      }
      const error = (data && data.error) || `http_${xhr.status}`;
      const detail = (data && data.detail) || (data && data.raw) || xhr.statusText;
      reject(new ApiError(xhr.status, error, String(detail || '').slice(0, 400)));
    });
    xhr.addEventListener('error', () =>
      reject(new ApiError(0, 'unreachable', 'Cannot reach the control plane.')));
    xhr.addEventListener('abort', () =>
      reject(new ApiError(0, 'canceled', 'Upload canceled.')));

    if (onOpen) onOpen(() => xhr.abort());
    xhr.send(form);
  });
}

/**
 * Upload one file. `kind` is `audio` or `images`; `destination` is `common` or
 * `channel`, and `channel` is only sent for the latter so a backend that reads
 * one field and not the other cannot write to the wrong tree.
 */
export async function uploadMedia({ kind, destination, channel, file, onProgress, onOpen }) {
  const route = await resolveUploadRoute(kind);
  const form = new FormData();
  form.append('files', file, file.name);
  form.append('kind', kind);
  form.append('destination', destination);
  if (destination === 'channel' && channel) form.append('channel', channel);
  return post(route(kind), form, { onProgress, onOpen });
}

/**
 * Find the upload route once per session, with an empty body: a route that is
 * there answers "no files" or similar, a route that is not answers 404 or 405.
 * Probing with the file itself would re-send the bytes for every wrong guess.
 */
async function resolveUploadRoute(kind) {
  if (uploadRoute) return uploadRoute;
  for (const candidate of UPLOAD_ROUTES) {
    const probe = new FormData();
    probe.append('kind', kind);
    try {
      await post(candidate(kind), probe);
    } catch (err) {
      if (!(err instanceof ApiError)) throw err;
      if (err.status === 404 || err.status === 405) continue;
      // 0 is unreachable and 401 is a bad token: neither says anything about
      // whether this route exists, so nothing is remembered.
      if (err.status === 0 || err.status === 401) throw err;
    }
    uploadRoute = candidate;
    return uploadRoute;
  }
  throw new ApiError(404, 'no_upload_route',
    'This control plane exposes no media upload endpoint.');
}

/**
 * Normalize an upload response into one result per file. The contract does not
 * fix this shape, so a bare acknowledgement is read as "the file went in" and a
 * per-file list is read entry by entry.
 */
export function asUploadResults(data, fallbackName) {
  const list = asList(data, 'results', 'files', 'uploaded', 'items');
  const entries = list.length ? list : [data && typeof data === 'object' ? data : {}];

  return entries.map((entry) => {
    const item = entry && typeof entry === 'object' ? entry : { path: String(entry || '') };
    const status = String(item.status || '').toLowerCase();
    const error = item.error || (status && status !== 'ok' && status !== 'stored' && status !== 'accepted' ? status : '');
    const rejected = item.ok === false || item.accepted === false || item.rejected === true || Boolean(error);
    return {
      name: item.name || item.filename || item.file || fallbackName,
      path: item.path || item.stored || item.destination || '',
      ok: !rejected,
      error: rejected ? String(error || 'rejected') : '',
      detail: item.detail || item.reason || item.message || '',
    };
  });
}

// --------------------------------------------------------------------------
// Shape tolerance
//
// The contract fixes paths, status codes and the channel-status field names, but
// not whether a collection arrives bare or wrapped. These readers accept either
// so a wrapper key does not become a blank panel.
// --------------------------------------------------------------------------

export function asList(value, ...keys) {
  if (Array.isArray(value)) return value;
  if (value && typeof value === 'object') {
    for (const key of keys) {
      if (Array.isArray(value[key])) return value[key];
    }
  }
  return [];
}

/** `GET /api/media/*` — "common + per channel", grouped or flat. */
export function asMediaGroups(value, channel) {
  const groups = [];
  const push = (tree, items) => {
    const list = items.map((item) => (typeof item === 'string' ? { path: item } : item)).filter((i) => i && i.path);
    if (list.length) groups.push({ tree, items: list });
  };

  if (Array.isArray(value)) {
    push('all', value);
    return groups;
  }
  if (!value || typeof value !== 'object') return groups;

  if (Array.isArray(value.common)) push('common', value.common);

  const perChannel = value.channels && typeof value.channels === 'object' ? value.channels : null;
  if (perChannel && channel && Array.isArray(perChannel[channel])) {
    push('channel', perChannel[channel]);
  } else if (Array.isArray(value.channel)) {
    push('channel', value.channel);
  } else if (perChannel) {
    for (const [name, items] of Object.entries(perChannel)) {
      if (Array.isArray(items) && (!channel || name === channel)) push(name, items);
    }
  }

  if (!groups.length) {
    for (const key of ['files', 'items', 'audio', 'images']) {
      if (Array.isArray(value[key])) {
        push('all', value[key]);
        break;
      }
    }
  }
  return groups;
}

/**
 * `GET /api/logs` — a plain-text body, or JSON carrying `text` (or the older
 * `lines` array). Only log output is returned as text; the envelope keys are
 * never shown, because channel and service are already on screen.
 */
export function asLog(body) {
  if (body == null) return { text: '', path: '' };
  if (typeof body === 'string') return fromLogText(body, '');
  if (typeof body !== 'object') return { text: String(body), path: '' };

  const path = typeof body.path === 'string' ? body.path : '';
  for (const key of ['text', 'raw', 'output', 'log']) {
    if (typeof body[key] === 'string') return fromLogText(body[key], path);
  }
  const lines = asList(body, 'lines', 'log', 'entries');
  if (lines.length) {
    return { text: lines.map((l) => (typeof l === 'string' ? l : JSON.stringify(l))).join('\n'), path };
  }
  return { text: '', path };
}

function fromLogText(text, path) {
  if (text.slice(0, 200).trimStart().startsWith('{')) {
    try {
      const parsed = JSON.parse(text);
      if (parsed && typeof parsed === 'object') {
        const inner = asLog(parsed);
        return { text: inner.text, path: inner.path || path };
      }
    } catch {
      /* a log line that merely opens with a brace */
    }
  }
  return { text, path };
}
