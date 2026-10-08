// State, loading and live updates for the touch UI. Talks only to the REST + SSE
// contract in docs/contracts/rest-api.md, through the same client the desktop panel uses.

import { ApiError, api, asList, asMediaGroups, authHeaders, hasToken } from '../../api.js';
import { EventStream } from '../../events.js';
import { basename, channelTone } from '../../render.js';

export const STATUS_KEYS = [
  'state', 'health', 'uptime_seconds', 'current_track', 'next_track', 'current_slide',
  'visualization', 'visualization_enabled', 'encoder', 'encoder_requested', 'fps', 'speed',
  'bitrate_kbps', 'cpu_cores', 'liquidsoap_buffer', 'rtmp', 'hls',
];
const DETAIL_KEYS = ['fault', 'fault_detail', 'warnings'];

export const state = {
  channels: new Map(),
  details: new Map(),
  data: new Map(),
  plugins: [],
  presets: [],
  capacity: null,
  system: null,
  skipped: [],
  conn: 'idle',
  reachable: null,
  version: '',
  activity: [],
  jobs: new Map(),
  configErrors: new Map(),
  vizPending: new Map(),
  vizGenerations: new Map(),
  vizCompletions: new Map(),
  current: null,
  booted: false,
  channelsLoaded: false,
};

// --------------------------------------------------------------------------
// Change notification
// --------------------------------------------------------------------------

const listeners = new Set();

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function emit(topic, name) {
  for (const fn of listeners) fn(topic, name);
}

// --------------------------------------------------------------------------
// Errors
// --------------------------------------------------------------------------

let toastFn = () => {};
let tokenFn = () => {};

/** The UI layer hands in how to show a toast and how to ask for a token. */
export function bindUi({ toast, askToken }) {
  toastFn = toast;
  tokenFn = askToken;
}

export function toast(...args) {
  return toastFn(...args);
}

export function report(label, err) {
  if (err instanceof ApiError) {
    if (err.status === 401) {
      tokenFn('The control plane rejected that token.');
      toastFn('bad', label, 'unauthorized');
      return;
    }
    toastFn('bad', `${label} \u2014 ${err.error}`, err.detail);
    return;
  }
  toastFn('bad', label, err && err.message ? err.message : String(err));
}

/** Runs one API call; a failure is reported and comes back as undefined. */
export async function guard(label, fn, okMessage) {
  try {
    const result = await fn();
    if (okMessage) toastFn('ok', label, okMessage);
    return result;
  } catch (err) {
    report(label, err);
    return undefined;
  }
}

// --------------------------------------------------------------------------
// Per-channel working data
// --------------------------------------------------------------------------

function blankList() {
  return { saved: [], draft: [], watched: [], onair: [], undo: [] };
}

export function chanData(name) {
  let d = state.data.get(name);
  if (!d) {
    d = {
      playlist: blankList(),
      slides: blankList(),
      soundboard: { clips: [], selected: null },
      audioForm: null,
      slidesForm: null,
      schedule: null,
      color: null,
      opacity: null,
      params: {},
      delivery: null,
      detailLoaded: false,
      selectionLoaded: false,
      mediaLoaded: false,
      // Per channel: another channel's own files must never reach this one's picker.
      media: { audio: [], images: [] },
    };
    state.data.set(name, d);
  }
  return d;
}

export function isListDirty(list) {
  return JSON.stringify(list.saved) !== JSON.stringify(list.draft);
}

