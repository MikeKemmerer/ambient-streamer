// Everything that changes something. Each action checks the lock first, and the
// consequential ones are wrapped in a confirmation sized to what they can do:
//   hold to confirm  start, stop, restart, resolution on a live channel, delete,
//                    and sending a sound to the stream
//   tap to confirm   saving a list or schedule, a delivery change, a preset, a
//                    live visualization switch, adding everything, discarding edits
//   tap twice        skip a track, put one on air

import { ApiError, api, asUploadResults, uploadMedia } from '../../api.js';
import { basename, fmtBytes } from '../../render.js';
import { confirmSheet, ensureUnlocked, toast } from '../lib/ui.js';
import {
  channelIsRunning, chanData, config, deliveryOf, emit, guard, isStopped, listDiff, loadChannelDetail,
  loadMedia, loadSoundboard, pluginLabel, queueVizCompletion, refreshChannels, report, resolutionOf,
  scheduleRefresh, slidesFormOf, audioFormOf, state, vizEnabled, TARGETS,
} from './store.js';

// --------------------------------------------------------------------------
// Lifecycle
// --------------------------------------------------------------------------

export async function lifecycle(name, action) {
  if (!ensureUnlocked()) return;
  await guard(`${action} ${name}`, () => api[action](name), 'accepted');
  scheduleRefresh(500);
}

const LIFECYCLE_COPY = {
  start: {
    title: (n) => `Start ${n}?`,
    label: 'Hold to start',
    ms: 1200,
    icon: 'play',
    tone: 'warn',
    callout: (n) => `${n} will start streaming. If YouTube is one of its targets, the broadcast goes live.`,
  },
  stop: {
    title: (n) => `Stop ${n}?`,
    label: 'Hold to stop',
    ms: 1500,
    icon: 'stop',
    tone: 'danger',
    callout: (n) => `${n} goes off the air until it is started again. YouTube can end a broadcast after a sustained gap.`,
  },
  restart: {
    title: (n) => `Restart ${n}?`,
    label: 'Hold to restart',
    ms: 1500,
    icon: 'restart',
    tone: 'danger',
    callout: () => 'The stream drops for about a second and reconnects as a new YouTube ingest session.',
  },
};

export async function confirmLifecycle(name, action) {
  if (!ensureUnlocked()) return false;
  const copy = LIFECYCLE_COPY[action];
  const ch = state.channels.get(name) || {};
  const delivery = deliveryOf(name);
  const ok = await confirmSheet({
    title: copy.title(name),
    tone: copy.tone,
    callout: copy.callout(name),
    calloutTone: copy.tone === 'danger' ? 'bad' : 'warn',
    facts: [
      ['now', ch.state || 'unknown'],
      ['delivers to', delivery.targets.join(' + ')],
      ['output', resolutionOf(name) || '\u2014'],
    ],
    confirm: { label: copy.label, mode: 'hold', ms: copy.ms, icon: copy.icon, variant: copy.tone === 'danger' ? 'danger' : 'primary' },
  });
  if (!ok) return false;
  await lifecycle(name, action);
  return true;
}

// --------------------------------------------------------------------------
// Audio
// --------------------------------------------------------------------------

export async function skipTrack(name) {
  if (!ensureUnlocked()) return;
  const result = await guard(`skip ${name}`, () => api.skip(name), 'accepted');
  if (result === undefined) return;
  toast('ok', 'skipped', 'the next track is on air');
  scheduleRefresh(1500);
}

export async function playTrack(name, path) {
  if (!ensureUnlocked()) return;
  const result = await guard(`play on ${name}`, () => api.play(name, path), 'accepted');
  if (result === undefined) return;
  toast('ok', 'now playing', basename(path));
  scheduleRefresh(1500);
}

function diffFacts(list, extra = []) {
  const diff = listDiff(list);
  const facts = [];
  if (diff.added) facts.push(['added', String(diff.added)]);
  if (diff.removed) facts.push(['removed', String(diff.removed)]);
  if (diff.reordered) facts.push(['order', 'changed']);
  facts.push(['total', String(list.draft.length)]);
  return [...facts, ...extra];
}

