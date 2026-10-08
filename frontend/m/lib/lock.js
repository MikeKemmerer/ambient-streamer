// The control lock. The page opens view-only; one deliberate tap on the padlock
// enables controls, and they lock again after a stretch of inactivity. This is the
// first of three guards: the lock stops stray touches, the confirmation sheets stop
// a wrong tap, and the hold gesture stops a tap that was meant for something else.

const PREFS_KEY = 'ambient.m.prefs';

const DEFAULTS = { lockOnOpen: true, autoLockSeconds: 120 };

function readPrefs() {
  try {
    return { ...DEFAULTS, ...JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') };
  } catch {
    return { ...DEFAULTS };
  }
}

let prefs = readPrefs();
let locked = prefs.lockOnOpen;
let idleTimer = null;
let deadline = 0;
let busy = 0;
const listeners = new Set();

export function getPrefs() {
  return { ...prefs };
}

export function setPrefs(patch) {
  prefs = { ...prefs, ...patch };
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch {
    /* private mode: preferences last for this page only */
  }
  arm();
  notify();
}

export function isLocked() {
  return locked;
}

/** Seconds until the controls lock again, or null when they will not. */
export function secondsLeft() {
  if (locked || !prefs.autoLockSeconds) return null;
  return Math.max(0, Math.ceil((deadline - Date.now()) / 1000));
}

export function onLockChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function notify() {
  document.documentElement.dataset.locked = String(locked);
  for (const fn of listeners) fn(locked);
}

export function unlock() {
  locked = false;
  arm();
  notify();
}

export function lock() {
  locked = true;
  clearTimeout(idleTimer);
  notify();
}

/** Anything the operator does counts as activity and pushes the deadline out. */
export function touch() {
  if (!locked) arm();
}

/** A sheet or a hold in progress must not lock the page out from under the operator. */
export function holdOpen(on) {
  busy = Math.max(0, busy + (on ? 1 : -1));
  if (!busy) touch();
}

function arm() {
  clearTimeout(idleTimer);
  if (locked || !prefs.autoLockSeconds) return;
  deadline = Date.now() + prefs.autoLockSeconds * 1000;
  idleTimer = setTimeout(function tick() {
    if (busy) {
      idleTimer = setTimeout(tick, 5000);
      return;
    }
    lock();
  }, prefs.autoLockSeconds * 1000);
}

export function initLock() {
  notify();
  for (const type of ['pointerdown', 'keydown', 'scroll']) {
    document.addEventListener(type, touch, { passive: true, capture: true });
  }
}
