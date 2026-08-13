// Operator control panel. Talks only to the REST + SSE contract in
// docs/contracts/rest-api.md.

import {
  ApiError,
  api,
  asList,
  asLog,
  asMediaGroups,
  asUploadResults,
  authHeaders,
  clearToken,
  hasToken,
  setToken,
  uploadMedia,
} from './api.js';
import { EventStream } from './events.js';
import { Preview } from './preview.js';
import {
  $,
  availableRow,
  basename,
  buildCard,
  channelTone,
  clear,
  el,
  flatten,
  fmtBytes,
  fmtClock,
  fmtNum,
  fmtUptime,
  healthTone,
  makeSortable,
  metricTile,
  metricTone,
  pickRow,
  renderKv,
  stateTone,
  toast,
  uploadRow,
} from './render.js';

const DASH = '—';
const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

// Status fields the contract defines. Events are merged through this whitelist so
// envelope keys (channel, at) never end up rendered as status.
const STATUS_KEYS = [
  'state', 'health', 'uptime_seconds', 'current_track', 'next_track', 'current_slide',
  'visualization', 'visualization_enabled', 'encoder', 'encoder_requested', 'fps', 'speed',
  'bitrate_kbps', 'cpu_cores', 'liquidsoap_buffer', 'rtmp', 'hls',
];

// Fields GET /api/channels/{name} adds on top of the summary. They are not merged
// from SSE, which carries the status subset only.
const DETAIL_KEYS = ['fault', 'fault_detail', 'warnings'];

// Upload: everything that differs between the two media panels. The extension
// lists are a courtesy filter for the operator, not a check — the control plane
// probes the actual contents, because a name and a Content-Type are whatever the
// uploading side says they are.
const UPLOAD = {
  audio: {
    folder: 'audio',
    noun: 'audio file',
    extensions: ['.mp3', '.flac', '.ogg', '.opus', '.m4a', '.aac', '.wav'],
    largeBytes: 250 * 1024 * 1024,
    list: () => state.playlist,
    rerender: () => renderAudioTab(),
  },
  images: {
    folder: 'images',
    noun: 'image',
    extensions: ['.jpg', '.jpeg', '.png', '.webp', '.bmp'],
    largeBytes: 40 * 1024 * 1024,
    list: () => state.slides,
    rerender: () => renderSlidesTab(),
  },
};

const UPLOAD_CONCURRENCY = 2;

const state = {
  channels: new Map(),
  details: new Map(),
  cards: new Map(),
  selected: null,
  plugins: [],
  presets: [],
  media: { audio: [], images: [] },
  capacity: null,
  system: null,
  playlist: { saved: [], draft: [], watched: [] },
  slides: { saved: [], draft: [], watched: [] },
  // Held apart from the config so the 1 Hz progress render cannot reset the picker.
  resolutionDraft: null,
  // Same, for the visualization on/off toggle: an unapplied tick must survive a refresh.
  vizEnabledDraft: null,
  // Null means "no pending edit"; an array is a staged hot set awaiting Apply.
  hotSetDraft: null,
  // The channel the delivery form is filled in for; null until its detail lands.
  deliveryFor: null,
  // Paths the Add all buttons would append: what is visible and not yet selected.
  addable: { audio: [], images: [] },
  // Upload rows and the chosen tree live here, never re-derived from a render, so
  // a status refresh or a media reload cannot wipe a transfer in progress.
  uploads: { audio: [], images: [] },
  uploadDest: { audio: 'channel', images: 'channel' },
  scheduleRows: [],
  jobs: new Map(),
  configErrors: new Map(),
  booted: false,
};

const preview = new Preview($('preview-video'), (text, engine) => {
  $('preview-status').textContent = text;
  $('preview-engine').textContent = engine ? `engine: ${engine}` : '';
});

const stream = new EventStream({
  headers: (extra) => authHeaders(extra),
  onEvent: handleEvent,
  onState: setStreamState,
});

// --------------------------------------------------------------------------
// Errors
// --------------------------------------------------------------------------

function report(label, err) {
  if (err instanceof ApiError) {
    if (err.status === 401) {
      openTokenDialog('The control plane rejected that token.');
      toast('bad', label, 'unauthorized');
      return;
    }
    toast('bad', `${label} \u2014 ${err.error}`, err.detail);
    return;
  }
  toast('bad', label, err && err.message ? err.message : String(err));
}

async function guard(label, fn, okMessage) {
  try {
    const result = await fn();
    if (okMessage) toast('ok', label, okMessage);
    return result;
  } catch (err) {
    report(label, err);
    return undefined;
  }
}

// --------------------------------------------------------------------------
// Bootstrap
// --------------------------------------------------------------------------

async function main() {
  if (new URLSearchParams(location.search).has('mock')) {
    await import('./mock/mock-api.js');
    $('mock-badge').hidden = false;
  }
  wire();
  pingHealth();
  setInterval(pingHealth, 30000);

  if (!hasToken()) {
    openTokenDialog('Paste the token from the host\u2019s .env (AMBIENT_API_TOKEN).');
    return;
  }
  boot();
}

async function boot() {
  if (state.booted) return;
  state.booted = true;
  await Promise.all([loadPlugins(), loadPresets(), refreshCapacity(), loadSystem()]);
  await refreshChannels();
  stream.start();
  setInterval(() => {
    if (hasToken()) refreshCapacity();
  }, 30000);
}

async function pingHealth() {
  const dot = $('backend-dot');
  try {
    await api.health();
    dot.dataset.tone = 'ok';
    dot.title = 'control plane reachable';
  } catch {
    dot.dataset.tone = 'bad';
    dot.title = 'control plane unreachable';
  }
}

// --------------------------------------------------------------------------
// Loading
// --------------------------------------------------------------------------

async function loadPlugins() {
  const data = await guard('plugins', () => api.plugins());
  if (data === undefined) return;
  state.plugins = asList(data, 'plugins', 'items').map(normalizePlugin).filter((p) => p.name);
}

function normalizePlugin(entry) {
  if (typeof entry === 'string') return { name: entry, display_name: entry };
  const manifest = entry && entry.manifest && typeof entry.manifest === 'object' ? entry.manifest : entry || {};
  return {
    name: manifest.name || entry.name || '',
    display_name: manifest.display_name || entry.display_name || manifest.name || entry.name || '',
    description: manifest.description || entry.description || '',
    cost: manifest.cost || entry.cost || null,
    available: entry.available !== false,
    preview_url: entry.preview_url || manifest.preview_url || '',
  };
}

async function loadPresets() {
  const data = await guard('presets', () => api.presets());
  if (data === undefined) return;
  state.presets = asList(data, 'presets', 'items').map((p) =>
    typeof p === 'string' ? { name: p, display_name: p } : { ...p, name: p.name || '' },
  ).filter((p) => p.name);
}

async function loadSystem() {
  const data = await guard('system', () => api.system());
  if (data !== undefined) state.system = data;
}

async function refreshCapacity() {
  try {
    state.capacity = await api.capacity();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return;
    state.capacity = null;
  }
  renderTopbar();
}

/** The one place state is re-read wholesale: startup, Refresh, and every SSE (re)connect. */
async function refreshChannels() {
  const data = await guard('channels', () => api.channels());
  if (data === undefined) return;

  const list = asList(data, 'channels', 'items').map((entry) =>
    typeof entry === 'string' ? { name: entry } : entry,
  ).filter((c) => c && c.name);

  // A channel whose config will not load is absent from `channels`. Left alone it
  // would simply vanish from the panel, which is the one thing an operator must
  // not have to notice on their own.
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
      const card = state.cards.get(name);
      if (card) card.root.remove();
      state.cards.delete(name);
      if (state.selected === name) selectChannel(null);
    }
  }

  renderSkipped(data);
  renderChannelList();
  renderTopbar();
  if (state.selected) loadChannelDetail(state.selected);
}

/** Directories the backend skipped — the `example` template and anything like it. */
function renderSkipped(data) {
  const note = $('channel-skipped');
  const entries = asList(data, 'ignored', 'skipped', 'hidden').map((entry) =>
    typeof entry === 'string'
      ? { name: entry, reason: '' }
      : { name: entry.name || entry.channel || entry.directory || entry.path || '', reason: entry.reason || entry.detail || '' },
  ).filter((entry) => entry.name);

  note.hidden = entries.length === 0;
  if (!entries.length) return;
  note.textContent = `${entries.length} director${entries.length === 1 ? 'y' : 'ies'} skipped: ${entries.map((e) => e.name).join(', ')}`;
  note.title = entries.map((e) => (e.reason ? `${e.name} — ${e.reason}` : e.name)).join('\n');
}

async function loadChannelDetail(name) {
  const data = await guard(`channel ${name}`, () => api.channel(name));
  if (data === undefined) return;
  state.details.set(name, data);
  const status = {};
  for (const key of [...STATUS_KEYS, ...DETAIL_KEYS]) if (key in data) status[key] = data[key];
  state.channels.set(name, { ...(state.channels.get(name) || { name }), ...status });
  if (state.selected !== name) return;

  updateCard(name);
  renderDetail();
  renderLookTab();
  renderScheduleTab();
  await Promise.all([loadSelection(name), loadMedia(name)]);
  syncAudioForm();
  syncSlidesForm();
  renderAudioTab();
  renderSlidesTab();
}