export async function savePlaylist(name) {
  if (!ensureUnlocked()) return false;
  const d = chanData(name);
  const form = audioFormOf(name);
  const ok = await confirmSheet({
    title: 'Save the playlist?',
    tone: 'info',
    body: `This replaces the whole play order on ${name}.`,
    facts: diffFacts(d.playlist, [
      ['shuffle', form.shuffle ? 'on' : 'off'],
      ['crossfade', `${form.crossfade} s`],
    ]),
    detail: 'Audio only. Liquidsoap picks up the new list without touching the video.',
    confirm: { label: 'Save playlist', mode: 'tap', icon: 'check' },
  });
  if (!ok) return false;
  const saved = await guard('save playlist', async () => {
    await api.patchChannel(name, {
      audio: { shuffle: form.shuffle, crossfade_seconds: form.crossfade },
    });
    await api.setPlaylist(name, d.playlist.draft);
    return true;
  }, `${d.playlist.draft.length} tracks`);
  if (saved) {
    d.audioForm = null;
    d.playlist.undo = [];
    await loadChannelDetail(name);
  }
  return Boolean(saved);
}

export async function saveSlides(name) {
  if (!ensureUnlocked()) return false;
  const d = chanData(name);
  const form = slidesFormOf(name);
  const ok = await confirmSheet({
    title: 'Save the slides?',
    tone: 'info',
    body: `This replaces the slide list on ${name}.`,
    facts: diffFacts(d.slides, [
      ['order', form.order],
      ['hold', `${form.hold} s`],
      ['fade', `${form.fade} s`],
    ]),
    detail: 'The slideshow producer rescans between slides. No encoder restart.',
    confirm: { label: 'Save slides', mode: 'tap', icon: 'check' },
  });
  if (!ok) return false;
  const saved = await guard('save slides', async () => {
    await api.patchChannel(name, {
      images: { order: form.order, hold_seconds: form.hold, fade_seconds: form.fade },
    });
    await api.setImages(name, d.slides.draft);
    return true;
  }, `${d.slides.draft.length} slides`);
  if (saved) {
    d.slidesForm = null;
    d.slides.undo = [];
    await loadChannelDetail(name);
  }
  return Boolean(saved);
}

/** Dropping unsaved edits is itself a loss, so it is confirmed. */
export async function confirmDiscard(what, count) {
  if (!ensureUnlocked()) return false;
  return confirmSheet({
    title: `Discard ${what}?`,
    tone: 'danger',
    body: count ? `${count} unsaved change${count === 1 ? '' : 's'} will be lost.` : 'Your unsaved changes will be lost.',
    confirm: { label: 'Discard changes', mode: 'tap', variant: 'danger', icon: 'trash' },
    cancelLabel: 'Keep editing',
  });
}

export async function confirmAddAll(noun, count, filtered) {
  if (!ensureUnlocked()) return false;
  return confirmSheet({
    title: `Add ${count} ${noun}${count === 1 ? '' : 's'}?`,
    tone: 'info',
    body: filtered
      ? `Adds every unselected ${noun} that matches the filter to the end of the list.`
      : `Adds every unselected ${noun} to the end of the list.`,
    detail: 'Nothing is saved until you save the list. You can undo this.',
    confirm: { label: `Add ${count}`, mode: 'tap', icon: 'plus' },
  });
}

// --------------------------------------------------------------------------
// Output and delivery
// --------------------------------------------------------------------------

export async function applyResolution(name, target) {
  if (!ensureUnlocked()) return false;
  const current = resolutionOf(name);
  if (!target || target === current) return false;
  const live = !isStopped(name);

  const ok = await confirmSheet({
    title: `Change ${name} to ${target}?`,
    tone: live ? 'danger' : 'info',
    body: live
      ? `The compositor is encoding at ${current}. Resolution is compiled into the filtergraph when FFmpeg launches, so it cannot change on a running graph.`
      : `${name} is stopped, so nothing is interrupted. It will start at ${target}.`,
    callout: live ? 'Restarts the channel: roughly a 1 s gap on air and a new YouTube ingest session.' : undefined,
    calloutTone: 'bad',
    detail: live ? 'Higher resolutions cost substantially more CPU. Check headroom under System first.' : undefined,
    facts: [['from', current || '\u2014'], ['to', target]],
    confirm: live
      ? { label: `Hold to restart at ${target}`, mode: 'hold', ms: 1500, icon: 'restart', variant: 'danger' }
      : { label: `Set ${target}`, mode: 'tap', icon: 'check' },
  });
  if (!ok) return false;
  // The channel can change state while the sheet is open (the watchdog restarts a
  // failed one, for instance). A tap-confirmed change must not land on a live stream.
  if (!live && !isStopped(name)) {
    toast('warn', `resolution ${name}`, 'The channel started while you were deciding. Nothing was changed; try again.');
    return false;
  }
  await guard(`resolution ${name}`, () => api.setResolution(name, target),
    live ? `restarting at ${target}` : `${target} \u2014 takes effect on the next start`);
  scheduleRefresh(800);
  return true;
}