export function numberOr(value, fallback) {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

// --------------------------------------------------------------------------
// Channel config accessors
// --------------------------------------------------------------------------

/** GET /api/channels/{name} is "full config + status": nested or flattened. */
export function config(name) {
  const detail = state.details.get(name);
  if (!detail) return {};
  return detail.config && typeof detail.config === 'object' ? detail.config : detail;
}

export function vizEnabled(name) {
  const viz = config(name).visualization || {};
  return viz.enabled !== false;
}

export function channelState(name) {
  return (state.channels.get(name) || {}).state || '';
}

export function channelIsRunning(name) {
  return ['starting', 'running', 'degraded'].includes(channelState(name));
}

export function isStopped(name) {
  const s = channelState(name);
  return s === 'stopped' || s === 'failed' || !s;
}

export function resolutionOf(name) {
  if (!name) return '';
  const detail = state.details.get(name) || {};
  return String(config(name).resolution || detail.resolution || '');
}

export const ENCODERS = ['libx264', 'h264_nvenc', 'h264_qsv'];
export const TARGETS = ['youtube', 'video', 'audio'];

export function deliveryOf(name) {
  const detail = name ? state.details.get(name) || {} : {};
  const ch = (name && state.channels.get(name)) || {};
  const reported = detail.delivery || ch.delivery;
  return {
    rtmpUrl: String(detail.rtmp_url || ''),
    encoder: String(detail.encoder_requested || ch.encoder_requested || ''),
    fps: Number(detail.fps_requested) || 0,
    hasKey: Boolean(detail.has_stream_key),
    targets: Array.isArray(reported) && reported.length ? reported.map(String) : ['youtube'],
    local: String(detail.local || ch.local || ''),
    localHeight: Number(detail.local_height_requested) || 0,
    localFps: Number(detail.local_fps_requested) || 0,
    editable: Boolean(name) && (ch.state === 'stopped' || ch.state === 'failed'),
    feeds: Array.isArray(detail.feeds) ? detail.feeds : [],
  };
}

function normalizePlugin(entry) {
  if (typeof entry === 'string') return { name: entry, display_name: entry, parameters: [], available: true };
  const manifest = entry && entry.manifest && typeof entry.manifest === 'object' ? entry.manifest : entry || {};
  return {
    name: manifest.name || entry.name || '',
    display_name: manifest.display_name || entry.display_name || manifest.name || entry.name || '',
    description: manifest.description || entry.description || '',
    cost: manifest.cost || entry.cost || null,
    available: entry.available !== false,
    parameters: Array.isArray(manifest.parameters) ? manifest.parameters
      : Array.isArray(entry.parameters) ? entry.parameters : [],
  };
}

export function pluginLabel(name) {
  const plugin = state.plugins.find((p) => p.name === name);
  return (plugin && plugin.display_name) || name;
}

// --------------------------------------------------------------------------
// Loading
// --------------------------------------------------------------------------

export async function loadPlugins() {
  const data = await guard('plugins', () => api.plugins());
  if (data === undefined) return;
  state.plugins = asList(data, 'plugins', 'items').map(normalizePlugin).filter((p) => p.name);
  emit('plugins');
}

export async function loadPresets() {
  const data = await guard('presets', () => api.presets());
  if (data === undefined) return;
  state.presets = asList(data, 'presets', 'items')
    .map((p) => (typeof p === 'string' ? { name: p, display_name: p } : { ...p, name: p.name || '' }))
    .filter((p) => p.name);
  emit('presets');
}

export async function loadSystem() {
  const data = await guard('system', () => api.system());
  if (data !== undefined) state.system = data;
  return data;
}

export async function refreshCapacity() {
  try {
    state.capacity = await api.capacity();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return;
    state.capacity = null;
  }
  emit('capacity');
}

export async function pingHealth() {
  try {
    const health = await api.health();
    state.reachable = true;
    const version = String((health && health.version) || '').trim();
    state.version = !version || version === 'unknown' ? '' : version.startsWith('v') || version === 'mock' ? version : `v${version}`;
  } catch {
    state.reachable = false;
  }
  emit('conn');
}

/** The one place channel state is re-read wholesale: startup, refresh, every reconnect. */
export async function refreshChannels() {
  const data = await guard('channels', () => api.channels());
  if (data === undefined) return;

  const list = asList(data, 'channels', 'items')
    .map((entry) => (typeof entry === 'string' ? { name: entry } : entry))
    .filter((c) => c && c.name);

  // A channel whose config will not load is absent from `channels`; surface it
  // rather than letting it vanish, because that is the thing to notice.
  for (const broken of asList(data, 'errors')) {
    const name = broken && broken.channel;
    if (!name) continue;
    const detail = broken.detail || 'config failed to load';
    list.push({ name, state: 'failed', health: 'disconnected', config_error: detail });
    if (state.configErrors.get(name) !== detail) {
      state.configErrors.set(name, detail);
      logEvent({ tone: 'bad', channel: name, text: detail });
    }
  }

  const seen = new Set();
  for (const ch of list) {
    seen.add(ch.name);
    state.channels.set(ch.name, { ...(state.channels.get(ch.name) || {}), ...ch });
  }
  for (const name of [...state.channels.keys()]) {
    if (!seen.has(name)) {
      state.channels.delete(name);
      state.details.delete(name);
      emit('gone', name);
    }
  }

  state.skipped = asList(data, 'ignored', 'skipped', 'hidden')
    .map((entry) => (typeof entry === 'string' ? { name: entry } : {
      name: entry.name || entry.channel || entry.directory || entry.path || '',
    }))
    .filter((entry) => entry.name);
  state.channelsLoaded = true;
  emit('channels');
  if (state.current) loadChannelDetail(state.current);
}

let refreshTimer = null;

/** channel.status carries state/health/speed only, so a transition invalidates the rest. */
export function scheduleRefresh(delay = 400) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => refreshChannels(), delay);
}

