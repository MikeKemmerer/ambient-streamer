// More: schedule, logs, delivery, output size and the delete page.

import { api, asLog } from '../../api.js';
import {
  applyDelivery, applyResolution, confirmDiscard, deleteChannel, deliveryBody, saveSchedule,
} from '../data/actions.js';
import {
  ENCODERS, TARGETS, chanData, channelState, config, deliveryOf, guard, isStopped, normalizedRules, resolutionOf,
  scheduleDirty, state,
} from '../data/store.js';
import { icon } from '../lib/icons.js';
import { hashFor } from '../lib/nav.js';
import {
  btn, callout, card, checkRow, chip, copyText, el, empty, field, segmented, selectInput, spinner, switchRow, textInput, toast,
} from '../lib/ui.js';
import { liveBox, saveBar } from './common.js';

const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const DASH = '\u2014';

// --------------------------------------------------------------------------
// Root menu
// --------------------------------------------------------------------------

function linkRow(name, sub, iconName, label, hint, tone) {
  return el('a', { class: 'menurow', href: hashFor(name, 'more', sub), dataset: { tone: tone || '' } }, [
    el('span', { class: 'menurow__icon' }, [icon(iconName, { size: 22 })]),
    el('span', { class: 'menurow__text' }, [
      el('span', { class: 'menurow__label', text: label }),
      hint ? el('span', { class: 'menurow__sub', text: hint }) : null,
    ]),
    icon('forward', { size: 18 }),
  ]);
}

function mountRoot(host, ctx) {
  const name = ctx.name;
  const box = liveBox(() => {
    const cfg = config(name);
    const rules = (cfg.schedule && cfg.schedule.rules) || [];
    const delivery = deliveryOf(name);
    const dirty = scheduleDirty(name);
    return [
      el('div', { class: 'stack stack--tight' }, [
        linkRow(name, 'schedule', 'calendar', 'Schedule', `${dirty ? 'unsaved changes \u00B7 ' : ''}${rules.length} rule${rules.length === 1 ? '' : 's'} \u00B7 ${(cfg.schedule && cfg.schedule.timezone) || 'UTC'}`),
        linkRow(name, 'logs', 'logs', 'Logs', 'Compositor, audio, producer and watchdog output'),
        linkRow(name, 'delivery', 'broadcast', 'Delivery', `${delivery.targets.join(' + ')} \u00B7 ${delivery.hasKey ? 'key set' : 'no key'}`),
        linkRow(name, 'output', 'image', 'Output size', `${resolutionOf(name) || DASH} \u00B7 ${delivery.encoder || 'default encoder'}`),
      ]),
      card({
        title: 'Channel',
        children: [el('dl', { class: 'kv' }, [
          el('dt', { text: 'genre' }), el('dd', { text: cfg.genre || DASH }),
          el('dt', { text: 'state' }), el('dd', { text: channelState(name) || DASH }),
          el('dt', { text: 'resolution' }), el('dd', { text: resolutionOf(name) || DASH }),
          el('dt', { text: 'fps' }), el('dd', { text: String(delivery.fps || DASH) }),
        ])],
      }),
      linkRow(name, 'danger', 'trash', 'Delete channel', 'Removes it from the control plane', 'danger'),
    ];
  }, { cls: 'stack' });
  host.append(box.node);
  return { update(topics) { if (topics.has('detail') || topics.has('status') || topics.has('drafts')) box.refresh(); } };
}

// --------------------------------------------------------------------------
// Schedule
// --------------------------------------------------------------------------

const WINDOW = /^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$/;

function scheduleDraft(name) {
  const d = chanData(name);
  if (!d.schedule) {
    const saved = config(name).schedule || {};
    d.schedule = { tz: saved.timezone || 'UTC', rules: normalizedRules(saved.rules), undo: [] };
  }
  return d.schedule;
}

