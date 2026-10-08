// Components for the touch UI. Nothing here uses innerHTML: every string that
// reaches the page came from disk or from an operator, so it is set as text.

import { el, clear } from '../../render.js';
import { icon } from './icons.js';
import { holdOpen, isLocked, unlock, touch } from './lock.js';

export { el, clear, icon };

// --------------------------------------------------------------------------
// Small helpers
// --------------------------------------------------------------------------

export function haptic(pattern = 12) {
  try {
    if (navigator.vibrate) navigator.vibrate(pattern);
  } catch {
    /* not every browser has a vibration motor to offer */
  }
}

export function prefersReducedMotion() {
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export async function copyText(text, what = 'copied') {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // Clipboard access is refused on insecure origins, which is where this UI
    // usually runs, so fall back to the selection-based copy.
    const area = el('textarea', { readonly: true, style: 'position:fixed;opacity:0;top:0' });
    area.value = text;
    document.body.append(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch {
      ok = false;
    }
    area.remove();
    if (!ok) {
      toast('warn', 'copy failed', 'Select and copy the text by hand.');
      return false;
    }
  }
  toast('ok', what);
  return true;
}

let idSeq = 0;
export function uid(prefix = 'm') {
  idSeq += 1;
  return `${prefix}${idSeq}`;
}

// --------------------------------------------------------------------------
// Buttons
// --------------------------------------------------------------------------

/**
 * `mut` marks a control that changes something. While the page is locked it is
 * dimmed, and the guard in installGuard() swallows the touch.
 */
export function btn({
  label, icon: iconName, variant = 'secondary', size = 'md', block = false, mut = false,
  onClick, title, disabled = false, ariaLabel, type = 'button', cls = '',
}) {
  const node = el('button', {
    type,
    class: `btn btn--${variant} btn--${size}${block ? ' btn--block' : ''}${cls ? ` ${cls}` : ''}`,
    title,
    'aria-label': ariaLabel,
    'data-mut': mut ? '' : undefined,
  });
  if (iconName) node.append(icon(iconName, { size: size === 'sm' ? 18 : 20 }));
  if (label) node.append(el('span', { class: 'btn__label', text: label }));
  node.disabled = disabled;
  if (onClick) node.addEventListener('click', onClick);
  return node;
}

export function iconBtn({ name, label, onClick, mut = false, variant = 'ghost', size = 'md', disabled = false }) {
  const node = el('button', {
    type: 'button',
    class: `iconbtn iconbtn--${variant} iconbtn--${size}`,
    'aria-label': label,
    title: label,
    'data-mut': mut ? '' : undefined,
  }, [icon(name, { size: size === 'sm' ? 18 : 22 })]);
  node.disabled = disabled;
  if (onClick) node.addEventListener('click', onClick);
  return node;
}

export function chip(text, tone = 'idle', extra = '') {
  return el('span', { class: `chip${extra ? ` ${extra}` : ''}`, dataset: { tone }, text });
}

// --------------------------------------------------------------------------
// Click shield
//
// A finger that completes a hold and then lifts lands on whatever is beneath the
// button. For a moment after a hold or a sheet closing, clicks are swallowed.
// --------------------------------------------------------------------------

let shieldUntil = 0;

/** Extends the shield; it never shortens one that is already running. */
export function shield(ms) {
  shieldUntil = Math.max(shieldUntil, performance.now() + ms);
}

/** The finger is up, so the shield only needs to cover the click that follows it. */
function resetShield(ms) {
  shieldUntil = performance.now() + ms;
}

// --------------------------------------------------------------------------
// Hold to confirm
//
// A tap is too easy to land by accident, on a glass screen, in a pocket, or
// while scrolling. Anything that interrupts a stream is confirmed by holding a
// button until a bar fills. Lifting early, or sliding the finger away, cancels.
// --------------------------------------------------------------------------

export function holdButton({
  label, holdLabel = 'Keep holding\u2026', ms = 1200, tone = 'danger', icon: iconName,
  onConfirm, mut = true, block = true,
}) {
  const fill = el('span', { class: 'hold__fill', 'aria-hidden': 'true' });
  const text = el('span', { class: 'hold__text', text: label });
  const node = el('button', {
    type: 'button',
    class: `hold hold--${tone}${block ? ' btn--block' : ''}`,
    'data-mut': mut ? '' : undefined,
    'aria-describedby': undefined,
  }, [fill, el('span', { class: 'hold__row' }, [iconName ? icon(iconName, { size: 22 }) : null, text])]);

  let frame = 0;
  let started = 0;
  let origin = null;
  let pointer = null;
  let finished = false;
  let hintTimer = null;
  let releaseResolve = null;

  function paint(progress) {
    fill.style.transform = `scaleX(${Math.max(0, Math.min(1, progress))})`;
  }

  function step() {
    const progress = (performance.now() - started) / ms;
    paint(progress);
    if (progress >= 1) {
      complete();
      return;
    }
    frame = requestAnimationFrame(step);
  }

  function begin(x, y) {
    if (finished || started || node.hasAttribute('data-blocked')) return;
    clearTimeout(hintTimer);
    started = performance.now();
    origin = { x, y };
    node.dataset.holding = 'true';
    text.textContent = holdLabel;
    holdOpen(true);
    haptic(8);
    frame = requestAnimationFrame(step);
  }

  function release(early = true) {
    if (finished) {
      // The hold already succeeded. What matters now is the finger coming up:
      // until it does, anything beneath this button would receive the lift as a tap.
      if (releaseResolve) {
        resetShield(450);
        releaseResolve();
        releaseResolve = null;
      }
      return;
    }
    if (!started) return;
    cancelAnimationFrame(frame);
    started = 0;
    pointer = null;
    node.dataset.holding = 'false';
    holdOpen(false);
    fill.style.transition = 'transform 160ms ease-out';
    paint(0);
    setTimeout(() => { fill.style.transition = ''; }, 200);
    if (early) {
      text.textContent = 'Hold a little longer';
      node.dataset.shake = 'true';
      hintTimer = setTimeout(() => {
        text.textContent = label;
        node.dataset.shake = 'false';
      }, 1300);
    } else {
      text.textContent = label;
    }
  }

  function complete() {
    finished = true;
    cancelAnimationFrame(frame);
    started = 0;
    node.dataset.holding = 'false';
    node.dataset.done = 'true';
    paint(1);
    text.textContent = 'Confirmed \u2014 let go';
    haptic([30, 40, 30]);
    holdOpen(false);
    shield(60000);
    const released = new Promise((resolve) => { releaseResolve = resolve; });
    // The button can be gone by the time the finger lifts (the sheet may have been
    // dismissed), so listen for the lift anywhere on the page.
    const lift = () => {
      document.removeEventListener('pointerup', lift, true);
      document.removeEventListener('pointercancel', lift, true);
      release();
    };
    document.addEventListener('pointerup', lift, true);
    document.addEventListener('pointercancel', lift, true);
    // A keyboard or switch user has no finger to wait for.
    if (pointer === null) lift();
    // Never wait forever on a release event that did not arrive.
    setTimeout(lift, 8000);
    onConfirm({ released });
  }

  node.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 && event.pointerType === 'mouse') return;
    if (isLocked() && node.hasAttribute('data-mut')) return;
    pointer = event.pointerId;
    try {
      node.setPointerCapture(event.pointerId);
    } catch {
      /* a synthetic pointer cannot be captured; the hold still works */
    }
    begin(event.clientX, event.clientY);
  });
  node.addEventListener('pointermove', (event) => {
    if (!started || event.pointerId !== pointer || !origin) return;
    if (Math.hypot(event.clientX - origin.x, event.clientY - origin.y) > 36) release(false);
  });
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
    node.addEventListener(type, () => {
      pointer = null;
      release(true);
    });
  }
  node.addEventListener('contextmenu', (event) => event.preventDefault());
  node.addEventListener('keydown', (event) => {
    if ((event.key === ' ' || event.key === 'Enter') && !event.repeat) {
      event.preventDefault();
      begin(0, 0);
    }
  });
  node.addEventListener('keyup', (event) => {
    if (event.key === ' ' || event.key === 'Enter') release(true);
  });
  node.addEventListener('blur', () => release(false));
  node.addEventListener('click', (event) => event.preventDefault());
  paint(0);
  return node;
}