export async function loadChannelDetail(name) {
  const data = await guard(`channel ${name}`, () => api.channel(name));
  if (data === undefined) return;
  state.details.set(name, data);
  const status = {};
  for (const key of [...STATUS_KEYS, ...DETAIL_KEYS]) if (key in data) status[key] = data[key];
  state.channels.set(name, { ...(state.channels.get(name) || { name }), ...status });
  chanData(name).detailLoaded = true;
  emit('detail', name);

  await Promise.all([loadSelection(name), loadMedia(name), loadSoundboard(name)]);
}

/** Keeps an unsaved draft: only a clean draft follows what the server now says. */
function adoptList(list, saved) {
  const wasClean = !isListDirty(list);
  list.saved = saved;
  if (wasClean) list.draft = [...saved];
}

export async function loadSelection(name) {
  const [tracks, slides] = await Promise.all([
    guard('playlist', () => api.playlist(name)),
    guard('images', () => api.images(name)),
  ]);
  const d = chanData(name);
  if (tracks !== undefined) {
    adoptList(d.playlist, asList(tracks, 'tracks', 'playlist', 'items').map(String));
    d.playlist.watched = asList(tracks, 'watched').map(String);
    const shown = asList(tracks, 'resolved').map(String);
    d.playlist.onair = asList(tracks, 'container_paths')
      .map((path, i) => ({ path: String(path), label: shown[i] || String(path) }));
  }
  if (slides !== undefined) {
    adoptList(d.slides, asList(slides, 'slides', 'images', 'items').map(String));
    d.slides.watched = asList(slides, 'watched').map(String);
  }
  d.selectionLoaded = true;
  emit('lists', name);
}

export async function loadMedia(name) {
  const [audio, images] = await Promise.all([
    guard('media/audio', () => api.mediaAudio()),
    guard('media/images', () => api.mediaImages()),
  ]);
  const d = chanData(name);
  if (audio !== undefined) d.media.audio = asMediaGroups(audio, name);
  if (images !== undefined) d.media.images = asMediaGroups(images, name);
  d.mediaLoaded = true;
  emit('media', name);
}

export async function loadSoundboard(name) {
  const data = await guard('soundboard', () => api.soundboard(name));
  if (data === undefined) return;
  const sb = chanData(name).soundboard;
  sb.clips = asList(data, 'clips').filter((clip) => clip && clip.container_path);
  if (!sb.clips.some((clip) => clip.container_path === sb.selected)) sb.selected = null;
  emit('soundboard', name);
}

// --------------------------------------------------------------------------
// Draft editing
//
// Edits to a playlist or slide list are held locally until saved, per channel, so
// leaving the screen does not lose them. Every edit keeps an undo step.
// --------------------------------------------------------------------------

function listOf(name, kind) {
  const d = chanData(name);
  return kind === 'audio' ? d.playlist : d.slides;
}

function remember(list) {
  list.undo.push([...list.draft]);
  if (list.undo.length > 50) list.undo.shift();
}

export function listMove(name, kind, from, to) {
  const list = listOf(name, kind);
  if (to < 0 || to >= list.draft.length || from === to) return;
  remember(list);
  const [moved] = list.draft.splice(from, 1);
  list.draft.splice(to, 0, moved);
  emit('drafts', name);
}