async function loadSelection(name) {
  const [tracks, slides] = await Promise.all([
    guard('playlist', () => api.playlist(name)),
    guard('images', () => api.images(name)),
  ]);
  if (state.selected !== name) return;
  if (tracks !== undefined) {
    state.playlist.saved = asList(tracks, 'tracks', 'playlist', 'items').map(String);
    state.playlist.draft = [...state.playlist.saved];
    state.playlist.watched = asList(tracks, 'watched').map(String);
  }
  if (slides !== undefined) {
    state.slides.saved = asList(slides, 'slides', 'images', 'items').map(String);
    state.slides.draft = [...state.slides.saved];
    state.slides.watched = asList(slides, 'watched').map(String);
  }
}

async function loadMedia(name) {
  const [audio, images] = await Promise.all([
    guard('media/audio', () => api.mediaAudio()),
    guard('media/images', () => api.mediaImages()),
  ]);
  if (audio !== undefined) state.media.audio = asMediaGroups(audio, name);
  if (images !== undefined) state.media.images = asMediaGroups(images, name);
}

// --------------------------------------------------------------------------
// Channel config accessors
// --------------------------------------------------------------------------

/** GET /api/channels/{name} is "full config + status" — nested or flattened. */
function config(name) {
  const detail = state.details.get(name);
  if (!detail) return {};
  return detail.config && typeof detail.config === 'object' ? detail.config : detail;
}

function hotSet(name) {
  const viz = config(name).visualization;
  return Array.isArray(viz && viz.hot_set) ? viz.hot_set : [];
}

/**
 * Absent means on: an older config that predates the switch still draws. Read from
 * the config rather than the status field, which lags a save by one refresh and
 * would drag the toggle back under the operator.
 */
function vizEnabled(name) {
  const viz = config(name).visualization || {};
  return viz.enabled !== false;
}

// --------------------------------------------------------------------------
// Channel list
// --------------------------------------------------------------------------

function renderChannelList() {
  const host = $('channel-list');
  const names = [...state.channels.keys()].sort();

  for (const name of names) {
    if (!state.cards.has(name)) {
      state.cards.set(name, buildCard(name, {
        onSelect: selectChannel,
        onStart: (n) => lifecycle(n, 'start'),
        onStop: (n) => lifecycle(n, 'stop'),
        onRestart: (n) => lifecycle(n, 'restart'),
      }));
    }
    const card = state.cards.get(name);
    if (card.root.parentNode !== host) host.append(card.root);
    card.update(state.channels.get(name), state.selected === name);
  }

  $('channel-count').textContent = names.length ? String(names.length) : '';
  $('channel-empty').hidden = names.length > 0;
}

function updateCard(name) {
  const card = state.cards.get(name);
  if (card) card.update(state.channels.get(name) || { name }, state.selected === name);
}

function selectChannel(name) {
  if (state.selected === name) return;
  if (preview.channel && preview.channel !== name) preview.stop();
  const previous = state.selected;
  state.selected = name;
  if (previous) updateCard(previous);

  $('detail-empty').hidden = Boolean(name);
  $('detail-body').hidden = !name;
  if (!name) return;

  updateCard(name);
  state.playlist = { saved: [], draft: [], watched: [] };
  state.slides = { saved: [], draft: [], watched: [] };
  state.resolutionDraft = null;
  state.vizEnabledDraft = null;
  state.hotSetDraft = null;
  resetDeliveryForm();
  renderDetail();
  loadChannelDetail(name);
}

async function lifecycle(name, action) {
  await guard(`${action} ${name}`, () => api[action](name), 'accepted');
  scheduleRefresh(500);
}

let refreshTimer = null;

/** channel.status carries state/health/speed only, so a transition invalidates the rest. */
function scheduleRefresh(delay = 400) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => refreshChannels(), delay);
}

// --------------------------------------------------------------------------
// Detail: header + overview
// --------------------------------------------------------------------------

function renderDetail() {
  const name = state.selected;
  if (!name) return;
  const ch = state.channels.get(name) || { name };
  const cfg = config(name);
  const tone = channelTone(ch);

  $('detail-name').textContent = name;
  $('detail-stripe').dataset.tone = tone;
  $('detail-state').textContent = ch.state || 'unknown';
  $('detail-state').dataset.tone = stateTone(ch.state);
  $('detail-health').textContent = ch.health || DASH;
  $('detail-health').dataset.tone = healthTone(ch.health);
  $('detail-health').hidden = !ch.health;
  $('detail-genre').textContent = cfg.genre || '';

  const stopped = ch.state === 'stopped' || ch.state === 'failed' || !ch.state;
  $('btn-start').disabled = !stopped;
  $('btn-stop').disabled = ch.state === 'stopped';
  $('btn-restart').disabled = ch.state === 'stopped';

  renderOverview(ch, cfg);
  syncDeleteButton();
}

function renderOverview(ch, cfg) {
  // The status field reports the selected plugin whether or not it is being drawn.
  const vizOff = state.selected && !vizEnabled(state.selected);
  renderKv($('onair-kv'), [
    ['track', ch.current_track ? basename(ch.current_track) : DASH],
    ['next', ch.next_track ? basename(ch.next_track) : DASH],
    ['slide', ch.current_slide ? basename(ch.current_slide) : DASH],
    ['visualization', ch.visualization || DASH, vizOff ? '(off \u2014 selected, not rendering)' : ''],
    ['preset', cfg.preset || 'none'],
    ['color', cfg.color ? cfg.color.mode : DASH],
  ]);

  const substituted = Boolean(ch.encoder_requested && ch.encoder && ch.encoder_requested !== ch.encoder);
  const warnings = Array.isArray(ch.warnings) ? ch.warnings : [];
  renderKv($('pipeline-kv'), [
    ['encoder', ch.encoder || DASH, substituted ? `(requested ${ch.encoder_requested} \u2014 probe failed)` : ''],
    ['resolution', resolutionOf(state.selected) || DASH],
    ['rtmp', ch.rtmp || DASH],
    ['hls', ch.hls || DASH],
    ['liquidsoap', ch.liquidsoap_buffer || DASH],
    ['uptime', ch.state === 'running' ? fmtUptime(ch.uptime_seconds) : DASH],
    ...(ch.fault ? [['fault', ch.fault, ch.fault_detail || '']] : []),
    ...(ch.config_error ? [['config', ch.config_error]] : []),
    ...(warnings.length ? [['warnings', warnings.join(' \u00B7 ')]] : []),
  ]);

  syncResolution();
  syncDelivery();
  syncPreviewLink();


  const grid = $('metric-grid');
  clear(grid);
  grid.append(
    metricTile('speed', fmtNum(ch.speed, 3, 'x'), metricTone('speed', ch.speed)),
    metricTile('fps', fmtNum(ch.fps, 1)),
    metricTile('bitrate', typeof ch.bitrate_kbps === 'number' ? `${Math.round(ch.bitrate_kbps)}k` : DASH),
    metricTile('cores', fmtNum(ch.cpu_cores, 2)),
  );
}

/** Resolution lives in the channel .env, so it may be reported flat or under config. */
function resolutionOf(name) {
  if (!name) return '';
  const detail = state.details.get(name) || {};
  const cfg = config(name);
  return String(cfg.resolution || detail.resolution || '');
}

function syncResolution() {
  const select = $('output-resolution');
  const current = resolutionOf(state.selected);
  select.value = state.resolutionDraft || current || '720p';
  $('btn-resolution-apply').disabled = !current || select.value === current;
}

async function applyResolution() {
  const name = state.selected;
  const target = $('output-resolution').value;
  const current = resolutionOf(name);
  if (!name || !target || target === current) return;

  // A stopped channel has no stream to interrupt: it simply starts at the new size.
  const live = (state.channels.get(name) || {}).state !== 'stopped';
  if (live) {
    const ok = await confirmRestart({
      title: `Change ${name} to ${target}?`,
      body: `The compositor is currently encoding at ${current}. Resolution is compiled into the `
        + 'filtergraph when FFmpeg launches, so it cannot be changed on a running graph the way a '
        + 'color or a visualization can.',
      cost: 'The channel restarts make-before-break: roughly a 1s gap on air, and a new YouTube ingest session.',
      note: 'Higher resolutions cost substantially more CPU per channel. Check headroom under System '
        + 'before moving up.',
      okText: `Restart ${name} at ${target}`,
    });
    if (!ok) {
      state.resolutionDraft = null;
      syncResolution();
      return;
    }
  }

  await guard(`resolution ${name}`, () => api.setResolution(name, target),
    live ? `restarting at ${target}` : `${target} \u2014 takes effect on the next start`);
  state.resolutionDraft = null;
  scheduleRefresh(800);
}

// --------------------------------------------------------------------------
// Delivery: stream key, RTMP URL, encoder, fps
//
// `.env` settings, so they only reach a new container. The control plane refuses
// them outright while the channel runs rather than moving a live broadcast, and
// the form says so instead of offering a submit that would come back 409.
// --------------------------------------------------------------------------

const ENCODERS = ['libx264', 'h264_nvenc', 'h264_qsv'];

const DELIVERY_INPUTS = [
  'delivery-key', 'delivery-rtmp', 'delivery-encoder', 'delivery-fps', 'delivery-fps-default',
];

/** rtmp_url, has_stream_key and fps_requested come from GET /api/channels/{name}. */
function deliveryOf(name) {
  const detail = name ? state.details.get(name) || {} : {};
  const ch = (name && state.channels.get(name)) || {};
  return {
    rtmpUrl: String(detail.rtmp_url || ''),
    encoder: String(detail.encoder_requested || ch.encoder_requested || ''),
    // Not ch.fps: that is the measured rate, and a stopped channel reports 0.
    fps: Number(detail.fps_requested) || 0,
    hasKey: Boolean(detail.has_stream_key),
    editable: Boolean(name) && (ch.state === 'stopped' || ch.state === 'failed'),
  };
}