/** Only what the operator changed, so an untouched key is never overwritten. */
export function deliveryBody(name, form) {
  const base = deliveryOf(name);
  const body = {};
  if (form.key.trim()) body.stream_key = form.key.trim();
  if (form.rtmp.trim() && form.rtmp.trim() !== base.rtmpUrl) body.rtmp_url = form.rtmp.trim();

  if (!form.encoder) {
    if (base.encoder) body.clear_encoder = true;
  } else if (form.encoder !== base.encoder) {
    body.encoder = form.encoder;
  }

  if (form.fpsDefault) body.clear_fps = true;
  else if (form.fps && Number(form.fps) !== base.fps) body.fps = Number(form.fps);

  const targets = TARGETS.filter((t) => form.targets.includes(t));
  if (targets.length && targets.join(',') !== base.targets.join(',')) body.targets = targets;

  const height = Number(form.localHeight);
  const localFps = Number(form.localFps);
  if (!height && !localFps) {
    if (base.localHeight || base.localFps) body.clear_local = true;
  } else {
    if (height && height !== base.localHeight) body.local_height = height;
    if (localFps && localFps !== base.localFps) body.local_fps = localFps;
  }
  return body;
}

const DELIVERY_LABELS = {
  stream_key: 'stream key', rtmp_url: 'ingest URL', encoder: 'encoder', clear_encoder: 'encoder', fps: 'fps',
  clear_fps: 'fps', targets: 'delivery targets', local_height: 'local HLS size', local_fps: 'local HLS fps',
  clear_local: 'local HLS size',
};

export async function applyDelivery(name, form) {
  if (!ensureUnlocked()) return false;
  const body = deliveryBody(name, form);
  const keys = Object.keys(body);
  if (!keys.length) return false;

  const labels = [...new Set(keys.map((k) => DELIVERY_LABELS[k] || k))];
  const ok = await confirmSheet({
    title: `Change where ${name} publishes?`,
    tone: 'warn',
    body: 'These settings live in the channel .env, so nothing here touches a stream that is on air.',
    facts: [
      ['changing', labels.join(', ')],
      ...(body.targets ? [['targets', body.targets.join(' + ')]] : []),
      ...(body.stream_key ? [['stream key', 'will be replaced']] : []),
    ],
    detail: 'Applies on the next start.',
    confirm: { label: 'Apply to next start', mode: 'tap', icon: 'check', variant: 'primary' },
  });
  if (!ok) return false;

  const result = await guard(`delivery ${name}`, () => api.setDelivery(name, body));
  if (result === undefined) return false;

  const changed = Array.isArray(result.changed) ? result.changed : [];
  if (!changed.length) {
    toast('warn', `delivery ${name}`, result.detail || 'nothing to change');
  } else {
    const targets = Array.isArray(result.delivery) ? result.delivery.join(' + ') : '\u2014';
    toast('ok', `delivery ${name} \u2014 ${changed.join(', ')}`,
      `${targets} \u00B7 ${result.encoder} at ${result.fps} fps \u00B7 ${result.detail || 'applies on the next start'}`);
  }
  const detail = state.details.get(name);
  if (detail) {
    state.details.set(name, {
      ...detail,
      rtmp_url: result.rtmp_url,
      has_stream_key: result.has_stream_key,
      encoder_requested: result.encoder,
      fps_requested: result.fps,
      delivery: result.delivery,
      youtube: result.youtube,
      local: result.local,
      local_height_requested: result.local_height_requested,
      local_fps_requested: result.local_fps_requested,
      feeds: result.feeds,
    });
  }
  chanData(name).delivery = null;
  emit('detail', name);
  scheduleRefresh(200);
  return true;
}

// --------------------------------------------------------------------------
// Channels
// --------------------------------------------------------------------------

export async function createChannel(body) {
  if (!ensureUnlocked()) return { ok: false, error: 'locked' };
  try {
    await api.createChannel(body);
  } catch (err) {
    return { ok: false, error: err instanceof ApiError ? `${err.error}: ${err.detail}` : String(err) };
  }
  toast('ok', 'channel created', `${body.name} \u2014 add YOUTUBE_STREAM_KEY to channels/${body.name}/.env`);
  await refreshChannels();
  return { ok: true };
}

