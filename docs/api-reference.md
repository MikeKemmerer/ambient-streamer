# API reference

Operator-facing reference for the control plane's REST endpoints and SSE stream, with worked
examples.

[`contracts/rest-api.md`](contracts/rest-api.md) is the **normative** specification. Where this
document adds detail it is describing what the implementation does; where the two ever
disagree, the contract wins and the difference is a defect.

Base URL: `http://127.0.0.1:8090`.

There is **no OpenAPI document and no Swagger UI** — `docs_url`, `redoc_url` and `openapi_url`
are all disabled, because the app holds the Docker socket and an interactive schema explorer on
that surface is not worth the convenience.

---

## Authentication

Every endpoint except `GET /api/health` requires:

```
Authorization: Bearer <AMBIENT_API_TOKEN>
```

**This is not optional.** The backend drives Docker and is therefore root-equivalent on the
host — anyone who reaches this API can start containers. The comparison is constant-time, and a
missing token is rejected rather than treated as "open".

The process **refuses to start** if `AMBIENT_API_TOKEN` is unset while bound to anything other
than `127.0.0.1`, `::1` or `localhost`. Bound to loopback with no token it starts and logs a
warning; every authenticated endpoint then returns `401`, because an empty expected token can
never match.

Generate one with `openssl rand -hex 32`.

---

## Conventions

- JSON in, JSON out.
- **A request that changes what is on air returns `202` and emits SSE.** It never blocks on the
  media pipeline, and a failure after acceptance surfaces as a `channel.status` event with
  `state: "failed"`, not as a status code.
- Errors are `{"error": "...", "detail": "..."}`. `error` is a stable machine token; `detail` is
  for humans and may change.

| Status | Meaning |
|--------|---------|
| `200` | success |
| `201` | created |
| `202` | accepted; work continues asynchronously |
| `400` | validation — bad name, bad path, bad config, empty patch |
| `401` | missing or wrong bearer token |
| `404` | unknown channel, unknown bumper, unmatched path |
| `409` | conflicting state — running, already exists, not in `hot_set`, out of capacity |
| `503` | a `docker` invocation failed, or the app is still starting |

Common `error` tokens: `unauthorized`, `unknown_channel`, `invalid_channel_name`,
`invalid_channel_config`, `invalid_media_path`, `invalid_preset`, `channel_exists`,
`channel_running`, `channel_busy`, `not_in_hot_set`, `insufficient_capacity`,
`channel_limit_reached`, `mount_in_use`, `supervisor_failed`.

---

## Endpoint index

| Method | Path | Status |
|--------|------|--------|
| GET | `/api/health` | contract |
| GET | `/api/system` | contract |
| GET | `/api/capacity` | contract |
| GET | `/api/logs` | contract |
| GET | `/api/events` | contract |
| GET | `/api/metrics` | **extension** — Prometheus text |
| GET | `/api/channels` | contract |
| POST | `/api/channels` | contract |
| GET | `/api/channels/{name}` | contract |
| PATCH | `/api/channels/{name}` | contract |
| DELETE | `/api/channels/{name}` | contract |
| POST | `/api/channels/{name}/start` | contract |
| POST | `/api/channels/{name}/stop` | contract |
| POST | `/api/channels/{name}/restart` | contract |
| GET | `/api/channels/{name}/preview` | **extension** — resolves the HLS URL |
| GET | `/api/media/audio` | contract |
| GET | `/api/media/images` | contract |
| POST | `/api/media/profiles` | **extension** — colour extraction |
| GET / PUT | `/api/channels/{name}/playlist` | contract |
| GET / PUT | `/api/channels/{name}/images` | contract |
| GET | `/api/plugins` | contract |
| PUT | `/api/channels/{name}/visualisation` | contract |
| GET | `/api/presets` | contract |
| POST | `/api/channels/{name}/preset` | contract |
| PUT | `/api/channels/{name}/colour` | contract |
| GET / PUT | `/api/channels/{name}/bumpers` | contract |
| POST | `/api/channels/{name}/bumpers/generate` | contract |
| GET | `/api/channels/{name}/bumpers/{id}/preview` | contract |

Everything in the contract is implemented. The three extensions are additive and do not change
any contract shape.

---

## System

### `GET /api/health`

The only unauthenticated endpoint. Deliberately reports nothing an anonymous caller should not
see.

```console
$ curl -s http://127.0.0.1:8090/api/health
{"status":"ok","uptime_seconds":84210.4,"channels":2,"watchdog":true}
```