/** Only what the operator actually changed, so an untouched key is never overwritten. */
function deliveryBody() {
  const base = deliveryOf(state.selected);
  const body = {};

  const key = $('delivery-key').value.trim();
  if (key) body.stream_key = key;

  const url = $('delivery-rtmp').value.trim();
  // An empty box means "unchanged": the contract has no way to clear an RTMP URL.
  if (url && url !== base.rtmpUrl) body.rtmp_url = url;

  const encoder = $('delivery-encoder').value;
  if (!encoder) {
    if (base.encoder) body.clear_encoder = true;
  } else if (encoder !== base.encoder) {
    body.encoder = encoder;
  }

  if ($('delivery-fps-default').checked) {
    body.clear_fps = true;
  } else {
    const fps = Number($('delivery-fps').value);
    if (fps && fps !== base.fps) body.fps = fps;
  }
  return body;
}

/**
 * The relay's own HLS URL, shown under the preview for VLC and friends. The
 * in-page player goes through the authenticated proxy instead; an external
 * player cannot send the bearer token, so it needs the published relay port.
 */
function syncPreviewLink() {
  const row = $('preview-link-row');
  const note = $('preview-link-note');
  const detail = state.details.get(state.selected) || {};
  const url = detail.hls_url;

  if (!url) {
    row.hidden = true;
    note.hidden = false;
    note.textContent = 'The relay\u2019s HLS port is not published, so there is no address an '
      + 'external player could reach. Set AMBIENT_HLS_PUBLISH to expose one.';
    return;
  }

  note.hidden = true;
  row.hidden = false;
  $('preview-url').textContent = url;
}

async function copyPreviewUrl() {
  const url = $('preview-url').textContent;
  if (!url) return;
  try {
    await navigator.clipboard.writeText(url);
    toast('ok', 'copied', 'paste it into VLC with Media \u203A Open Network Stream');
  } catch {
    // Clipboard access is refused on insecure origins, which is exactly where
    // this UI usually runs, so fall back to selecting the text.
    const range = document.createRange();
    range.selectNodeContents($('preview-url'));
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    toast('warn', 'copy', 'the browser refused clipboard access \u2014 the URL is selected, press Ctrl+C');
  }
}

function syncDelivery() {
  const name = state.selected;
  const base = deliveryOf(name);
  // Before the detail load lands there is no baseline to compare against, so the
  // form is not "dirty" — it is simply not filled in yet.
  const dirty = state.deliveryFor === name && Object.keys(deliveryBody()).length > 0;

  // Repopulating a dirty form would throw away what the operator is typing, and
  // the 1 Hz progress render calls through here.
  if (!dirty) {
    setValue($('delivery-rtmp'), base.rtmpUrl);
    setValue($('delivery-encoder'), ENCODERS.includes(base.encoder) ? base.encoder : '');
    setValue($('delivery-fps'), base.fps ? String(base.fps) : '');
    state.deliveryFor = base.encoder ? name : null;
  }

  const keyState = $('delivery-key-state');
  keyState.textContent = base.hasKey ? 'key set' : 'no key';
  keyState.dataset.tone = base.hasKey ? 'ok' : 'warn';

  $('delivery-effective').textContent =
    `In effect: ${base.encoder || DASH} at ${base.fps || DASH} fps. "Use default" hands the setting `
    + 'back to the global one, and the answer says what that resolved to.';

  const lock = $('delivery-lock');
  lock.hidden = base.editable;
  lock.textContent = name ? `stop ${name} to change where it publishes` : '';
  for (const id of DELIVERY_INPUTS) $(id).disabled = !base.editable;
  $('delivery-fps').disabled = !base.editable || $('delivery-fps-default').checked;
  $('btn-delivery-apply').disabled = !base.editable || !dirty;
}

/** Assigning an identical value can still move the caret in a focused field. */
function setValue(input, value) {
  if (input.value !== value) input.value = value;
}

function resetDeliveryForm() {
  $('delivery-key').value = '';
  $('delivery-fps-default').checked = false;
  state.deliveryFor = null;
}

async function applyDelivery() {
  const name = state.selected;
  const body = deliveryBody();
  if (!name || !Object.keys(body).length) return;

  const result = await guard(`delivery ${name}`, () => api.setDelivery(name, body));
  if (result === undefined) {
    // The typed key stays in its password field and out of every message.
    syncDelivery();
    return;
  }

  const changed = Array.isArray(result.changed) ? result.changed : [];
  if (!changed.length) {
    toast('warn', `delivery ${name}`, result.detail || 'nothing to change');
  } else {
    toast('ok', `delivery ${name} \u2014 ${changed.join(', ')}`,
      `now ${result.encoder} at ${result.fps} fps \u00B7 ${result.detail || 'applies on the next start'}`);
  }
  // The response is the new baseline. Waiting for the refresh instead would leave
  // the form comparing against what it just replaced, i.e. dirty against itself.
  const detail = state.details.get(name);
  if (detail) {
    state.details.set(name, {
      ...detail,
      rtmp_url: result.rtmp_url,
      has_stream_key: result.has_stream_key,
      encoder_requested: result.encoder,
      fps_requested: result.fps,
    });
  }
  resetDeliveryForm();
  scheduleRefresh(200);
  syncDelivery();
}

function syncDeleteButton() {
  const name = state.selected;
  const ch = name ? state.channels.get(name) || {} : {};
  const typed = $('delete-confirm').value.trim();
  $('btn-delete').disabled = !name || ch.state !== 'stopped' || typed !== name;
}

// --------------------------------------------------------------------------
// Detail: audio + slides
// --------------------------------------------------------------------------

function treeOf(path) {
  if (path.startsWith('common/')) return 'common';
  if (path.startsWith('channels/')) return 'channel';
  return '';
}

function renderOrderedList(listEl, items, onChange) {
  clear(listEl);
  if (!items.length) {
    listEl.append(el('li', { class: 'muted small', text: 'nothing selected' }));
    return;
  }
  items.forEach((path, index) => {
    listEl.append(pickRow(path, index, treeOf(path), {
      onMove: (from, delta) => {
        const to = from + delta;
        if (to < 0 || to >= items.length) return;
        const [moved] = items.splice(from, 1);
        items.splice(to, 0, moved);
        onChange();
      },
      onRemove: (i) => {
        items.splice(i, 1);
        onChange();
      },
    }));
  });
}

/** Renders the right-hand column and reports what an Add all would append, in list order. */
function renderAvailableList(listEl, groups, used, filterText, onAdd) {
  clear(listEl);
  const usedSet = new Set(used);
  const needle = filterText.trim().toLowerCase();
  const addable = [];
  let count = 0;
  let total = 0;
  for (const group of groups) {
    for (const item of group.items) {
      total += 1;
      if (needle && !item.path.toLowerCase().includes(needle)) continue;
      const isUsed = usedSet.has(item.path);
      listEl.append(availableRow(item.path, group.tree, isUsed, onAdd));
      if (!isUsed) addable.push(item.path);
      count += 1;
    }
  }
  if (!count) listEl.append(el('li', { class: 'muted small', text: 'no media matches' }));
  return { addable, filtered: Boolean(needle), total };
}

function syncAddAll(button, { addable, filtered, total }, noun) {
  button.disabled = addable.length === 0;
  button.textContent = addable.length ? `Add all ${addable.length}` : 'Add all';
  button.title = addable.length === 0
    ? `Every available ${noun} is already in the list.`
    : filtered
      ? `Appends the ${addable.length} unselected ${noun}s matching the filter (${total} available in total). Revert undoes it.`
      : `Appends all ${addable.length} unselected ${noun}s in the order shown. Revert undoes it.`;
}

function renderAudioTab() {
  const name = state.selected;
  if (!name) return;

  renderOrderedList($('playlist-selected'), state.playlist.draft, () => renderAudioTab());
  const available = renderAvailableList($('audio-available'), state.media.audio, state.playlist.draft, $('audio-filter').value, (path) => {
    state.playlist.draft.push(path);
    renderAudioTab();
  });
  state.addable.audio = available.addable;
  syncAddAll($('btn-playlist-add-all'), available, 'track');

  $('playlist-count').textContent = String(state.playlist.draft.length);
  $('playlist-dirty').hidden = !isDirty(state.playlist);
  syncUpload('audio');
}

// Form fields are seeded from config on load only. Re-deriving them on every list
// render would discard a value the operator typed before touching the list.
function syncAudioForm() {
  const audio = config(state.selected).audio || {};
  if (document.activeElement !== $('audio-shuffle')) $('audio-shuffle').checked = Boolean(audio.shuffle);
  if (document.activeElement !== $('audio-crossfade')) {
    $('audio-crossfade').value = numberOr(audio.crossfade_seconds, 5);
  }
}

function renderSlidesTab() {
  const name = state.selected;
  if (!name) return;

  renderOrderedList($('slides-selected'), state.slides.draft, () => renderSlidesTab());
  const available = renderAvailableList($('image-available'), state.media.images, state.slides.draft, $('image-filter').value, (path) => {
    state.slides.draft.push(path);
    renderSlidesTab();
  });
  state.addable.images = available.addable;
  syncAddAll($('btn-slides-add-all'), available, 'image');

  $('slides-count').textContent = String(state.slides.draft.length);
  $('slides-dirty').hidden = !isDirty(state.slides);
  syncUpload('images');
}

