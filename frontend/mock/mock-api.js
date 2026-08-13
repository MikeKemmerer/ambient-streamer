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
//  listed in the handover notes; do not treat them as the backend's behavior.
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
    description: 'Stereo vectorscope; the only branch with commandable color.',
    cost: { cores_720p30: 0.25, scale_1080p: 1.9 },
    available: true,
  },
  {
    name: 'showspectrum-waterfall',
    display_name: 'Spectrum Waterfall',
    description: 'Scrolling spectrogram. Installed but in no channel\u2019s hot set.',
    cost: { cores_720p30: 0.31, scale_1080p: 1.9 },
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
  common: ['common/images/forest.jpg', 'common/images/rain window.jpg', 'common/images/harbor.png'],
  channels: {
    lofi: ['channels/lofi/images/city night.jpg', 'channels/lofi/images/desk.jpg', 'channels/lofi/images/train.webp'],
    chant: ['channels/chant/images/icon-01.jpg', 'channels/chant/images/icon-02.jpg'],
    deepspace: ['channels/deepspace/images/nebula.jpg'],
  },
};

// Upload rules. The extension list is the cheap check; the magic-byte probe
// stands in for the real backend actually looking at the file, which is the only
// check that means anything when the name and the Content-Type come from the
// uploading side.
const UPLOAD_KINDS = {
  audio: {
    folder: 'audio',
    extensions: ['.mp3', '.flac', '.ogg', '.opus', '.m4a', '.aac', '.wav'],
    library: MEDIA_AUDIO,
  },
  images: {
    folder: 'images',
    extensions: ['.jpg', '.jpeg', '.png', '.webp', '.bmp'],
    library: MEDIA_IMAGES,
  },
};

const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

// What an unset CHANNEL_ENCODER / CHANNEL_FPS falls back to.
const DEFAULT_RTMP_URL = 'rtmp://a.rtmp.youtube.com/live2';
const DEFAULT_ENCODER = 'libx264';
const DEFAULT_FPS = 30;
const STREAM_KEY_RE = /^[A-Za-z0-9_-]{0,64}$/;
const RTMP_URL_RE = /^rtmps?:\/\/[A-Za-z0-9.-]+(?::\d{1,5})?(?:\/[A-Za-z0-9._~/-]*)?$/;

const SIGNATURES = {
  audio: [[0x49, 0x44, 0x33], [0xff, 0xfb], [0xff, 0xf3], [0xff, 0xf2], [0x66, 0x4c, 0x61, 0x43],
    [0x4f, 0x67, 0x67, 0x53], [0x52, 0x49, 0x46, 0x46]],
  images: [[0x89, 0x50, 0x4e, 0x47], [0xff, 0xd8, 0xff], [0x52, 0x49, 0x46, 0x46], [0x42, 0x4d]],
};

function channel(name, status, config, playlist, images) {
  return {
    name,
    status: { name, ...status },
    config,
    playlist,
    images,
    watched: { audio: [], images: [] },
    // Stands in for the channel .env. null means "inherit the global default".
    delivery: { stream_key: '', rtmp_url: DEFAULT_RTMP_URL, encoder: null, fps: null },
  };
}

const db = new Map([
  ['lofi', channel('lofi', {
    state: 'running',
    uptime_seconds: 84210,
    current_track: 'channels/lofi/audio/lofi-only.mp3',
    next_track: 'common/audio/rain-loop.mp3',
    current_slide: 'common/images/forest.jpg',
    visualization: 'showfreqs-bars',
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
    resolution: '720p',
    audio: { tracks: [], shuffle: false, crossfade_seconds: 5.0 },
    images: { slides: [], order: 'sequential', hold_seconds: 20.0, fade_seconds: 2.0 },
    visualization: { enabled: true, active: 'showfreqs-bars', hot_set: ['showfreqs-bars', 'showwaves-classic'] },
    color: { mode: 'automatic', manual: { accent: '#4FC3F7', tint: '#101820' }, transition_seconds: 2.0 },
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
    visualization: 'showwaves-classic',
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
    resolution: '1080p',
    audio: { tracks: [], shuffle: false, crossfade_seconds: 2.0 },
    images: { slides: [], order: 'sequential', hold_seconds: 45.0, fade_seconds: 3.0 },
    visualization: { enabled: true, active: 'showwaves-classic', hot_set: ['showwaves-classic'] },
    color: { mode: 'manual', manual: { accent: '#D4AF37', tint: '#2A0E0E' }, transition_seconds: 4.0 },
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
    visualization: 'avectorscope-lissajous',
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
    resolution: '720p',
    audio: { tracks: [], shuffle: true, crossfade_seconds: 8.0 },
    images: { slides: [], order: 'shuffle', hold_seconds: 60.0, fade_seconds: 6.0 },
    // starfield-particles is deliberately not in PLUGINS: the one genuine error state.
    visualization: { enabled: true, active: 'avectorscope-lissajous', hot_set: ['avectorscope-lissajous', 'showfreqs-bars', 'starfield-particles'] },
    color: { mode: 'manual', manual: { accent: '#7A5CFF', tint: '#05060B' }, transition_seconds: 6.0 },
    preset: 'deep-space',
    bumpers: { enabled: false, mode: 'tracks', every_tracks: 4, every_minutes: 20, sources: [] },
    schedule: { timezone: 'UTC', rules: [] },
  },
  ['channels/deepspace/audio/drift.mp3'],
  ['channels/deepspace/images/nebula.jpg'])],
]);

