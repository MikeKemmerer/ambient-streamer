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
  "visualization": "showfreqs-bars",
  "visualization_enabled": true,
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

## Plugins, presets, color

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/plugins` | installed plugins + manifests |
| PUT | `/api/channels/{name}/visualization` | switch active plugin (must be in `hot_set`) |
| GET | `/api/presets` | available presets |
| POST | `/api/channels/{name}/preset` | apply a preset |
| PUT | `/api/channels/{name}/color` | set mode and manual colors |

Switching to a plugin **in** `hot_set` returns `200` and switches in one frame.

Switching to an installed plugin **outside** `hot_set` returns `202`: the plugin
is staged into the running configuration and the channel performs a
make-before-break restart, which costs a real gap of seconds (see on-disk.md). An
installed plugin is always *usable* — it simply cannot switch instantly,
because an FFmpeg filtergraph is fixed at launch and a switchable branch has to
already be rendering. Callers that will not accept a restart pass
`?allow_restart=false` and get `409 restart_required` instead.

A plugin that is not installed at all returns `404 unknown_plugin`.

## Standby: taking the visualization off air without a restart

`PUT /api/channels/{name}/visualization/visible` with `{"visible": false}`.

This is the one visualization on/off that is live. It sends `overlay@viz enable 0`
over zmq, riding the composite's timeline switch — measured landing in one frame
on a running channel, with the composer's container start time unchanged either
side, so nothing was replaced.

It is refused with `409 visualization_not_built` when `visualization.enabled` is
false: that graph has no composite to bypass. The two settings answer different
questions, and only one of them saves anything:

| | Branches render? | Cost | Change costs |
|---|---|---|---|
| `enabled: false` | no | the floor | a restart |
| `enabled: true, visible: false` | yes | same as on | one frame |
| `enabled: true, visible: true` | yes | same as on | — |

## Tuning a plugin

`PUT /api/channels/{name}/visualization/parameters` with
`{"plugin": "showfreqs-bars", "values": {"detail": 2048}}`.

Names and ranges are declared per plugin and reported by `GET /api/plugins`.
Values outside a declared range are **clamped, not refused**, and the clamped
value is what gets stored — because FFmpeg accepts an out-of-range filter option,
ignores it, and renders the branch wrong at exit 0, so refusing would be the only
way an operator learned about it and clamping is the safer failure. An
*undeclared* name is refused, since it can only be a mistake.

Substituted into the fragment at launch, so this restarts a channel that is
actually drawing that plugin; tuning one nobody is looking at is just saved.

## Turning the visualization off

`PATCH /api/channels/{name}` with `{"visualization": {"enabled": false}}`.

Not a live change — the filtergraph is fixed at launch, so it applies on the
next start. `active` and `hot_set` are deliberately left alone, so switching it
back on restores the same look.

Off is the largest lever a channel has, because it removes the plugin branches,
the selector and the alpha composite together. Measured on a live 1080p30
channel on a 7-core host already running three channels:

| | CPU | encode speed |
|---|---|---|
| visualization on | 0.97 cores, very erratic (sd 0.87) | **0.415x** — could not hold realtime |
| visualization off | 0.77 cores, steady (sd 0.02) | **0.999x** |

The two CPU figures look close only because the on case is starved rather than
busy: it is doing 41 % of the work. Per second of finished stream it costs about
2.3 cores against 0.77. `projected_cores` falls to the pipeline floor.

Every status body — the `GET /api/channels` summary as well as the detail —
carries both `visualization` and `visualization_enabled`. `visualization` names
the **selected** plugin whether or not it is being drawn, so it must never be
rendered on its own: a channel drawing nothing would still appear to have a
visualization. `visualization_enabled` is the field that answers "is anything
being rendered", and it is on the summary precisely so the channel list can
answer that without fetching each channel's config.

## Media upload

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/media/upload` | upload audio or images |

`multipart/form-data`. Fields: `files` (repeated), `destination`
(`common` \| `channel`), `channel` (only when `destination=channel`), and
`kind` — both `audio`/`images` and the singular `image` are accepted.

Returns one entry per file so a partial batch reports precisely what failed:
`200` all stored, `207` mixed, `400` none.

```json
{"results": [
  {"name": "rain.mp3", "ok": true,  "path": "common/audio/rain.mp3", "error": null},
  {"name": "bad.mp3",  "ok": false, "error": "unsupported_content", "detail": "..."}
]}
```

Error tokens: `invalid_filename`, `unsupported_extension`, `unsupported_content`,
`file_too_large`, `already_exists`, `no_space`.

A colliding name is **rejected**, never overwritten — a running channel may be
mid-read on that file. `?on_conflict=rename` opts into de-duplication and
reports the real stored path.

Uploads are validated by **probing the actual bytes**, not the extension or
`Content-Type`; both are attacker-controlled. The write is atomic (same-directory
temp then `rename()`) so a directory-watched folder never sees a partial file.

| Method | Path | Purpose |
|---|---|---|
| PUT | `/api/channels/{name}/resolution` | change output resolution |

Returns `202`. Resolution changes the filtergraph, so this is **not** a live
change — the channel performs a make-before-break restart.

## Ingest settings

| Method | Path | Purpose |
|---|---|---|
| PUT | `/api/channels/{name}/delivery` | stream key, RTMP URL, encoder, fps |

Every field is optional; only what is sent changes, so editing the fps can never
overwrite the stream key. `clear_encoder` and `clear_fps` fall back to the global
default, which is distinct from omitting the field.

```json
{"stream_key": "abcd-1234", "rtmp_url": "rtmp://a.rtmp.youtube.com/live2",
 "encoder": "libx264", "fps": 30, "clear_encoder": false, "clear_fps": false}
```

**Refused with `409 channel_running` while the channel is up**, rather than
restarting it. These settings decide *where* the stream goes; changing them
under a live broadcast would move it mid-flight. They apply on the next start.

Also `400 invalid_stream_key` and `400 invalid_rtmp_url`. The URL is handed to
the publisher as an argument, so anything that is not a plain `rtmp://` or
`rtmps://` URL is refused rather than escaped.

The stream key is **write-only**. It is never returned by any endpoint, never
logged, and never placed in argv. `GET /api/channels/{name}` reports only
`has_stream_key`, alongside `rtmp_url`, `encoder_requested` and `fps_requested`.

`fps_requested` is what was configured; the `fps` field is what is measured and
reads `0` on a stopped channel.

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
| `channel.visualization` | plugin switched |
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