// --------------------------------------------------------------------------
// Tap twice
//
// For frequent, low-cost actions such as skipping a track. The first tap arms
// the button and says what the second will do; it disarms itself after a moment.
// --------------------------------------------------------------------------

export function armButton({
  label, armedLabel, icon: iconName, onConfirm, ms = 3200, variant = 'secondary',
  mut = true, block = false, size = 'md', disabled = false,
}) {
  const bar = el('span', { class: 'arm__bar', 'aria-hidden': 'true' });
  const text = el('span', { class: 'btn__label', text: label });
  const node = el('button', {
    type: 'button',
    class: `btn btn--${variant} btn--${size} arm${block ? ' btn--block' : ''}`,
    'data-mut': mut ? '' : undefined,
  }, [iconName ? icon(iconName, { size: size === 'sm' ? 18 : 20 }) : null, text, bar]);
  node.disabled = disabled;

  let timer = null;

  function disarm() {
    clearTimeout(timer);
    node.dataset.armed = 'false';
    text.textContent = label;
    bar.style.animation = 'none';
    document.removeEventListener('pointerdown', outside, true);
  }

  function outside(event) {
    if (!node.contains(event.target)) disarm();
  }

  node.addEventListener('click', () => {
    if (node.dataset.armed === 'true') {
      disarm();
      haptic(20);
      onConfirm();
      return;
    }
    node.dataset.armed = 'true';
    text.textContent = armedLabel || `Tap again: ${label}`;
    bar.style.animation = 'none';
    // Restart the countdown animation from zero.
    void bar.offsetWidth;
    bar.style.animation = `arm-count ${ms}ms linear forwards`;
    haptic(8);
    timer = setTimeout(disarm, ms);
    document.addEventListener('pointerdown', outside, true);
  });
  node.disarm = disarm;
  return node;
}