export async function deleteChannel(name) {
  if (!ensureUnlocked()) return false;
  if (!isStopped(name)) {
    toast('warn', 'delete', `Stop ${name} first.`);
    return false;
  }
  const ok = await confirmSheet({
    title: `Delete ${name}?`,
    tone: 'danger',
    body: 'This removes the channel from the control plane.',
    callout: 'This cannot be undone from here. Its playlist, slides and settings go with it.',
    calloutTone: 'bad',
    typeName: name,
    confirm: { label: 'Hold to delete', mode: 'hold', ms: 2000, icon: 'trash', variant: 'danger' },
  });
  if (!ok) return false;
  if (!isStopped(name)) {
    toast('warn', 'delete', `${name} started while you were deciding. Nothing was deleted.`);
    return false;
  }
  const result = await guard(`delete ${name}`, () => api.deleteChannel(name), 'deleted');
  if (result === undefined) return false;
  state.data.delete(name);
  await refreshChannels();
  return true;
}

// --------------------------------------------------------------------------
// Soundboard
// --------------------------------------------------------------------------

export async function sendSound(name, clip) {
  if (!ensureUnlocked()) return false;
  const result = await guard(`soundboard ${name}`, () => api.playSound(name, clip.container_path), 'accepted');
  if (result !== undefined) toast('ok', 'soundboard', `${clip.name} sent to stream`);
  return result !== undefined;
}

/** A safety control, so it works even while the page is locked. */
export async function stopSounds(name) {
  const result = await guard(`stop soundboard ${name}`, () => api.stopSoundboard(name), 'accepted');
  if (result !== undefined) toast('ok', 'soundboard', 'stopped');
}

// --------------------------------------------------------------------------
// Look
// --------------------------------------------------------------------------

export async function confirmVizPower(name, target) {
  if (!ensureUnlocked()) return false;
  const live = channelIsRunning(name);
  const ok = await confirmSheet({
    title: target ? 'Turn the visualization on?' : 'Turn the visualization off?',
    tone: 'info',
    body: live
      ? (target
        ? 'Starts the selected visualizer child. The compositor and stream keep running.'
        : 'Stops only the visualizer child. The compositor and stream keep running on a transparent fallback.')
      : 'Saved now and applied the next time the channel starts.',
    confirm: { label: target ? 'Turn on' : 'Turn off', mode: 'tap', icon: 'check' },
  });
  if (!ok) return false;
  const result = await guard(`visualization ${target ? 'on' : 'off'}`, () => api.setVisualizationEnabled(name, target));
  if (result === undefined) return false;
  const cfg = config(name);
  cfg.visualization = { ...(cfg.visualization || {}), enabled: target };
  state.channels.set(name, { ...(state.channels.get(name) || { name }), visualization_enabled: target });
  const queued = queueVizCompletion(name, result, {
    kind: 'power',
    label: `visualization ${target ? 'on' : 'off'}`,
    success: target ? 'visualizer child started; stream unchanged' : 'visualizer child stopped; stream unchanged',
  });
  if (!queued) {
    toast('ok', `visualization ${target ? 'on' : 'off'}`,
      `saved \u2014 ${target ? 'starts' : 'stays off'} on the next channel start`);
  }
  emit('detail', name);
  emit('status', name);
  return true;
}

export async function confirmVizVisible(name, visible) {
  if (!ensureUnlocked()) return false;
  const ok = await confirmSheet({
    title: visible ? 'Show the visualization on air?' : 'Hide the visualization?',
    tone: 'info',
    body: channelIsRunning(name)
      ? 'Lands in one frame. The visualizer child keeps rendering and using CPU while hidden.'
      : 'Saved now and applied the next time the channel starts.',
    confirm: { label: visible ? 'Show' : 'Hide', mode: 'tap', icon: 'check' },
  });
  if (!ok) return false;
  const result = await guard('visualization', () => api.setVisible(name, visible), 'accepted');
  await loadChannelDetail(name);
  if (result !== undefined) toast('ok', visible ? 'visualization showing' : 'visualization hidden', result.detail || '');
  return result !== undefined;
}

