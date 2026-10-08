// App-level sheets: menu, settings, API token, system and capacity, new channel.

import { clearToken, setToken } from '../../api.js';
import { flatten, fmtNum } from '../../render.js';
import { createChannel } from '../data/actions.js';
import {
  boot, loadSystem, pingHealth, refreshCapacity, refreshChannels, resetForToken, state, stream, toast as storeToast,
} from '../data/store.js';
import {
  btn, callout, el, ensureUnlocked, field, kv, note, openSheet, selectInput, stepper, switchRow, textInput, toast,
} from '../lib/ui.js';
import { getPrefs, setPrefs } from '../lib/lock.js';
import { icon } from '../lib/icons.js';

// --------------------------------------------------------------------------
// Token
// --------------------------------------------------------------------------

let tokenSheet = null;

export function openTokenSheet(message, { onSaved } = {}) {
  if (tokenSheet) return tokenSheet;
  const input = textInput({ type: 'password', placeholder: 'AMBIENT_API_TOKEN', mut: false, mono: true });
  input.setAttribute('autocomplete', 'off');
  const error = message ? callout(message, 'bad') : null;

  const save = () => {
    const value = input.value.trim();
    if (!value) return;
    setToken(value);
    input.value = '';
    tokenSheet.close(true);
    resetForToken();
    boot();
    if (onSaved) onSaved();
  };
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') save();
  });

  tokenSheet = openSheet({
    title: 'API token',
    body: [
      el('p', { class: 'sheet__text', text: 'Every endpoint except health needs a bearer token. It is kept in this browser\u2019s localStorage and only ever sent in an Authorization header.' }),
      error,
      field('Token', input, 'The same token the desktop panel uses. It is the AMBIENT_API_TOKEN value from the host\u2019s .env.'),
    ],
    footer: [
      btn({ label: 'Save token', variant: 'primary', size: 'lg', block: true, onClick: save }),
      btn({
        label: 'Forget token on this phone', variant: 'quiet', block: true,
        onClick: () => {
          clearToken();
          stream.stop();
          state.booted = false;
          storeToast('warn', 'token', 'forgotten on this browser');
          tokenSheet.close(false);
        },
      }),
    ],
    onClose: () => { tokenSheet = null; },
  });
  return tokenSheet;
}

// --------------------------------------------------------------------------
// System
// --------------------------------------------------------------------------

function pickNumber(obj, keys) {
  for (const key of keys) {
    let node = obj;
    for (const part of key.split('.')) node = node && typeof node === 'object' ? node[part] : undefined;
    if (typeof node === 'number' && Number.isFinite(node)) return node;
  }
  return null;
}

/** Cores in use, the host total and what is left, however the capacity payload spells them. */
export function capacityNumbers() {
  const cap = state.capacity || {};
  const channels = [...state.channels.values()];
  const used = pickNumber(cap, ['cores_used', 'used_cores', 'projected_cores', 'projected', 'cores.used'])
    ?? channels.reduce((sum, c) => sum + (typeof c.cpu_cores === 'number' ? c.cpu_cores : 0), 0);
  const total = pickNumber(cap, ['cores_total', 'total_cores', 'cores_available_total', 'cores', 'host_cores', 'cores.total'])
    ?? pickNumber(state.system || {}, ['cores', 'cpu_cores', 'host.cores']);
  const headroom = pickNumber(cap, ['headroom_cores', 'cores_available', 'available_cores', 'headroom', 'available', 'cores.available'])
    ?? (total === null ? null : total - used);
  const max = pickNumber(cap, ['max_channels', 'limits.max_channels', 'channels_max'])
    ?? pickNumber(state.system || {}, ['max_channels', 'limits.max_channels']);
  return { used, total, headroom, max };
}

