// DOM construction, formatting and the tone rules the dashboard reads by.
// Nothing here uses innerHTML: every string that reaches the page came from disk
// or from an operator, so it is set with textContent.

export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = String(value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2), value);
    } else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, String(value));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  node.replaceChildren();
}

export function $(id) {
  return document.getElementById(id);
}

// --------------------------------------------------------------------------
// Formatting
// --------------------------------------------------------------------------

const DASH = '—';

export function fmtUptime(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return DASH;
  const s = Math.floor(seconds);
  const days = Math.floor(s / 86400);
  const hh = String(Math.floor((s % 86400) / 3600)).padStart(2, '0');
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
  const ss = String(s % 60).padStart(2, '0');
  return days > 0 ? `${days}d ${hh}:${mm}` : `${hh}:${mm}:${ss}`;
}

export function fmtNum(value, digits = 1, suffix = '') {
  if (typeof value !== 'number' || !Number.isFinite(value)) return DASH;
  return value.toFixed(digits) + suffix;
}

export function fmtClock(iso) {
  const d = iso ? new Date(iso) : new Date();
  if (Number.isNaN(d.getTime())) return DASH;
  return d.toTimeString().slice(0, 8);
}

export function fmtBytes(bytes) {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return DASH;
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

/** Split a media path into a dim directory and a readable basename. */
export function splitPath(path) {
  const text = String(path || '');
  const cut = text.lastIndexOf('/');
  return cut < 0 ? { dir: '', base: text } : { dir: text.slice(0, cut + 1), base: text.slice(cut + 1) };
}

export function basename(path) {
  return splitPath(path).base;
}

// --------------------------------------------------------------------------
// Tone
// --------------------------------------------------------------------------

// A UI hint only. The authoritative floor is watchdog.min_speed in ambient.yaml,
// which the status payload does not carry.
const SPEED_WARN = 0.99;
const SPEED_FAIL = 0.97;

const STATE_TONE = {
  running: 'ok',
  starting: 'info',
  degraded: 'warn',
  failed: 'bad',
  stopped: 'idle',
};

const HEALTH_TONE = {
  healthy: 'ok',
  starving: 'warn',
  stalled: 'bad',
  disconnected: 'bad',
};

const RANK = { idle: 0, ok: 1, info: 1, warn: 2, bad: 3 };

export function stateTone(state) {
  return STATE_TONE[state] || 'idle';
}

export function healthTone(health) {
  return HEALTH_TONE[health] || 'idle';
}

export function worstTone(...tones) {
  return tones.reduce((a, b) => (RANK[b] > RANK[a] ? b : a), 'idle');
}

/** Tone for the whole channel — what the card stripe and the list read by. */
export function channelTone(ch) {
  if (!ch || ch.state === 'stopped' || !ch.state) return 'idle';
  const tones = [stateTone(ch.state)];
  // A degraded channel that is also stalled or falling behind wall clock escalates
  // to red; degraded on its own stays amber so the two levels stay distinguishable.
  if (ch.state === 'running' || ch.state === 'degraded') {
    tones.push(healthTone(ch.health));
    tones.push(metricTone('speed', ch.speed));
    tones.push(metricTone('rtmp', ch.rtmp));
    tones.push(metricTone('hls', ch.hls));
    tones.push(metricTone('buffer', ch.liquidsoap_buffer));
  }
  return worstTone(...tones);
}

export function metricTone(kind, value) {
  if (value === null || value === undefined || value === '') return 'idle';
  switch (kind) {
    case 'speed':
      if (typeof value !== 'number') return 'idle';
      if (value < SPEED_FAIL) return 'bad';
      if (value < SPEED_WARN) return 'warn';
      return 'ok';
    case 'rtmp':
      return value === 'connected' ? 'ok' : 'bad';
    case 'hls':
      return value === 'ok' ? 'ok' : 'warn';
    case 'buffer':
      return value === 'ok' ? 'ok' : 'warn';
    default:
      return 'idle';
  }
}

// --------------------------------------------------------------------------
// Channel card
// --------------------------------------------------------------------------

function chip(k, v, tone = 'idle') {
  return el('span', { class: 'mchip', dataset: { tone } }, [
    el('span', { class: 'k', text: `${k} ` }),
    document.createTextNode(v),
  ]);
}

/**
 * Build a card once and return an update function. Cards are updated in place
 * rather than rebuilt so a 1 Hz channel.progress does not flicker the list.
 */
export function buildCard(name, handlers) {
  const dot = el('span', { class: 'dot' });
  const nameEl = el('span', { class: 'card-name', text: name });
  const stateChip = el('span', { class: 'chip', text: DASH });
  const healthChip = el('span', { class: 'chip', text: DASH });
  const uptime = el('span', { class: 'card-uptime', text: DASH });

  const trackVal = el('span', { class: 'val', text: DASH });
  const nextVal = el('span', { class: 'val next', text: DASH });
  const trackLine = el('div', { class: 'card-line' }, [
    el('span', { class: 'glyph', text: '\u266A' }),
    trackVal,
    el('span', { class: 'sep', text: '\u2192' }),
    nextVal,
  ]);

  const slideVal = el('span', { class: 'val', text: DASH });
  const vizVal = el('span', { class: 'val', text: DASH });
  const lookLine = el('div', { class: 'card-line' }, [
    el('span', { class: 'glyph', text: '\u25A3' }),
    slideVal,
    el('span', { class: 'glyph', text: '\u25C8' }),
    vizVal,
  ]);

  const metrics = el('div', { class: 'card-metrics' });

  const btnStart = el('button', { type: 'button', text: 'Start', onclick: stop(handlers.onStart) });
  const btnStop = el('button', { type: 'button', text: 'Stop', onclick: stop(handlers.onStop) });
  const btnRestart = el('button', { type: 'button', text: 'Restart', onclick: stop(handlers.onRestart) });

  const root = el('div', { class: 'card', dataset: { channel: name, tone: 'idle' }, onclick: () => handlers.onSelect(name) }, [
    el('div', { class: 'card-top' }, [dot, nameEl, stateChip, healthChip, uptime]),
    trackLine,
    lookLine,
    metrics,
    el('div', { class: 'card-actions' }, [btnStart, btnStop, btnRestart]),
  ]);

  function stop(fn) {
    return (event) => {
      event.stopPropagation();
      fn(name);
    };
  }

  function update(ch, selected) {
    const tone = channelTone(ch);
    root.dataset.tone = tone;
    root.dataset.selected = selected ? 'true' : 'false';
    dot.dataset.tone = tone;

    stateChip.textContent = ch.state || 'unknown';
    stateChip.dataset.tone = stateTone(ch.state);
    healthChip.textContent = ch.health || DASH;
    healthChip.dataset.tone = healthTone(ch.health);
    healthChip.hidden = !ch.health || ch.state === 'stopped';
    uptime.textContent = ch.state === 'running' ? fmtUptime(ch.uptime_seconds) : DASH;

    trackVal.textContent = ch.current_track ? basename(ch.current_track) : DASH;
    trackVal.title = ch.current_track || '';
    nextVal.textContent = ch.next_track ? basename(ch.next_track) : DASH;
    nextVal.title = ch.next_track || '';
    slideVal.textContent = ch.current_slide ? basename(ch.current_slide) : DASH;
    slideVal.title = ch.current_slide || '';
    vizVal.textContent = ch.visualization || DASH;

    const substituted = Boolean(ch.encoder_requested && ch.encoder && ch.encoder_requested !== ch.encoder);
    clear(metrics);
    metrics.append(
      chip('enc', substituted ? `${ch.encoder} \u2190 ${ch.encoder_requested}` : ch.encoder || DASH,
        substituted ? 'warn' : 'idle'),
      chip('fps', fmtNum(ch.fps, 1)),
      chip('speed', fmtNum(ch.speed, 3, 'x'), metricTone('speed', ch.speed)),
      chip('kb/s', typeof ch.bitrate_kbps === 'number' ? String(Math.round(ch.bitrate_kbps)) : DASH),
      chip('cores', fmtNum(ch.cpu_cores, 2)),
      chip('rtmp', ch.rtmp || DASH, metricTone('rtmp', ch.rtmp)),
      chip('hls', ch.hls || DASH, metricTone('hls', ch.hls)),
      chip('buf', ch.liquidsoap_buffer || DASH, metricTone('buffer', ch.liquidsoap_buffer)),
    );

    const stopped = ch.state === 'stopped' || ch.state === 'failed' || !ch.state;
    btnStart.disabled = !stopped;
    btnStop.disabled = ch.state === 'stopped';
    btnRestart.disabled = ch.state === 'stopped';
  }

  return { root, update };
}

// --------------------------------------------------------------------------
// Key/value blocks
// --------------------------------------------------------------------------

export function renderKv(dl, pairs) {
  clear(dl);
  for (const [key, value, sub] of pairs) {
    dl.append(el('dt', { text: key }));
    const dd = el('dd', {}, [value === null || value === undefined || value === '' ? DASH : String(value)]);
    if (sub) dd.append(el('span', { class: 'sub', text: ` ${sub}` }));
    dl.append(dd);
  }
}

/** Flatten an object of unknown shape into label/value pairs for renderKv. */
export function flatten(obj, prefix = '') {
  const pairs = [];
  if (!obj || typeof obj !== 'object') return pairs;
  for (const [key, value] of Object.entries(obj)) {
    const label = prefix ? `${prefix}.${key}` : key;
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      pairs.push(...flatten(value, label));
    } else if (Array.isArray(value)) {
      pairs.push([label, value.map((v) => (typeof v === 'object' ? JSON.stringify(v) : String(v))).join(', ')]);
    } else {
      pairs.push([label, value === null ? DASH : String(value)]);
    }
  }
  return pairs;
}

