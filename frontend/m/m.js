// Touch UI entry point: routing, the app bar, the tab bar and the connection banner.
// The desktop panel lives at ../ and is untouched; this page shares only its API
// client, event stream and preview modules.

import { hasToken } from '../api.js';
import { channelTone, fmtUptime, healthTone, stateTone } from '../render.js';
import {
  anyDirty, audioDirty, bindUi, boot, loadChannelDetail, scheduleDirty, slidesDirty, state, subscribe,
} from './data/store.js';
import { icon } from './lib/icons.js';
import { hashFor } from './lib/nav.js';
import { initLock, isLocked, lock, onLockChange, secondsLeft, unlock } from './lib/lock.js';
import {
  applyLockToDom, btn, el, empty, haptic, installGuard, spinner, toast,
} from './lib/ui.js';
import { openMenuSheet, openTokenSheet } from './screens/sheets.js';

const TABS = [
  { id: 'live', label: 'Live', icon: 'activity' },
  { id: 'audio', label: 'Audio', icon: 'music' },
  { id: 'visuals', label: 'Visuals', icon: 'image' },
  { id: 'sounds', label: 'Sounds', icon: 'speaker' },
  { id: 'more', label: 'More', icon: 'more' },
];
const TAB_IDS = TABS.map((t) => t.id);

const MORE_TITLES = {
  schedule: 'Schedule', logs: 'Logs', delivery: 'Delivery', output: 'Output size', danger: 'Delete channel',
};

const view = document.getElementById('view');
const appbar = document.getElementById('appbar');
const tabbar = document.getElementById('tabbar');
const banner = document.getElementById('banner');

let current = null;
let chrome = null;
let route = null;
let lastEntered = null;
const scrollMemory = new Map();

// --------------------------------------------------------------------------
// Routing
// --------------------------------------------------------------------------

function parseRoute() {
  const parts = location.hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  if (!parts.length) return { screen: 'home' };
  if (parts[0] === 'activity') return { screen: 'activity' };
  if (parts[0] === 'c' && parts[1]) {
    const tab = TAB_IDS.includes(parts[2]) ? parts[2] : 'live';
    return { screen: 'channel', name: parts[1], tab, sub: parts[3] || '' };
  }
  return { screen: 'home' };
}

function keyOf(r) {
  if (r.screen !== 'channel') return r.screen;
  return `c:${r.name}:${r.tab}:${r.sub}`;
}

/** Tab changes replace the entry so Back leaves the channel; pages inside More push. */
function go(hash, { replace = false } = {}) {
  if (replace) {
    history.replaceState(history.state, '', hash);
    render();
  } else if (location.hash === hash) {
    render();
  } else {
    location.hash = hash;
  }
}

window.addEventListener('hashchange', render);

function context(r) {
  return {
    route: r,
    name: r.name,
    tab: r.tab,
    sub: r.sub,
    go,
    hashFor,
    // Remembers a segment in the address without growing the history.
    replaceSub(sub) {
      history.replaceState(history.state, '', hashFor(r.name, r.tab, sub));
    },
  };
}

async function loadScreen(r) {
  if (r.screen === 'home') return import('./screens/home.js').then((m) => m.mountHome);
  if (r.screen === 'activity') return import('./screens/home.js').then((m) => m.mountActivity);
  switch (r.tab) {
    case 'audio': return import('./screens/audio.js').then((m) => m.mountAudio);
    case 'visuals': return import('./screens/visuals.js').then((m) => m.mountVisuals);
    case 'sounds': return import('./screens/sounds.js').then((m) => m.mountSounds);
    case 'more': return import('./screens/more.js').then((m) => m.mountMore);
    default: return import('./screens/live.js').then((m) => m.mountLive);
  }
}

let renderToken = 0;

async function render() {
  route = parseRoute();
  const key = keyOf(route);
  if (current && current.key === key) {
    schedulePaint(['route']);
    return;
  }

  const token = (renderToken += 1);
  if (current) {
    scrollMemory.set(current.key, window.scrollY);
    if (current.instance && current.instance.destroy) current.instance.destroy();
    current = null;
  }
  state.current = route.screen === 'channel' ? route.name : null;
  buildChrome(route);
  view.replaceChildren();

  if (route.screen === 'channel') {
    if (!state.channelsLoaded) {
      view.append(spinner('Loading channels\u2026'));
      current = { key, pending: true, instance: null };
      paintChrome();
      return;
    }
    if (!state.channels.has(route.name)) {
      view.append(
        empty(`No channel called \u201C${route.name}\u201D`, 'It may have been deleted, or the link is out of date.'),
        el('div', { class: 'row', style: 'justify-content:center' }, [
          btn({ label: 'All channels', variant: 'primary', onClick: () => go('#/') }),
        ]),
      );
      current = { key, pending: true, instance: null };
      paintChrome();
      return;
    }
    if (lastEntered !== route.name) {
      lastEntered = route.name;
      loadChannelDetail(route.name);
    }
  } else {
    lastEntered = null;
  }

  const mountFn = await loadScreen(route);
  if (token !== renderToken) return;
  view.replaceChildren();
  const instance = mountFn(view, context(route));
  current = { key, instance, route };
  window.scrollTo(0, route.screen === 'home' ? scrollMemory.get(key) || 0 : 0);
  paintChrome();
  paintLock();
}

