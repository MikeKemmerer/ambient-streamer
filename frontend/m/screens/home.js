// Home: every channel at a glance, and the activity feed.

import { basename, channelTone, fmtNum, fmtUptime, healthTone, metricTone, stateTone } from '../../render.js';
import { hashFor } from '../lib/nav.js';
import { state } from '../data/store.js';
import { icon } from '../lib/icons.js';
import { btn, callout, chip, el, empty, segmented, spinner } from '../lib/ui.js';
import { capacityNumbers, openCreateSheet } from './sheets.js';

const DASH = '\u2014';

// --------------------------------------------------------------------------
// Channel card
// --------------------------------------------------------------------------

function metricChip(label, value, tone = 'idle') {
  return el('span', { class: 'mchip', dataset: { tone } }, [
    el('span', { class: 'mchip__k', text: label }),
    el('span', { class: 'mchip__v num', text: value }),
  ]);
}

function channelCard(name) {
  const dot = el('span', { class: 'dot' });
  const nameEl = el('span', { class: 'chcard__name', text: name });
  const stateChip = chip(DASH);
  const healthChip = chip(DASH);
  const uptime = el('span', { class: 'chcard__uptime num', text: '' });
  const track = el('span', { class: 'chcard__track clamp2', text: DASH });
  const next = el('span', { class: 'chcard__next clamp1', text: '' });
  const look = el('span', { class: 'chcard__look clamp1', text: '' });
  const fault = el('div', { class: 'chcard__fault', hidden: true });
  const metrics = el('div', { class: 'mchips' });

  const root = el('a', { class: 'chcard', href: hashFor(name), dataset: { tone: 'idle' }, 'aria-label': `Open ${name}` }, [
    el('div', { class: 'chcard__top' }, [dot, nameEl, el('span', { class: 'spacer' }), uptime, icon('forward', { size: 18 })]),
    el('div', { class: 'row chcard__chips' }, [stateChip, healthChip]),
    el('div', { class: 'chcard__now' }, [
      el('span', { class: 'chcard__glyph', 'aria-hidden': 'true' }, [icon('music', { size: 16 })]),
      el('span', { class: 'chcard__text' }, [track, next]),
    ]),
    el('div', { class: 'chcard__now' }, [
      el('span', { class: 'chcard__glyph', 'aria-hidden': 'true' }, [icon('image', { size: 16 })]),
      el('span', { class: 'chcard__text' }, [look]),
    ]),
    fault,
    metrics,
  ]);

  function update() {
    const ch = state.channels.get(name) || { name };
    const tone = channelTone(ch);
    root.dataset.tone = tone;
    dot.dataset.tone = tone;
    stateChip.textContent = ch.state || 'unknown';
    stateChip.dataset.tone = stateTone(ch.state);
    healthChip.textContent = ch.health || DASH;
    healthChip.dataset.tone = healthTone(ch.health);
    healthChip.hidden = !ch.health || ch.state === 'stopped';
    uptime.textContent = ch.state === 'running' ? fmtUptime(ch.uptime_seconds) : '';

    track.textContent = ch.current_track ? basename(ch.current_track) : DASH;
    next.textContent = ch.next_track ? `next: ${basename(ch.next_track)}` : '';
    next.hidden = !ch.next_track;
    const vizOff = ch.visualization_enabled === false;
    look.textContent = [ch.current_slide ? basename(ch.current_slide) : null, vizOff ? 'viz off' : ch.visualization]
      .filter(Boolean).join('  \u00B7  ') || DASH;

    const problem = ch.config_error || (ch.fault ? `${ch.fault}${ch.fault_detail ? `: ${ch.fault_detail}` : ''}` : '');
    fault.hidden = !problem;
    fault.textContent = problem;

    const live = ch.state === 'running' || ch.state === 'degraded' || ch.state === 'starting';
    metrics.replaceChildren();
    metrics.hidden = !live;
    if (live) {
      metrics.append(
        metricChip('speed', fmtNum(ch.speed, 3, 'x'), metricTone('speed', ch.speed)),
        metricChip('fps', fmtNum(ch.fps, 1)),
        metricChip('kb/s', typeof ch.bitrate_kbps === 'number' ? String(Math.round(ch.bitrate_kbps)) : DASH),
      );
      if (metricTone('rtmp', ch.rtmp) === 'bad') metrics.append(metricChip('rtmp', ch.rtmp || DASH, 'bad'));
      if (metricTone('hls', ch.hls) === 'warn') metrics.append(metricChip('hls', ch.hls || DASH, 'warn'));
      if (metricTone('buffer', ch.liquidsoap_buffer) === 'warn') metrics.append(metricChip('buffer', ch.liquidsoap_buffer || DASH, 'warn'));
    }
  }

  update();
  return { root, update };
}

// --------------------------------------------------------------------------
// Home
// --------------------------------------------------------------------------

