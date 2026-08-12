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

async function request(method, path, body) {
  const headers = { Accept: 'application/json' };
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
  return data;
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
  logs: ({ channel, service, lines }) => request('GET', `/api/logs${query({ channel, service, lines })}`),

  channels: () => request('GET', '/api/channels'),
  channel: (name) => request('GET', `/api/channels/${enc(name)}`),
  createChannel: (body) => request('POST', '/api/channels', body),
  patchChannel: (name, body) => request('PATCH', `/api/channels/${enc(name)}`, body),
  deleteChannel: (name) => request('DELETE', `/api/channels/${enc(name)}`),
  start: (name) => request('POST', `/api/channels/${enc(name)}/start`),
  stop: (name) => request('POST', `/api/channels/${enc(name)}/stop`),
  restart: (name) => request('POST', `/api/channels/${enc(name)}/restart`),

  mediaAudio: () => request('GET', '/api/media/audio'),
  mediaImages: () => request('GET', '/api/media/images'),
  playlist: (name) => request('GET', `/api/channels/${enc(name)}/playlist`),
  setPlaylist: (name, tracks) => request('PUT', `/api/channels/${enc(name)}/playlist`, { tracks }),
  images: (name) => request('GET', `/api/channels/${enc(name)}/images`),
  setImages: (name, slides) => request('PUT', `/api/channels/${enc(name)}/images`, { slides }),

  plugins: () => request('GET', '/api/plugins'),
  setVisualisation: (name, active) =>
    request('PUT', `/api/channels/${enc(name)}/visualisation`, { active }),
  presets: () => request('GET', '/api/presets'),
  applyPreset: (name, preset) => request('POST', `/api/channels/${enc(name)}/preset`, { preset }),
  setColour: (name, colour) => request('PUT', `/api/channels/${enc(name)}/colour`, colour),
};

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

export function asText(value) {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  if (typeof value.raw === 'string') return value.raw;
  if (typeof value.text === 'string') return value.text;
  const lines = asList(value, 'lines', 'log', 'entries');
  if (lines.length) {
    return lines.map((l) => (typeof l === 'string' ? l : JSON.stringify(l))).join('\n');
  }
  return JSON.stringify(value, null, 2);
}