// --------------------------------------------------------------------------
// Chrome: app bar and tab bar
// --------------------------------------------------------------------------

function lockPill() {
  const pill = el('button', { type: 'button', class: 'lockpill keep-live' });
  pill.addEventListener('click', () => {
    haptic(10);
    if (isLocked()) unlock();
    else lock();
  });
  return pill;
}

function buildChrome(r) {
  const pill = lockPill();
  const dot = el('span', { class: 'dot', dataset: { tone: 'idle' } });
  const title = el('span', { text: '' });
  const sub = el('div', { class: 'appbar__sub', text: '' });
  const titleRow = el('div', { class: 'appbar__title' }, [r.screen === 'channel' && !(r.tab === 'more' && r.sub) ? dot : null, title]);
  const menu = el('button', {
    type: 'button', class: 'iconbtn iconbtn--ghost iconbtn--md', 'aria-label': 'Menu', onclick: openMenuSheet,
  }, [icon('more', { size: 24, stroke: 3 })]);

  let lead;
  if (r.screen === 'channel') {
    const up = r.tab === 'more' && r.sub ? hashFor(r.name, 'more') : '#/';
    lead = el('button', {
      type: 'button', class: 'iconbtn iconbtn--ghost iconbtn--md', 'aria-label': r.tab === 'more' && r.sub ? 'Back to More' : 'All channels',
      onclick: () => go(up),
    }, [icon('back', { size: 26 })]);
  } else {
    lead = el('span', { class: 'appbar__brand' }, [icon('wave', { size: 26 })]);
  }

  appbar.replaceChildren(lead, el('div', { class: 'appbar__titles' }, [titleRow, sub]), pill, menu);
  chrome = { r, dot, title, sub, pill };

  tabbar.replaceChildren();
  if (r.screen === 'channel') {
    for (const tab of TABS) {
      const b = el('button', {
        type: 'button', class: 'tab', dataset: { tab: tab.id },
        onclick: () => go(hashFor(r.name, tab.id), { replace: true }),
      }, [icon(tab.icon, { size: 24 }), el('span', { text: tab.label })]);
      tabbar.append(b);
    }
  } else {
    tabbar.append(
      el('button', { type: 'button', class: 'tab', dataset: { tab: 'home' }, onclick: () => go('#/', { replace: true }) },
        [icon('channels', { size: 24 }), el('span', { text: 'Channels' })]),
      el('button', { type: 'button', class: 'tab', dataset: { tab: 'activity' }, onclick: () => go('#/activity', { replace: true }) },
        [icon('activity', { size: 24 }), el('span', { text: 'Activity' })]),
      el('button', { type: 'button', class: 'tab', onclick: openMenuSheet }, [icon('more', { size: 24, stroke: 3 }), el('span', { text: 'Menu' })]),
    );
  }
}

function fmtLeft(seconds) {
  if (seconds === null) return 'Unlocked';
  const m = Math.floor(seconds / 60);
  const s = String(seconds % 60).padStart(2, '0');
  return `${m}:${s}`;
}

function paintLock() {
  if (!chrome) return;
  const locked = isLocked();
  const left = secondsLeft();
  chrome.pill.dataset.locked = String(locked);
  chrome.pill.replaceChildren(icon(locked ? 'lock' : 'unlock', { size: 18 }), el('span', { text: locked ? 'Locked' : fmtLeft(left) }));
  chrome.pill.setAttribute('aria-label', locked
    ? 'Controls are locked. Tap to unlock.'
    : 'Controls are unlocked. Tap to lock.');
}

