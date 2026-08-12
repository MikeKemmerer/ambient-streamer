// ===========================================================================
//  MOCK API — NOT PART OF THE RUNNING SYSTEM.
//
//  Loaded only when the page is opened with ?mock, from app.js. It installs a
//  window.fetch shim in front of /api/* so the panel can be rendered and driven
//  without a control plane. Nothing outside this directory imports it, and the
//  real code path never touches it.
//
//  It is written to docs/contracts/rest-api.md, so where the contract is silent
//  about a body shape this file encodes a *guess*, not a fact. Those guesses are
//  listed in the handover notes; do not treat them as the backend's behaviour.
// ===========================================================================

const realFetch = window.fetch.bind(window);
const LATENCY = 90;

const now = () => new Date().toISOString();

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

const PLUGINS = [
  {
    name: 'showfreqs-bars',
    display_name: 'Spectrum Bars',
    description: 'Frequency spectrum drawn as vertical bars.',
    cost: { cores_720p30: 0.24, scale_1080p: 1.9 },
    available: true,
  },
  {
    name: 'showwaves-classic',
    display_name: 'Classic Waveform',
    description: 'Scrolling waveform across the frame.',
    cost: { cores_720p30: 0.21, scale_1080p: 1.9 },
    available: true,
  },
  {
    name: 'avectorscope-lissajous',
    display_name: 'Lissajous Vectorscope',
    description: 'Stereo vectorscope; the only branch with commandable colour.',
    cost: { cores_720p30: 0.25, scale_1080p: 1.9 },
    available: true,
  },
];

const PRESETS = [
  { name: 'calm-ocean', display_name: 'Calm Ocean', description: 'Cool blues, slow fades, gentle spectrum.' },
  { name: 'warm-sunset', display_name: 'Warm Sunset', description: 'Amber wash, long crossfades.' },
  { name: 'deep-space', display_name: 'Deep Space', description: 'Near-black tint with a cold accent.' },
  { name: 'orthodox-chant', display_name: 'Orthodox Chant', description: 'Gold on deep red, very slow slides.' },
  { name: 'minimalist-line-art', display_name: 'Minimalist Line Art', description: 'Monochrome, thin strokes.' },
  { name: 'neon-spectrum', display_name: 'Neon Spectrum', description: 'High-saturation magenta and cyan.' },
];

const MEDIA_AUDIO = {
  common: [
    'common/audio/station-open.mp3',
    'common/audio/rain-loop.mp3',
    'common/audio/night-drive.mp3',
    'common/audio/slow tape 02.flac',
  ],
  channels: {
    lofi: ['channels/lofi/audio/lofi-only.mp3', 'channels/lofi/audio/dust.mp3', 'channels/lofi/audio/cassette.m4a'],
    chant: ['channels/chant/audio/kekragarion.flac', 'channels/chant/audio/anixantos.flac'],
    deepspace: ['channels/deepspace/audio/drift.mp3'],
  },
};

const MEDIA_IMAGES = {
  common: ['common/images/forest.jpg', 'common/images/rain window.jpg', 'common/images/harbour.png'],
  channels: {
    lofi: ['channels/lofi/images/city night.jpg', 'channels/lofi/images/desk.jpg', 'channels/lofi/images/train.webp'],
    chant: ['channels/chant/images/icon-01.jpg', 'channels/chant/images/icon-02.jpg'],
    deepspace: ['channels/deepspace/images/nebula.jpg'],
  },
};

function channel(name, status, config, playlist, images) {
  return { name, status: { name, ...status }, config, playlist, images };
}