// --------------------------------------------------------------------------
// Form controls
// --------------------------------------------------------------------------

export function field(labelText, input, hint) {
  const id = input.id || uid('f');
  input.id = id;
  return el('div', { class: 'field' }, [
    el('label', { class: 'field__label', for: id, text: labelText }),
    input,
    hint ? el('p', { class: 'field__hint', text: hint }) : null,
  ]);
}

export function textInput({ value = '', placeholder = '', type = 'text', mut = true, onInput, mono = false, ...rest }) {
  const node = el('input', {
    type,
    class: `input${mono ? ' input--mono' : ''}`,
    placeholder,
    autocomplete: 'off',
    autocapitalize: 'off',
    spellcheck: 'false',
    'data-mut': mut ? '' : undefined,
    ...rest,
  });
  node.value = value;
  if (onInput) node.addEventListener('input', () => onInput(node.value));
  return node;
}

export function selectInput({ options, value = '', mut = true, onChange }) {
  const node = el('select', { class: 'input select', 'data-mut': mut ? '' : undefined },
    options.map((o) => {
      const opt = typeof o === 'string' ? { value: o, label: o } : o;
      return el('option', { value: opt.value, text: opt.label ?? opt.value });
    }));
  node.value = value;
  if (onChange) node.addEventListener('change', () => onChange(node.value));
  return node;
}

/** A number with large − and + buttons, because a spinner arrow is not a touch target. */
export function stepper({ value, min = 0, max = 100, step = 1, unit = '', decimals = 0, onChange, mut = true, ariaLabel }) {
  let current = Number.isFinite(value) ? value : min;
  const input = el('input', {
    type: 'text',
    class: 'stepper__value',
    inputmode: decimals ? 'decimal' : 'numeric',
    autocomplete: 'off',
    'aria-label': ariaLabel,
    'data-mut': mut ? '' : undefined,
  });

  const clamp = (v) => Math.min(max, Math.max(min, v));
  const round = (v) => Number(v.toFixed(decimals));
  const show = () => { input.value = `${current.toFixed(decimals)}${unit ? ` ${unit}` : ''}`; };

  function set(v, fire = true) {
    const next = round(clamp(v));
    const changed = next !== current;
    current = next;
    show();
    if (changed && fire && onChange) onChange(current);
  }

  const minus = el('button', {
    type: 'button', class: 'stepper__btn', 'aria-label': 'decrease', 'data-mut': mut ? '' : undefined,
    onclick: () => set(current - step),
  }, [icon('minus', { size: 20 })]);
  const plus = el('button', {
    type: 'button', class: 'stepper__btn', 'aria-label': 'increase', 'data-mut': mut ? '' : undefined,
    onclick: () => set(current + step),
  }, [icon('plus', { size: 20 })]);

  input.addEventListener('focus', () => { input.value = String(current); input.select(); });
  input.addEventListener('change', () => {
    const parsed = parseFloat(input.value.replace(',', '.'));
    if (Number.isFinite(parsed)) set(parsed);
    else show();
  });
  input.addEventListener('blur', show);
  show();

  const root = el('div', { class: 'stepper' }, [minus, input, plus]);
  root.setValue = (v) => { if (document.activeElement !== input) set(v, false); };
  root.getValue = () => current;
  return root;
}