export function listRemove(name, kind, index) {
  const list = listOf(name, kind);
  if (index < 0 || index >= list.draft.length) return null;
  remember(list);
  const [removed] = list.draft.splice(index, 1);
  emit('drafts', name);
  return removed;
}

export function listAdd(name, kind, paths) {
  const list = listOf(name, kind);
  const add = [].concat(paths);
  if (!add.length) return;
  remember(list);
  list.draft.push(...add);
  emit('drafts', name);
}

export function listUndo(name, kind) {
  const list = listOf(name, kind);
  const previous = list.undo.pop();
  if (!previous) return false;
  list.draft = previous;
  emit('drafts', name);
  return true;
}

export function listRevert(name, kind) {
  const list = listOf(name, kind);
  list.draft = [...list.saved];
  list.undo = [];
  emit('drafts', name);
}

/** What a save would change, in words an operator can check before committing. */
export function listDiff(list) {
  const before = new Map();
  list.saved.forEach((p) => before.set(p, (before.get(p) || 0) + 1));
  const after = new Map();
  list.draft.forEach((p) => after.set(p, (after.get(p) || 0) + 1));
  let added = 0;
  let removed = 0;
  for (const [p, n] of after) added += Math.max(0, n - (before.get(p) || 0));
  for (const [p, n] of before) removed += Math.max(0, n - (after.get(p) || 0));
  const reordered = !added && !removed && isListDirty(list);
  return { added, removed, reordered };
}

export function audioFormOf(name) {
  const d = chanData(name);
  const audio = config(name).audio || {};
  return d.audioForm || { shuffle: Boolean(audio.shuffle), crossfade: numberOr(audio.crossfade_seconds, 5) };
}

export function audioFormDirty(name) {
  const d = chanData(name);
  if (!d.audioForm) return false;
  const audio = config(name).audio || {};
  return d.audioForm.shuffle !== Boolean(audio.shuffle)
    || d.audioForm.crossfade !== numberOr(audio.crossfade_seconds, 5);
}

export function setAudioForm(name, patch) {
  const d = chanData(name);
  d.audioForm = { ...audioFormOf(name), ...patch };
  emit('drafts', name);
}

export function slidesFormOf(name) {
  const d = chanData(name);
  const images = config(name).images || {};
  return d.slidesForm || {
    order: images.order || 'sequential',
    hold: numberOr(images.hold_seconds, 20),
    fade: numberOr(images.fade_seconds, 2),
  };
}

export function slidesFormDirty(name) {
  const d = chanData(name);
  if (!d.slidesForm) return false;
  const images = config(name).images || {};
  return d.slidesForm.order !== (images.order || 'sequential')
    || d.slidesForm.hold !== numberOr(images.hold_seconds, 20)
    || d.slidesForm.fade !== numberOr(images.fade_seconds, 2);
}

export function setSlidesForm(name, patch) {
  const d = chanData(name);
  d.slidesForm = { ...slidesFormOf(name), ...patch };
  emit('drafts', name);
}

export function audioDirty(name) {
  const d = chanData(name);
  return isListDirty(d.playlist) || audioFormDirty(name);
}

export function slidesDirty(name) {
  const d = chanData(name);
  return isListDirty(d.slides) || slidesFormDirty(name);
}

export function scheduleDirty(name) {
  const d = chanData(name);
  if (!d.schedule) return false;
  const saved = config(name).schedule || {};
  return JSON.stringify({ tz: d.schedule.tz, rules: d.schedule.rules })
    !== JSON.stringify({ tz: saved.timezone || 'UTC', rules: normalizedRules(saved.rules) });
}

export function normalizedRules(rules) {
  return (Array.isArray(rules) ? rules : []).map((r) => ({
    name: r.name || '',
    preset: r.preset || '',
    when: r.when || '',
    days: Array.isArray(r.days) ? [...r.days] : [],
    date: r.date || '',
  }));
}

export function anyDirty() {
  for (const [name] of state.data) {
    if (audioDirty(name) || slidesDirty(name) || scheduleDirty(name)) return true;
  }
  return false;
}

// --------------------------------------------------------------------------
// Activity log
// --------------------------------------------------------------------------

let activitySeq = 0;