function mountSchedule(host, ctx) {
  const name = ctx.name;
  const d = chanData(name);
  let draft = scheduleDraft(name);

  const bar = saveBar({
    saveLabel: 'Save schedule',
    onSave: () => saveSchedule(name, draft.tz, draft.rules).then((ok) => { if (ok) { draft = scheduleDraft(name); paintRules(); paintBar(); } }),
    onDiscard: async () => {
      if (await confirmDiscard('your schedule edits')) {
        d.schedule = null;
        draft = scheduleDraft(name);
        tz.value = draft.tz;
        paintRules();
        paintBar();
      }
    },
  });
  const tz = textInput({ value: draft.tz, placeholder: 'America/Los_Angeles', mono: true, 'aria-label': 'timezone' });
  tz.addEventListener('input', () => { draft.tz = tz.value; paintBar(); });
  const rulesHost = el('div', { class: 'stack' });
  const presetOptions = () => [{ value: '', label: '\u2014 preset \u2014' }, ...state.presets.map((p) => ({ value: p.name, label: p.display_name || p.name }))];

  function paintBar() {
    bar.set({ dirty: scheduleDirty(name), label: `${draft.rules.length} rule${draft.rules.length === 1 ? '' : 's'} \u00B7 unsaved`, canUndo: false });
  }

  function ruleCard(rule, index) {
    const match = WINDOW.exec(rule.when || '');
    const raw = Boolean(rule.when) && !match;
    const from = el('input', { type: 'time', class: 'input', 'data-mut': '', 'aria-label': 'window starts' });
    const to = el('input', { type: 'time', class: 'input', 'data-mut': '', 'aria-label': 'window ends' });
    if (match) { from.value = `${match[1]}:${match[2]}`; to.value = `${match[3]}:${match[4]}`; }
    const sync = () => { rule.when = from.value && to.value ? `${from.value}-${to.value}` : ''; paintBar(); };
    from.addEventListener('change', sync);
    to.addEventListener('change', sync);

    const rawInput = textInput({
      value: rule.when, placeholder: '06:00-11:00', mono: true, 'aria-label': 'window',
      onInput: (v) => { rule.when = v.trim(); paintBar(); },
    });

    const nameInput = textInput({ value: rule.name, placeholder: 'morning', onInput: (v) => { rule.name = v.trim(); paintBar(); }, 'aria-label': 'rule name' });
    const preset = selectInput({ options: presetOptions(), value: rule.preset || '', onChange: (v) => { rule.preset = v; paintBar(); } });
    if (rule.preset && !state.presets.some((p) => p.name === rule.preset)) {
      preset.append(el('option', { value: rule.preset, text: `${rule.preset} (unknown)` }));
      preset.value = rule.preset;
    }
    const date = el('input', { type: 'date', class: 'input', 'data-mut': '', 'aria-label': 'date', value: rule.date || '' });
    date.addEventListener('change', () => { rule.date = date.value; paintBar(); });

    const days = el('div', { class: 'days' }, WEEKDAYS.map((day) => {
      const on = rule.days.includes(day);
      const b = el('button', {
        type: 'button', class: 'daychip', 'aria-pressed': String(on), 'data-mut': '', text: day,
        onclick: () => {
          const next = !rule.days.includes(day);
          rule.days = next ? WEEKDAYS.filter((x) => rule.days.includes(x) || x === day) : rule.days.filter((x) => x !== day);
          b.setAttribute('aria-pressed', String(next));
          paintBar();
        },
      });
      return b;
    }));

    return el('section', { class: 'card rule' }, [
      el('header', { class: 'card__head' }, [
        el('div', { class: 'card__titles' }, [el('h3', { class: 'card__title', text: `Rule ${index + 1}` })]),
        btn({
          label: 'Remove', icon: 'trash', variant: 'danger', size: 'sm', mut: true,
          onClick: () => {
            const snapshot = JSON.stringify(draft.rules);
            draft.rules.splice(index, 1);
            paintRules();
            paintBar();
            toast('info', 'rule removed', rule.name || `rule ${index + 1}`, {
              action: { label: 'Undo', run: () => { draft.rules = JSON.parse(snapshot); paintRules(); paintBar(); } },
            });
          },
        }),
      ]),
      field('Name', nameInput),
      field('Time window', raw
        ? rawInput
        : el('div', { class: 'timerow' }, [from, el('span', { class: 'muted', text: 'to' }), to]),
      raw ? 'Existing window in an unusual format; edit it as text (HH:MM-HH:MM).' : 'Leave empty to match all day.'),
      field('Days', days, 'None selected means every day.'),
      field('Or a single date', date, 'A date outranks days and windows.'),
      field('Preset', preset),
    ]);
  }

  function paintRules() {
    rulesHost.replaceChildren(
      ...(draft.rules.length
        ? draft.rules.map(ruleCard)
        : [empty('No rules', 'The channel keeps its own settings all day.')]),
    );
  }

  host.append(el('div', { class: 'stack' }, [
    card({
      title: 'Schedule',
      subtitle: 'Resolution order, most specific first: date \u2192 days + window \u2192 window \u2192 channel default. First match wins.',
      children: [field('Timezone', tz)],
    }),
    rulesHost,
    btn({
      label: 'Add rule', icon: 'plus', variant: 'secondary', block: true, size: 'lg', mut: true,
      onClick: () => {
        draft.rules.push({ name: '', preset: '', when: '', days: [], date: '' });
        paintRules();
        paintBar();
        rulesHost.lastElementChild && rulesHost.lastElementChild.scrollIntoView({ behavior: 'smooth', block: 'center' });
      },
    }),
  ]), bar);

  paintRules();
  paintBar();
  return { update(topics) { if (topics.has('presets')) paintRules(); } };
}