export function metricTile(label, value, tone = 'idle') {
  return el('div', { class: 'metric', dataset: { tone } }, [
    el('div', { class: 'm-label', text: label }),
    el('div', { class: 'm-value', text: value }),
  ]);
}

// --------------------------------------------------------------------------
// Ordered list editor rows
// --------------------------------------------------------------------------

export function pickRow(path, index, tree, handlers) {
  const { dir, base } = splitPath(path);
  const name = el('span', { class: 'name', title: path }, [
    el('span', { class: 'dir', text: dir }),
    el('span', { class: 'base', text: base }),
  ]);

  const btns = el('div', { class: 'btns' }, [
    el('button', { class: 'icon', type: 'button', title: 'move up', text: '\u2191', onclick: () => handlers.onMove(index, -1) }),
    el('button', { class: 'icon', type: 'button', title: 'move down', text: '\u2193', onclick: () => handlers.onMove(index, 1) }),
    el('button', { class: 'icon', type: 'button', title: 'remove', text: '\u2715', onclick: () => handlers.onRemove(index) }),
  ]);

  return el('li', { draggable: 'true', dataset: { index: String(index) } }, [
    el('span', { class: 'idx', text: String(index + 1) }),
    name,
    tree ? el('span', { class: 'tree', text: tree }) : null,
    btns,
  ]);
}