Use it for a container healthcheck or an uptime monitor.

### `GET /api/system`

Host facts and **probe results**, not the encoder list. An encoder present in
`ffmpeg -encoders` whose device is missing is reported unavailable, because the list is not
evidence — on both hosts tested it advertised `h264_qsv` with no Intel device present.

```json
{
  "cores": 7.0,
  "memory_bytes": 33395765248,
  "reserved_cores": 1.0,
  "max_channels": 8,
  "encoders": [
    { "encoder": "h264_nvenc", "available": false, "detail": "Cannot load libcuda.so.1" },
    { "encoder": "h264_qsv",   "available": false, "detail": "Error creating a QSV device" },
    { "encoder": "libx264",    "available": true,  "detail": "" }
  ],
  "fallback_encoder": "libx264",
  "bind_address": "127.0.0.1",
  "authenticated": true,
  "paths": {
    "root": "/srv/ambient",
    "common": "/srv/ambient/common",
    "channels": "/srv/ambient/channels",
    "logs": "/var/log/ambient"
  },
  "warnings": []
}
```

Each probe is a short real encode (15 frames of black at 320×240), cached per process. The
first call after startup is therefore slower than later ones.

### `GET /api/capacity`

Projected versus measured CPU, per channel and in total.

```json
{
  "cores": 7.0,
  "reserved_cores": 1.0,
  "available_cores": 6.0,
  "projected_cores": 0.48,
  "measured_cores": 1.53,
  "headroom_cores": 5.52,
  "channels": [
    { "channel": "lofi", "projected_cores": 0.24, "measured_cores": 1.49, "state": "running" }
  ]
}
```

**`projected_cores` counts only the visualisation branches.** It is the sum of each hot
plugin's declared `cost.cores_720p30`, scaled for geometry and frame rate. It does **not**
include the encoder, the preview encode, MP3 decode or JPEG decode — which is why a channel
projecting 0.24 measures around 1.5. Use `measured_cores` for capacity decisions and read
[scaling.md](scaling.md).

`measured_cores` comes from `docker stats --no-stream`, sampled every 30 s.

### `GET /api/logs`

```
GET /api/logs?channel=lofi&service=compositor&lines=200
```

| Parameter | Default | Values |
|-----------|---------|--------|
| `channel` | required | a channel name |
| `service` | `compositor` | `compositor` `liquidsoap` `producer` `watchdog` |
| `lines` | `200` | 1–2000 |

```json
{
  "channel": "lofi",
  "service": "compositor",
  "lines": ["...", "..."],
  "path": "/var/log/ambient/lofi/compositor.log"
}
```

Reads the host log file, which is empty until the container processes write to it. `docker logs`
is the more reliable source today — see [operations.md](operations.md).

### `GET /api/metrics`

Prometheus text exposition, hand-written rather than via a client library. **Requires the
bearer token**, which most Prometheus scrape configs can supply via `authorization`.

| Metric | Type | Labels |
|--------|------|--------|
| `ambient_channel_up` | gauge | `channel` |
| `ambient_channel_healthy` | gauge | `channel` |
| `ambient_channel_state` | gauge | `channel`, `state` |
| `ambient_channel_speed` | gauge | `channel` |
| `ambient_channel_fps` | gauge | `channel` |
| `ambient_channel_out_time_seconds` | gauge | `channel` |
| `ambient_channel_cpu_cores` | gauge | `container` |
| `ambient_watchdog_restarts_total` | counter | `channel` |
| `ambient_sse_subscribers` | gauge | — |
| `ambient_sse_dropped_subscribers_total` | counter | — |