function paintChrome() {
  if (!chrome) return;
  const r = chrome.r;
  if (r.screen === 'channel') {
    const ch = state.channels.get(r.name) || {};
    const tone = channelTone(ch);
    chrome.dot.dataset.tone = tone;
    if (r.tab === 'more' && r.sub) {
      chrome.title.textContent = MORE_TITLES[r.sub] || 'More';
      chrome.sub.textContent = r.name;
    } else {
      chrome.title.textContent = r.name;
      const bits = [ch.state || 'unknown'];
      if (ch.health && ch.state !== 'stopped') bits.push(ch.health);
      if (ch.state === 'running') bits.push(fmtUptime(ch.uptime_seconds));
      chrome.sub.textContent = bits.join(' \u00B7 ');
    }
    for (const tab of tabbar.querySelectorAll('.tab[data-tab]')) {
      const active = tab.dataset.tab === r.tab;
      if (active) tab.setAttribute('aria-current', 'page');
      else tab.removeAttribute('aria-current');
      const dirty = tab.dataset.tab === 'audio' ? audioDirty(r.name)
        : tab.dataset.tab === 'visuals' ? slidesDirty(r.name)
          : tab.dataset.tab === 'more' ? scheduleDirty(r.name) : false;
      let dotEl = tab.querySelector('.tab__dot');
      if (dirty && !dotEl) tab.append(el('span', { class: 'tab__dot', title: 'unsaved changes' }));
      if (!dirty && dotEl) dotEl.remove();
    }
  } else {
    const channels = [...state.channels.values()];
    const running = channels.filter((c) => c.state === 'running').length;
    chrome.title.textContent = r.screen === 'activity' ? 'Activity' : 'Channels';
    chrome.sub.textContent = state.channelsLoaded
      ? `${running} running \u00B7 ${channels.length} channel${channels.length === 1 ? '' : 's'}`
      : '';
    for (const tab of tabbar.querySelectorAll('.tab[data-tab]')) {
      const active = (tab.dataset.tab === 'home' && r.screen === 'home') || (tab.dataset.tab === 'activity' && r.screen === 'activity');
      if (active) tab.setAttribute('aria-current', 'page');
      else tab.removeAttribute('aria-current');
    }
  }
  paintBanner();
}

function paintBanner() {
  let message = '';
  let tone = 'warn';
  let action = null;
  if (!hasToken()) {
    message = 'No API token on this phone.';
    tone = 'bad';
    action = { label: 'Add token', run: () => openTokenSheet('') };
  } else if (state.conn === 'unauthorized') {
    message = 'The control plane rejected the token.';
    tone = 'bad';
    action = { label: 'Fix', run: () => openTokenSheet('The control plane rejected that token.') };
  } else if (state.reachable === false) {
    message = 'Cannot reach the control plane.';
    tone = 'bad';
  } else if (['reconnecting', 'dropped'].includes(state.conn)) {
    message = 'Live updates lost \u2014 reconnecting\u2026';
  }
  banner.hidden = !message;
  if (!message) return;
  banner.dataset.tone = tone;
  banner.replaceChildren(
    icon('warning', { size: 20 }),
    el('span', { text: message }),
    action ? btn({ label: action.label, size: 'sm', variant: 'secondary', onClick: action.run }) : null,
  );
}

// --------------------------------------------------------------------------
// Updates
// --------------------------------------------------------------------------

let pending = new Set();
let frame = 0;

function schedulePaint(topics) {
  for (const t of topics) pending.add(t);
  if (!frame) frame = requestAnimationFrame(flush);
}

function flush() {
  frame = 0;
  const topics = pending;
  pending = new Set();
  if (current && current.pending && state.channelsLoaded) {
    // The channel list arrived after a deep link was opened.
    current = null;
    render();
    return;
  }
  paintChrome();
  if (current && current.instance && current.instance.update) current.instance.update(topics);
}

subscribe((topic) => schedulePaint([topic]));

onLockChange(() => {
  paintLock();
  applyLockToDom(document);
  schedulePaint(['lock']);
});
setInterval(() => { if (!isLocked()) paintLock(); }, 1000);

// New nodes arrive constantly; the lock has to reach them without every screen asking.
let lockFrame = 0;
new MutationObserver(() => {
  if (lockFrame) return;
  lockFrame = requestAnimationFrame(() => {
    lockFrame = 0;
    applyLockToDom(document);
  });
}).observe(document.body, { childList: true, subtree: true });

window.addEventListener('beforeunload', (event) => {
  if (anyDirty()) {
    event.preventDefault();
    event.returnValue = '';
  }
});

// --------------------------------------------------------------------------
// Boot
// --------------------------------------------------------------------------

async function main() {
  if (new URLSearchParams(location.search).has('mock')) await import('../mock/mock-api.js');
  initLock();
  installGuard();
  bindUi({ toast, askToken: (message) => openTokenSheet(message) });
  render();
  if (!hasToken()) {
    openTokenSheet('Paste the token from the host\u2019s .env (AMBIENT_API_TOKEN).');
    return;
  }
  boot();
}

main();