const db = new Map([
  ['lofi', channel('lofi', {
    state: 'running',
    uptime_seconds: 84210,
    current_track: 'channels/lofi/audio/lofi-only.mp3',
    next_track: 'common/audio/rain-loop.mp3',
    current_slide: 'common/images/forest.jpg',
    visualisation: 'showfreqs-bars',
    encoder: 'libx264',
    encoder_requested: 'libx264',
    fps: 30.0,
    speed: 0.998,
    bitrate_kbps: 3010,
    cpu_cores: 1.51,
    liquidsoap_buffer: 'ok',
    rtmp: 'connected',
    hls: 'ok',
    health: 'healthy',
  }, {
    version: 1,
    name: 'lofi',
    genre: 'lo-fi hip hop',
    audio: { tracks: [], shuffle: false, crossfade_seconds: 5.0 },
    images: { slides: [], order: 'sequential', hold_seconds: 20.0, fade_seconds: 2.0 },
    visualisation: { active: 'showfreqs-bars', hot_set: ['showfreqs-bars', 'showwaves-classic'] },
    colour: { mode: 'automatic', manual: { accent: '#4FC3F7', tint: '#101820' }, transition_seconds: 2.0 },
    preset: null,
    bumpers: { enabled: false, mode: 'tracks', every_tracks: 4, every_minutes: 20, sources: [] },
    schedule: {
      timezone: 'America/Los_Angeles',
      rules: [
        { name: 'morning', when: '06:00-11:00', days: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri'], preset: 'warm-sunset' },
        { name: 'evening', when: '18:00-23:00', preset: 'calm-ocean' },
      ],
    },
  },
  ['common/audio/station-open.mp3', 'channels/lofi/audio/lofi-only.mp3', 'common/audio/rain-loop.mp3', 'channels/lofi/audio/dust.mp3'],
  ['common/images/forest.jpg', 'channels/lofi/images/city night.jpg', 'channels/lofi/images/desk.jpg'])],

  ['chant', channel('chant', {
    state: 'degraded',
    uptime_seconds: 4021,
    current_track: 'channels/chant/audio/kekragarion.flac',
    next_track: 'channels/chant/audio/anixantos.flac',
    current_slide: 'channels/chant/images/icon-01.jpg',
    visualisation: 'showwaves-classic',
    encoder: 'libx264',
    encoder_requested: 'h264_nvenc',
    fps: 27.4,
    speed: 0.94,
    bitrate_kbps: 2640,
    cpu_cores: 1.94,
    liquidsoap_buffer: 'starving',
    rtmp: 'connected',
    hls: 'ok',
    health: 'starving',
  }, {
    version: 1,
    name: 'chant',
    genre: 'byzantine chant',
    audio: { tracks: [], shuffle: false, crossfade_seconds: 2.0 },
    images: { slides: [], order: 'sequential', hold_seconds: 45.0, fade_seconds: 3.0 },
    visualisation: { active: 'showwaves-classic', hot_set: ['showwaves-classic'] },
    colour: { mode: 'manual', manual: { accent: '#D4AF37', tint: '#2A0E0E' }, transition_seconds: 4.0 },
    preset: 'orthodox-chant',
    bumpers: { enabled: false, mode: 'tracks', every_tracks: 4, every_minutes: 20, sources: [] },
    schedule: { timezone: 'America/Los_Angeles', rules: [] },
  },
  ['channels/chant/audio/kekragarion.flac', 'channels/chant/audio/anixantos.flac'],
  ['channels/chant/images/icon-01.jpg', 'channels/chant/images/icon-02.jpg'])],

  ['deepspace', channel('deepspace', {
    state: 'stopped',
    uptime_seconds: 0,
    current_track: null,
    next_track: null,
    current_slide: null,
    visualisation: 'avectorscope-lissajous',
    encoder: 'h264_nvenc',
    encoder_requested: 'h264_nvenc',
    fps: null,
    speed: null,
    bitrate_kbps: null,
    cpu_cores: null,
    liquidsoap_buffer: null,
    rtmp: 'disconnected',
    hls: 'stopped',
    health: null,
  }, {
    version: 1,
    name: 'deepspace',
    genre: 'dark ambient',
    audio: { tracks: [], shuffle: true, crossfade_seconds: 8.0 },
    images: { slides: [], order: 'shuffle', hold_seconds: 60.0, fade_seconds: 6.0 },
    visualisation: { active: 'avectorscope-lissajous', hot_set: ['avectorscope-lissajous', 'showfreqs-bars'] },
    colour: { mode: 'manual', manual: { accent: '#7A5CFF', tint: '#05060B' }, transition_seconds: 6.0 },
    preset: 'deep-space',
    bumpers: { enabled: false, mode: 'tracks', every_tracks: 4, every_minutes: 20, sources: [] },
    schedule: { timezone: 'UTC', rules: [] },
  },
  ['channels/deepspace/audio/drift.mp3'],
  ['channels/deepspace/images/nebula.jpg'])],
]);

// --------------------------------------------------------------------------
// Event bus
// --------------------------------------------------------------------------

const subscribers = new Set();

function emit(event, data) {
  const frame = `event: ${event}\ndata: ${JSON.stringify({ channel: null, at: now(), ...data })}\n\n`;
  for (const sub of subscribers) sub.push(frame);
}

// Test hook: drop every open event stream, the way a backend restart would.
window.__mockDropStream = () => {
  const count = subscribers.size;
  for (const sub of [...subscribers]) {
    subscribers.delete(sub);
    sub.close();
  }
  return count;
};

function statusEvent(name) {
  const s = db.get(name).status;
  emit('channel.status', { channel: name, state: s.state, health: s.health, speed: s.speed });
}

function jitter(base, spread) {
  return base + (Math.random() - 0.5) * spread;
}

let ticks = 0;

setInterval(() => {
  ticks += 1;
  for (const [name, entry] of db) {
    const s = entry.status;
    if (s.state !== 'running' && s.state !== 'degraded') continue;
    s.uptime_seconds += 1;
    s.speed = Number(jitter(s.state === 'degraded' ? 0.94 : 0.999, 0.012).toFixed(3));
    s.fps = Number(jitter(s.state === 'degraded' ? 27.4 : 30.0, 0.5).toFixed(1));
    s.bitrate_kbps = Math.round(jitter(s.state === 'degraded' ? 2640 : 3010, 120));
    s.cpu_cores = Number(jitter(s.state === 'degraded' ? 1.94 : 1.51, 0.12).toFixed(2));
    emit('channel.progress', {
      channel: name,
      speed: s.speed,
      fps: s.fps,
      bitrate_kbps: s.bitrate_kbps,
      cpu_cores: s.cpu_cores,
      uptime_seconds: s.uptime_seconds,
    });
  }

  if (ticks % 11 === 0) {
    const entry = db.get('lofi');
    const list = entry.playlist;
    const index = list.indexOf(entry.status.current_track);
    entry.status.current_track = list[(index + 1) % list.length];
    entry.status.next_track = list[(index + 2) % list.length];
    emit('channel.track', {
      channel: 'lofi',
      current_track: entry.status.current_track,
      next_track: entry.status.next_track,
    });
  }

  if (ticks % 7 === 0) {
    const entry = db.get('lofi');
    const list = entry.images;
    const index = list.indexOf(entry.status.current_slide);
    entry.status.current_slide = list[(index + 1) % list.length];
    emit('channel.slide', { channel: 'lofi', current_slide: entry.status.current_slide });
  }

  if (ticks % 37 === 0) {
    emit('watchdog.event', {
      channel: 'chant',
      fault: 'liquidsoap buffer starving for 12s',
      action: 'restarted liquidsoap behind the icecast fallback mount',
      recovered: true,
    });
  }

  if (ticks % 53 === 0) {
    emit('capacity.warning', {
      channel: null,
      detail: 'projected cost 9.6 of 11.0 usable cores; one more 720p channel would oversubscribe the host',
    });
  }

  if (ticks % 29 === 0) {
    const steps = [0.15, 0.4, 0.7, 1];
    steps.forEach((progress, i) => {
      setTimeout(() => {
        emit('job.progress', {
          channel: 'lofi',
          id: `bumper-${Math.floor(ticks / 29)}`,
          kind: 'bumper synthesis',
          status: progress === 1 ? 'done' : 'running',
          progress,
        });
      }, i * 700);
    });
  }
}, 1000);

// --------------------------------------------------------------------------
// Router
// --------------------------------------------------------------------------

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const fail = (status, error, detail) => json({ error, detail }, status);

function eventStream(signal) {
  let sub;
  const body = new ReadableStream({
    start(controller) {
      const encoder = new TextEncoder();
      sub = {
        push(text) {
          try {
            controller.enqueue(encoder.encode(text));
          } catch {
            subscribers.delete(sub);
          }
        },
        close() {
          try {
            controller.close();
          } catch {
            /* already closed */
          }
        },
      };
      subscribers.add(sub);
      sub.push(': mock stream open\n\n');
      if (signal) {
        signal.addEventListener('abort', () => {
          subscribers.delete(sub);
          sub.close();
        });
      }
    },
    cancel() {
      subscribers.delete(sub);
    },
  });
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
}

function summary(entry) {
  return { ...entry.status };
}

function logLines(name, service, lines) {
  const out = [];
  for (let i = Number(lines) || 200; i > 0; i -= 1) {
    const stamp = new Date(Date.now() - i * 1000).toISOString();
    out.push(service === 'compositor'
      ? `${stamp} ${name}-compositor  frame=${100000 - i * 30} fps=30 q=23.0 size=${900000 - i * 380}kB bitrate=3010.4kbits/s speed=0.999x`
      : `${stamp} ${name}-${service}  [source:3] Prepared "/media/channel/audio/track-${i % 7}.mp3" (RID 4${i}).`);
  }
  return out.join('\n');
}

async function route(url, init) {
  const path = url.pathname;
  const method = (init.method || 'GET').toUpperCase();
  const body = init.body ? JSON.parse(init.body) : null;

  if (path === '/api/health') return json({ status: 'ok', version: 'mock' });

  const headers = new Headers(init.headers || {});
  if (!/^Bearer .+/.test(headers.get('Authorization') || '')) {
    return fail(401, 'unauthorised', 'Missing or malformed Authorization header.');
  }

  if (path === '/api/events') return eventStream(init.signal);

  if (path === '/api/system') {
    return json({
      cores: 12,
      memory_bytes: 33500000000,
      reserved_cores: 1.0,
      max_channels: 8,
      encoders: [
        { encoder: 'h264_nvenc', available: true, detail: 'probe encode ok (RTX 3060)' },
        { encoder: 'h264_qsv', available: false, detail: '/dev/dri absent — not available under Docker Desktop/WSL2' },
        { encoder: 'libx264', available: true, detail: 'probe encode ok' },
      ],
      fallback_encoder: 'libx264',
      bind_address: '127.0.0.1',
      authenticated: true,
      paths: { root: '/srv/ambient', common: '/srv/ambient/common', channels: '/srv/ambient/channels' },
      warnings: [],
    });
  }

  if (path === '/api/capacity') {
    const measured = [...db.values()].reduce((sum, e) => sum + (e.status.cpu_cores || 0), 0);
    const projected = [...db.values()].reduce((sum, e) => sum + (e.status.state === 'stopped' ? 0 : 1.5), 0);
    return json({
      cores: 12,
      reserved_cores: 1.0,
      available_cores: 11.0,
      projected_cores: Number(projected.toFixed(3)),
      measured_cores: Number(measured.toFixed(3)),
      headroom_cores: Number((11.0 - projected).toFixed(3)),
      channels: [...db.values()].map((e) => ({
        channel: e.name,
        projected_cores: 1.5,
        measured_cores: e.status.cpu_cores || 0,
        state: e.status.state,
      })),
    });
  }

  if (path === '/api/logs') {
    const name = url.searchParams.get('channel');
    if (!name) return fail(400, 'missing_channel', 'channel is a required query parameter.');
    const service = url.searchParams.get('service') || 'compositor';
    if (!['compositor', 'liquidsoap', 'producer', 'watchdog'].includes(service)) {
      return fail(400, 'invalid_log_service', `unknown service ${service}`);
    }
    return json({
      channel: name,
      service,
      lines: logLines(name, service, url.searchParams.get('lines')).split('\n'),
      path: `/var/log/ambient/${name}/${service}.log`,
    });
  }

  if (path === '/api/plugins') return json({ plugins: PLUGINS });
  if (path === '/api/presets') return json({ presets: PRESETS });
  if (path === '/api/media/audio') return json(MEDIA_AUDIO);
  if (path === '/api/media/images') return json(MEDIA_IMAGES);

  if (path === '/api/channels' && method === 'GET') {
    return json({
      channels: [...db.values()].map(summary),
      // A channel whose config will not parse is reported here, not in `channels`.
      errors: [{ channel: 'broken-yaml', detail: "visualisation.active 'nope' is not in hot_set ['showfreqs-bars']" }],
    });
  }

  if (path === '/api/channels' && method === 'POST') {
    const name = body && body.name;
    if (!name || !/^[a-z0-9]([a-z0-9_-]{0,30}[a-z0-9])?$/.test(name)) {
      return fail(400, 'invalid_name', 'Channel name must be lowercase letters, digits, dash or underscore.');
    }
    if (db.has(name)) return fail(409, 'exists', `Channel ${name} already exists.`);
    db.set(name, channel(name, {
      state: 'stopped', uptime_seconds: 0, current_track: null, next_track: null, current_slide: null,
      visualisation: body.visualisation ? body.visualisation.active : 'showfreqs-bars',
      encoder: body.encoder || 'libx264', encoder_requested: body.encoder || 'libx264',
      fps: null, speed: null, bitrate_kbps: null, cpu_cores: null,
      liquidsoap_buffer: null, rtmp: 'disconnected', hls: 'stopped', health: null,
    }, {
      version: 1, name, genre: body.genre || '',
      audio: { tracks: [], shuffle: false, crossfade_seconds: 5.0 },
      images: { slides: [], order: 'sequential', hold_seconds: 20.0, fade_seconds: 2.0 },
      visualisation: body.visualisation || { active: 'showfreqs-bars', hot_set: ['showfreqs-bars'] },
      colour: { mode: 'automatic', manual: { accent: '#4FC3F7', tint: '#101820' }, transition_seconds: 2.0 },
      preset: null,
      bumpers: { enabled: false, mode: 'tracks', every_tracks: 4, every_minutes: 20, sources: [] },
      schedule: { timezone: 'UTC', rules: [] },
    }, [], []));
    statusEvent(name);
    return json({ name, created: true }, 201);
  }

  const match = path.match(/^\/api\/channels\/([^/]+)(?:\/(.+))?$/);
  if (!match) return fail(404, 'not_found', `No mock route for ${path}`);

  const name = decodeURIComponent(match[1]);
  const tail = match[2] || '';
  const entry = db.get(name);
  if (!entry) return fail(404, 'unknown_channel', `Channel ${name} does not exist.`);

  if (!tail && method === 'GET') {
    const extra = name === 'chant'
      ? { fault: 'liquidsoap_starving', fault_detail: 'icecast fallback mount served 12s of the last minute' }
      : { fault: null, fault_detail: '' };
    return json({ ...entry.status, ...extra, warnings: entry.warnings || [], config: entry.config });
  }

  if (!tail && method === 'PATCH') {
    for (const [key, value] of Object.entries(body || {})) {
      entry.config[key] = value && typeof value === 'object' && !Array.isArray(value)
        ? { ...entry.config[key], ...value }
        : value;
    }
    return json({ ...entry.status, config: entry.config }, 202);
  }

  if (!tail && method === 'DELETE') {
    if (entry.status.state !== 'stopped') {
      return fail(409, 'not_stopped', 'A channel must be stopped before it can be deleted.');
    }
    db.delete(name);
    return json({ deleted: name });
  }

  if (tail === 'start') {
    if (entry.status.state === 'running') return fail(409, 'already_running', `${name} is already running.`);
    entry.status.state = 'starting';
    entry.status.health = null;
    statusEvent(name);
    setTimeout(() => {
      Object.assign(entry.status, {
        state: 'running', health: 'healthy', uptime_seconds: 1, fps: 30, speed: 0.999,
        bitrate_kbps: 3010, cpu_cores: 1.5, liquidsoap_buffer: 'ok', rtmp: 'connected', hls: 'ok',
        current_track: entry.playlist[0] || null, next_track: entry.playlist[1] || null,
        current_slide: entry.images[0] || null,
      });
      statusEvent(name);
    }, 2500);
    return json({ accepted: true }, 202);
  }

  if (tail === 'stop') {
    Object.assign(entry.status, {
      state: 'stopped', health: null, uptime_seconds: 0, fps: null, speed: null, bitrate_kbps: null,
      cpu_cores: null, liquidsoap_buffer: null, rtmp: 'disconnected', hls: 'stopped',
      current_track: null, next_track: null, current_slide: null,
    });
    statusEvent(name);
    return json({ accepted: true }, 202);
  }

  if (tail === 'restart') {
    entry.status.state = 'starting';
    statusEvent(name);
    setTimeout(() => {
      entry.status.state = 'running';
      entry.status.health = 'healthy';
      entry.status.uptime_seconds = 1;
      statusEvent(name);
    }, 2500);
    return json({ accepted: true }, 202);
  }

  if (tail === 'playlist') {
    if (method === 'GET') return json({ tracks: entry.playlist });
    entry.playlist = Array.isArray(body && body.tracks) ? body.tracks : [];
    if (!entry.playlist.length) return fail(400, 'empty_selection', 'A channel with no audio cannot stream.');
    return json({ tracks: entry.playlist }, 202);
  }

  if (tail === 'images') {
    if (method === 'GET') return json({ slides: entry.images });
    entry.images = Array.isArray(body && body.slides) ? body.slides : [];
    return json({ slides: entry.images }, 202);
  }

  if (tail === 'visualisation') {
    const active = body && body.active;
    if (!entry.config.visualisation.hot_set.includes(active)) {
      return fail(409, 'not_in_hot_set',
        `${active} is not instantiated in this channel's filtergraph; promoting it needs a compositor restart.`);
    }
    entry.config.visualisation.active = active;
    entry.status.visualisation = active;
    emit('channel.visualisation', { channel: name, visualisation: active });
    return json({ visualisation: active }, 202);
  }

  if (tail === 'colour') {
    entry.config.colour = { ...entry.config.colour, ...(body || {}) };
    return json({ colour: entry.config.colour }, 202);
  }

  if (tail === 'preset') {
    const preset = body && body.preset;
    const known = PRESETS.find((p) => p.name === preset);
    if (!known) return fail(404, 'unknown_preset', `No preset named ${preset}.`);
    if (preset === 'neon-spectrum' && !entry.config.visualisation.hot_set.includes('avectorscope-lissajous')) {
      return fail(409, 'not_in_hot_set',
        'neon-spectrum selects avectorscope-lissajous, which is not in this channel\u2019s hot set.');
    }
    entry.config.preset = preset;
    return json({ preset }, 202);
  }

  return fail(404, 'not_found', `No mock route for ${method} ${path}`);
}

window.fetch = async (input, init = {}) => {
  const raw = typeof input === 'string' ? input : input.url;
  const url = new URL(raw, location.origin);
  if (typeof input !== 'string' || !url.pathname.startsWith('/api/')) return realFetch(input, init);
  const response = await route(url, init);
  if (url.pathname !== '/api/events') await new Promise((r) => setTimeout(r, LATENCY));
  return response;
};

console.info('[mock] /api/* is served from frontend/mock/mock-api.js — no control plane is being contacted');