export function segmented({ options, value, onChange, ariaLabel, cls = '' }) {
  const root = el('div', { class: `seg${cls ? ` ${cls}` : ''}`, role: 'tablist', 'aria-label': ariaLabel });
  const buttons = new Map();

  function set(next, fire = false) {
    for (const [key, b] of buttons) b.setAttribute('aria-selected', String(key === next));
    if (fire && onChange) onChange(next);
  }

  for (const option of options) {
    const b = el('button', {
      type: 'button', role: 'tab', class: 'seg__btn', 'aria-selected': String(option.value === value),
      onclick: () => set(option.value, true),
    }, [
      option.icon ? icon(option.icon, { size: 18 }) : null,
      el('span', { text: option.label }),
      option.badge ? el('span', { class: 'seg__badge', text: option.badge, dataset: { tone: option.tone || 'warn' } }) : null,
    ]);
    buttons.set(option.value, b);
    root.append(b);
  }
  root.set = (next) => set(next, false);
  return root;
}

export function switchRow({ label, hint, checked = false, onChange, mut = true, disabled = false, tone }) {
  const input = el('input', { type: 'checkbox', class: 'switch__input', role: 'switch', 'data-mut': mut ? '' : undefined });
  input.checked = checked;
  input.disabled = disabled;
  input.addEventListener('change', () => onChange && onChange(input.checked));
  const root = el('label', { class: 'switch', dataset: { tone: tone || '' } }, [
    el('span', { class: 'switch__text' }, [
      el('span', { class: 'switch__label', text: label }),
      hint ? el('span', { class: 'switch__hint', text: hint }) : null,
    ]),
    input,
    el('span', { class: 'switch__track', 'aria-hidden': 'true' }, [el('span', { class: 'switch__thumb' })]),
  ]);
  root.input = input;
  return root;
}

export function checkRow({ label, hint, checked = false, onChange, mut = true, disabled = false, radio = false, name }) {
  const input = el('input', {
    type: radio ? 'radio' : 'checkbox', class: 'check__input', name, 'data-mut': mut ? '' : undefined,
  });
  input.checked = checked;
  input.disabled = disabled;
  input.addEventListener('change', () => onChange && onChange(input.checked));
  const root = el('label', { class: 'check' }, [
    input,
    el('span', { class: 'check__box', 'aria-hidden': 'true' }, [icon('check', { size: 16, stroke: 3 })]),
    el('span', { class: 'check__text' }, [
      el('span', { class: 'check__label', text: label }),
      hint ? el('span', { class: 'check__hint', text: hint }) : null,
    ]),
  ]);
  root.input = input;
  return root;
}

// --------------------------------------------------------------------------
// Cards and structure
// --------------------------------------------------------------------------

export function card({ title, subtitle, actions, children = [], cls = '', tone }) {
  const head = title || actions
    ? el('header', { class: 'card__head' }, [
      el('div', { class: 'card__titles' }, [
        title ? el('h3', { class: 'card__title', text: title }) : null,
        subtitle ? el('p', { class: 'card__sub', text: subtitle }) : null,
      ]),
      actions ? el('div', { class: 'card__actions' }, [].concat(actions)) : null,
    ])
    : null;
  return el('section', { class: `card${cls ? ` ${cls}` : ''}`, dataset: { tone: tone || '' } }, [head, ...[].concat(children)]);
}

