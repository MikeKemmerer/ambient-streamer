// Pieces shared between screens: self-refreshing regions, the ordered-list editor,
// the media picker, the upload panel and the sticky save bar.

import { fmtBytes, splitPath } from '../../render.js';
import {
  UPLOAD, cancelUpload, clearFinishedUploads, confirmAddAll, destPath, enqueue, hasFinishedUploads,
  uploadDest, uploadSummary, uploads,
} from '../data/actions.js';
import { listAdd, listMove, listRemove, listUndo, state } from '../data/store.js';
import { icon } from '../lib/icons.js';
import { btn, callout, chip, el, empty, iconBtn, segmented, textInput, toast } from '../lib/ui.js';

const TEXT_FIELDS = /^(INPUT|TEXTAREA)$/;

// A region must not be rebuilt between a finger going down and coming up on a
// control inside it: the browser would see the press and the lift land on
// different elements and drop the tap. Live events arrive constantly, so this is
// tracked once for the whole page.
let pressedNode = null;
let liftTimer = null;
const waiting = new Set();

function released() {
  clearTimeout(liftTimer);
  pressedNode = null;
  for (const paint of waiting) setTimeout(paint, 60);
  waiting.clear();
}

// The browser delivers the click after the pointer events (and a focus change in
// between can trigger a rebuild), so a press only ends when the click arrives. The
// timer covers a press that never becomes one, such as a scroll.
function pressed(event) {
  clearTimeout(liftTimer);
  pressedNode = event.target;
}

function lifted() {
  clearTimeout(liftTimer);
  liftTimer = setTimeout(released, 600);
}

document.addEventListener('pointerdown', pressed, true);
document.addEventListener('mousedown', pressed, true);
for (const type of ['pointerup', 'pointercancel', 'mouseup']) document.addEventListener(type, lifted, true);
document.addEventListener('click', released, true);

/**
 * A region that rebuilds itself from state. It never rebuilds under a finger or a
 * cursor: while a field inside has focus, a press is in progress, or a hold is
 * running, the rebuild waits until that is over.
 */
export function liveBox(render, { tag = 'div', cls = '' } = {}) {
  const node = el(tag, { class: cls });
  let deferred = false;

  function paint() {
    const active = document.activeElement;
    const typing = active && node.contains(active) && TEXT_FIELDS.test(active.tagName)
      && !['checkbox', 'radio', 'button', 'range', 'color', 'file', 'submit'].includes(active.type);
    const pressed = pressedNode && node.contains(pressedNode);
    if (typing || pressed || node.querySelector('[data-holding="true"], [data-armed="true"], [data-dragging="true"]')) {
      deferred = true;
      if (pressed) waiting.add(paint);
      return;
    }
    deferred = false;
    node.replaceChildren(...[].concat(render()).filter(Boolean));
  }

  node.addEventListener('focusout', () => {
    if (deferred) setTimeout(paint, 0);
  });
  paint();
  return { node, refresh: paint };
}

export function pathName(path) {
  const { dir, base } = splitPath(path);
  return el('span', { class: 'path' }, [
    dir ? el('span', { class: 'path__dir', text: dir }) : null,
    el('span', { class: 'path__base', text: base }),
  ]);
}

export function treeOf(path) {
  if (path.startsWith('common/')) return 'common';
  if (path.startsWith('channels/')) return 'channel';
  return '';
}

function treeTag(path) {
  const tree = treeOf(path);
  return tree ? el('span', { class: 'tree', dataset: { tree }, text: tree }) : null;
}

// --------------------------------------------------------------------------
// Slider
//
// A bare range input is hard to set precisely with a thumb, so it gets nudge
// buttons either side and a large value readout. `onInput` fires while dragging
// (cheap, local); `onChange` fires once on release or a nudge.
// --------------------------------------------------------------------------