`ambient_channel_speed` and `ambient_channel_out_time_seconds` are the two worth alerting on.
See [operations.md](operations.md#alerting).

---

## Channels

### `GET /api/channels`

Summary for every channel, plus a per-channel error list so one broken `config.yaml` does not
blank the whole page.

```json
{
  "channels": [
    {
      "name": "lofi",
      "state": "running",
      "uptime_seconds": 84210.0,
      "current_track": null,
      "next_track": null,
      "current_slide": null,
      "visualisation": "showfreqs-bars",
      "encoder": "libx264",
      "encoder_requested": "libx264",
      "fps": 30.0,
      "speed": 0.996,
      "bitrate_kbps": 3085,
      "cpu_cores": 1.49,
      "liquidsoap_buffer": "ok",
      "rtmp": "disconnected",
      "hls": "down",
      "health": "healthy"
    }
  ],
  "errors": []
}
```

The summary form skips the relay round-trip, so `rtmp` and `hls` are placeholders here. Fetch
the channel individually for the real values.

### `GET /api/channels/{name}`

The full health dashboard: every summary field plus relay state, warnings, the fault detail,
the resolved config and the projected cost.

```json
{
  "name": "lofi",
  "state": "running",
  "health": "healthy",
  "speed": 0.996,
  "bitrate_kbps": 3085,
  "rtmp": "connected",
  "hls": "ok",
  "warnings": [],
  "fault": null,
  "fault_detail": "",
  "config": { "version": 1, "name": "lofi", "...": "..." },
  "resolution": "720p",
  "projected_cores": 0.24
}
```

| Field | Domain |
|-------|--------|
| `state` | `stopped` `starting` `running` `degraded` `failed` |
| `health` | `healthy` `starving` `stalled` `disconnected` |
| `rtmp` | `connected` `disconnected` `unknown` |
| `hls` | `ok` `down` `unknown` |
| `liquidsoap_buffer` | `ok` `down` |

`encoder_requested` is reported separately from `encoder`: a channel whose configured encoder
fails its probe starts on the fallback rather than failing, and the operator has to be able to
see that happened.

`rtmp` and `hls` read `unknown` when the MediaMTX control API is unreachable. After a failed
probe the backend stops asking for 30 s — a name that does not resolve can block far longer
than the socket timeout.

> `current_track`, `next_track` and `current_slide` are read from
> `/run/ambient/<channel>/now.json`, which **nothing currently writes.** They are always `null`
> on a running channel. `visualisation` and `encoder` fall back to the configured values.

### `POST /api/channels`

```json
{
  "name": "lofi",
  "genre": "ambient",
  "resolution": "720p",
  "fps": 30,
  "encoder": "libx264",
  "mount": "/lofi",
  "fallback_mount": "/lofi-fallback",
  "cpu_limit": 2.0,
  "memory_limit": "2g",
  "rtmp_url": "rtmp://a.rtmp.youtube.com/live2",
  "stream_key": "xxxx-xxxx-xxxx-xxxx-xxxx"
}
```

Only `name` is required. `201` on success:

```json
{
  "name": "lofi",
  "directory": "/srv/ambient/channels/lofi",
  "state": "stopped",
  "next": "add audio to channels/lofi/audio, then POST /api/channels/lofi/start"
}
```

`stream_key` is **write-only**: it is written to `channels/<name>/.env` with mode `0600` and is
never echoed back, never returned by any endpoint, and never logged.

Creates `audio/`, `images/`, `bumpers/` and `profiles/`, plus `config.yaml` and `.env`.

| Failure | Status / error |
|---------|----------------|
| name does not match `^[a-z0-9](?:[a-z0-9_-]{0,30}[a-z0-9])?$` | `400 invalid_channel_name` |
| directory exists | `409 channel_exists` |
| `limits.max_channels` reached | `409 channel_limit_reached` |
| mount already used by another channel | `409 mount_in_use` |
| `mount` equals `fallback_mount`, or is not a valid Icecast mount | `400 invalid_mount` |

> **This does not finish the job.** It does not append the channel to `channels/mounts.list`,
> does not generate the Icecast fallback MP3, and does not SIGHUP Icecast. Do those three by
> hand — see
> [quickstart.md §7](quickstart.md#7-register-the-channels-icecast-mount-and-fallback) — or the
> channel's Liquidsoap will be refused by Icecast.

### `PATCH /api/channels/{name}`

Only the live-editable surface. Anything needing a container recreated lives in `.env` and is
not patchable.

Accepted keys: `genre`, `audio`, `images`, `visualisation`, `colour`, `preset`, `bumpers`,
`schedule`. Dict-valued keys are shallow-merged into the existing value; everything else is
replaced.

```json
{ "images": { "hold_seconds": 30.0 }, "genre": "deep ambient" }
```

```json
{ "name": "lofi", "changed": ["images", "genre"], "warnings": [] }
```

Writes `config.yaml` atomically, then recompiles `playlist.m3u`, `images.list` and
`docker-compose.yml`. An empty body is `400 empty_patch`; changing `name` is
`400 immutable_field`.

### `DELETE /api/channels/{name}`

Refuses while **any** container for the channel still exists, running or exited:
`409 channel_running`. Stop it first. Removes the whole channel directory, including media.

### `POST /api/channels/{name}/start`

`202`. Recompiles first, then checks capacity, then brings the Compose project up.

```json
{ "accepted": true, "channel": "lofi", "action": "start" }
```

`409 insufficient_capacity` when the channel's `projected_cores` on top of currently measured
usage would exceed `cores − limits.reserved_cores`. Because `projected_cores` counts only
visualisation branches, this guard is generous — it will not save you from oversubscribing.

### `POST /api/channels/{name}/stop`

`202`. Brings down the replacement slot first, then the canonical project, so a stop issued
mid-restart leaves nothing behind.

### `POST /api/channels/{name}/restart`

`202`, **make-before-break**.

```json
{ "accepted": true, "channel": "lofi", "action": "restart", "mode": "make-before-break" }
```

The replacement composer starts in a second Compose project under a renamed container, claims
the relay path via MediaMTX `overridePublisher`, and only then is the outgoing one removed.
Liquidsoap is left alone — audio lives behind Icecast and must not be disturbed by a
compositor swap.

Handover waits for the replacement's `out_time` to advance, bounded at 60 s. If the replacement
never publishes, the outgoing composer is removed anyway and the response carries
`took_over: false` in the supervisor's result — two composers cost two composers.

Measured: kill-then-restart costs 5.14 s on the YouTube leg; make-before-break costs 1.03 s.
Neither is zero.

### `GET /api/channels/{name}/preview`

```json
{ "channel": "lofi", "hls": "http://mediamtx:8888/lofi/preview/index.m3u8" }
```

That is the **internal** relay address from `ambient.yaml`'s `relay.hls`. MediaMTX publishes no
host port, so a browser cannot open it. The operator URL in
[`contracts/on-disk.md`](contracts/on-disk.md) is
`http://<host>:8090/<channel>/preview/index.m3u8`, proxied by the backend — **that proxy is not
implemented.** See [quickstart.md §9](quickstart.md#9-verify) for how to reach the preview
today.

---

## Media

### `GET /api/media/audio`, `GET /api/media/images`

The library, grouped by tree. Paths are relative to the repository root.

```json
{
  "common": [
    { "path": "common/audio/rain-loop.mp3", "name": "rain-loop.mp3", "bytes": 8421120 }
  ],
  "channels": {
    "lofi": [
      { "path": "channels/lofi/audio/dusk.mp3", "name": "dusk.mp3", "bytes": 6103040 }
    ]
  }
}
```

`GET /api/media/images` adds a `profile` summary per image, or `null`:

```json
{
  "path": "common/images/forest.jpg",
  "name": "forest.jpg",
  "bytes": 2048576,
  "profile": {
    "dominant": "#2E4A3B", "accent": "#8FD6A8",
    "brightness": 0.34, "warmth": -0.42, "mood": "calm",
    "extractor": "pillow-kmeans-v1"
  }
}
```

### `POST /api/media/profiles`

Extract missing colour profiles. See [color-profiles.md](color-profiles.md#extracting-profiles).

### `GET`/`PUT /api/channels/{name}/playlist` and `/images`

`GET` returns both the selection **as written** and what it resolved to:

```json
{
  "channel": "lofi",
  "tracks": ["common/audio/intro.mp3", "channels/lofi/audio/**"],
  "shuffle": false,
  "crossfade_seconds": 5.0,
  "resolved": ["common/audio/intro.mp3", "channels/lofi/audio/dusk.mp3"],
  "container_paths": ["/media/common/audio/intro.mp3", "/media/channel/audio/dusk.mp3"],
  "watched": ["channels/lofi/audio"],
  "warnings": []
}
```

`watched` is the set of directories a change will be picked up from with no restart. An entry
appears there only for a glob or an empty selection.

`PUT` **replaces the whole ordered list**, it does not patch it:

```json
{ "tracks": ["common/audio/intro.mp3", "channels/lofi/audio/**"], "shuffle": false }
```

Replacement rather than positional patching is deliberate: reordering is the common operation,
and a positional patch API for a drag-and-drop UI invites lost-update races between two open
browsers.

Every path is normalised, prefix-checked against the two allowed trees, symlink-resolved, and
prefix-checked **again** before it is written. These paths arrive over HTTP; a `../` traversal
would otherwise mount arbitrary host files into a broadcast. A rejected path is
`400 invalid_media_path`. More than 5000 entries is `400 selection_too_large`.

On success the channel is recompiled. Liquidsoap picks up the new `playlist.m3u` on its own
schedule and the producer rescans `images.list` between slides — **no restart, no gap**.

`PUT .../images` additionally accepts `order` (`sequential` | `shuffle`), `hold_seconds` and
`fade_seconds`; `PUT .../playlist` accepts `shuffle` and `crossfade_seconds`. The image timing
values are persisted but do not currently reach the producer — see
[quickstart.md §11](quickstart.md#11-known-gaps-that-will-surprise-you).

---

## Plugins, presets, colour

### `GET /api/plugins`

```json
{
  "plugins": [
    {
      "name": "showfreqs-bars",
      "display_name": "Spectrum Bars",
      "description": "Frequency spectrum drawn as vertical bars, log-scaled on both axes.",
      "version": "1.0.0",
      "author": "ambient-streamer",
      "commandable": [],
      "cost": { "cores_720p30": 0.24, "scale_1080p": 1.9 },
      "requires_filters": ["showfreqs"],
      "output_size": null
    }
  ]
}
```

Five plugins ship: `showfreqs-bars`, `showwaves-classic`, `minimal-line`, `neon-spectrum` and
`avectorscope-lissajous`. Only `avectorscope-lissajous` declares any `commandable` parameters.

`output_size` is `null` for every shipped plugin, meaning "derive it from the channel" — see
[plugin-development.md](plugin-development.md#validation).

### `PUT /api/channels/{name}/visualisation`

```json
{ "active": "showwaves-line" }
```

```json
{ "channel": "lofi", "active": "showwaves-line", "hot_set": ["showfreqs-bars", "showwaves-line"] }
```

Persists `config.yaml`, then sends `streamselect@sel map <index>` — measured frame-exact, one
clean cut, no dropped frames.

A name outside `hot_set` is `409 not_in_hot_set`. It is **not** silently promoted, because
promoting it means a new filtergraph, which means a restart the caller did not ask for.

Delivery is best-effort: on a stopped channel the config change persists and the command is
logged as undelivered rather than failing the request.

### `GET /api/presets`

Presets are `presets/<name>.yaml`. Six ship: `calm-ocean`, `deep-space`, `warm-sunset`,
`neon-spectrum`, `minimalist-line-art` and `orthodox-chant`. A malformed preset file is skipped
with a log warning rather than breaking the list.

A preset has no field for anything that would need a restart — not `hot_set`, not resolution,
fps or encoder, not media selection. What it can reach is exactly what the escape hatches
reach: `visualisation.active`, colour, slideshow timing, crossfade, and constant `eq`/`hue`
effects.

> A preset whose `visualisation.active` names a plugin outside the channel's `hot_set` is
> rejected with `409`. Since `hot_set` does not currently reach the compositor, a preset that
> switches plugin changes `config.yaml` and sends a `streamselect` command that has no branch
> to select.

### `POST /api/channels/{name}/preset`

```json
{ "preset": "evening" }
```

```json
{ "accepted": true, "channel": "lofi", "preset": "evening", "changed": ["visualisation.active", "colour"], "commands": 4 }
```

`409 not_in_hot_set` if the preset names a plugin this channel did not instantiate.
`400 invalid_preset` if the preset is unknown or malformed.

### `PUT /api/channels/{name}/colour`

```json
{ "mode": "manual", "manual": { "accent": "#8FD6A8", "tint": "#101820" }, "transition_seconds": 3.0 }
```

```json
{
  "channel": "lofi",
  "colour": { "mode": "manual", "manual": { "accent": "#8FD6A8", "tint": "#101820" }, "transition_seconds": 3.0 },
  "commands": [
    "hue@hue h -22.7+(...)*min(max((t-412.4)/3,0),1)",
    "eq@eq saturation 1+(...)*min(max((t-412.4)/3,0),1)",
    "eq@eq brightness 0+(...)*min(max((t-412.4)/3,0),1)"
  ]
}
```

Commands are emitted only in `manual` mode. Each is a single self-animating expression, not a
command stream — the ZMQ path ceilings at about 31.5 commands/s.

See [color-profiles.md](color-profiles.md#the-two-appliers) for why a manual colour on a channel
with extracted profiles only holds until the next slide.

---

## Bumpers

Station IDs: text in `channels/<name>/bumpers.yaml`, generated audio in
`channels/<name>/bumpers/<id>.mp3`.

| Method | Path | Notes |
|--------|------|-------|
| GET | `/api/channels/{name}/bumpers` | insertion settings, source text, generated files |
| PUT | `/api/channels/{name}/bumpers` | update settings and/or text; recompiles, no restart |
| POST | `/api/channels/{name}/bumpers/generate` | `202` |
| GET | `/api/channels/{name}/bumpers/{id}/preview` | `audio/mpeg` for browser playback |

Bumper ids match `^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$`. The preview endpoint resolves the
path and verifies the parent directory afterwards, so `{id}` cannot escape the bumpers folder.

Bumpers are audio, and audio lives behind Icecast — changing them never restarts the
compositor.

> **`POST .../generate` synthesises nothing.** It validates that `bumpers.yaml` lists at least
> one bumper, emits a `job.progress` event with `status: "queued"`, and returns. The one-shot
> TTS container it describes does not exist in this repository. `400 no_bumper_text` if the
> file lists none.

---

## Server-Sent Events

```
GET /api/events
Accept: text/event-stream
Authorization: Bearer <token>
```

```
: connected

event: channel.status
data: {"channel":"lofi","at":"2026-08-12T09:14:02Z","state":"running","health":"healthy","speed":0.996}

: keepalive
```

Every event carries `channel` (or `null` for system-wide) and an ISO-8601 UTC `at`. A `: keepalive`
comment is sent every 15 s so proxies hold the connection open. Responses set
`X-Accel-Buffering: no`, because nginx buffers `text/event-stream` by default and delays every
event.

| Event | When | Emitted today |
|-------|------|---------------|
| `channel.status` | state or health changes; also on create, start, stop, restart, patch | yes |
| `channel.progress` | periodic — `speed`, `fps`, `bitrate_kbps`, `uptime_seconds` | yes |
| `channel.visualisation` | plugin switched | yes |
| `watchdog.event` | fault detected or recovery performed | yes |
| `job.progress` | long-running job, e.g. bumper generation | yes |
| `channel.track` | track change | **no — nothing publishes it** |
| `channel.slide` | slide change | **no — nothing publishes it** |
| `capacity.warning` | projected cost approaching the limit | **no — nothing publishes it** |

The three unemitted names are reserved in the hub's frozen event set, so publishing them later
is additive. A client should tolerate their absence, which means never treating SSE as the only
source of a value.

### Rules a client must follow

- **Events are advisory.** A reconnecting client re-reads state from `GET /api/channels`. There
  is no event log, no replay, no `Last-Event-ID`, and clients must not assume one.
- **`channel.progress` is throttled to at most 1 Hz per channel.** FFmpeg `-progress` emits far
  faster than any UI needs.
- **A slow consumer is dropped, never buffered without bound.** Each subscriber has a 64-frame
  queue; overflowing it closes that connection and increments
  `ambient_sse_dropped_subscribers_total`. One stuck browser tab must not grow the backend's
  memory.

### `watchdog.event` payloads

```json
{ "channel": "lofi", "at": "...", "event": "restarting", "fault": "out_time_stalled",
  "detail": "out_time held at 402.30s for 16.0s of wallclock", "attempt": 1, "backoff_seconds": 5.0 }
```

`event` is one of `restarting`, `restarted`, `restart_failed`. `fault` is one of
`composer_exit`, `no_progress`, `out_time_stalled`, `speed_below_minimum`.

---

## Worked example: create and start a channel

```bash
TOKEN=$(grep '^AMBIENT_API_TOKEN=' .env | cut -d= -f2)
API=http://127.0.0.1:8090

curl -sS -X POST "$API/api/channels" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"lofi","genre":"ambient","mount":"/lofi","fallback_mount":"/lofi-fallback",
       "stream_key":"xxxx-xxxx-xxxx-xxxx-xxxx"}'

# Not done by the API — Icecast still needs the mount:
echo lofi >> channels/mounts.list
scripts/make-fallback.sh lofi
docker kill -s HUP ambient-icecast

cp ~/music/*.mp3 channels/lofi/audio/
cp ~/pictures/*.jpg channels/lofi/images/

curl -sS -X POST "$API/api/media/profiles?channel=lofi" -H "Authorization: Bearer $TOKEN"
curl -sS -X POST "$API/api/channels/lofi/start"        -H "Authorization: Bearer $TOKEN"

curl -sS -N "$API/api/events" -H "Authorization: Bearer $TOKEN"
```

---

## Related

| Document | Covers |
|----------|--------|
| [`contracts/rest-api.md`](contracts/rest-api.md) | the normative REST + SSE specification |
| [`contracts/config.md`](contracts/config.md) | every configuration key the API reads and writes |
| [operations.md](operations.md) | what the health fields mean and what to do about them |
| [quickstart.md](quickstart.md) | getting a channel to this point at all |
| [docker-deployment.md](docker-deployment.md) | where the API is exposed and why it is loopback-bound |