// --------------------------------------------------------------------------
// Logs
// --------------------------------------------------------------------------

const SERVICES = ['compositor', 'liquidsoap', 'producer', 'watchdog'];

function mountLogs(host, ctx) {
  const name = ctx.name;
  const ui = { service: 'compositor', lines: '200', follow: false, wrap: true, text: '', path: '', loading: false };
  let timer = null;

  const out = el('pre', { class: 'logout', tabindex: '0', 'aria-label': 'log output', text: '\u2014' });
  const pathLine = el('p', { class: 'note mono', text: '' });

  async function refresh() {
    ui.loading = true;
    const body = await guard('logs', () => api.logs({ channel: name, service: ui.service, lines: ui.lines }));
    ui.loading = false;
    if (body === undefined) return;
    const { text, path } = asLog(body);
    pathLine.textContent = path;
    const pinned = out.scrollTop + out.clientHeight >= out.scrollHeight - 24;
    out.textContent = text || '(empty)';
    ui.text = text;
    if (pinned) out.scrollTop = out.scrollHeight;
  }

  function syncFollow() {
    clearInterval(timer);
    timer = ui.follow ? setInterval(refresh, 5000) : null;
  }

  const service = segmented({
    ariaLabel: 'log service', value: ui.service,
    options: SERVICES.map((s) => ({ value: s, label: s })),
    onChange: (v) => { ui.service = v; refresh(); },
  });

  host.append(el('div', { class: 'stack' }, [
    card({
      title: 'Service',
      children: [
        service,
        field('Lines', selectInput({ options: ['100', '200', '500', '1000'], value: ui.lines, mut: false, onChange: (v) => { ui.lines = v; refresh(); } })),
        switchRow({ label: 'Refresh every 5 s', checked: false, mut: false, onChange: (on) => { ui.follow = on; syncFollow(); } }),
        switchRow({ label: 'Wrap long lines', checked: true, mut: false, onChange: (on) => { out.dataset.wrap = String(on); } }),
        el('div', { class: 'row' }, [
          btn({ label: 'Refresh', icon: 'restart', variant: 'primary', onClick: refresh }),
          btn({ label: 'Copy', icon: 'copy', variant: 'secondary', onClick: () => copyText(ui.text || '', 'log copied') }),
          btn({ label: 'Jump to end', icon: 'arrowdown', variant: 'quiet', onClick: () => { out.scrollTop = out.scrollHeight; } }),
        ]),
        pathLine,
      ],
    }),
    out,
  ]));
  out.dataset.wrap = 'true';
  refresh();

  return { update() {}, destroy() { clearInterval(timer); } };
}

// --------------------------------------------------------------------------
// Delivery
// --------------------------------------------------------------------------

const TARGET_COPY = {
  youtube: ['YouTube', 'Public broadcast. Needs a stream key.'],
  video: ['Internal HLS \u2014 video', 'Full size on your network. Never reaches YouTube.'],
  audio: ['Internal HLS \u2014 audio only', 'No video encode at all. The cheapest feed.'],
};

function deliveryDraft(name) {
  const d = chanData(name);
  if (!d.delivery) {
    const base = deliveryOf(name);
    d.delivery = {
      key: '', rtmp: base.rtmpUrl, encoder: ENCODERS.includes(base.encoder) ? base.encoder : '',
      fps: base.fps ? String(base.fps) : '', fpsDefault: false, targets: [...base.targets],
      localHeight: base.localHeight ? String(base.localHeight) : '', localFps: base.localFps ? String(base.localFps) : '',
    };
  }
  return d.delivery;
}