export function slider({ min, max, step = 1, value, decimals = 0, unit = '', onInput, onChange, mut = true, ariaLabel }) {
  let current = Number(value);
  const read = el('span', { class: 'slider__value num', text: '' });
  const input = el('input', {
    type: 'range', class: 'slider__input', min: String(min), max: String(max), step: String(step),
    'aria-label': ariaLabel, 'data-mut': mut ? '' : undefined,
  });
  const show = () => { read.textContent = `${current.toFixed(decimals)}${unit ? ` ${unit}` : ''}`; };
  const clamp = (v) => Math.min(max, Math.max(min, Number(v.toFixed(decimals + 2))));

  function set(v, fire) {
    current = clamp(v);
    input.value = String(current);
    show();
    if (onInput) onInput(current);
    if (fire && onChange) onChange(current);
  }

  input.value = String(current);
  show();
  input.addEventListener('input', () => set(Number(input.value), false));
  input.addEventListener('change', () => { if (onChange) onChange(current); });
  for (const type of ['pointerdown', 'touchstart']) {
    input.addEventListener(type, () => { input.dataset.dragging = 'true'; }, { passive: true });
  }
  for (const type of ['pointerup', 'pointercancel', 'blur']) {
    input.addEventListener(type, () => { input.dataset.dragging = 'false'; });
  }

  const nudge = (delta, label, iconName) => el('button', {
    type: 'button', class: 'slider__nudge', 'aria-label': label, 'data-mut': mut ? '' : undefined,
    onclick: () => set(current + delta, true),
  }, [icon(iconName, { size: 20 })]);

  const root = el('div', { class: 'slider' }, [
    read,
    el('div', { class: 'slider__row' }, [nudge(-step, `decrease ${ariaLabel || ''}`, 'minus'), input, nudge(step, `increase ${ariaLabel || ''}`, 'plus')]),
  ]);
  root.setValue = (v) => { if (input.dataset.dragging !== 'true') { current = clamp(Number(v)); input.value = String(current); show(); } };
  root.getValue = () => current;
  return root;
}

// --------------------------------------------------------------------------
// Sticky save bar
// --------------------------------------------------------------------------

export function saveBar({ onSave, onDiscard, onUndo, saveLabel = 'Save' }) {
  const text = el('span', { class: 'savebar__text', text: '' });
  const undo = iconBtn({ name: 'undo', label: 'Undo last edit', onClick: onUndo, mut: true });
  const discard = btn({ label: 'Discard', variant: 'quiet', size: 'sm', mut: true, onClick: onDiscard });
  const save = btn({ label: saveLabel, icon: 'check', variant: 'primary', size: 'md', mut: true, onClick: onSave });
  const root = el('div', { class: 'savebar', hidden: true, role: 'region', 'aria-label': 'Unsaved changes' }, [
    el('div', { class: 'savebar__info' }, [icon('warning', { size: 18 }), text]),
    el('div', { class: 'savebar__actions' }, [undo, discard, save]),
  ]);
  root.set = ({ dirty, label, canUndo }) => {
    root.hidden = !dirty;
    text.textContent = label || 'Unsaved changes';
    undo.hidden = !canUndo || !onUndo;
  };
  return root;
}

// --------------------------------------------------------------------------
// Ordered list editor
//
// Reordering by drag is unreliable on a touch screen, so a tap selects a row and
// opens a toolbar of large buttons for it. Every edit is a draft with an undo.
// --------------------------------------------------------------------------

const CHUNK = 40;

