# Contract — REST API and SSE

Owner: lead. Consumers: backend-api, frontend.

Base: `http://127.0.0.1:8090`. Bound to loopback by default.

## Authentication

Every endpoint except `GET /api/health` requires:

```
Authorization: Bearer <AMBIENT_API_TOKEN>
```

**This is not optional.** The backend mounts the Docker socket, which is
root-equivalent on the host — anyone who reaches this API can start containers.
The token is generated at install time and the backend refuses to start if it
is unset while bound to anything other than loopback.

## Conventions

- JSON in, JSON out.
- `200` success, `202` accepted for anything asynchronous, `400` validation,
  `401` auth, `404` unknown channel, `409` conflicting state, `503` degraded.
- Errors: `{"error": "...", "detail": "..."}` — `error` is a stable machine
  token, `detail` is for humans.
- A request that would change what is on air returns `202` and emits SSE
  events. It never blocks on the media pipeline.

## Channels

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/channels` | list with summary status |
| POST | `/api/channels` | create |
| GET | `/api/channels/{name}` | full config + status |
| PATCH | `/api/channels/{name}` | update config |
| DELETE | `/api/channels/{name}` | delete (must be stopped) |
| POST | `/api/channels/{name}/start` | start |
| POST | `/api/channels/{name}/stop` | stop |
| POST | `/api/channels/{name}/restart` | make-before-break restart |

`GET /api/channels/{name}` returns the health-dashboard fields:

```json
{
  "name": "lofi",
  "state": "running",
  "uptime_seconds": 84210,
  "current_track": "rain-loop.mp3",
  "next_track": "lofi-only.mp3",
  "current_slide": "forest.jpg",
  "visualisation": "showfreqs-bars",
  "encoder": "libx264",
  "encoder_requested": "h264_nvenc",
  "fps": 30.0,
  "speed": 1.0,
  "bitrate_kbps": 3000,
  "cpu_cores": 1.09,
  "liquidsoap_buffer": "ok",
  "rtmp": "connected",
  "hls": "ok",
  "health": "healthy"
}
```

`state` ∈ `stopped` `starting` `running` `degraded` `failed`.
`health` ∈ `healthy` `starving` `stalled` `disconnected`.

`encoder_requested` is reported separately from `encoder` because a channel
whose configured encoder fails its probe starts on the fallback rather than
failing. The operator has to be able to see that happened.

## Media

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/media/audio` | available audio, `common` + per channel |
| GET | `/api/media/images` | available images, with profile summaries |
| GET | `/api/channels/{name}/playlist` | ordered selection |
| PUT | `/api/channels/{name}/playlist` | replace ordered selection |
| GET | `/api/channels/{name}/images` | ordered slide selection |
| PUT | `/api/channels/{name}/images` | replace ordered slide selection |

`PUT` replaces the whole ordered list rather than patching it. Reordering is
the common operation and a positional patch API for a drag-and-drop UI invites
lost-update races between two open browsers.

Every path is validated against the two-tree rule in
[media-selection.md](media-selection.md) before it is written.

## Plugins, presets, colour

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/plugins` | installed plugins + manifests |
| PUT | `/api/channels/{name}/visualisation` | switch active plugin (must be in `hot_set`) |
| GET | `/api/presets` | available presets |
| POST | `/api/channels/{name}/preset` | apply a preset |
| PUT | `/api/channels/{name}/colour` | set mode and manual colours |

Switching to a plugin outside `hot_set` returns `409` with
`error: "not_in_hot_set"`. It is not silently promoted, because that would
require a restart the caller did not ask for.

## Bumpers

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/channels/{name}/bumpers` | text + generated files |
| PUT | `/api/channels/{name}/bumpers` | update text and interval |
| POST | `/api/channels/{name}/bumpers/generate` | `202`, synthesise |
| GET | `/api/channels/{name}/bumpers/{id}/preview` | audio for browser playback |

## System

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | unauthenticated liveness |
| GET | `/api/system` | host cores, memory, encoders, capacity |
| GET | `/api/capacity` | projected cost vs available |
| GET | `/api/logs?channel=&service=&lines=` | tail |
| GET | `/api/events` | **SSE stream** |

`GET /api/system` reports probe results, not the encoder list — an encoder
present in `ffmpeg -encoders` whose device is missing is reported unavailable.

## SSE

`GET /api/events`, `text/event-stream`. In-memory queue per subscriber with a
lock; no broker.

```
event: channel.status
data: {"channel":"lofi","state":"running","health":"healthy","speed":1.0}
```

| Event | When |
|---|---|
| `channel.status` | state or health changes |
| `channel.progress` | periodic; speed, fps, bitrate, uptime |
| `channel.track` | track change |
| `channel.slide` | slide change |
| `channel.visualisation` | plugin switched |
| `watchdog.event` | fault detected or recovery performed |
| `capacity.warning` | projected cost approaching the limit |
| `job.progress` | long-running job, e.g. bumper generation |

Rules:

- Every event carries `channel` (or `null` for system-wide) and an ISO-8601
  `at`.
- `channel.progress` is throttled to at most 1 Hz per channel. It is derived
  from FFmpeg `-progress`, which emits far faster than any UI needs.
- A slow consumer is dropped, never buffered without bound. One stuck browser
  tab must not grow the backend's memory.
- Events are advisory. A reconnecting client re-reads state via
  `GET /api/channels` rather than replaying missed events; there is no event
  log and clients must not assume one.