/** Appends rather than replaces, so the operator's existing order survives. */
function addAll(kind) {
  const pair = kind === 'audio' ? state.playlist : state.slides;
  const paths = state.addable[kind === 'audio' ? 'audio' : 'images'];
  if (!paths.length) return;
  const already = new Set(pair.draft);
  const added = paths.filter((path) => !already.has(path));
  pair.draft.push(...added);
  if (kind === 'audio') renderAudioTab();
  else renderSlidesTab();
  toast('ok', `added ${added.length} ${kind === 'audio' ? 'tracks' : 'slides'}`,
    'not saved yet — Revert undoes it, Save writes the whole ordered list');
}

function syncSlidesForm() {
  const images = config(state.selected).images || {};
  if (document.activeElement !== $('slides-order')) $('slides-order').value = images.order || 'sequential';
  if (document.activeElement !== $('slides-hold')) $('slides-hold').value = numberOr(images.hold_seconds, 20);
  if (document.activeElement !== $('slides-fade')) $('slides-fade').value = numberOr(images.fade_seconds, 2);
}

function isDirty(pair) {
  return JSON.stringify(pair.saved) !== JSON.stringify(pair.draft);
}

function numberOr(value, fallback) {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

async function savePlaylist() {
  const name = state.selected;
  if (!name) return;
  // PATCH first, then PUT. The list PUT is authoritative, so a server that
  // replaces the whole `audio` object on PATCH cannot drop the tracks.
  await guard('save playlist', async () => {
    await api.patchChannel(name, {
      audio: {
        shuffle: $('audio-shuffle').checked,
        crossfade_seconds: Number($('audio-crossfade').value),
      },
    });
    await api.setPlaylist(name, state.playlist.draft);
  }, `${state.playlist.draft.length} tracks`);
  await loadChannelDetail(name);
}

async function saveSlides() {
  const name = state.selected;
  if (!name) return;
  await guard('save slides', async () => {
    await api.patchChannel(name, {
      images: {
        order: $('slides-order').value,
        hold_seconds: Number($('slides-hold').value),
        fade_seconds: Number($('slides-fade').value),
      },
    });
    await api.setImages(name, state.slides.draft);
  }, `${state.slides.draft.length} slides`);
  await loadChannelDetail(name);
}

// --------------------------------------------------------------------------
// Detail: media upload
//
// One row per file, updated in place. Rows live in state.uploads and in a list
// no render function clears, so a status refresh arriving mid-transfer cannot
// take the panel away from under the operator.
// --------------------------------------------------------------------------

let uploadSeq = 0;

function destPath(kind, destination, channel) {
  const folder = UPLOAD[kind].folder;
  return destination === 'common' ? `common/${folder}/` : `channels/${channel || '\u2026'}/${folder}/`;
}

function finished(entry) {
  return entry.status !== 'queued' && entry.status !== 'uploading';
}

function syncUpload(kind) {
  const dest = state.uploadDest[kind];
  const zone = $(`${kind}-dropzone`);
  zone.dataset.dest = dest;
  $(`${kind}-dz-tree`).textContent = dest === 'common' ? 'common' : 'channel';
  $(`${kind}-dz-path`).textContent = destPath(kind, dest, state.selected);
  $(`${kind}-dest-channel`).textContent = destPath(kind, 'channel', state.selected);
  const radio = document.querySelector(`input[name="${kind}-dest"][value="${dest}"]`);
  if (radio) radio.checked = true;
  $(`btn-${kind}-upload-clear`).hidden = !state.uploads[kind].some(finished);
}

function enqueue(kind, files) {
  const spec = UPLOAD[kind];
  const destination = state.uploadDest[kind];
  const channel = state.selected;
  const list = [...(files || [])];
  if (!list.length) return;
  if (destination === 'channel' && !channel) {
    toast('warn', 'upload', 'Select a channel first, or upload to the shared library.');
    return;
  }

  let large = 0;
  for (const file of list) {
    const known = spec.extensions.some((ext) => file.name.toLowerCase().endsWith(ext));
    const entry = {
      id: (uploadSeq += 1),
      kind,
      file,
      name: file.name,
      size: file.size,
      destination,
      channel,
      target: destPath(kind, destination, channel),
      status: known ? 'queued' : 'skipped',
      progress: 0,
      detail: known
        ? ''
        : `not one of ${spec.extensions.join(' ')} \u2014 not sent. The control plane decides what a `
          + 'file really is; this only saves you the upload.',
      abort: null,
      reported: false,
    };
    entry.path = entry.target + entry.name;
    if (known && file.size >= spec.largeBytes) {
      large += 1;
      entry.detail = `${fmtBytes(file.size)} \u2014 large; this will take a while and cannot be resumed.`;
    }
    const row = uploadRow(entry, cancelUpload);
    entry.row = row;
    state.uploads[kind].push(entry);
    $(`${kind}-upload-list`).append(row.root);
  }

  if (large) toast('warn', 'upload', `${large} file${large === 1 ? ' is' : 's are'} very large \u2014 the transfer will take a while.`);
  $(`${kind}-upload-summary`).hidden = true;
  syncUpload(kind);
  pump(kind);
}

function pump(kind) {
  const rows = state.uploads[kind];
  let running = rows.filter((entry) => entry.status === 'uploading').length;
  for (const entry of rows) {
    if (running >= UPLOAD_CONCURRENCY) break;
    if (entry.status !== 'queued') continue;
    running += 1;
    send(entry);
  }
  if (!running) finishBatch(kind);
}

async function send(entry) {
  entry.status = 'uploading';
  entry.progress = 0;
  entry.detail = '';
  entry.row.update();

  try {
    const data = await uploadMedia({
      kind: entry.kind,
      destination: entry.destination,
      channel: entry.channel,
      file: entry.file,
      onProgress: (value) => {
        entry.progress = value;
        entry.row.update();
      },
      onOpen: (abort) => {
        entry.abort = abort;
      },
    });
    const results = asUploadResults(data, entry.name);
    const result = results.find((r) => r.name === entry.name) || results[0] || { ok: true };
    if (result.ok) {
      entry.status = 'done';
      entry.progress = 1;
      entry.path = result.path || entry.path;
      entry.detail = `stored as ${entry.path}`;
    } else {
      entry.status = 'failed';
      entry.detail = [result.error, result.detail].filter(Boolean).join(' \u2014 ') || 'rejected';
    }
  } catch (err) {
    if (err instanceof ApiError && err.error === 'canceled') {
      entry.status = 'canceled';
      entry.detail = 'canceled';
    } else {
      entry.status = 'failed';
      entry.detail = err instanceof ApiError ? `${err.error} \u2014 ${err.detail}` : String((err && err.message) || err);
      if (err instanceof ApiError && err.status === 401) openTokenDialog('The control plane rejected that token.');
    }
  }

  entry.abort = null;
  entry.file = null; // the transfer is over; nothing should keep the File alive
  entry.row.update();
  pump(entry.kind);
}

function cancelUpload(entry) {
  if (entry.status === 'queued') {
    entry.status = 'canceled';
    entry.detail = 'canceled before it was sent';
    entry.file = null;
    entry.row.update();
    pump(entry.kind);
    return;
  }
  if (entry.status === 'uploading' && entry.abort) entry.abort();
}

/**
 * Directories this channel's selection is watched on. A watched folder picks a
 * new file up on its own; an explicit list never does, by design.
 */
function watchedDirs(kind) {
  const pair = UPLOAD[kind].list();
  const dirs = new Set(pair.watched.map((path) => `${String(path).replace(/\/+$/, '')}/`));
  if (dirs.size) return [...dirs];

  // The backend did not report `watched`, so read it off the selection form:
  // empty means the channel's own folder, and a glob means its parent.
  if (!pair.saved.length && state.selected) dirs.add(destPath(kind, 'channel', state.selected));
  for (const entry of pair.saved) {
    const star = entry.indexOf('*');
    if (star < 0) continue;
    const cut = entry.lastIndexOf('/', star);
    if (cut > 0) dirs.add(entry.slice(0, cut + 1));
  }
  return [...dirs];
}

function summarize(kind, stored) {
  const dirs = watchedDirs(kind);
  const targets = [...new Set(stored.map((entry) => entry.target))];
  const live = targets.filter((target) => dirs.some((dir) => target === dir || target.startsWith(dir)));
  const noun = stored.length === 1 ? UPLOAD[kind].noun : `${UPLOAD[kind].noun}s`;
  const where = `${stored.length} ${noun} stored in ${targets.join(', ')}.`;

  // The verdict below is about the channel on screen. If the operator moved on
  // while the transfer ran, say where the files went and claim nothing more.
  if (stored.some((entry) => entry.destination === 'channel' && entry.channel !== state.selected)) return where;

  if (live.length === targets.length) {
    return `${where} That folder is directory-watched for this channel, so the upload is already `
      + 'live \u2014 the list was rewritten in place and there is nothing further to save.';
  }
  if (!live.length) {
    return `${where} This channel selects ${kind === 'audio' ? 'tracks' : 'slides'} explicitly, so `
      + 'nothing changes on air until you add them on the left and Save.';
  }
  return `${where} Some of it landed in a watched folder and is already live; the rest needs adding `
    + 'on the left and saving.';
}

async function finishBatch(kind) {
  const rows = state.uploads[kind];
  if (rows.some((entry) => entry.status === 'queued' || entry.status === 'uploading')) return;
  const batch = rows.filter((entry) => !entry.reported);
  if (!batch.length) return;
  for (const entry of batch) entry.reported = true;

  const stored = batch.filter((entry) => entry.status === 'done');
  const failed = batch.filter((entry) => entry.status === 'failed');
  const skipped = batch.filter((entry) => entry.status === 'skipped');

  if (stored.length && state.selected) {
    await loadMedia(state.selected);
    renderAudioTab();
    renderSlidesTab();
  }

  const summary = $(`${kind}-upload-summary`);
  const parts = [];
  if (stored.length) parts.push(summarize(kind, stored));
  if (failed.length) parts.push(`${failed.length} rejected by the control plane \u2014 the reason is on each row.`);
  if (skipped.length) parts.push(`${skipped.length} not sent: the extension is not one this channel can play.`);
  summary.textContent = parts.join(' ');
  summary.dataset.tone = failed.length ? 'bad' : stored.length ? 'ok' : 'warn';
  summary.hidden = !parts.length;

  if (batch.length) {
    const tone = failed.length ? (stored.length ? 'warn' : 'bad') : 'ok';
    toast(tone, `upload \u00B7 ${kind}`,
      `${stored.length} stored, ${failed.length} rejected${skipped.length ? `, ${skipped.length} skipped` : ''}`);
  }
  syncUpload(kind);
}

function clearFinished(kind) {
  const keep = [];
  for (const entry of state.uploads[kind]) {
    if (finished(entry)) entry.row.root.remove();
    else keep.push(entry);
  }
  state.uploads[kind] = keep;
  $(`${kind}-upload-summary`).hidden = true;
  syncUpload(kind);
}

function wireUpload(kind) {
  const zone = $(`${kind}-dropzone`);
  const input = $(`${kind}-file-input`);

  $(`btn-${kind}-pick`).addEventListener('click', () => input.click());
  input.addEventListener('change', () => {
    enqueue(kind, input.files);
    input.value = ''; // so the same file can be chosen twice in a row
  });

  for (const radio of document.querySelectorAll(`input[name="${kind}-dest"]`)) {
    radio.addEventListener('change', () => {
      if (radio.checked) state.uploadDest[kind] = radio.value;
      syncUpload(kind);
    });
  }

  const over = (event) => {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
    zone.dataset.over = 'true';
  };
  zone.addEventListener('dragenter', over);
  zone.addEventListener('dragover', over);
  zone.addEventListener('dragleave', (event) => {
    if (!zone.contains(event.relatedTarget)) zone.dataset.over = 'false';
  });
  zone.addEventListener('drop', (event) => {
    event.preventDefault();
    zone.dataset.over = 'false';
    enqueue(kind, event.dataTransfer && event.dataTransfer.files);
  });

  $(`btn-${kind}-upload-clear`).addEventListener('click', () => clearFinished(kind));
  syncUpload(kind);
}

// --------------------------------------------------------------------------
// Detail: look (visualization, color, preset)
// --------------------------------------------------------------------------

function renderLookTab() {
  const name = state.selected;
  if (!name) return;
  const cfg = config(name);
  const active = (state.channels.get(name) || {}).visualization || (cfg.visualization || {}).active || '';
  const hot = hotSet(name);
  const live = (state.channels.get(name) || {}).state !== 'stopped';
  const enabled = vizEnabled(name);

  syncVizPower();

  const list = $('plugin-list');
  clear(list);
  list.dataset.inert = String(!enabled);
  const draftHot = state.hotSetDraft || hot;

  const known = new Set(state.plugins.map((p) => p.name));
  const rows = [...state.plugins];
  // A hot plugin the backend does not report as installed is the one genuinely
  // broken case here: the graph names a branch that cannot be built.
  for (const plugin of hot) if (!known.has(plugin)) rows.push({ name: plugin, display_name: plugin, missing: true });

  if (!rows.length) list.append(el('li', { class: 'muted small', text: 'no plugins reported' }));

  for (const plugin of rows) {
    const isHot = hot.includes(plugin.name);
    const isActive = plugin.name === active;
    const missing = Boolean(plugin.missing) || plugin.available === false;
    const cost = plugin.cost && typeof plugin.cost.cores_720p30 === 'number'
      ? `${plugin.cost.cores_720p30.toFixed(2)} cores @720p30`
      : '';

    const tag = !enabled
      ? { tone: 'warn', text: 'not rendering' }
      : missing
        ? { tone: 'bad', text: 'not installed' }
        : isHot
          ? { tone: 'ok', text: 'instant' }
          : { tone: 'warn', text: live ? '~1s gap' : 'on next start' };

    const where = !enabled
      ? (isHot ? 'in hot set \u2014 costs nothing while the visualization is off' : 'installed, not instantiated')
      : missing
        ? 'in hot_set but not installed \u2014 this channel cannot build that branch'
        : isHot
          ? `hot set \u00B7 always rendering${cost ? ` \u00B7 ${cost}` : ''}`
          : `installed, not instantiated${cost ? ` \u00B7 ${cost}` : ''}`;

    list.append(el('li', { class: 'plugin', dataset: { hot: String(isHot), active: String(isActive), missing: String(missing && enabled) } }, [
      el('div', { class: 'pmeta' }, [
        el('div', { class: 'pname' }, [
          plugin.display_name || plugin.name,
          el('span', { class: 'pid', text: `  ${plugin.name}` }),
        ]),
        plugin.description ? el('div', { class: 'pdesc', text: plugin.description }) : null,
        el('div', { class: 'pcost', text: where }),
      ]),
      el('div', { class: 'pactions' }, [
        // Hot set membership is the CPU budget, and switching a plugin in can only
        // ever grow it, so removal has to be explicit rather than implied.
        el('label', {
          class: 'inline small',
          title: missing
            ? 'not installed, so it cannot be instantiated'
            : isActive
              ? 'on air \u2014 switch to another plugin before dropping this one'
              : 'instantiate this branch at launch so it can be switched to instantly',
        }, [
          el('input', {
            type: 'checkbox',
            checked: draftHot.includes(plugin.name),
            disabled: missing || isActive,
            onchange: (event) => toggleHotSet(plugin.name, event.target.checked),
          }),
          'hot',
        ]),
        el('span', { class: 'chip', dataset: { tone: tag.tone }, text: tag.text }),
        isActive
          ? el('span', { class: 'chip', dataset: { tone: 'info' }, text: enabled ? 'on air' : 'selected' })
          : el('button', {
              type: 'button',
              disabled: missing || !enabled,
              title: enabled ? '' : 'The visualization is off; switch it on to change what renders.',
              text: isHot || !live ? 'Switch' : 'Switch \u2014 restarts',
              onclick: () => switchVisualization(plugin.name, isHot || !live),
            }),
      ]),
    ]));
  }

  const color = cfg.color || {};
  const manual = color.manual || {};
  syncHotSet();
  for (const radio of document.querySelectorAll('input[name="color-mode"]')) {
    radio.checked = radio.value === (color.mode || 'automatic');
  }
  setColorInput('color-accent', 'color-accent-hex', manual.accent || '#4FC3F7');
  setColorInput('color-tint', 'color-tint-hex', manual.tint || '#101820');
  $('color-transition').value = numberOr(color.transition_seconds, 2);

  const select = $('preset-select');
  if (document.activeElement !== select) {
    clear(select);
    select.append(el('option', { value: '', text: '— none —' }));
    for (const preset of state.presets) {
      select.append(el('option', { value: preset.name, text: preset.display_name || preset.name }));
    }
    select.value = cfg.preset || '';
  }
  showPresetDescription();
}

function setColorInput(colorId, hexId, value) {
  const hex = /^#[0-9a-fA-F]{6}$/.test(value) ? value : '#000000';
  if (document.activeElement !== $(colorId)) $(colorId).value = hex;
  if (document.activeElement !== $(hexId)) $(hexId).value = hex.toUpperCase();
}

function showPresetDescription() {
  const preset = state.presets.find((p) => p.name === $('preset-select').value);
  $('preset-description').textContent = preset ? preset.description || '' : '';
}

/**
 * The on/off switch above the plugin list. Off is the cheapest a channel can be —
 * a live 1080p30 channel held 0.999x realtime without it and only 0.415x with it —
 * and it is not a live change, so it follows the resolution card's dirty-then-Apply
 * shape rather than firing on the tick.
 */
function syncVizPower() {
  const name = state.selected;
  const box = $('viz-enabled');
  const apply = $('btn-viz-apply');
  const chip = $('viz-cores');
  const note = $('viz-power-state');
  note.className = 'small vp-state';

  if (!name) {
    box.checked = true;
    apply.disabled = true;
    chip.hidden = true;
    note.textContent = '';
    return;
  }

  const saved = vizEnabled(name);
  const shown = state.vizEnabledDraft === null ? saved : state.vizEnabledDraft;
  if (document.activeElement !== box) box.checked = shown;
  apply.disabled = shown === saved;
  $('viz-power').dataset.enabled = String(shown);

  const projected = (state.details.get(name) || {}).projected_cores;
  chip.hidden = typeof projected !== 'number';
  if (typeof projected === 'number') chip.textContent = `projects ${projected.toFixed(2)} cores`;

  const active = (config(name).visualization || {}).active || '';
  const running = (state.channels.get(name) || {}).state !== 'stopped';

  if (shown !== saved) {
    const when = running
      ? 'the channel is running, so it takes effect when you restart it'
      : 'it takes effect on the next start';
    note.textContent = shown
      ? `Not applied. Apply puts the branches back in the graph \u2014 ${when}.`
      : `Not applied. Apply keeps ${active || 'the current plugin'} selected and stops rendering it `
        + `\u2014 ${when}.`;
    return;
  }
  if (!saved) {
    note.textContent = `Off. The list below is what will come back, not what is on air — `
      + `${active || 'the selected plugin'} is still chosen and returns unchanged when this is `
      + 'switched on.';
    return;
  }
  note.textContent = '';
}

async function applyVizEnabled() {
  const name = state.selected;
  if (!name || state.vizEnabledDraft === null) return;
  const target = state.vizEnabledDraft;
  if (target === vizEnabled(name)) return;

  const running = (state.channels.get(name) || {}).state !== 'stopped';
  const result = await guard(`visualization ${target ? 'on' : 'off'}`,
    () => api.setVisualizationEnabled(name, target));
  if (result === undefined) return;

  toast('ok', `visualization ${target ? 'on' : 'off'}`,
    running ? 'saved \u2014 restart the channel to put it on air' : 'saved \u2014 applies on the next start');
  state.vizEnabledDraft = null;
  await loadChannelDetail(name);
  scheduleRefresh();
}

/** Membership is a CPU budget, so it is staged and applied, never live per click. */
function toggleHotSet(plugin, wanted) {
  const current = state.hotSetDraft || hotSet(state.selected);
  const next = wanted
    ? [...current, plugin]
    : current.filter((p) => p !== plugin);
  state.hotSetDraft = next;
  renderLookTab();
}

function estimateHotSetCores(names) {
  const scale = { '480p': 0.6, '720p': 1.0, '1080p': 1.9, '1440p': 3.4, '2160p': 7.6 };
  const factor = scale[resolutionOf(state.selected)] || 1;
  let cores = 0;
  for (const name of names) {
    const plugin = state.plugins.find((p) => p.name === name);
    cores += (plugin && plugin.cost ? plugin.cost.cores_720p30 : 0.28) * factor;
  }
  return cores;
}

function syncHotSet() {
  const name = state.selected;
  const saved = hotSet(name);
  const draft = state.hotSetDraft || saved;
  const dirty = draft.length !== saved.length || draft.some((p) => !saved.includes(p));
  const note = $('hot-set-state');
  const chip = $('hot-set-cores');

  $('btn-hot-set-apply').disabled = !dirty || !draft.length;
  $('btn-hot-set-revert').disabled = !dirty;

  if (!draft.length) {
    note.textContent = 'A channel needs at least one branch. Switch the visualization off instead '
      + 'of emptying the hot set.';
    note.dataset.tone = 'bad';
    chip.hidden = true;
    return;
  }
  if (!dirty) {
    note.textContent = '';
    note.dataset.tone = '';
    chip.hidden = true;
    return;
  }

  const delta = estimateHotSetCores(draft) - estimateHotSetCores(saved);
  chip.hidden = false;
  chip.textContent = `${delta >= 0 ? '+' : ''}${delta.toFixed(2)} cores`;
  chip.dataset.tone = delta > 0 ? 'warn' : 'ok';
  note.dataset.tone = 'warn';
  const running = (state.channels.get(name) || {}).state !== 'stopped';
  note.textContent = vizEnabled(name) && running
    ? 'Not applied. Changing which branches exist rebuilds the graph, so Apply restarts the '
      + 'compositor \u2014 about a 1s gap and a new YouTube ingest session.'
    : 'Not applied. Applies on the next start; nothing is drawing these branches right now.';
}

async function applyHotSet() {
  const name = state.selected;
  const draft = state.hotSetDraft;
  if (!name || !draft || !draft.length) return;

  const active = (config(name).visualization || {}).active;
  const body = { hot_set: draft };
  // Dropping the branch on air needs a replacement named, or the backend refuses.
  if (!draft.includes(active)) body.active = draft[0];

  const result = await guard('hot set', () => api.setHotSet(name, body), 'accepted');
  if (result === undefined) return;

  state.hotSetDraft = null;
  toast('ok', 'hot set', result.detail || 'saved');
  await loadChannelDetail(name);
  scheduleRefresh();
}

async function switchVisualization(plugin, instant) {
  if (!name) return;
  const live = (state.channels.get(name) || {}).state !== 'stopped';
  const error = $('viz-error');
  error.hidden = true;

  if (!instant) {
    const label = (state.plugins.find((p) => p.name === plugin) || {}).display_name || plugin;
    const ok = await confirmRestart({
      title: `Switch ${name} to ${label}?`,
      body: `${label} is installed but is not in this channel's hot set, so its branch was never `
        + 'built. An FFmpeg filtergraph is fixed at launch, so there is nothing running to cut to.',
      cost: 'The channel restarts make-before-break: roughly a 1s gap on air, and a new YouTube '
        + 'ingest session.',
      note: 'To make this switch instant in future, add the plugin to hot_set \u2014 at the cost of '
        + 'about 0.28 cores per idle branch at 720p, measured, whether or not it is on screen.',
      okText: 'Restart and switch',
    });
    if (!ok) return;
  }

  try {
    await api.setVisualization(name, plugin);
    if (!instant) {
      toast('warn', 'visualization', `${plugin} \u2014 staged; the channel is restarting`);
      scheduleRefresh(800);
    } else if (live) {
      toast('ok', 'visualization', `${plugin} \u2014 switched on the running graph`);
    } else {
      toast('ok', 'visualization', `${plugin} \u2014 will be on air at the next start`);
      scheduleRefresh(400);
    }
  } catch (err) {
    if (err instanceof ApiError && err.error === 'not_in_hot_set') {
      error.hidden = false;
      error.textContent =
        `The control plane refused to switch to “${plugin}”: it is not in this channel's hot set, `
        + 'and this build of the API will not stage a restart for you. Add it to hot_set and restart '
        + 'the channel deliberately.';
      toast('warn', 'not_in_hot_set', plugin);
      return;
    }
    report('visualization', err);
  }
}

async function saveColor() {
  const name = state.selected;
  if (!name) return;
  const mode = document.querySelector('input[name="color-mode"]:checked');
  await guard('color', () => api.setColor(name, {
    mode: mode ? mode.value : 'automatic',
    manual: { accent: $('color-accent').value, tint: $('color-tint').value },
    transition_seconds: Number($('color-transition').value),
  }), 'applied');
  loadChannelDetail(name);
}

async function applyPreset() {
  const name = state.selected;
  const preset = $('preset-select').value;
  if (!name || !preset) return;
  const error = $('viz-error');
  error.hidden = true;
  try {
    await api.applyPreset(name, preset);
    toast('ok', 'preset', preset);
    loadChannelDetail(name);
  } catch (err) {
    if (err instanceof ApiError && err.error === 'not_in_hot_set') {
      error.hidden = false;
      error.textContent =
        `Preset “${preset}” selects a visualization outside this channel's hot set, so it was rejected ` +
        'rather than silently promoted. Add that plugin to hot_set and restart the channel first.';
      toast('warn', 'not_in_hot_set', preset);
      return;
    }
    report('preset', err);
  }
}

// --------------------------------------------------------------------------
// Detail: schedule
// --------------------------------------------------------------------------

function renderScheduleTab() {
  const name = state.selected;
  if (!name) return;
  const schedule = config(name).schedule || {};
  if (document.activeElement !== $('schedule-tz')) {
    $('schedule-tz').value = schedule.timezone || 'UTC';
  }
  const rules = Array.isArray(schedule.rules) ? schedule.rules : [];
  const host = $('schedule-rules');
  clear(host);
  state.scheduleRows = [];
  if (!rules.length) host.append(el('p', { class: 'muted small', text: 'No rules. The channel keeps its own settings all day.' }));
  for (const rule of rules) addScheduleRow(rule);
}

function addScheduleRow(rule = {}) {
  const host = $('schedule-rules');
  const placeholder = host.querySelector('p');
  if (placeholder) placeholder.remove();

  const nameInput = el('input', { type: 'text', value: rule.name || '', placeholder: 'morning' });
  const whenInput = el('input', { type: 'text', value: rule.when || '', placeholder: '06:00-11:00', pattern: '([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]' });
  const dateInput = el('input', { type: 'date', value: rule.date || '' });

  const presetSelect = el('select');
  presetSelect.append(el('option', { value: '', text: '— preset —' }));
  for (const preset of state.presets) {
    presetSelect.append(el('option', { value: preset.name, text: preset.display_name || preset.name }));
  }
  if (rule.preset && !state.presets.some((p) => p.name === rule.preset)) {
    presetSelect.append(el('option', { value: rule.preset, text: `${rule.preset} (unknown)` }));
  }
  presetSelect.value = rule.preset || '';

  const dayBoxes = new Map();
  const days = el('div', { class: 'days' });
  for (const day of WEEKDAYS) {
    const box = el('input', { type: 'checkbox' });
    box.checked = Array.isArray(rule.days) && rule.days.includes(day);
    dayBoxes.set(day, box);
    days.append(el('label', {}, [box, day]));
  }

  const row = el('div', { class: 'rule' }, [
    el('label', {}, ['name', nameInput]),
    el('label', {}, ['window', whenInput]),
    // not a <label>: wrapping seven checkboxes in one label makes the first of
    // them inherit the whole group's text as its accessible name.
    el('div', { class: 'rule-field' }, [el('span', { class: 'rule-label', text: 'days' }), days]),
    el('label', {}, ['date', dateInput]),
    el('label', {}, ['preset', presetSelect]),
    el('button', { type: 'button', class: 'danger', text: 'Remove', onclick: () => {
      row.remove();
      state.scheduleRows = state.scheduleRows.filter((r) => r.root !== row);
    } }),
  ]);

  host.append(row);
  state.scheduleRows.push({
    root: row,
    read() {
      const picked = WEEKDAYS.filter((day) => dayBoxes.get(day).checked);
      return {
        name: nameInput.value.trim(),
        preset: presetSelect.value,
        when: whenInput.value.trim() || null,
        days: picked.length ? picked : null,
        date: dateInput.value || null,
      };
    },
  });
}

async function saveSchedule() {
  const name = state.selected;
  if (!name) return;
  const rules = state.scheduleRows.map((row) => row.read());
  for (const rule of rules) {
    if (!rule.name || !rule.preset) {
      toast('warn', 'schedule', 'every rule needs a name and a preset');
      return;
    }
    if (!rule.when && !rule.date) {
      toast('warn', 'schedule', `rule “${rule.name}” needs a window or a date`);
      return;
    }
  }
  await guard('save schedule', () => api.patchChannel(name, {
    schedule: { timezone: $('schedule-tz').value.trim() || 'UTC', rules },
  }), `${rules.length} rules`);
  loadChannelDetail(name);
}

// --------------------------------------------------------------------------
// Detail: logs
// --------------------------------------------------------------------------

let logTimer = null;

async function refreshLogs() {
  const name = state.selected;
  if (!name) return;
  const body = await guard('logs', () => api.logs({
    channel: name,
    service: $('log-service').value,
    lines: $('log-lines').value,
  }));
  if (body === undefined) return;

  const { text, path } = asLog(body);
  const pathEl = $('log-path');
  pathEl.textContent = path;
  pathEl.title = path;

  const out = $('log-output');
  const pinned = out.scrollTop + out.clientHeight >= out.scrollHeight - 24;
  out.textContent = text || '(empty)';
  if (pinned) out.scrollTop = out.scrollHeight;
}

function syncLogFollow() {
  clearInterval(logTimer);
  logTimer = null;
  if ($('log-follow').checked) logTimer = setInterval(refreshLogs, 5000);
}

// --------------------------------------------------------------------------
// Topbar, system dialog
// --------------------------------------------------------------------------

function pickNumber(obj, keys) {
  for (const key of keys) {
    let node = obj;
    for (const part of key.split('.')) {
      node = node && typeof node === 'object' ? node[part] : undefined;
    }
    if (typeof node === 'number' && Number.isFinite(node)) return node;
  }
  return null;
}

function renderTopbar() {
  const channels = [...state.channels.values()];
  const running = channels.filter((c) => c.state === 'running').length;
  const cap = state.capacity || {};
  const max = pickNumber(cap, ['max_channels', 'limits.max_channels', 'channels_max'])
    ?? pickNumber(state.system || {}, ['max_channels', 'limits.max_channels']);

  $('stat-channels').textContent = max ? `${channels.length}/${max}` : String(channels.length);
  $('stat-running').textContent = String(running);

  const used = pickNumber(cap, ['cores_used', 'used_cores', 'projected_cores', 'projected', 'cores.used'])
    ?? channels.reduce((sum, c) => sum + (typeof c.cpu_cores === 'number' ? c.cpu_cores : 0), 0);
  const total = pickNumber(cap, ['cores_total', 'total_cores', 'cores_available_total', 'cores', 'host_cores', 'cores.total'])
    ?? pickNumber(state.system || {}, ['cores', 'cpu_cores', 'host.cores']);
  const headroom = pickNumber(cap, ['headroom_cores', 'cores_available', 'available_cores', 'headroom', 'available', 'cores.available'])
    ?? (total === null ? null : total - used);

  $('stat-cores').textContent = total === null ? fmtNum(used, 2) : `${used.toFixed(2)}/${total.toFixed(1)}`;
  const headroomEl = $('stat-headroom');
  headroomEl.textContent = headroom === null ? DASH : headroom.toFixed(2);
  headroomEl.style.color = headroom !== null && headroom < 0.5 ? 'var(--bad)' : '';
}

function renderSystemDialog() {
  renderKv($('system-kv'), flatten(omit(state.system || {}, ['encoders'])));

  const list = $('encoder-list');
  clear(list);
  const encoders = state.system && state.system.encoders;
  const entries = Array.isArray(encoders)
    ? encoders.map((e) => (typeof e === 'string' ? { name: e, available: true } : { ...e, name: e.encoder || e.name }))
    : Object.entries(encoders || {}).map(([name, value]) => ({
        name,
        available: value === true || (value && value.available === true),
        detail: value && typeof value === 'object' ? value.detail || value.reason || '' : '',
      }));
  if (!entries.length) list.append(el('li', { class: 'muted small', text: 'no probe results reported' }));
  for (const entry of entries) {
    list.append(el('li', {}, [
      el('span', { class: 'dot', dataset: { tone: entry.available ? 'ok' : 'bad' } }),
      entry.name || '?',
      el('span', { class: 'muted small', text: entry.detail || entry.reason || (entry.available ? 'probe ok' : 'unavailable') }),
    ]));
  }

  const cap = state.capacity || {};
  renderKv($('capacity-kv'), flatten(cap));
  const used = pickNumber(cap, ['cores_used', 'used_cores', 'projected_cores', 'projected'])
    ?? [...state.channels.values()].reduce((s, c) => s + (typeof c.cpu_cores === 'number' ? c.cpu_cores : 0), 0);
  const total = pickNumber(cap, ['cores_total', 'total_cores', 'cores', 'host_cores']);
  const fill = $('capacity-fill');
  const ratio = total ? Math.max(0, Math.min(1, used / total)) : 0;
  fill.style.width = `${(ratio * 100).toFixed(1)}%`;
  fill.dataset.tone = ratio > 0.9 ? 'bad' : ratio > 0.75 ? 'warn' : 'ok';
}

function omit(obj, keys) {
  const copy = { ...obj };
  for (const key of keys) delete copy[key];
  return copy;
}

// --------------------------------------------------------------------------
// SSE
// --------------------------------------------------------------------------

const STREAM_TONE = {
  connected: 'ok',
  connecting: 'info',
  reconnecting: 'warn',
  dropped: 'warn',
  unauthorized: 'bad',
  idle: 'idle',
};

function setStreamState(status) {
  const pill = $('sse-pill');
  pill.textContent = `events: ${status}`;
  pill.dataset.tone = STREAM_TONE[status] || 'idle';
  if (status === 'connected') {
    // No event log exists upstream, so a reconnect re-reads rather than replays.
    refreshChannels();
  }
  if (status === 'unauthorized') openTokenDialog('The event stream rejected that token.');
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
  updateCard(name);
  if (state.selected === name) renderDetail();
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
      mergeStatus(channel, { visualization: data.visualization || data.active });
      if (state.selected === channel) renderLookTab();
      logEvent({ tone: 'info', channel, at: data.at, text: `visualization ${data.visualization || data.active || ''}` });
      break;
    case 'watchdog.event':
      logEvent({
        tone: data.recovered || data.action === 'recovered' ? 'warn' : 'bad',
        channel,
        at: data.at,
        text: describe(data, ['fault', 'action', 'reason', 'detail', 'message']),
      });
      toast(data.recovered ? 'warn' : 'bad', `watchdog${channel ? ` \u00B7 ${channel}` : ''}`,
        describe(data, ['fault', 'action', 'reason', 'detail', 'message']));
      break;
    case 'capacity.warning':
      logEvent({ tone: 'warn', channel, at: data.at, text: describe(data, ['detail', 'message', 'reason']) });
      toast('warn', 'capacity', describe(data, ['detail', 'message', 'reason']));
      refreshCapacity();
      break;
    case 'job.progress':
      renderJob(data);
      break;
    default:
      logEvent({ tone: 'idle', channel, at: data.at, text: `${name} ${describe(data, [])}` });
  }
}