export async function applyOpacity(name, percent) {
  if (!ensureUnlocked()) return false;
  const opacity = percent / 100;
  const result = await guard('visualization opacity', () => api.setVisualizationOpacity(name, opacity), 'accepted');
  if (result === undefined) {
    await loadChannelDetail(name);
    return false;
  }
  const cfg = config(name);
  cfg.visualization = { ...(cfg.visualization || {}), opacity };
  chanData(name).opacity = null;
  toast('ok', 'visualization opacity', result.detail || 'saved');
  emit('detail', name);
  return true;
}

export async function switchVisualization(name, plugin) {
  if (!ensureUnlocked()) return false;
  const live = channelIsRunning(name);
  const label = pluginLabel(plugin);
  const ok = await confirmSheet({
    title: `Switch to ${label}?`,
    tone: 'info',
    body: live
      ? 'Replaces only the visualizer child. The compositor and YouTube ingest session stay up while a transparent fallback covers the handoff.'
      : 'Saved now and applied the next time the channel starts.',
    confirm: { label: `Switch to ${label}`, mode: 'tap', icon: 'check' },
  });
  if (!ok) return false;
  try {
    const result = await api.setVisualization(name, plugin);
    const cfg = config(name);
    cfg.visualization = { ...(cfg.visualization || {}), active: plugin };
    state.channels.set(name, { ...(state.channels.get(name) || { name }), visualization: plugin });
    const queued = queueVizCompletion(name, result, {
      kind: 'switch', plugin, label: `switch to ${label}`,
      success: `${label} applied; visualizer replaced live; stream unchanged`,
    });
    if (!queued) {
      toast('ok', 'visualization', live && !vizEnabled(name)
        ? `${plugin} selected; visualizer remains off`
        : `${plugin} \u2014 will be on air at the next start`);
    }
    emit('detail', name);
    emit('status', name);
    scheduleRefresh(400);
    return true;
  } catch (err) {
    report('visualization', err);
    return false;
  }
}

export async function applyParameters(name, plugin, specs) {
  if (!ensureUnlocked()) return false;
  const d = chanData(name);
  const restartsChild = channelIsRunning(name) && vizEnabled(name)
    && (config(name).visualization || {}).active === plugin.name;
  if (restartsChild) {
    const ok = await confirmSheet({
      title: `Apply ${plugin.display_name || plugin.name} settings?`,
      tone: 'info',
      body: 'This restarts the active visualizer child so the settings take effect.',
      detail: 'The compositor and the YouTube ingest session stay up; a transparent fallback covers the handoff.',
      confirm: { label: 'Apply settings', mode: 'tap', icon: 'check' },
    });
    if (!ok) return false;
  }
  const saved = ((config(name).visualization || {}).parameters || {})[plugin.name] || {};
  const draft = d.params[plugin.name] || {};
  const values = {};
  for (const spec of specs) {
    values[spec.name] = spec.name in draft ? draft[spec.name]
      : spec.name in saved ? saved[spec.name] : spec.default;
  }
  const result = await guard(`tune ${plugin.name}`, () => api.setPluginParameters(name, plugin.name, values));
  if (result === undefined) return false;
  delete d.params[plugin.name];
  const active = (config(name).visualization || {}).active === plugin.name;
  const queued = queueVizCompletion(name, result, {
    kind: 'parameters', plugin: plugin.name, label: `${plugin.display_name || plugin.name} settings`,
    success: 'settings applied; visualizer child restarted, stream unchanged',
  });
  if (!queued) {
    toast('ok', plugin.display_name || plugin.name,
      active && channelIsRunning(name) && vizEnabled(name)
        ? 'settings applied'
        : 'settings saved for the next time this visualization starts');
  }
  await loadChannelDetail(name);
  scheduleRefresh();
  return true;
}

export async function applyColor(name, form) {
  if (!ensureUnlocked()) return false;
  const result = await guard('color', () => api.setColor(name, {
    mode: form.mode,
    manual: { accent: form.accent, tint: form.tint },
    transition_seconds: form.transition,
  }), 'applied');
  if (result === undefined) return false;
  chanData(name).color = null;
  await loadChannelDetail(name);
  return true;
}