// lofi curates its tracks by hand but leaves its slides in folder mode, so both
// halves of "is this upload already live?" are reachable on one channel.
db.get('lofi').watched.images = ['channels/lofi/images'];
db.get('deepspace').watched.audio = ['channels/deepspace/audio'];
db.get('deepspace').watched.images = ['channels/deepspace/images'];

// deepspace is the stopped channel, so it is the one whose delivery is editable:
// it starts with no key at all, and an encoder override to clear back to default.
for (const entry of db.values()) entry.delivery.encoder = entry.status.encoder_requested;
db.get('lofi').delivery.stream_key = 'seeded-key-lofi';
db.get('chant').delivery.stream_key = 'seeded-key-chant';
db.get('chant').delivery.fps = 30;

// Capacity model, following backend/ambient/plugins.py: pipeline + preview + one
// cost per instantiated branch. Off, no branch is instantiated at all, so the
// channel falls to the pipeline floor.
const PIPELINE_CORES_720P30 = 1.02;
const PREVIEW_CORES = 0.2;
const IDLE_BRANCH_CORES_720P30 = 0.28;
const RES_SCALE = { '480p': 0.6, '720p': 1.0, '1080p': 1.9, '1440p': 3.4, '2160p': 7.6 };

function projectedCores(entry) {
  const viz = entry.config.visualization || {};
  const scale = RES_SCALE[entry.config.resolution] || 1.0;
  let cores = PIPELINE_CORES_720P30 * scale + PREVIEW_CORES;
  if (viz.enabled !== false) {
    for (const name of viz.hot_set || []) {
      const plugin = PLUGINS.find((p) => p.name === name);
      cores += (plugin ? plugin.cost.cores_720p30 : IDLE_BRANCH_CORES_720P30) * scale;
    }
  }
  return Number(cores.toFixed(3));
}

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
  // Derived from the config the way the backend derives it, so the fixtures
  // cannot drift out of step with the switch.
  return {
    ...entry.status,
    visualization_enabled: (entry.config.visualization || {}).enabled !== false,
  };
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

// --------------------------------------------------------------------------
// Upload
// --------------------------------------------------------------------------

async function sniff(file, kind) {
  const head = new Uint8Array(await file.slice(0, 12).arrayBuffer());
  // ISO-BMFF (m4a/aac in mp4) carries its brand at offset 4, not 0.
  if (kind === 'audio' && String.fromCharCode(...head.slice(4, 8)) === 'ftyp') return true;
  return SIGNATURES[kind].some((sig) => sig.every((byte, i) => head[i] === byte));
}