export function note(text, tone = 'idle') {
  return el('p', { class: 'note', dataset: { tone }, text });
}

export function callout(text, tone = 'warn', iconName = 'warning') {
  return el('div', { class: 'callout', dataset: { tone } }, [
    icon(iconName, { size: 20 }),
    el('span', { text }),
  ]);
}

export function empty(text, sub) {
  return el('div', { class: 'empty' }, [
    el('p', { class: 'empty__text', text }),
    sub ? el('p', { class: 'empty__sub', text: sub }) : null,
  ]);
}

export function kv(pairs) {
  const dl = el('dl', { class: 'kv' });
  for (const [key, value, sub] of pairs) {
    dl.append(el('dt', { text: key }));
    const dd = el('dd', {}, [value === null || value === undefined || value === '' ? '\u2014' : String(value)]);
    if (sub) dd.append(el('span', { class: 'kv__sub', text: ` ${sub}` }));
    dl.append(dd);
  }
  return dl;
}

export function spinner(text = 'Loading\u2026') {
  return el('div', { class: 'spinner', role: 'status' }, [el('span', { class: 'spinner__dot' }), text]);
}

// --------------------------------------------------------------------------
// Toasts
// --------------------------------------------------------------------------

export function toast(tone, title, detail, { action, ms } = {}) {
  const host = document.getElementById('toasts');
  if (!host) return null;
  const node = el('div', { class: 'toast', dataset: { tone }, role: tone === 'bad' ? 'alert' : 'status' }, [
    el('div', { class: 'toast__body' }, [
      el('div', { class: 'toast__title', text: title }),
      detail ? el('div', { class: 'toast__detail', text: detail }) : null,
    ]),
  ]);
  if (action) {
    node.append(el('button', {
      type: 'button', class: 'toast__action', text: action.label,
      onclick: (event) => { event.stopPropagation(); node.remove(); action.run(); },
    }));
  }
  node.addEventListener('click', () => node.remove());
  host.append(node);
  while (host.children.length > 3) host.firstElementChild.remove();
  setTimeout(() => node.remove(), ms || (tone === 'bad' ? 9000 : tone === 'warn' ? 6000 : 3200));
  if (tone === 'bad') haptic([40, 60, 40]);
  return node;
}

// --------------------------------------------------------------------------
// Sheets
//
// Everything modal is a bottom sheet. Opening one pushes a history entry so the
// phone's back gesture closes it instead of leaving the page.
// --------------------------------------------------------------------------

const sheets = [];
let sheetSeq = 0;
// history.back() calls we made ourselves; their popstate must not close another sheet.
const selfPops = [];

function setBackgroundInert(on) {
  const app = document.getElementById('app');
  if (app) {
    if (on) app.setAttribute('inert', '');
    else app.removeAttribute('inert');
  }
}