function mountDelivery(host, ctx) {
  const name = ctx.name;
  const d = chanData(name);

  const box = liveBox(() => {
    const base = deliveryOf(name);
    const form = deliveryDraft(name);
    const editable = base.editable;
    const body = deliveryBody(name, form);
    const dirty = Object.keys(body).length > 0 || form.targets.join(',') !== base.targets.join(',');
    const wantsYouTube = form.targets.includes('youtube');
    const lock = (node) => { if (!editable) node.disabled = true; return node; };

    const apply = btn({
      label: 'Apply to next start', icon: 'check', variant: 'primary', size: 'lg', block: true, mut: true,
      disabled: !editable || !dirty || form.targets.length === 0,
      onClick: async () => { if (await applyDelivery(name, form)) { d.delivery = null; box.refresh(); } },
    });
    const sync = () => {
      const b = deliveryBody(name, form);
      apply.disabled = !editable || (Object.keys(b).length === 0 && form.targets.join(',') === base.targets.join(',')) || form.targets.length === 0;
    };

    const keyInput = textInput({
      type: 'password', placeholder: 'unchanged', mono: true, value: form.key,
      onInput: (v) => { form.key = v; sync(); }, 'aria-label': 'stream key',
    });
    keyInput.setAttribute('autocomplete', 'new-password');
    const rtmp = textInput({ value: form.rtmp, placeholder: 'rtmp://a.rtmp.youtube.com/live2', mono: true, onInput: (v) => { form.rtmp = v; sync(); }, 'aria-label': 'ingest URL' });
    const encoder = selectInput({
      options: [{ value: '', label: 'use default' }, ...ENCODERS.map((e) => ({ value: e, label: e }))], value: form.encoder,
      onChange: (v) => { form.encoder = v; sync(); },
    });
    const fps = textInput({
      value: form.fps, placeholder: 'default', inputmode: 'numeric', onInput: (v) => { form.fps = v; sync(); }, 'aria-label': 'frames per second',
    });
    const fpsDefault = checkRow({ label: 'Use the global fps', checked: form.fpsDefault, onChange: (on) => { form.fpsDefault = on; box.refresh(); } });
    const localHeight = selectInput({
      options: [{ value: '', label: 'default' }, ...['360', '540', '720', '1080'].map((h) => ({ value: h, label: `${h}p` }))], value: form.localHeight,
      onChange: (v) => { form.localHeight = v; sync(); },
    });
    const localFps = textInput({ value: form.localFps, placeholder: 'default', inputmode: 'numeric', onInput: (v) => { form.localFps = v; sync(); }, 'aria-label': 'local HLS fps' });

    for (const node of [keyInput, rtmp, encoder, fps, localHeight, localFps]) if (!editable) node.disabled = true;
    if (!wantsYouTube) { keyInput.disabled = true; rtmp.disabled = true; }
    fps.disabled = !editable || form.fpsDefault;

    const targets = TARGETS.map((target) => lock(checkRow({
      label: TARGET_COPY[target][0], hint: TARGET_COPY[target][1], checked: form.targets.includes(target), disabled: !editable,
      onChange: (on) => {
        form.targets = on ? TARGETS.filter((t) => form.targets.includes(t) || t === target) : form.targets.filter((t) => t !== target);
        box.refresh();
      },
    })));

    return [
      editable ? null : callout(`Stop ${name} to change where it publishes. These settings only apply on the next start.`, 'warn'),
      card({
        title: 'Where it publishes',
        subtitle: 'Pick one or more. A channel that delivers nowhere would render into nothing.',
        children: [...targets, form.targets.length ? null : callout('Pick at least one target.', 'bad')],
      }),
      wantsYouTube
        ? card({
          title: 'YouTube',
          actions: chip(base.hasKey ? 'key set' : 'no key', base.hasKey ? 'ok' : 'warn'),
          children: [
            field('Stream key', keyInput, 'Write-only. The control plane never sends a key back, so this starts empty and leaving it empty keeps the stored key.'),
            field('Ingest URL', rtmp),
          ],
        })
        : null,
      card({
        title: 'Encoding',
        subtitle: `In effect: ${base.encoder || DASH} at ${base.fps || DASH} fps. "Use default" hands the setting back to the global one.`,
        children: [field('Encoder', encoder), field('Frame rate', fps), fpsDefault],
      }),
      form.targets.includes('video')
        ? card({
          title: 'Internal video feed',
          subtitle: `Internal video: ${base.local || DASH}. Leave both on default and it follows the channel.`,
          children: [field('Size', localHeight), field('Frame rate', localFps)],
        })
        : null,
      apply,
    ];
  }, { cls: 'stack' });

  host.append(box.node);
  return {
    update(topics) {
      if (topics.has('detail') || topics.has('status')) box.refresh();
    },
  };
}