function describe(data, keys) {
  for (const key of keys) {
    if (typeof data[key] === 'string' && data[key]) return data[key];
  }
  const rest = omit(data, ['channel', 'at']);
  const parts = Object.entries(rest).map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`);
  return parts.join(' ') || '(no detail)';
}

function logEvent({ tone = 'idle', channel, at, text, id, progress }) {
  const host = $('event-log');
  let row = id ? state.jobs.get(id) : null;

  if (!row) {
    row = el('li', { dataset: { tone } }, [
      el('span', { class: 'ev-time', text: fmtClock(at) }),
      el('span', { class: 'ev-chan', text: channel || 'system' }),
      el('span', { class: 'ev-text', text }),
    ]);
    if (typeof progress === 'number') {
      const bar = el('div', { class: 'ev-bar' }, [el('span')]);
      row.append(bar);
    }
    host.prepend(row);
    if (id) state.jobs.set(id, row);
    while (host.children.length > 80) {
      const last = host.lastElementChild;
      for (const [key, node] of state.jobs) if (node === last) state.jobs.delete(key);
      last.remove();
    }
  } else {
    row.dataset.tone = tone;
    row.children[0].textContent = fmtClock(at);
    row.children[2].textContent = text;
  }

  const bar = row.querySelector('.ev-bar > span');
  if (bar && typeof progress === 'number') {
    bar.style.width = `${Math.max(0, Math.min(100, progress * (progress <= 1 ? 100 : 1))).toFixed(0)}%`;
  }
  return row;
}

function renderJob(data) {
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
}

// --------------------------------------------------------------------------
// Dialogs
// --------------------------------------------------------------------------

function openTokenDialog(message) {
  const dialog = $('token-dialog');
  const error = $('token-error');
  error.hidden = !message;
  error.textContent = message || '';
  $('token-input').value = '';
  if (!dialog.open) dialog.showModal();
}

/**
 * Gate for anything that is not a live change. The stream stops for about a second,
 * so the cost is stated before the operator commits, never after.
 */
function confirmRestart({ title, body, cost, note, okText }) {
  const dialog = $('confirm-dialog');
  $('confirm-title').textContent = title;
  $('confirm-body').textContent = body;
  $('confirm-cost').textContent = cost;
  $('confirm-note').textContent = note || '';
  $('confirm-note').hidden = !note;
  $('btn-confirm-ok').textContent = okText;

  return new Promise((resolve) => {
    const finish = (value) => {
      dialog.removeEventListener('close', onClose);
      $('btn-confirm-ok').removeEventListener('click', onOk);
      $('btn-confirm-cancel').removeEventListener('click', onCancel);
      resolve(value);
    };
    const onOk = () => {
      dialog.removeEventListener('close', onClose);
      dialog.close();
      finish(true);
    };
    const onCancel = () => dialog.close();
    const onClose = () => finish(false);

    $('btn-confirm-ok').addEventListener('click', onOk);
    $('btn-confirm-cancel').addEventListener('click', onCancel);
    dialog.addEventListener('close', onClose);
    dialog.showModal();
  });
}

function openCreateDialog() {
  const viz = $('create-viz');
  const hotset = $('create-hotset');
  clear(viz);
  clear(hotset);
  for (const plugin of state.plugins) {
    viz.append(el('option', { value: plugin.name, text: plugin.display_name || plugin.name }));
    const box = el('input', { type: 'checkbox', value: plugin.name });
    box.checked = state.plugins.indexOf(plugin) === 0;
    hotset.append(el('label', { class: 'inline' }, [box, plugin.name]));
  }
  if (!state.plugins.length) {
    hotset.append(el('span', { class: 'muted small', text: 'no plugins reported by /api/plugins' }));
  }
  $('create-error').hidden = true;
  $('create-dialog').showModal();
}

async function submitCreate() {
  const name = $('create-name').value.trim();
  const active = $('create-viz').value;
  const hot = [...$('create-hotset').querySelectorAll('input:checked')].map((box) => box.value);
  if (active && !hot.includes(active)) hot.push(active);

  const body = {
    name,
    genre: $('create-genre').value.trim(),
    resolution: $('create-resolution').value,
    fps: Number($('create-fps').value),
    encoder: $('create-encoder').value,
    visualization: { active, hot_set: hot },
  };

  try {
    await api.createChannel(body);
    $('create-dialog').close();
    toast('ok', 'channel created', `${name} \u2014 add YOUTUBE_STREAM_KEY to channels/${name}/.env`);
    await refreshChannels();
    selectChannel(name);
  } catch (err) {
    const error = $('create-error');
    error.hidden = false;
    error.textContent = err instanceof ApiError ? `${err.error}: ${err.detail}` : String(err);
  }
}

async function deleteChannel() {
  const name = state.selected;
  if (!name) return;
  await guard(`delete ${name}`, () => api.deleteChannel(name), 'deleted');
  $('delete-confirm').value = '';
  selectChannel(null);
  await refreshChannels();
}

// --------------------------------------------------------------------------
// Wiring
// --------------------------------------------------------------------------

function wire() {
  $('btn-refresh').addEventListener('click', () => {
    refreshChannels();
    refreshCapacity();
    pingHealth();
  });
  $('btn-token').addEventListener('click', () => openTokenDialog(''));
  $('btn-new-channel').addEventListener('click', () => openCreateDialog());
  $('btn-system').addEventListener('click', async () => {
    await Promise.all([loadSystem(), refreshCapacity()]);
    renderSystemDialog();
    $('system-dialog').showModal();
  });
  $('btn-system-close').addEventListener('click', () => $('system-dialog').close());
  $('btn-clear-events').addEventListener('click', () => {
    clear($('event-log'));
    state.jobs.clear();
  });

  $('token-form').addEventListener('submit', (event) => {
    event.preventDefault();
    const value = $('token-input').value.trim();
    if (!value) return;
    setToken(value);
    $('token-input').value = '';
    $('token-dialog').close();
    state.booted = false;
    stream.stop();
    boot(); // boot() starts the event stream itself
  });
  $('btn-token-cancel').addEventListener('click', () => $('token-dialog').close());
  $('btn-token-forget').addEventListener('click', () => {
    clearToken();
    stream.stop();
    state.booted = false;
    toast('warn', 'token', 'forgotten on this browser');
  });

  $('create-form').addEventListener('submit', (event) => {
    event.preventDefault();
    submitCreate();
  });
  $('btn-create-cancel').addEventListener('click', () => $('create-dialog').close());

  $('btn-start').addEventListener('click', () => lifecycle(state.selected, 'start'));
  $('btn-stop').addEventListener('click', () => lifecycle(state.selected, 'stop'));
  $('btn-restart').addEventListener('click', () => lifecycle(state.selected, 'restart'));
  $('delete-confirm').addEventListener('input', syncDeleteButton);
  $('btn-delete').addEventListener('click', () => deleteChannel());
  $('output-resolution').addEventListener('change', (event) => {
    state.resolutionDraft = event.target.value;
    syncResolution();
  });
  $('btn-resolution-apply').addEventListener('click', () => applyResolution());
  $('viz-enabled').addEventListener('change', (event) => {
    state.vizEnabledDraft = event.target.checked;
    syncVizPower();
  });
  $('btn-viz-apply').addEventListener('click', () => applyVizEnabled());

  for (const id of DELIVERY_INPUTS) {
    $(id).addEventListener('input', () => syncDelivery());
    $(id).addEventListener('change', () => syncDelivery());
  }
  $('btn-delivery-apply').addEventListener('click', () => applyDelivery());

  $('tabs').addEventListener('click', (event) => {
    const tab = event.target.closest('.tab');
    if (tab) selectTab(tab.dataset.tab);
  });

  $('btn-preview-start').addEventListener('click', () => {
    if (state.selected) preview.start(state.selected);
  });
  $('btn-preview-stop').addEventListener('click', () => preview.stop());
  $('btn-preview-copy').addEventListener('click', () => copyPreviewUrl());
  $('btn-hot-set-apply').addEventListener('click', () => applyHotSet());
  $('btn-hot-set-revert').addEventListener('click', () => {
    state.hotSetDraft = null;
    renderLookTab();
  });
  $('preview-audio').addEventListener('change', (event) => preview.setAudio(event.target.checked));
  $('preview-video').addEventListener('click', () => $('preview-video').play().catch(() => {}));

  $('audio-filter').addEventListener('input', () => renderAudioTab());
  $('image-filter').addEventListener('input', () => renderSlidesTab());
  $('btn-playlist-add-all').addEventListener('click', () => addAll('audio'));
  $('btn-slides-add-all').addEventListener('click', () => addAll('images'));
  $('btn-playlist-save').addEventListener('click', () => savePlaylist());
  $('btn-playlist-revert').addEventListener('click', () => {
    state.playlist.draft = [...state.playlist.saved];
    renderAudioTab();
  });
  $('btn-slides-save').addEventListener('click', () => saveSlides());
  $('btn-slides-revert').addEventListener('click', () => {
    state.slides.draft = [...state.slides.saved];
    renderSlidesTab();
  });

  makeSortable($('playlist-selected'), (from, to) => {
    const [moved] = state.playlist.draft.splice(from, 1);
    state.playlist.draft.splice(to, 0, moved);
    renderAudioTab();
  });
  makeSortable($('slides-selected'), (from, to) => {
    const [moved] = state.slides.draft.splice(from, 1);
    state.slides.draft.splice(to, 0, moved);
    renderSlidesTab();
  });

  wireUpload('audio');
  wireUpload('images');
  // A file dropped anywhere else would otherwise navigate away from the panel,
  // taking any unsaved list edit with it.
  for (const type of ['dragover', 'drop']) {
    document.addEventListener(type, (event) => {
      const target = event.target instanceof Element ? event.target : null;
      if (!target || !target.closest('.dropzone')) event.preventDefault();
    });
  }

  bindColor('color-accent', 'color-accent-hex');
  bindColor('color-tint', 'color-tint-hex');
  $('btn-color-save').addEventListener('click', () => saveColor());
  $('preset-select').addEventListener('change', showPresetDescription);
  $('btn-preset-apply').addEventListener('click', () => applyPreset());

  $('btn-rule-add').addEventListener('click', () => addScheduleRow());
  $('btn-schedule-save').addEventListener('click', () => saveSchedule());

  $('btn-log-refresh').addEventListener('click', () => refreshLogs());
  $('log-service').addEventListener('change', () => refreshLogs());
  $('log-lines').addEventListener('change', () => refreshLogs());
  $('log-follow').addEventListener('change', syncLogFollow);
}

function bindColor(colorId, hexId) {
  $(colorId).addEventListener('input', () => {
    $(hexId).value = $(colorId).value.toUpperCase();
  });
  $(hexId).addEventListener('change', () => {
    const value = $(hexId).value.trim();
    if (/^#[0-9a-fA-F]{6}$/.test(value)) $(colorId).value = value;
    else $(hexId).value = $(colorId).value.toUpperCase();
  });
}

function selectTab(name) {
  for (const tab of document.querySelectorAll('.tab')) {
    tab.setAttribute('aria-selected', String(tab.dataset.tab === name));
  }
  for (const panel of document.querySelectorAll('.tabpanel')) {
    panel.hidden = panel.dataset.panel !== name;
  }
  if (name === 'logs') refreshLogs();
}

main();