export function openSheet({
  title, subtitle, body, footer, tone = '', dismissable = true, tall = false, onClose,
}) {
  const id = `sheet${(sheetSeq += 1)}`;
  const returnFocus = document.activeElement;
  const host = document.getElementById('sheets');

  const grabber = el('div', { class: 'sheet__grab', 'aria-hidden': 'true' });
  const closeBtn = dismissable
    ? el('button', { type: 'button', class: 'iconbtn iconbtn--ghost iconbtn--md', 'aria-label': 'Close', onclick: () => handle.close() }, [icon('close')])
    : null;
  const head = el('header', { class: 'sheet__head' }, [
    el('div', { class: 'sheet__titles' }, [
      el('h2', { class: 'sheet__title', id: `${id}-t`, text: title || '' }),
      subtitle ? el('p', { class: 'sheet__sub', text: subtitle }) : null,
    ]),
    closeBtn,
  ]);
  const bodyEl = el('div', { class: 'sheet__body' }, [].concat(body || []));
  const footEl = footer ? el('footer', { class: 'sheet__foot' }, [].concat(footer)) : null;
  const panel = el('section', {
    class: `sheet${tall ? ' sheet--tall' : ''}`, role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': `${id}-t`,
    dataset: { tone }, tabindex: '-1',
  }, [grabber, head, bodyEl, footEl]);
  const backdrop = el('div', { class: 'sheet__backdrop' });
  const root = el('div', { class: 'sheet-root', dataset: { state: 'enter' } }, [backdrop, panel]);

  let resolve;
  const closed = new Promise((r) => { resolve = r; });
  let finished = false;

  const handle = {
    el: panel, body: bodyEl, footer: footEl, closed,
    close(result) {
      return finish(result, false);
    },
    isClosing() { return finished; },
    setBody(nodes) { bodyEl.replaceChildren(...[].concat(nodes)); },
  };

  function finish(result, viaPop) {
    if (finished) return closed;
    finished = true;
    shield(350);
    const index = sheets.indexOf(entry);
    if (index >= 0) sheets.splice(index, 1);
    root.dataset.state = 'leave';
    let popped = Promise.resolve();
    if (!viaPop) {
      popped = new Promise((r) => selfPops.push(r));
      history.back();
    }
    const animated = new Promise((r) => setTimeout(r, prefersReducedMotion() ? 0 : 200));
    Promise.all([popped, animated]).then(() => {
      root.remove();
      if (!sheets.length) setBackgroundInert(false);
      holdOpen(false);
      if (returnFocus && returnFocus.focus && document.contains(returnFocus)) returnFocus.focus({ preventScroll: true });
      if (onClose) onClose(result);
      resolve(result);
    });
    return closed;
  }

  const entry = { id, finish };
  sheets.push(entry);
  history.pushState({ sheet: id }, '');
  setBackgroundInert(true);
  holdOpen(true);
  host.append(root);
  // The first moments after a sheet appears are dead to touch, so the finger that
  // opened it cannot land on a button that slid in under it.
  panel.dataset.settling = 'true';
  setTimeout(() => { panel.dataset.settling = 'false'; }, 380);
  requestAnimationFrame(() => {
    root.dataset.state = 'open';
    panel.focus({ preventScroll: true });
  });

  if (dismissable) {
    backdrop.addEventListener('click', () => handle.close());
    root.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') handle.close();
    });
  }

  // Drag the head down to dismiss.
  let drag = null;
  head.addEventListener('pointerdown', (event) => {
    if (!dismissable || event.target.closest('button')) return;
    drag = { y: event.clientY, dy: 0, id: event.pointerId };
    head.setPointerCapture(event.pointerId);
    panel.style.transition = 'none';
  });
  head.addEventListener('pointermove', (event) => {
    if (!drag || event.pointerId !== drag.id) return;
    drag.dy = Math.max(0, event.clientY - drag.y);
    panel.style.transform = `translateY(${drag.dy}px)`;
  });
  const endDrag = () => {
    if (!drag) return;
    const far = drag.dy > 110;
    drag = null;
    panel.style.transition = '';
    panel.style.transform = '';
    if (far) handle.close();
  };
  head.addEventListener('pointerup', endDrag);
  head.addEventListener('pointercancel', endDrag);

  return handle;
}

window.addEventListener('popstate', () => {
  if (selfPops.length) {
    selfPops.shift()();
    return;
  }
  const top = sheets[sheets.length - 1];
  // A back gesture arrived while a sheet is open: that sheet is what goes away.
  if (top) top.finish(false, true);
});

export function sheetOpen() {
  return sheets.length > 0;
}

/**
 * The confirmation every consequential action goes through. `mode: 'hold'` makes
 * the operator press and hold; `typeName` additionally requires typing a name.
 */