export function availableRow(path, tree, used, onAdd) {
  const { dir, base } = splitPath(path);
  return el('li', { class: used ? 'used' : '' }, [
    el('button', { class: 'icon', type: 'button', title: 'add', text: '+', onclick: () => onAdd(path) }),
    el('span', { class: 'name', title: path }, [
      el('span', { class: 'dir', text: dir }),
      el('span', { class: 'base', text: base }),
    ]),
    tree ? el('span', { class: 'tree', text: tree }) : null,
  ]);
}

/**
 * One row per file in an upload batch. Built once and updated in place, so a
 * re-render of the media lists beside it cannot wipe a transfer in progress.
 */
export function uploadRow(entry, onCancel) {
  const name = el('span', { class: 'u-name', title: entry.name, text: entry.name });
  const size = el('span', { class: 'u-size num', text: fmtBytes(entry.size) });
  const chip = el('span', { class: 'chip u-state', dataset: { tone: 'idle' }, text: 'queued' });
  const cancel = el('button', { class: 'icon u-cancel', type: 'button', title: 'cancel', text: '\u2715', onclick: () => onCancel(entry) });
  const fill = el('span');
  const bar = el('div', { class: 'u-bar' }, [fill]);
  const detail = el('div', { class: 'u-detail small' });

  const root = el('li', { dataset: { tone: 'idle' } }, [name, size, chip, cancel, bar, detail]);

  function update() {
    const tone = entry.status === 'done' ? 'ok'
      : entry.status === 'failed' ? 'bad'
        : entry.status === 'skipped' || entry.status === 'canceled' ? 'warn'
          : entry.status === 'uploading' ? 'info' : 'idle';
    root.dataset.tone = tone;
    chip.dataset.tone = tone;
    chip.textContent = entry.status === 'uploading'
      ? `${Math.round((entry.progress || 0) * 100)}%`
      : entry.status;
    detail.textContent = entry.detail || '';
    detail.hidden = !entry.detail;
    bar.hidden = entry.status !== 'uploading' && entry.status !== 'queued';
    fill.style.width = `${Math.round((entry.progress || 0) * 100)}%`;
    cancel.hidden = entry.status !== 'uploading' && entry.status !== 'queued';
    name.title = entry.path || entry.name;
  }

  update();
  return { root, update };
}