export function orderedList({ name, kind, ui, noun }) {
  const filter = textInput({ type: 'search', placeholder: `filter ${noun}s in this list`, mut: false });
  filter.setAttribute('aria-label', `filter ${noun}s in this list`);
  const filterWrap = el('div', { class: 'olist__filter' }, [filter]);
  filter.addEventListener('input', () => { ui.listFilter = filter.value; ui.listShown = CHUNK; box.refresh(); });

  const box = liveBox(() => {
    const list = kind === 'audio' ? state.data.get(name).playlist : state.data.get(name).slides;
    const items = list.draft;
    const needle = (ui.listFilter || '').trim().toLowerCase();
    const shown = ui.listShown || CHUNK;
    const rows = [];

    items.forEach((path, index) => {
      if (needle && !path.toLowerCase().includes(needle)) return;
      rows.push({ path, index });
    });
    filterWrap.hidden = items.length < 12;

    if (!items.length) {
      return empty(`No ${noun}s selected`, `Add some from the library. The list is empty, so nothing plays from it yet.`);
    }
    if (!rows.length) return empty(`No ${noun}s match`);

    const out = rows.slice(0, shown).map(({ path, index }) => {
      const open = ui.selected === index;
      const bar = open
        ? el('div', { class: 'rowtools' }, [
          rowTool('up', 'Up', index === 0, () => { listMove(name, kind, index, index - 1); ui.selected = index - 1; }),
          rowTool('down', 'Down', index === items.length - 1, () => { listMove(name, kind, index, index + 1); ui.selected = index + 1; }),
          rowTool('top', 'Top', index === 0, () => { listMove(name, kind, index, 0); ui.selected = 0; }),
          rowTool('bottom', 'Bottom', index === items.length - 1, () => { listMove(name, kind, index, items.length - 1); ui.selected = items.length - 1; }),
          rowTool('trash', 'Remove', false, () => {
            const removed = listRemove(name, kind, index);
            ui.selected = null;
            toast('info', `removed ${noun}`, removed ? splitPath(removed).base : '', {
              action: { label: 'Undo', run: () => listUndo(name, kind) },
            });
          }, 'danger'),
        ])
        : null;
      return el('li', { class: 'orow', dataset: { open: String(open) } }, [
        el('button', {
          type: 'button', class: 'orow__main', 'aria-expanded': String(open),
          onclick: () => { ui.selected = open ? null : index; box.refresh(); },
        }, [
          el('span', { class: 'orow__idx num', text: String(index + 1) }),
          pathName(path),
          treeTag(path),
        ]),
        bar,
      ]);
    });
    if (rows.length > shown) {
      out.push(el('li', {}, [btn({
        label: `Show ${Math.min(CHUNK, rows.length - shown)} more (${rows.length - shown} left)`, variant: 'quiet', block: true,
        onClick: () => { ui.listShown = shown + CHUNK; box.refresh(); },
      })]));
    }
    return el('ul', { class: 'olist' }, out);
  });

  function rowTool(iconName, label, disabled, run, tone) {
    const b = el('button', {
      type: 'button', class: 'rowtool', 'data-mut': '', disabled, 'aria-label': label, dataset: { tone: tone || '' },
      onclick: (event) => { event.stopPropagation(); run(); },
    }, [icon(iconName, { size: 22 }), el('span', { text: label })]);
    return b;
  }

  const node = el('div', { class: 'stack stack--tight' }, [filterWrap, box.node]);
  return { node, refresh: box.refresh };
}

// --------------------------------------------------------------------------
// Media picker
// --------------------------------------------------------------------------

export function mediaPicker({ name, kind, ui, noun }) {
  const filter = textInput({ type: 'search', placeholder: `search ${noun}s`, mut: false });
  filter.setAttribute('aria-label', `search ${noun}s`);
  filter.addEventListener('input', () => { ui.pickFilter = filter.value; ui.pickShown = CHUNK; box.refresh(); });

  const box = liveBox(() => {
    const d = state.data.get(name);
    const list = kind === 'audio' ? d.playlist : d.slides;
    const groups = kind === 'audio' ? d.media.audio : d.media.images;
    const used = new Set(list.draft);
    const needle = (ui.pickFilter || '').trim().toLowerCase();
    const shown = ui.pickShown || CHUNK;

    const matches = [];
    let total = 0;
    for (const group of groups) {
      for (const item of group.items) {
        total += 1;
        if (needle && !item.path.toLowerCase().includes(needle)) continue;
        matches.push({ path: item.path, tree: group.tree });
      }
    }
    const addable = matches.filter((m) => !used.has(m.path)).map((m) => m.path);

    const head = el('div', { class: 'row row--nowrap picker__head' }, [
      el('span', { class: 'note', text: `${matches.length} of ${total} ${noun}s` }),
      el('span', { class: 'spacer' }),
      btn({
        label: addable.length ? `Add all ${addable.length}` : 'Add all', variant: 'secondary', size: 'sm', mut: true,
        disabled: addable.length === 0,
        onClick: async () => {
          if (await confirmAddAll(noun, addable.length, Boolean(needle))) {
            listAdd(name, kind, addable);
            toast('ok', `added ${addable.length} ${noun}${addable.length === 1 ? '' : 's'}`, 'not saved yet');
          }
        },
      }),
    ]);

    if (!d.mediaLoaded) return [head, el('p', { class: 'note', text: 'Loading the library\u2026' })];
    if (!matches.length) return [head, empty(total ? `No ${noun}s match` : `No ${noun}s in the library`, total ? '' : 'Upload some below.')];

    const rows = matches.slice(0, shown).map(({ path }) => {
      const isUsed = used.has(path);
      return el('li', { class: 'prow', dataset: { used: String(isUsed) } }, [
        el('button', {
          type: 'button', class: 'prow__add', 'data-mut': '', 'aria-label': `add ${splitPath(path).base}`,
          onclick: () => {
            listAdd(name, kind, path);
            toast('ok', `added ${noun}`, splitPath(path).base, { ms: 2200 });
          },
        }, [icon('plus', { size: 22 })]),
        pathName(path),
        isUsed ? chip('in list', 'info', 'chip--plain') : treeTag(path),
      ]);
    });
    if (matches.length > shown) {
      rows.push(el('li', {}, [btn({
        label: `Show more (${matches.length - shown} left)`, variant: 'quiet', block: true,
        onClick: () => { ui.pickShown = shown + CHUNK; box.refresh(); },
      })]));
    }
    return [head, el('ul', { class: 'plist' }, rows)];
  });

  return { node: el('div', { class: 'stack stack--tight' }, [filter, box.node]), refresh: box.refresh };
}