async function upload(form) {
  if (!form) return fail(400, 'not_multipart', 'Expected a multipart/form-data body.');

  const kind = String(form.get('kind') || '');
  const spec = UPLOAD_KINDS[kind];
  if (!spec) return fail(400, 'invalid_kind', `kind must be audio or images, not "${kind}".`);

  const destination = String(form.get('destination') || 'channel');
  if (!['common', 'channel'].includes(destination)) {
    return fail(400, 'invalid_destination', `destination must be common or channel, not "${destination}".`);
  }

  const name = form.get('channel') ? String(form.get('channel')) : '';
  if (destination === 'channel') {
    if (!name) return fail(400, 'missing_channel', 'destination=channel needs a channel.');
    if (!db.has(name)) return fail(404, 'unknown_channel', `Channel ${name} does not exist.`);
  }

  const files = form.getAll('files').filter((value) => value instanceof File);
  if (!files.length) return fail(400, 'no_files', 'No file parts in the request.');

  const dir = destination === 'common' ? `common/${spec.folder}` : `channels/${name}/${spec.folder}`;
  const noun = kind === 'audio' ? 'audio' : 'an image';
  const results = [];
  for (const file of files) {
    const base = file.name.split(/[\\/]/).pop();
    const stored = `${dir}/${base}`;
    if (!spec.extensions.some((ext) => base.toLowerCase().endsWith(ext))) {
      results.push({ name: file.name, ok: false, error: 'unsupported_extension', detail: `${base} is not ${noun} the pipeline can open.` });
    } else if (file.size > MAX_UPLOAD_BYTES) {
      results.push({ name: file.name, ok: false, error: 'too_large', detail: `${file.size} bytes exceeds the 25 MB limit.` });
    } else if (!(await sniff(file, kind))) {
      results.push({
        name: file.name,
        ok: false,
        error: 'content_probe_failed',
        detail: `${base} is named like ${noun} but its contents are not \u2014 the name and the Content-Type are not evidence.`,
      });
    } else {
      const list = destination === 'common' ? spec.library.common : spec.library.channels[name];
      if (!list.includes(stored)) list.push(stored);
      results.push({ name: file.name, ok: true, path: stored, bytes: file.size });
    }
  }
  return json({ destination, channel: destination === 'channel' ? name : null, results });
}