/** HTML5 drag reorder over rows carrying data-index. */
export function makeSortable(list, onReorder) {
  let from = null;

  list.addEventListener('dragstart', (event) => {
    const li = event.target.closest('li[data-index]');
    if (!li) return;
    from = Number(li.dataset.index);
    li.classList.add('dragging');
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', String(from));
  });

  list.addEventListener('dragover', (event) => {
    if (from === null) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    const li = event.target.closest('li[data-index]');
    for (const row of list.children) row.classList.remove('drop-before', 'drop-after');
    if (!li) return;
    const box = li.getBoundingClientRect();
    li.classList.add(event.clientY < box.top + box.height / 2 ? 'drop-before' : 'drop-after');
  });

  list.addEventListener('drop', (event) => {
    if (from === null) return;
    event.preventDefault();
    const li = event.target.closest('li[data-index]');
    let to = list.children.length - 1;
    if (li) {
      const box = li.getBoundingClientRect();
      to = Number(li.dataset.index);
      if (event.clientY > box.top + box.height / 2 && to < from) to += 1;
      if (event.clientY < box.top + box.height / 2 && to > from) to -= 1;
    }
    const start = from;
    cleanup();
    if (start !== to) onReorder(start, to);
  });

  list.addEventListener('dragend', cleanup);

  function cleanup() {
    from = null;
    for (const row of list.children) row.classList.remove('dragging', 'drop-before', 'drop-after');
  }
}

// --------------------------------------------------------------------------
// Toasts
// --------------------------------------------------------------------------

export function toast(tone, title, detail, host = $('toasts')) {
  const node = el('div', { class: 'toast', dataset: { tone } }, [
    el('div', { class: 't-title', text: title }),
    detail ? el('div', { class: 't-detail', text: detail }) : null,
  ]);
  node.addEventListener('click', () => node.remove());
  host.prepend(node);
  setTimeout(() => node.remove(), tone === 'bad' ? 12000 : 6000);
  while (host.children.length > 6) host.lastElementChild.remove();
  return node;
}