// --------------------------------------------------------------------------
// Upload panel
// --------------------------------------------------------------------------

export function uploadPanel({ kind, getChannel, title }) {
  const spec = UPLOAD[kind];
  const input = el('input', {
    type: 'file', multiple: true, hidden: true,
    accept: `${spec.extensions.join(',')},${kind === 'images' ? 'image/*' : 'audio/*'}`,
  });
  input.addEventListener('change', () => {
    enqueue(kind, getChannel(), input.files);
    input.value = '';
  });

  const dest = segmented({
    ariaLabel: 'upload destination',
    options: [{ value: 'channel', label: 'This channel' }, { value: 'common', label: 'Shared library' }],
    value: uploadDest[kind],
    onChange: (value) => { uploadDest[kind] = value; box.refresh(); },
  });

  const box = liveBox(() => {
    const channel = getChannel();
    const rows = uploads[kind];
    const summary = uploadSummary[kind];
    return [
      el('p', { class: 'note' }, [
        'Stored in ',
        el('span', { class: 'mono', text: destPath(kind, uploadDest[kind], channel) }),
        uploadDest[kind] === 'common' ? ' \u2014 every channel can use it.' : '',
      ]),
      rows.length
        ? el('ul', { class: 'ulist' }, rows.map((entry) => uploadRow(entry)))
        : null,
      summary ? callout(summary.text, summary.tone === 'bad' ? 'bad' : summary.tone === 'ok' ? 'ok' : 'warn', summary.tone === 'ok' ? 'check' : 'warning') : null,
      hasFinishedUploads(kind) ? btn({ label: 'Clear finished', variant: 'quiet', size: 'sm', onClick: () => clearFinishedUploads(kind) }) : null,
    ];
  });

  const node = el('section', { class: 'card' }, [
    el('header', { class: 'card__head' }, [el('div', { class: 'card__titles' }, [
      el('h3', { class: 'card__title', text: title || `Upload ${spec.noun}s` }),
      el('p', { class: 'card__sub', text: `${spec.extensions.join(' ')}. The control plane checks what is really inside each file.` }),
    ])]),
    dest,
    btn({
      label: `Choose ${spec.noun}s`, icon: 'upload', variant: 'primary', block: true, size: 'lg', mut: true,
      onClick: () => input.click(),
    }),
    input,
    box.node,
  ]);
  return { node, refresh: box.refresh };
}

function uploadRow(entry) {
  const tone = entry.status === 'done' ? 'ok' : entry.status === 'failed' ? 'bad'
    : entry.status === 'skipped' || entry.status === 'canceled' ? 'warn' : entry.status === 'uploading' ? 'info' : 'idle';
  const active = entry.status === 'uploading' || entry.status === 'queued';
  return el('li', { class: 'urow', dataset: { tone } }, [
    el('div', { class: 'urow__top' }, [
      el('span', { class: 'urow__name', text: entry.name }),
      el('span', { class: 'urow__size num', text: fmtBytes(entry.size) }),
    ]),
    el('div', { class: 'urow__mid' }, [
      chip(entry.status === 'uploading' ? `${Math.round((entry.progress || 0) * 100)}%` : entry.status, tone),
      active ? btn({ label: 'Cancel', variant: 'quiet', size: 'sm', onClick: () => cancelUpload(entry) }) : null,
    ]),
    active ? el('div', { class: 'meter', dataset: { tone: 'info' } }, [el('span', { style: `width:${Math.round((entry.progress || 0) * 100)}%` })]) : null,
    entry.detail ? el('p', { class: 'urow__detail', text: entry.detail }) : null,
  ]);
}