export function logEvent({ tone = 'idle', channel, at, text, id, progress }) {
  const when = at ? new Date(at) : new Date();
  const time = Number.isNaN(when.getTime()) ? new Date() : when;
  let row = id ? state.jobs.get(id) : null;
  if (!row) {
    row = { key: (activitySeq += 1), tone, channel: channel || 'system', time, text, progress };
    state.activity.unshift(row);
    if (id) state.jobs.set(id, row);
    while (state.activity.length > 100) {
      const last = state.activity.pop();
      for (const [key, value] of state.jobs) if (value === last) state.jobs.delete(key);
    }
  } else {
    Object.assign(row, { tone, time, text, progress });
  }
  emit('activity');
  return row;
}

// --------------------------------------------------------------------------
// Live events
// --------------------------------------------------------------------------

export const stream = new EventStream({
  headers: (extra) => authHeaders(extra),
  onEvent: handleEvent,
  onState: setStreamState,
});

function setStreamState(status) {
  state.conn = status;
  if (status === 'connected') {
    state.vizPending.clear();
    state.vizGenerations.clear();
    state.vizCompletions.clear();
    refreshChannels();
  }
  if (status === 'unauthorized') tokenFn('The event stream rejected that token.');
  emit('conn');
}

function mergeStatus(name, payload) {
  if (!name) return;
  if (!state.channels.has(name)) {
    scheduleRefresh(0);
    return;
  }
  const previous = state.channels.get(name);
  const next = { ...previous };
  for (const key of STATUS_KEYS) {
    if (payload[key] !== undefined) next[key] = payload[key];
  }
  state.channels.set(name, next);
  if (previous.state !== next.state) scheduleRefresh();
  emit('status', name);
}

function omit(obj, keys) {
  const copy = { ...obj };
  for (const key of keys) delete copy[key];
  return copy;
}