async function route(url, init) {
  const path = url.pathname;
  const method = (init.method || 'GET').toUpperCase();
  const form = init.body instanceof FormData ? init.body : null;
  const body = init.body && !form ? JSON.parse(init.body) : null;

  if (path === '/api/health') return json({ status: 'ok', version: 'mock' });

  const headers = new Headers(init.headers || {});
  if (!/^Bearer .+/.test(headers.get('Authorization') || '')) {
    return fail(401, 'unauthorized', 'Missing or malformed Authorization header.');
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
    const projected = [...db.values()].reduce(
      (sum, e) => sum + (e.status.state === 'stopped' ? 0 : projectedCores(e)), 0);
    return json({
      cores: 12,
      reserved_cores: 1.0,
      available_cores: 11.0,
      projected_cores: Number(projected.toFixed(3)),
      measured_cores: Number(measured.toFixed(3)),
      headroom_cores: Number((11.0 - projected).toFixed(3)),
      channels: [...db.values()].map((e) => ({
        channel: e.name,
        projected_cores: projectedCores(e),
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
    const text = logLines(name, service, url.searchParams.get('lines'));
    const file = `/var/log/ambient/${name}/${service}.log`;

    // The backend has not confirmed its final shape, so all three are reachable
    // from the console: window.__mockLogFormat = 'text' | 'json' | 'lines'.
    if (window.__mockLogFormat === 'lines') {
      return json({ channel: name, service, lines: text.split('\n'), path: file });
    }
    if (window.__mockLogFormat === 'json') {
      return json({ channel: name, service, text, path: file });
    }
    return new Response(text, {
      status: 200,
      headers: { 'Content-Type': 'text/plain; charset=utf-8', 'X-Log-Path': file },
    });
  }

  if (path === '/api/plugins') return json({ plugins: PLUGINS });
  if (path === '/api/presets') return json({ presets: PRESETS });
  if (path === '/api/media/audio' && method === 'GET') return json(MEDIA_AUDIO);
  if (path === '/api/media/images' && method === 'GET') return json(MEDIA_IMAGES);

  // This mock puts uploads on one route only, so the client's route probe has
  // something real to be turned away from rather than a listing it might read as
  // a successful upload.
  if (method === 'POST' && /^\/api\/media\/(audio|images)(\/upload)?$/.test(path)) {
    return fail(405, 'method_not_allowed', `${path} does not accept POST.`);
  }
  if (path === '/api/media/upload' && method === 'POST') return upload(form);

  if (path === '/api/channels' && method === 'GET') {
    return json({
      channels: [...db.values()].map(summary),
      // A channel whose config will not parse is reported here, not in `channels`.
      errors: [{ channel: 'broken-yaml', detail: "visualization.active 'nope' is not in hot_set ['showfreqs-bars']" }],
      // Directories under channels/ that are not channels at all.
      ignored: [
        { name: 'example', reason: 'template directory shipped with the repo' },
        { name: '.ipynb_checkpoints', reason: 'not a channel directory' },
      ],
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
      visualization: body.visualization ? body.visualization.active : 'showfreqs-bars',
      encoder: body.encoder || 'libx264', encoder_requested: body.encoder || 'libx264',
      fps: null, speed: null, bitrate_kbps: null, cpu_cores: null,
      liquidsoap_buffer: null, rtmp: 'disconnected', hls: 'stopped', health: null,
    }, {
      version: 1, name, genre: body.genre || '',
      resolution: body.resolution || '720p',
      audio: { tracks: [], shuffle: false, crossfade_seconds: 5.0 },
      images: { slides: [], order: 'sequential', hold_seconds: 20.0, fade_seconds: 2.0 },
      visualization: body.visualization || { enabled: true, active: 'showfreqs-bars', hot_set: ['showfreqs-bars'] },
      color: { mode: 'automatic', manual: { accent: '#4FC3F7', tint: '#101820' }, transition_seconds: 2.0 },
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
    return json({
      ...summary(entry),
      ...extra,
      warnings: entry.warnings || [],
      config: entry.config,
      resolution: entry.config.resolution,
      projected_cores: projectedCores(entry),
      // What was asked for, not what is measured: a stopped channel reports 0 fps.
      fps_requested: entry.delivery.fps || DEFAULT_FPS,
      rtmp_url: entry.delivery.rtmp_url,
      // Presence only. The key is a credential and is never echoed back.
      has_stream_key: Boolean(entry.delivery.stream_key),
      hls_url: `http://${location.hostname}:8888/${name}/preview/index.m3u8`,
    });
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
    if (method === 'GET') return json({ tracks: entry.playlist, watched: entry.watched.audio });
    entry.playlist = Array.isArray(body && body.tracks) ? body.tracks : [];
    if (!entry.playlist.length) return fail(400, 'empty_selection', 'A channel with no audio cannot stream.');
    return json({ tracks: entry.playlist }, 202);
  }

  if (tail === 'images') {
    if (method === 'GET') return json({ slides: entry.images, watched: entry.watched.images });
    entry.images = Array.isArray(body && body.slides) ? body.slides : [];
    return json({ slides: entry.images }, 202);
  }

  if (tail === 'visualization') {
    const active = body && body.active;
    const installed = PLUGINS.some((p) => p.name === active);
    if (!installed) {
      return fail(404, 'unknown_plugin', `No plugin named ${active} is installed.`);
    }
    entry.config.visualization.active = active;
    entry.status.visualization = active;

    // Outside hot_set the graph has no branch to cut to, so the channel is
    // restarted make-before-break rather than switched.
    if (!entry.config.visualization.hot_set.includes(active)) {
      entry.config.visualization.hot_set.push(active);
      entry.status.state = 'starting';
      statusEvent(name);
      setTimeout(() => {
        entry.status.state = 'running';
        entry.status.health = 'healthy';
        entry.status.uptime_seconds = 1;
        statusEvent(name);
        emit('channel.visualization', { channel: name, visualization: active });
      }, 2500);
      return json({ visualization: active, restart: 'make-before-break' }, 202);
    }

    emit('channel.visualization', { channel: name, visualization: active });
    return json({ visualization: active }, 202);
  }

  if (tail === 'resolution') {
    const value = body && body.resolution;
    if (!['480p', '720p', '1080p', '1440p', '2160p'].includes(value)) {
      return fail(400, 'invalid_resolution', `${value} is not a supported resolution.`);
    }
    entry.config.resolution = value;
    if (entry.status.state !== 'stopped') {
      entry.status.state = 'starting';
      statusEvent(name);
      setTimeout(() => {
        entry.status.state = 'running';
        entry.status.health = 'healthy';
        entry.status.uptime_seconds = 1;
        statusEvent(name);
      }, 2500);
    }
    return json({ resolution: value, restart: 'make-before-break' }, 202);
  }

  // Refused while the channel runs rather than restarting it: these settings
  // decide where the stream goes.
  if (tail === 'delivery' && method === 'PUT') {
    if (entry.status.state !== 'stopped') {
      return fail(409, 'channel_running', `stop ${name} before changing where it publishes`);
    }
    const b = body || {};
    const changed = [];

    if (b.stream_key !== undefined && b.stream_key !== null) {
      const key = String(b.stream_key).trim();
      if (!STREAM_KEY_RE.test(key)) {
        return fail(400, 'invalid_stream_key', 'a stream key is letters, digits, - and _');
      }
      entry.delivery.stream_key = key;
      changed.push('stream_key');
    }

    if (b.rtmp_url !== undefined && b.rtmp_url !== null) {
      const url = String(b.rtmp_url).trim();
      if (!RTMP_URL_RE.test(url)) {
        return fail(400, 'invalid_rtmp_url', `'${url}' is not an rtmp:// or rtmps:// URL`);
      }
      entry.delivery.rtmp_url = url;
      changed.push('rtmp_url');
    }

    if (b.clear_encoder) {
      entry.delivery.encoder = null;
      changed.push('encoder');
    } else if (b.encoder !== undefined && b.encoder !== null) {
      if (!['libx264', 'h264_nvenc', 'h264_qsv'].includes(b.encoder)) {
        return fail(400, 'invalid_encoder', `${b.encoder} is not a supported encoder.`);
      }
      entry.delivery.encoder = b.encoder;
      changed.push('encoder');
    }

    if (b.clear_fps) {
      entry.delivery.fps = null;
      changed.push('fps');
    } else if (b.fps !== undefined && b.fps !== null) {
      if (!Number.isInteger(b.fps) || b.fps < 1 || b.fps > 60) {
        return fail(400, 'invalid_fps', 'fps must be a whole number between 1 and 60.');
      }
      entry.delivery.fps = b.fps;
      changed.push('fps');
    }

    if (!changed.length) {
      return json({ accepted: false, channel: name, changed: [], detail: 'nothing to change' });
    }
    entry.status.encoder_requested = entry.delivery.encoder || DEFAULT_ENCODER;
    entry.status.encoder = entry.status.encoder_requested;
    emit('channel.status', { channel: name, state: entry.status.state, changed });
    return json({
      accepted: true,
      channel: name,
      changed,
      // Never the key itself, only whether one is present.
      has_stream_key: Boolean(entry.delivery.stream_key),
      rtmp_url: entry.delivery.rtmp_url,
      encoder: entry.delivery.encoder || DEFAULT_ENCODER,
      fps: entry.delivery.fps || DEFAULT_FPS,
      detail: 'applies on the next start',
    });
  }

  if (tail === 'color') {
    entry.config.color = { ...entry.config.color, ...(body || {}) };
    return json({ color: entry.config.color }, 202);
  }

  if (tail === 'preset') {
    const preset = body && body.preset;
    const known = PRESETS.find((p) => p.name === preset);
    if (!known) return fail(404, 'unknown_preset', `No preset named ${preset}.`);
    if (preset === 'neon-spectrum' && !entry.config.visualization.hot_set.includes('avectorscope-lissajous')) {
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

// --------------------------------------------------------------------------
// XHR shim
//
// Uploads go out over XMLHttpRequest, because fetch() cannot say how much of a
// request body has been sent and per-file progress is the point. Anything that
// is not /api/ — hls.js fetching preview segments — is left on the real
// transport untouched.
// --------------------------------------------------------------------------

const RealXHR = window.XMLHttpRequest;

function settle(xhr, status, text, type) {
  for (const [key, value] of Object.entries({ status, statusText: '', responseText: text, response: text, readyState: 4 })) {
    Object.defineProperty(xhr, key, { value, configurable: true });
  }
  xhr.dispatchEvent(new ProgressEvent(type));
}

async function transfer(xhr, body) {
  const pending = xhr.__mock;
  let total = 0;
  if (body instanceof FormData) {
    for (const [, value] of body.entries()) total += value instanceof File ? value.size : String(value).length;
  }

  // ~8 MB/s, so a large file is genuinely slow to watch and the panel has to
  // survive whatever else happens while it is in flight.
  const duration = Math.min(8000, Math.max(240, (total / (8 * 1024 * 1024)) * 1000));
  const steps = 16;
  for (let i = 1; i <= steps; i += 1) {
    await new Promise((r) => setTimeout(r, duration / steps));
    if (pending.aborted) return;
    xhr.upload.dispatchEvent(new ProgressEvent('progress', {
      lengthComputable: true,
      loaded: Math.round((total * i) / steps),
      total,
    }));
  }

  const response = await route(pending.url, { method: pending.method, headers: pending.headers, body });
  const text = await response.text();
  if (pending.aborted) return;
  settle(xhr, response.status, text, 'load');
}

class MockXHR extends RealXHR {
  open(method, url, ...rest) {
    const target = new URL(url, location.origin);
    this.__mock = target.pathname.startsWith('/api/')
      ? { url: target, method: (method || 'GET').toUpperCase(), headers: new Headers(), aborted: false }
      : null;
    if (!this.__mock) super.open(method, url, ...rest);
  }

  setRequestHeader(name, value) {
    if (!this.__mock) super.setRequestHeader(name, value);
    else this.__mock.headers.set(name, value);
  }

  send(body) {
    if (!this.__mock) super.send(body);
    else transfer(this, body);
  }

  abort() {
    if (!this.__mock) {
      super.abort();
      return;
    }
    this.__mock.aborted = true;
    settle(this, 0, '', 'abort');
  }
}

window.XMLHttpRequest = MockXHR;

console.info('[mock] /api/* is served from frontend/mock/mock-api.js — no control plane is being contacted');