export async function applyPreset(name, preset) {
  if (!ensureUnlocked() || !preset) return false;
  const found = state.presets.find((p) => p.name === preset);
  const ok = await confirmSheet({
    title: `Apply preset ${found ? found.display_name || found.name : preset}?`,
    tone: 'info',
    body: (found && found.description) || 'Applies the preset to this channel.',
    detail: 'A preset never restarts the stream. If it changes the visualization, only the visualizer child is replaced.',
    confirm: { label: 'Apply preset', mode: 'tap', icon: 'check' },
  });
  if (!ok) return false;
  const result = await guard('preset', () => api.applyPreset(name, preset), 'applied');
  if (result === undefined) return false;
  await loadChannelDetail(name);
  return true;
}

// --------------------------------------------------------------------------
// Schedule
// --------------------------------------------------------------------------

export async function saveSchedule(name, tz, rules) {
  if (!ensureUnlocked()) return false;
  for (const rule of rules) {
    if (!rule.name || !rule.preset) {
      toast('warn', 'schedule', 'Every rule needs a name and a preset.');
      return false;
    }
    if (!rule.when && !rule.date) {
      toast('warn', 'schedule', `Rule \u201C${rule.name}\u201D needs a window or a date.`);
      return false;
    }
  }
  const ok = await confirmSheet({
    title: 'Save the schedule?',
    tone: 'info',
    body: `${rules.length} rule${rules.length === 1 ? '' : 's'} in ${tz || 'UTC'}. The channel keeps its own settings outside them.`,
    detail: 'Rules take effect when their window opens. Most specific first: date, days + window, window.',
    confirm: { label: 'Save schedule', mode: 'tap', icon: 'check' },
  });
  if (!ok) return false;
  const payload = rules.map((r) => ({
    name: r.name, preset: r.preset, when: r.when || null, days: r.days.length ? r.days : null, date: r.date || null,
  }));
  const result = await guard('save schedule', () => api.patchChannel(name, {
    schedule: { timezone: tz.trim() || 'UTC', rules: payload },
  }), `${rules.length} rules`);
  if (result === undefined) return false;
  chanData(name).schedule = null;
  await loadChannelDetail(name);
  return true;
}

// --------------------------------------------------------------------------
// Uploads
//
// One row per file, kept outside any render so a status refresh cannot take a
// transfer in progress away from the operator.
// --------------------------------------------------------------------------

export const UPLOAD = {
  audio: {
    folder: 'audio', noun: 'audio file', largeBytes: 250 * 1024 * 1024,
    extensions: ['.mp3', '.flac', '.ogg', '.opus', '.m4a', '.aac', '.wav'],
  },
  images: {
    folder: 'images', noun: 'image', largeBytes: 40 * 1024 * 1024,
    extensions: ['.jpg', '.jpeg', '.png', '.webp', '.bmp'],
  },
  soundboard: {
    folder: 'soundboard', noun: 'sound effect', largeBytes: 25 * 1024 * 1024,
    extensions: ['.mp3', '.flac', '.ogg', '.opus', '.m4a', '.aac', '.wav'],
  },
};

const UPLOAD_CONCURRENCY = 2;
export const uploads = { audio: [], images: [], soundboard: [] };
export const uploadSummary = { audio: null, images: null, soundboard: null };
export const uploadDest = { audio: 'channel', images: 'channel', soundboard: 'common' };
let uploadSeq = 0;

export function destPath(kind, destination, channel) {
  const folder = UPLOAD[kind].folder;
  return destination === 'common' ? `common/${folder}/` : `channels/${channel || '\u2026'}/${folder}/`;
}

const isFinished = (entry) => entry.status !== 'queued' && entry.status !== 'uploading';

export function enqueue(kind, channel, files) {
  if (!ensureUnlocked()) return;
  const spec = UPLOAD[kind];
  const destination = uploadDest[kind];
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
      id: (uploadSeq += 1), kind, file, name: file.name, size: file.size, destination, channel,
      target: destPath(kind, destination, channel),
      status: known ? 'queued' : 'skipped',
      progress: 0,
      detail: known ? '' : `not one of ${spec.extensions.join(' ')} \u2014 not sent`,
      abort: null,
      reported: false,
    };
    entry.path = entry.target + entry.name;
    if (known && file.size >= spec.largeBytes) {
      large += 1;
      entry.detail = `${fmtBytes(file.size)} \u2014 large; this will take a while and cannot be resumed.`;
    }
    uploads[kind].push(entry);
  }
  if (large) toast('warn', 'upload', `${large} very large file${large === 1 ? '' : 's'} \u2014 this will take a while.`);
  uploadSummary[kind] = null;
  emit('uploads', kind);
  pump(kind);
}