function describe(data, keys) {
  for (const key of keys) {
    if (typeof data[key] === 'string' && data[key]) return data[key];
  }
  const rest = omit(data, ['channel', 'at']);
  const parts = Object.entries(rest).map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`);
  return parts.join(' ') || '(no detail)';
}

function handleEvent(name, payload) {
  const data = payload && typeof payload === 'object' ? payload : {};
  const channel = data.channel || null;

  switch (name) {
    case 'channel.status':
      mergeStatus(channel, data);
      logEvent({
        tone: channelTone({ state: data.state, health: data.health, speed: data.speed }),
        channel,
        at: data.at,
        text: `${data.state || '?'}${data.health ? ` / ${data.health}` : ''}`,
      });
      break;
    case 'channel.progress':
      mergeStatus(channel, data);
      break;
    case 'channel.track':
      mergeStatus(channel, {
        current_track: data.current_track || data.track,
        next_track: data.next_track || data.next,
      });
      logEvent({ tone: 'idle', channel, at: data.at, text: `track ${basename(data.current_track || data.track || '')}` });
      break;
    case 'channel.slide':
      mergeStatus(channel, { current_slide: data.current_slide || data.slide });
      break;
    case 'channel.visualization':
      handleVisualizationEvent(channel, data);
      break;
    case 'watchdog.event': {
      const text = describe(data, ['fault', 'action', 'reason', 'detail', 'message']);
      logEvent({ tone: data.recovered || data.action === 'recovered' ? 'warn' : 'bad', channel, at: data.at, text });
      toastFn(data.recovered ? 'warn' : 'bad', `watchdog${channel ? ` \u00B7 ${channel}` : ''}`, text);
      break;
    }
    case 'capacity.warning': {
      const text = describe(data, ['detail', 'message', 'reason']);
      logEvent({ tone: 'warn', channel, at: data.at, text });
      toastFn('warn', 'capacity', text);
      refreshCapacity();
      break;
    }
    case 'job.progress': {
      const id = data.id || data.job_id || data.job || 'job';
      const progress = typeof data.progress === 'number' ? data.progress : undefined;
      const status = data.status || data.state || '';
      const pct = progress === undefined ? '' : ` ${(progress * (progress <= 1 ? 100 : 1)).toFixed(0)}%`;
      logEvent({
        tone: status === 'failed' ? 'bad' : status === 'done' || status === 'complete' ? 'ok' : 'info',
        channel: data.channel,
        at: data.at,
        id,
        progress,
        text: `${data.kind || data.name || id}${status ? ` ${status}` : ''}${pct}`,
      });
      break;
    }
    default:
      logEvent({ tone: 'idle', channel, at: data.at, text: `${name} ${describe(data, [])}` });
  }
}

// --------------------------------------------------------------------------
// Visualization actions are asynchronous: the API answers with a generation and
// the compositor reports completion on the event stream.
// --------------------------------------------------------------------------

export function generationOf(result) {
  const generation = Number(result && result.generation);
  return Number.isInteger(generation) && generation > 0 ? generation : null;
}

/** Returns true when a completion event will announce the result. */
export function queueVizCompletion(name, result, action) {
  const generation = generationOf(result);
  if (generation === null) return false;
  const latest = state.vizGenerations.get(name) || 0;
  if (generation < latest) return true;
  state.vizGenerations.set(name, generation);
  state.vizPending.set(name, { ...action, generation, state: 'queued' });
  const key = `${name}:${generation}`;
  const completion = state.vizCompletions.get(key);
  if (completion) {
    state.vizCompletions.delete(key);
    queueMicrotask(() => handleVisualizationEvent(name, completion));
  }
  emit('viz', name);
  return true;
}

function handleVisualizationEvent(channel, data) {
  const generation = Number(data.generation);
  const hasGeneration = Number.isInteger(generation) && generation > 0;
  const latest = state.vizGenerations.get(channel) || 0;
  if (hasGeneration && generation < latest) return;
  if (hasGeneration && generation > latest) state.vizGenerations.set(channel, generation);

  const active = data.visualization || data.active;
  if (active) mergeStatus(channel, { visualization: active });
  const enabled = data.visualization_enabled !== undefined ? data.visualization_enabled : data.enabled;
  if (enabled !== undefined) mergeStatus(channel, { visualization_enabled: enabled });
  const cfg = config(channel);
  if (cfg.visualization) {
    if (active) cfg.visualization.active = active;
    if (enabled !== undefined) cfg.visualization.enabled = enabled;
    if (data.visible !== undefined) cfg.visualization.visible = data.visible;
    if (data.opacity !== undefined) cfg.visualization.opacity = data.opacity;
  }

  const pending = state.vizPending.get(channel);
  const matches = pending && hasGeneration && generation === pending.generation;
  const eventState = String(data.state || '').toLowerCase();
  const failed = Boolean(data.error) || ['error', 'failed', 'failure'].includes(eventState);
  const succeeded = data.applied === true
    || ['applied', 'complete', 'completed', 'succeeded', 'success'].includes(eventState);
  const terminal = failed || succeeded || ['superseded', 'canceled', 'cancelled'].includes(eventState);

  if (matches) {
    if (terminal) {
      state.vizPending.delete(channel);
      if (failed) toastFn('bad', pending.label, data.detail || data.error || 'visualizer action failed');
      else if (succeeded) toastFn('ok', pending.label, data.detail || pending.success);
      scheduleRefresh();
    } else {
      pending.state = eventState || 'pending';
    }
  } else if (hasGeneration && terminal) {
    state.vizCompletions.set(`${channel}:${generation}`, data);
    for (const key of state.vizCompletions.keys()) {
      const [savedChannel, savedGeneration] = key.split(':');
      if (savedChannel === channel && Number(savedGeneration) < generation) state.vizCompletions.delete(key);
    }
  }

  emit('viz', channel);
  logEvent({
    tone: data.error ? 'bad' : data.applied === true ? 'ok' : 'info',
    channel,
    at: data.at,
    text: data.detail || `visualization ${active || data.action || data.state || ''}`,
  });
}

// --------------------------------------------------------------------------
// Boot
// --------------------------------------------------------------------------

export async function boot() {
  if (state.booted) return;
  state.booted = true;
  pingHealth();
  await Promise.all([loadPlugins(), loadPresets(), refreshCapacity(), loadSystem()]);
  await refreshChannels();
  stream.start();
  setInterval(() => {
    if (hasToken()) refreshCapacity();
    pingHealth();
  }, 30000);
}

export function resetForToken() {
  state.booted = false;
  stream.stop();
}
