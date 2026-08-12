// Operator control panel. Talks only to the REST + SSE contract in
// docs/contracts/rest-api.md.

import {
  ApiError,
  api,
  asList,
  asMediaGroups,
  asText,
  authHeaders,
  clearToken,
  hasToken,
  setToken,
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
} from './render.js';

const DASH = '—';
const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

// Status fields the contract defines. Events are merged through this whitelist so
// envelope keys (channel, at) never end up rendered as status.
const STATUS_KEYS = [
  'state', 'health', 'uptime_seconds', 'current_track', 'next_track', 'current_slide',
  'visualisation', 'encoder', 'encoder_requested', 'fps', 'speed', 'bitrate_kbps',
  'cpu_cores', 'liquidsoap_buffer', 'rtmp', 'hls',
];

// Fields GET /api/channels/{name} adds on top of the summary. They are not merged
// from SSE, which carries the status subset only.
const DETAIL_KEYS = ['fault', 'fault_detail', 'warnings'];

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
  playlist: { saved: [], draft: [] },
  slides: { saved: [], draft: [] },
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
      toast('bad', label, 'unauthorised');
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
  state.plugins = asList(data, 'plugins', 'items').map(normalisePlugin).filter((p) => p.name);
}

function normalisePlugin(entry) {
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

  renderChannelList();
  renderTopbar();
  if (state.selected) loadChannelDetail(state.selected);
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
  }
  if (slides !== undefined) {
    state.slides.saved = asList(slides, 'slides', 'images', 'items').map(String);
    state.slides.draft = [...state.slides.saved];
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
  const viz = config(name).visualisation;
  return Array.isArray(viz && viz.hot_set) ? viz.hot_set : [];
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
  state.playlist = { saved: [], draft: [] };
  state.slides = { saved: [], draft: [] };
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
  renderKv($('onair-kv'), [
    ['track', ch.current_track ? basename(ch.current_track) : DASH],
    ['next', ch.next_track ? basename(ch.next_track) : DASH],
    ['slide', ch.current_slide ? basename(ch.current_slide) : DASH],
    ['visualisation', ch.visualisation || DASH],
    ['preset', cfg.preset || 'none'],
    ['colour', cfg.colour ? cfg.colour.mode : DASH],
  ]);

  const substituted = Boolean(ch.encoder_requested && ch.encoder && ch.encoder_requested !== ch.encoder);
  const warnings = Array.isArray(ch.warnings) ? ch.warnings : [];
  renderKv($('pipeline-kv'), [
    ['encoder', ch.encoder || DASH, substituted ? `(requested ${ch.encoder_requested} \u2014 probe failed)` : ''],
    ['rtmp', ch.rtmp || DASH],
    ['hls', ch.hls || DASH],
    ['liquidsoap', ch.liquidsoap_buffer || DASH],
    ['uptime', ch.state === 'running' ? fmtUptime(ch.uptime_seconds) : DASH],
    ...(ch.fault ? [['fault', ch.fault, ch.fault_detail || '']] : []),
    ...(ch.config_error ? [['config', ch.config_error]] : []),
    ...(warnings.length ? [['warnings', warnings.join(' \u00B7 ')]] : []),
  ]);

  const grid = $('metric-grid');
  clear(grid);
  grid.append(
    metricTile('speed', fmtNum(ch.speed, 3, 'x'), metricTone('speed', ch.speed)),
    metricTile('fps', fmtNum(ch.fps, 1)),
    metricTile('bitrate', typeof ch.bitrate_kbps === 'number' ? `${Math.round(ch.bitrate_kbps)}k` : DASH),
    metricTile('cores', fmtNum(ch.cpu_cores, 2)),
  );
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

function renderAvailableList(listEl, groups, used, filterText, onAdd) {
  clear(listEl);
  const usedSet = new Set(used);
  const needle = filterText.trim().toLowerCase();
  let count = 0;
  for (const group of groups) {
    for (const item of group.items) {
      if (needle && !item.path.toLowerCase().includes(needle)) continue;
      listEl.append(availableRow(item.path, group.tree, usedSet.has(item.path), onAdd));
      count += 1;
    }
  }
  if (!count) listEl.append(el('li', { class: 'muted small', text: 'no media matches' }));
}

function renderAudioTab() {
  const name = state.selected;
  if (!name) return;

  renderOrderedList($('playlist-selected'), state.playlist.draft, () => renderAudioTab());
  renderAvailableList($('audio-available'), state.media.audio, state.playlist.draft, $('audio-filter').value, (path) => {
    state.playlist.draft.push(path);
    renderAudioTab();
  });

  $('playlist-count').textContent = String(state.playlist.draft.length);
  $('playlist-dirty').hidden = !isDirty(state.playlist);
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
  renderAvailableList($('image-available'), state.media.images, state.slides.draft, $('image-filter').value, (path) => {
    state.slides.draft.push(path);
    renderSlidesTab();
  });

  $('slides-count').textContent = String(state.slides.draft.length);
  $('slides-dirty').hidden = !isDirty(state.slides);
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
// Detail: look (visualisation, colour, preset)
// --------------------------------------------------------------------------

function renderLookTab() {
  const name = state.selected;
  if (!name) return;
  const cfg = config(name);
  const active = (state.channels.get(name) || {}).visualisation || (cfg.visualisation || {}).active || '';
  const hot = hotSet(name);

  const list = $('plugin-list');
  clear(list);

  const known = new Set(state.plugins.map((p) => p.name));
  const rows = [...state.plugins];
  for (const plugin of hot) if (!known.has(plugin)) rows.push({ name: plugin, display_name: plugin });

  if (!rows.length) list.append(el('li', { class: 'muted small', text: 'no plugins reported' }));

  for (const plugin of rows) {
    const isHot = hot.includes(plugin.name);
    const isActive = plugin.name === active;
    const cost = plugin.cost && typeof plugin.cost.cores_720p30 === 'number'
      ? `${plugin.cost.cores_720p30.toFixed(2)} cores @720p30`
      : '';
    list.append(el('li', { class: 'plugin', dataset: { hot: String(isHot), active: String(isActive) } }, [
      el('div', { class: 'pmeta' }, [
        el('div', { class: 'pname' }, [
          plugin.display_name || plugin.name,
          el('span', { class: 'pid', text: `  ${plugin.name}` }),
        ]),
        plugin.description ? el('div', { class: 'pdesc', text: plugin.description }) : null,
        el('div', { class: 'pcost', text: isHot ? `hot set \u00B7 ${cost}` : `not in hot set${cost ? ` \u00B7 ${cost}` : ''}` }),
      ]),
      isActive
        ? el('span', { class: 'chip', dataset: { tone: 'info' }, text: 'on air' })
        : el('button', {
            type: 'button',
            text: isHot ? 'Switch' : 'Switch (not hot)',
            onclick: () => switchVisualisation(plugin.name),
          }),
    ]));
  }

  const colour = cfg.colour || {};
  const manual = colour.manual || {};
  for (const radio of document.querySelectorAll('input[name="colour-mode"]')) {
    radio.checked = radio.value === (colour.mode || 'automatic');
  }
  setColourInput('colour-accent', 'colour-accent-hex', manual.accent || '#4FC3F7');
  setColourInput('colour-tint', 'colour-tint-hex', manual.tint || '#101820');
  $('colour-transition').value = numberOr(colour.transition_seconds, 2);

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

function setColourInput(colourId, hexId, value) {
  const hex = /^#[0-9a-fA-F]{6}$/.test(value) ? value : '#000000';
  if (document.activeElement !== $(colourId)) $(colourId).value = hex;
  if (document.activeElement !== $(hexId)) $(hexId).value = hex.toUpperCase();
}

function showPresetDescription() {
  const preset = state.presets.find((p) => p.name === $('preset-select').value);
  $('preset-description').textContent = preset ? preset.description || '' : '';
}

async function switchVisualisation(plugin) {
  const name = state.selected;
  if (!name) return;
  const error = $('viz-error');
  error.hidden = true;
  try {
    await api.setVisualisation(name, plugin);
    toast('ok', 'visualisation', plugin);
  } catch (err) {
    if (err instanceof ApiError && err.error === 'not_in_hot_set') {
      error.hidden = false;
      error.textContent =
        `“${plugin}” is not in this channel's hot set, so its branch was never instantiated. ` +
        'Promoting it rebuilds the filtergraph, which needs a compositor restart — the API will not ' +
        'do that behind your back. Add it to hot_set and restart the channel deliberately.';
      toast('warn', 'not_in_hot_set', plugin);
      return;
    }
    report('visualisation', err);
  }
}

async function saveColour() {
  const name = state.selected;
  if (!name) return;
  const mode = document.querySelector('input[name="colour-mode"]:checked');
  await guard('colour', () => api.setColour(name, {
    mode: mode ? mode.value : 'automatic',
    manual: { accent: $('colour-accent').value, tint: $('colour-tint').value },
    transition_seconds: Number($('colour-transition').value),
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
        `Preset “${preset}” selects a visualisation outside this channel's hot set, so it was rejected ` +
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
  const data = await guard('logs', () => api.logs({
    channel: name,
    service: $('log-service').value,
    lines: $('log-lines').value,
  }));
  if (data === undefined) return;
  const out = $('log-output');
  const pinned = out.scrollTop + out.clientHeight >= out.scrollHeight - 24;
  out.textContent = asText(data) || '(empty)';
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
  unauthorised: 'bad',
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
  if (status === 'unauthorised') openTokenDialog('The event stream rejected that token.');
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
    case 'channel.visualisation':
      mergeStatus(channel, { visualisation: data.visualisation || data.active });
      if (state.selected === channel) renderLookTab();
      logEvent({ tone: 'info', channel, at: data.at, text: `visualisation ${data.visualisation || data.active || ''}` });
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
    visualisation: { active, hot_set: hot },
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

  $('tabs').addEventListener('click', (event) => {
    const tab = event.target.closest('.tab');
    if (tab) selectTab(tab.dataset.tab);
  });

  $('btn-preview-start').addEventListener('click', () => {
    if (state.selected) preview.start(state.selected);
  });
  $('btn-preview-stop').addEventListener('click', () => preview.stop());
  $('preview-audio').addEventListener('change', (event) => preview.setAudio(event.target.checked));
  $('preview-video').addEventListener('click', () => $('preview-video').play().catch(() => {}));

  $('audio-filter').addEventListener('input', () => renderAudioTab());
  $('image-filter').addEventListener('input', () => renderSlidesTab());
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

  bindColour('colour-accent', 'colour-accent-hex');
  bindColour('colour-tint', 'colour-tint-hex');
  $('btn-colour-save').addEventListener('click', () => saveColour());
  $('preset-select').addEventListener('change', showPresetDescription);
  $('btn-preset-apply').addEventListener('click', () => applyPreset());

  $('btn-rule-add').addEventListener('click', () => addScheduleRow());
  $('btn-schedule-save').addEventListener('click', () => saveSchedule());

  $('btn-log-refresh').addEventListener('click', () => refreshLogs());
  $('log-service').addEventListener('change', () => refreshLogs());
  $('log-lines').addEventListener('change', () => refreshLogs());
  $('log-follow').addEventListener('change', syncLogFollow);
}

function bindColour(colourId, hexId) {
  $(colourId).addEventListener('input', () => {
    $(hexId).value = $(colourId).value.toUpperCase();
  });
  $(hexId).addEventListener('change', () => {
    const value = $(hexId).value.trim();
    if (/^#[0-9a-fA-F]{6}$/.test(value)) $(colourId).value = value;
    else $(hexId).value = $(colourId).value.toUpperCase();
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