export async function openSystemSheet() {
  const body = el('div', { class: 'stack' });
  const sheet = openSheet({ title: 'System & capacity', tall: true, body });

  const render = () => {
    const omit = { ...(state.system || {}) };
    delete omit.encoders;
    const { used, total } = capacityNumbers();
    const ratio = total ? Math.max(0, Math.min(1, used / total)) : 0;
    const tone = ratio > 0.9 ? 'bad' : ratio > 0.75 ? 'warn' : 'ok';

    const encoders = state.system && state.system.encoders;
    const entries = Array.isArray(encoders)
      ? encoders.map((e) => (typeof e === 'string' ? { name: e, available: true } : { ...e, name: e.encoder || e.name }))
      : Object.entries(encoders || {}).map(([name, value]) => ({
        name,
        available: value === true || (value && value.available === true),
        detail: value && typeof value === 'object' ? value.detail || value.reason || '' : '',
      }));

    const bar = el('div', { class: 'meter', dataset: { tone } }, [el('span', { style: `width:${(ratio * 100).toFixed(1)}%` })]);
    body.replaceChildren(
      el('section', { class: 'card' }, [
        el('h3', { class: 'card__title', text: 'Capacity' }),
        el('div', { class: 'row row--nowrap' }, [
          el('span', { class: 'num', text: total === null ? `${fmtNum(used, 2)} cores` : `${used.toFixed(2)} / ${total.toFixed(1)} cores` }),
        ]),
        bar,
        kv(flatten(state.capacity || {})),
      ]),
      el('section', { class: 'card' }, [
        el('h3', { class: 'card__title', text: 'Encoders' }),
        note('Probe results, not ffmpeg -encoders.'),
        entries.length
          ? el('ul', { class: 'plain-list' }, entries.map((e) => el('li', { class: 'row row--nowrap' }, [
            el('span', { class: 'dot', dataset: { tone: e.available ? 'ok' : 'bad' } }),
            el('span', { class: 'mono', text: e.name || '?' }),
            el('span', { class: 'muted', text: e.detail || e.reason || (e.available ? 'probe ok' : 'unavailable') }),
          ])))
          : note('no probe results reported'),
      ]),
      el('section', { class: 'card' }, [
        el('h3', { class: 'card__title', text: 'Host' }),
        kv(flatten(omit)),
      ]),
    );
  };

  render();
  await Promise.all([loadSystem(), refreshCapacity()]);
  render();
  return sheet;
}

// --------------------------------------------------------------------------
// Settings
// --------------------------------------------------------------------------

export function openSettingsSheet() {
  const prefs = getPrefs();
  const minutes = stepper({
    value: Math.round((prefs.autoLockSeconds || 120) / 60 * 2) / 2,
    min: 0.5, max: 30, step: 0.5, decimals: 1, unit: 'min', mut: false, ariaLabel: 'auto-lock minutes',
    onChange: (v) => setPrefs({ autoLockSeconds: Math.round(v * 60) }),
  });
  const never = switchRow({
    label: 'Never lock automatically',
    hint: 'Controls stay unlocked until you lock them. Not recommended for a phone you carry.',
    checked: !prefs.autoLockSeconds, mut: false,
    onChange: (on) => setPrefs({ autoLockSeconds: on ? 0 : Math.round(minutes.getValue() * 60) }),
  });
  openSheet({
    title: 'Settings',
    body: [
      el('section', { class: 'card' }, [
        el('h3', { class: 'card__title', text: 'Control lock' }),
        note('The page opens view-only. Unlock to make changes; it locks again by itself when you stop touching it.'),
        switchRow({
          label: 'Start locked', hint: 'Recommended. Opening the page never leaves controls live.',
          checked: prefs.lockOnOpen, mut: false, onChange: (on) => setPrefs({ lockOnOpen: on }),
        }),
        field('Lock after', minutes, 'Counted from your last touch.'),
        never,
      ]),
      el('section', { class: 'card' }, [
        el('h3', { class: 'card__title', text: 'How changes are protected' }),
        el('ul', { class: 'bullets' }, [
          el('li', { text: 'Start, stop, restart, resolution changes on a live channel, delete, and sending a sound to the stream: press and hold.' }),
          el('li', { text: 'Saving lists or schedules, delivery changes, presets, and switching or restarting the visualizer: confirm in a sheet.' }),
          el('li', { text: 'Color, opacity and the visualizer settings you stage are applied by their own Apply button.' }),
          el('li', { text: 'Skip and play-now: tap twice.' }),
          el('li', { text: 'Unsaved edits are kept on this page and flagged until you save or discard them.' }),
        ]),
      ]),
    ],
  });
}

// --------------------------------------------------------------------------
// New channel
// --------------------------------------------------------------------------