export function confirmSheet({
  title, body, facts, callout: calloutText, calloutTone = 'warn', tone = 'warn',
  confirm = {}, cancelLabel = 'Cancel', typeName, detail,
}) {
  const {
    label = 'Confirm', mode = 'tap', ms = 1200, icon: iconName, variant = tone === 'danger' ? 'danger' : 'primary',
  } = confirm;

  return new Promise((resolve) => {
    let outcome = false;
    const content = [];
    if (body) content.push(el('p', { class: 'sheet__text', text: body }));
    if (calloutText) content.push(callout(calloutText, calloutTone));
    if (facts && facts.length) content.push(kv(facts));
    if (detail) content.push(el('p', { class: 'sheet__fine', text: detail }));

    let closing = false;
    const done = (value) => {
      // The first way out wins: a Cancel, a back gesture or a backdrop tap must not be
      // overturned by a hold that finishes afterwards.
      if (closing || sheet.isClosing()) return;
      closing = true;
      outcome = value;
      sheet.close(value);
    };
    // A completed hold keeps the sheet up until the finger lifts, so the lift cannot
    // land on the screen underneath as a tap.
    const fire = (info) => {
      if (typeName && !gate) return;
      if (info && info.released) info.released.then(() => setTimeout(() => done(true), 140));
      else done(true);
    };

    let gate = null;
    let typed = null;
    if (typeName) {
      typed = textInput({
        placeholder: `type ${typeName}`, mut: false, mono: true, enterkeyhint: 'done',
        onInput: (v) => {
          gate = v.trim() === typeName;
          action.toggleAttribute('data-blocked', !gate);
          action.setAttribute('aria-disabled', String(!gate));
        },
        'aria-label': `type ${typeName} to confirm`,
      });
      // Enter puts the keyboard away, which is what uncovers the hold button.
      typed.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') typed.blur();
      });
      content.push(field(`Type ${typeName} to confirm`, typed));
      gate = false;
    }

    const action = mode === 'hold'
      ? holdButton({ label, ms, tone: variant === 'danger' ? 'danger' : 'warn', icon: iconName, onConfirm: fire, mut: false })
      : btn({ label, icon: iconName, variant, block: true, size: 'lg', onClick: fire });
    if (typeName) {
      action.setAttribute('data-blocked', '');
      action.setAttribute('aria-disabled', 'true');
    }

    const foot = [];
    if (mode === 'hold') foot.push(el('p', { class: 'sheet__hint', text: `Press and hold for ${(ms / 1000).toFixed(ms % 1000 ? 1 : 0)} s` }));
    foot.push(action);
    foot.push(btn({ label: cancelLabel, variant: 'secondary', block: true, size: 'lg', onClick: () => done(false) }));

    const sheet = openSheet({
      title, tone, body: content, footer: foot, onClose: () => resolve(outcome),
    });
  });
}

// --------------------------------------------------------------------------
// The lock guard
// --------------------------------------------------------------------------

let hintToast = null;

export function lockedHint() {
  if (hintToast && document.contains(hintToast)) return;
  hintToast = toast('info', 'Controls are locked', 'Unlock to make changes.', {
    action: { label: 'Unlock', run: () => unlock() }, ms: 4000,
  });
}

/** Returns true when the page is unlocked; otherwise explains why nothing happened. */
export function ensureUnlocked() {
  if (!isLocked()) return true;
  lockedHint();
  return false;
}

export function applyLockToDom(root = document) {
  const locked = isLocked();
  for (const node of root.querySelectorAll('[data-mut]')) {
    if (node.matches('input:not([type=checkbox]):not([type=radio]):not([type=range])')) node.readOnly = locked;
    if (node.matches('select, input[type=checkbox], input[type=radio], input[type=range]')) {
      if (locked) {
        if (!node.disabled) {
          node.dataset.lockedByGuard = 'true';
          node.disabled = true;
        }
      } else if (node.dataset.lockedByGuard) {
        delete node.dataset.lockedByGuard;
        node.disabled = false;
      }
    }
    node.setAttribute('aria-disabled', locked ? 'true' : 'false');
  }
}

export function installGuard() {
  const block = (event) => {
    if (!isLocked()) return;
    // Moving focus and dismissing things must keep working while locked.
    if (event.type === 'keydown' && ['Tab', 'Escape', 'Shift'].includes(event.key)) return;
    const target = event.target instanceof Element ? event.target.closest('[data-mut]') : null;
    if (!target) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (event.type === 'click' || event.type === 'pointerup') lockedHint();
    if (event.type === 'focusin' && target.blur) target.blur();
  };
  for (const type of ['pointerdown', 'pointerup', 'click', 'keydown', 'change', 'input', 'focusin']) {
    document.addEventListener(type, block, { capture: true });
  }
  // Swallow stray clicks right after a hold completes or a sheet closes.
  document.addEventListener('click', (event) => {
    if (performance.now() < shieldUntil) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  }, { capture: true });
  touch();
}