function fleetSummary(host) {
  const channels = [...state.channels.values()];
  const count = (s) => channels.filter((c) => c.state === s).length;
  const attention = channels.filter((c) => channelTone(c) === 'bad' || c.state === 'degraded');
  const { used, total, headroom } = capacityNumbers();

  const stat = (label, value, tone) => el('div', { class: 'stat', dataset: { tone: tone || '' } }, [
    el('div', { class: 'stat__v num', text: value }),
    el('div', { class: 'stat__k', text: label }),
  ]);

  const nodes = [
    el('div', { class: 'stats' }, [
      stat('running', String(count('running')), count('running') ? 'ok' : ''),
      stat('degraded', String(count('degraded')), count('degraded') ? 'warn' : ''),
      stat('failed', String(count('failed')), count('failed') ? 'bad' : ''),
      stat('headroom', headroom === null ? DASH : headroom.toFixed(1), headroom !== null && headroom < 0.5 ? 'bad' : ''),
    ]),
  ];
  if (total !== null) {
    nodes.push(el('p', { class: 'note', text: `${used.toFixed(2)} of ${total.toFixed(1)} cores in use` }));
  }
  if (attention.length) {
    nodes.push(callout(
      `${attention.length === 1 ? attention[0].name : `${attention.length} channels`} need${attention.length === 1 ? 's' : ''} attention: ${attention.map((c) => c.name).join(', ')}.`,
      'bad',
    ));
  }
  host.replaceChildren(...nodes);
}

export function mountHome(host) {
  const summary = el('section', { class: 'card summary' });
  const list = el('div', { class: 'stack stack--tight' });
  const skipped = el('p', { class: 'note' });
  const addBtn = btn({ label: 'New channel', icon: 'plus', variant: 'secondary', block: true, mut: true, onClick: () => openCreateSheet() });
  const cards = new Map();

  host.append(el('div', { class: 'stack' }, [summary, list, skipped, addBtn]));

  function paintList() {
    const names = [...state.channels.keys()].sort();
    for (const name of names) {
      if (!cards.has(name)) cards.set(name, channelCard(name));
    }
    for (const [name, card] of cards) {
      if (!state.channels.has(name)) {
        card.root.remove();
        cards.delete(name);
      }
    }
    names.forEach((name, i) => {
      const card = cards.get(name);
      if (list.children[i] !== card.root) list.insertBefore(card.root, list.children[i] || null);
    });
    if (!state.channelsLoaded) {
      list.replaceChildren(spinner('Loading channels\u2026'));
      cards.clear();
    } else if (!names.length) {
      list.replaceChildren(empty('No channels yet', 'Create one to get started.'));
    } else {
      for (const child of [...list.children]) if (![...cards.values()].some((c) => c.root === child)) child.remove();
    }
    skipped.hidden = !state.skipped.length;
    skipped.textContent = state.skipped.length
      ? `${state.skipped.length} director${state.skipped.length === 1 ? 'y' : 'ies'} skipped: ${state.skipped.map((s) => s.name).join(', ')}`
      : '';
    fleetSummary(summary);
  }

  paintList();
  return {
    update(topics) {
      if (topics.has('channels') || topics.has('gone') || topics.has('capacity')) paintList();
      else if (topics.has('status')) {
        for (const card of cards.values()) card.update();
        fleetSummary(summary);
      }
    },
  };
}

// --------------------------------------------------------------------------
// Activity
// --------------------------------------------------------------------------

function fmtTime(date) {
  return date.toTimeString().slice(0, 8);
}

export function mountActivity(host) {
  const ui = { filter: 'all' };
  const list = el('ul', { class: 'alog' });
  const filter = segmented({
    ariaLabel: 'filter activity',
    options: [{ value: 'all', label: 'Everything' }, { value: 'problems', label: 'Problems' }],
    value: ui.filter,
    onChange: (value) => { ui.filter = value; paint(); },
  });
  const clear = btn({
    label: 'Clear', variant: 'quiet', size: 'sm',
    onClick: () => { state.activity.length = 0; state.jobs.clear(); paint(); },
  });

  host.append(el('div', { class: 'stack' }, [
    el('div', { class: 'row row--nowrap' }, [el('div', { class: 'spacer' }, [filter]), clear]),
    list,
  ]));

  function paint() {
    const rows = state.activity.filter((r) => ui.filter === 'all' || r.tone === 'bad' || r.tone === 'warn');
    clear.hidden = !state.activity.length;
    if (!rows.length) {
      list.replaceChildren(empty(
        ui.filter === 'problems' ? 'No problems logged' : 'Nothing yet',
        'Status changes, tracks, watchdog actions and jobs show up here as they happen.',
      ));
      return;
    }
    list.replaceChildren(...rows.map((r) => el('li', { class: 'alog__row', dataset: { tone: r.tone } }, [
      el('div', { class: 'alog__meta' }, [
        el('span', { class: 'alog__time num', text: fmtTime(r.time) }),
        el('span', { class: 'alog__chan', text: r.channel }),
      ]),
      el('div', { class: 'alog__text', text: r.text }),
      typeof r.progress === 'number'
        ? el('div', { class: 'meter', dataset: { tone: 'info' } }, [
          el('span', { style: `width:${Math.max(0, Math.min(100, r.progress * (r.progress <= 1 ? 100 : 1))).toFixed(0)}%` }),
        ])
        : null,
    ])));
  }

  paint();
  return {
    update(topics) {
      if (topics.has('activity')) paint();
    },
  };
}