function pump(kind) {
  const rows = uploads[kind];
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
  emit('uploads', entry.kind);
  try {
    const data = await uploadMedia({
      kind: entry.kind === 'soundboard' ? 'soundboard' : entry.kind,
      destination: entry.destination,
      channel: entry.channel,
      file: entry.file,
      onProgress: (value) => {
        entry.progress = value;
        emit('uploads', entry.kind);
      },
      onOpen: (abort) => { entry.abort = abort; },
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
      if (err instanceof ApiError && err.status === 401) report('upload', err);
    }
  }
  entry.abort = null;
  entry.file = null;
  emit('uploads', entry.kind);
  pump(entry.kind);
}

export function cancelUpload(entry) {
  if (entry.status === 'queued') {
    entry.status = 'canceled';
    entry.detail = 'canceled before it was sent';
    entry.file = null;
    emit('uploads', entry.kind);
    pump(entry.kind);
    return;
  }
  if (entry.status === 'uploading' && entry.abort) entry.abort();
}

export function clearFinishedUploads(kind) {
  uploads[kind] = uploads[kind].filter((entry) => !isFinished(entry));
  uploadSummary[kind] = null;
  emit('uploads', kind);
}

export function hasFinishedUploads(kind) {
  return uploads[kind].some(isFinished);
}

function watchedDirs(kind, channel) {
  const d = chanData(channel);
  const pair = kind === 'audio' ? d.playlist : d.slides;
  const dirs = new Set(pair.watched.map((path) => `${String(path).replace(/\/+$/, '')}/`));
  if (dirs.size) return [...dirs];
  if (!pair.saved.length) dirs.add(destPath(kind, 'channel', channel));
  for (const entry of pair.saved) {
    const star = entry.indexOf('*');
    if (star < 0) continue;
    const cut = entry.lastIndexOf('/', star);
    if (cut > 0) dirs.add(entry.slice(0, cut + 1));
  }
  return [...dirs];
}

function summarize(kind, stored, channel) {
  const targets = [...new Set(stored.map((entry) => entry.target))];
  const noun = stored.length === 1 ? UPLOAD[kind].noun : `${UPLOAD[kind].noun}s`;
  if (kind === 'soundboard') return `${stored.length} ${noun} stored in ${targets.join(', ')}.`;
  const where = `${stored.length} ${noun} stored in ${targets.join(', ')}.`;
  if (!channel || stored.some((entry) => entry.destination === 'channel' && entry.channel !== channel)) return where;
  const dirs = watchedDirs(kind, channel);
  const live = targets.filter((target) => dirs.some((dir) => target === dir || target.startsWith(dir)));
  if (live.length === targets.length) return `${where} That folder is watched, so it is already in this channel's rotation.`;
  if (!live.length) {
    return `${where} This channel picks ${kind === 'audio' ? 'tracks' : 'slides'} explicitly, so add them to the list and save.`;
  }
  return `${where} Some landed in a watched folder and are live; add the rest to the list and save.`;
}

async function finishBatch(kind) {
  const rows = uploads[kind];
  if (rows.some((entry) => entry.status === 'queued' || entry.status === 'uploading')) return;
  const batch = rows.filter((entry) => !entry.reported);
  if (!batch.length) return;
  for (const entry of batch) entry.reported = true;

  const stored = batch.filter((entry) => entry.status === 'done');
  const failed = batch.filter((entry) => entry.status === 'failed');
  const skipped = batch.filter((entry) => entry.status === 'skipped');
  const channel = batch[0].channel || state.current;

  if (stored.length && channel) {
    await loadMedia(channel);
    await loadSoundboard(channel);
  }

  const parts = [];
  if (stored.length) parts.push(summarize(kind, stored, channel));
  if (failed.length) parts.push(`${failed.length} rejected by the control plane \u2014 the reason is on each row.`);
  if (skipped.length) parts.push(`${skipped.length} not sent: the extension is not one this channel can play.`);
  uploadSummary[kind] = parts.length
    ? { text: parts.join(' '), tone: failed.length ? 'bad' : stored.length ? 'ok' : 'warn' }
    : null;

  const tone = failed.length ? (stored.length ? 'warn' : 'bad') : 'ok';
  toast(tone, `upload \u00B7 ${kind}`,
    `${stored.length} stored, ${failed.length} rejected${skipped.length ? `, ${skipped.length} skipped` : ''}`);
  emit('uploads', kind);
}