export function openCreateSheet() {
  if (!ensureUnlocked()) return;
  const errorHost = el('div');
  const name = textInput({
    placeholder: 'lofi', mono: true, mut: false, required: true,
    pattern: '[a-z0-9]([a-z0-9_\\-]{0,30}[a-z0-9])?',
  });
  const genre = textInput({ placeholder: 'lo-fi hip hop', mut: false });
  const resolution = selectInput({ options: ['480p', '720p', '1080p', '1440p', '2160p'], value: '720p', mut: false });
  const fps = stepper({ value: 30, min: 1, max: 60, step: 1, mut: false, ariaLabel: 'frames per second' });
  const encoder = selectInput({ options: ['libx264', 'h264_nvenc', 'h264_qsv'], value: 'libx264', mut: false });
  const viz = selectInput({
    options: state.plugins.map((p) => ({ value: p.name, label: p.display_name || p.name })), mut: false,
  });

  const submit = btn({
    label: 'Create channel', variant: 'primary', size: 'lg', block: true,
    onClick: async () => {
      errorHost.replaceChildren();
      const value = name.value.trim();
      if (!value || !name.checkValidity()) {
        errorHost.append(callout('Use lowercase letters, digits, - and _ (start and end with a letter or digit).', 'bad'));
        name.focus();
        return;
      }
      submit.disabled = true;
      const result = await createChannel({
        name: value,
        genre: genre.value.trim(),
        resolution: resolution.value,
        fps: fps.getValue(),
        encoder: encoder.value,
        visualization: { enabled: true, active: viz.value },
      });
      submit.disabled = false;
      if (!result.ok) {
        errorHost.append(callout(result.error, 'bad'));
        return;
      }
      sheet.close(true).then(() => { location.hash = `#/c/${encodeURIComponent(value)}`; });
    },
  });

  const sheet = openSheet({
    title: 'New channel',
    tall: true,
    body: [
      field('Name', name, 'Lowercase letters, digits, - and _.'),
      field('Genre', genre),
      field('Resolution', resolution),
      field('Frame rate', fps),
      field('Encoder', encoder),
      field('Active visualization', viz),
      note('The YouTube stream key is not set here. Put it in channels/<name>/.env as YOUTUBE_STREAM_KEY; the relay reads it when the path becomes ready.'),
      errorHost,
    ],
    footer: [submit, btn({ label: 'Cancel', variant: 'secondary', size: 'lg', block: true, onClick: () => sheet.close(false) })],
  });
  return sheet;
}

// --------------------------------------------------------------------------
// Menu
// --------------------------------------------------------------------------

function menuRow({ iconName, label, sub, onClick, tone }) {
  return el('button', { type: 'button', class: 'menurow', dataset: { tone: tone || '' }, onclick: onClick }, [
    el('span', { class: 'menurow__icon' }, [icon(iconName, { size: 22 })]),
    el('span', { class: 'menurow__text' }, [
      el('span', { class: 'menurow__label', text: label }),
      sub ? el('span', { class: 'menurow__sub', text: sub }) : null,
    ]),
    icon('forward', { size: 18 }),
  ]);
}

export function openMenuSheet() {
  const sheet = openSheet({
    title: 'Menu',
    subtitle: state.version ? `Control plane ${state.version}` : undefined,
    body: [
      menuRow({
        iconName: 'server', label: 'System & capacity', sub: 'Host, encoders and headroom',
        onClick: () => sheet.close().then(openSystemSheet),
      }),
      menuRow({
        iconName: 'lock', label: 'Settings', sub: 'Control lock and auto-lock',
        onClick: () => sheet.close().then(openSettingsSheet),
      }),
      menuRow({
        iconName: 'key', label: 'API token', sub: 'Change or forget this phone\u2019s token',
        onClick: () => sheet.close().then(() => openTokenSheet('')),
      }),
      menuRow({
        iconName: 'restart', label: 'Refresh everything', sub: 'Re-read channels, capacity and health',
        onClick: () => {
          sheet.close();
          refreshChannels();
          refreshCapacity();
          pingHealth();
          toast('info', 'refreshing');
        },
      }),
      menuRow({
        iconName: 'desktop', label: 'Open the desktop site', sub: 'The full operator panel',
        onClick: () => {
          try { localStorage.setItem('ambient.ui', 'desktop'); } catch { /* the choice just will not stick */ }
          location.href = '../?desktop';
        },
      }),
    ],
  });
  return sheet;
}