// --------------------------------------------------------------------------
// Output size
// --------------------------------------------------------------------------

function mountOutput(host, ctx) {
  const name = ctx.name;
  const ui = { draft: null };
  const box = liveBox(() => {
    const current = resolutionOf(name);
    const draft = ui.draft || current || '720p';
    const live = !isStopped(name);
    const select = selectInput({
      options: ['480p', '720p', '1080p', '1440p', '2160p'], value: draft,
      onChange: (v) => { ui.draft = v; box.refresh(); },
    });
    return [
      card({
        title: 'Output size',
        subtitle: `Currently ${current || DASH}.`,
        children: [
          field('Resolution', select),
          live ? callout('Not a live change. Applying restarts the compositor: about a 1 s gap and a new YouTube ingest session.', 'bad') : callout(`${name} is stopped, so nothing is interrupted. It starts at the new size.`, 'info', 'info'),
          btn({
            label: draft === current ? 'Already set' : `Change to ${draft}`, icon: 'check', variant: live ? 'danger' : 'primary', size: 'lg', block: true, mut: true,
            disabled: !current || draft === current,
            onClick: async () => { if (await applyResolution(name, draft)) { ui.draft = null; box.refresh(); } },
          }),
          el('p', { class: 'note', text: 'Resolution is compiled into the filtergraph when FFmpeg launches, which is why it cannot change on a running channel. Higher resolutions cost substantially more CPU; check headroom under System first.' }),
        ],
      }),
    ];
  }, { cls: 'stack' });
  host.append(box.node);
  return { update(topics) { if (topics.has('detail') || topics.has('status')) box.refresh(); } };
}

// --------------------------------------------------------------------------
// Danger
// --------------------------------------------------------------------------

function mountDanger(host, ctx) {
  const name = ctx.name;
  const box = liveBox(() => {
    const stopped = channelState(name) === 'stopped';
    return [
      card({
        title: 'Delete channel',
        tone: 'bad',
        children: [
          el('p', { class: 'sheet__text', text: `Removes ${name} from the control plane. A channel must be stopped first.` }),
          stopped ? null : callout(`${name} is ${channelState(name) || 'not stopped'}. Stop it from the Live tab before deleting.`, 'warn'),
          btn({
            label: 'Delete\u2026', icon: 'trash', variant: 'danger', size: 'lg', block: true, mut: true, disabled: !stopped,
            onClick: async () => { if (await deleteChannel(name)) ctx.go('#/'); },
          }),
          el('p', { class: 'note', text: 'You will be asked to type the channel name and then hold a button.' }),
        ],
      }),
    ];
  }, { cls: 'stack' });
  host.append(box.node);
  return { update(topics) { if (topics.has('status') || topics.has('detail')) box.refresh(); } };
}

// --------------------------------------------------------------------------

/** Forms seed themselves from the channel config, so they wait until it has loaded. */
function whenLoaded(mountFn, host, ctx) {
  if (chanData(ctx.name).detailLoaded) return mountFn(host, ctx);
  host.append(spinner('Loading\u2026'));
  let inner = null;
  return {
    update(topics) {
      if (inner) {
        if (inner.update) inner.update(topics);
      } else if (chanData(ctx.name).detailLoaded) {
        host.replaceChildren();
        inner = mountFn(host, ctx);
      }
    },
    destroy() { if (inner && inner.destroy) inner.destroy(); },
  };
}

export function mountMore(host, ctx) {
  switch (ctx.sub) {
    case 'schedule': return whenLoaded(mountSchedule, host, ctx);
    case 'logs': return mountLogs(host, ctx);
    case 'delivery': return whenLoaded(mountDelivery, host, ctx);
    case 'output': return whenLoaded(mountOutput, host, ctx);
    case 'danger': return mountDanger(host, ctx);
    default: return mountRoot(host, ctx);
  }
}
