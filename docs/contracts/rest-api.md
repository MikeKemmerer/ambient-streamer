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
| PUT | `/api/channels/{name}/visualization` | replace the isolated visualizer child with an installed plugin |
| PUT | `/api/channels/{name}/visualization/opacity` | change layer opacity live, 0 through 1 |
| GET | `/api/presets` | available presets |
| POST | `/api/channels/{name}/preset` | apply a preset |
| PUT | `/api/channels/{name}/color` | set mode and manual colors |

Switching to any installed plugin returns `202` while the channel is running:
only `<channel>-visualizer` is replaced. The program compositor and YouTube
ingest session remain unchanged; its framekeeper emits transparent fallback
during the child handoff. A stopped channel records the choice for its next
start and returns `200`.

A plugin that is not installed at all returns `404 unknown_plugin`.

## Skipping a track

`POST /api/channels/{name}/skip` → `202`.

Free, and the only channel action that is. Liquidsoap owns audio in its own
process and the compositor is a consumer of a live Icecast mount, so it never
learns a track changed — verified on a live channel with the composer's
container start time unchanged either side and the stream holding 0.999x.

The backend talks to Liquidsoap's telnet server over the compose network, on a
strict whitelist: the same socket accepts `shutdown`. It sends `icecast.skip`,
which was measured moving the audio and answering `Done`. `playlist.skip` is
deliberately **not** offered — it answers `OK` and only advances the playlist
cursor, leaving what is playing exactly where it was.

## Playing one specific track

`POST /api/channels/{name}/play` with `{"track": "<container path>"}` → `202`.
Costs the same as a skip: nothing.

`playlist` has no "play this one" verb, which is why the audio graph carries a
`request.queue` in front of the playlist:

```
programme = fallback(track_sensitive=false, [request.queue, playlist])
```

`track_sensitive=false` so a push interrupts the current track rather than
waiting for it to end — that is what an operator clicking a track means. When
the request is exhausted the fallback drops back to the playlist on its own.
Both halves were measured on a live channel.

`track` must be one of the channel's own `container_paths` from
`GET /api/channels/{name}/playlist`. Anything else is `404 unknown_track`.
This is not cosmetic validation: `queue.push` resolves whatever it is handed,
so an unchecked value is an arbitrary file read on the Liquidsoap container and
an outbound fetch for any `http://` URI.

**The track tracker has to sit below the crossfade.** `crossfade` merges a
mid-track switch into the track it is already playing, so a queue takeover
produces no track mark at the output at all — measured: the audio changed and
the reported track did not move for the whole 20 s the probe watched. The
`on_track` handler is attached to the `fallback`, not to the output. The cost is
that it fires up to `crossfade_seconds` early, because crossfade reads that far
ahead.

There is no seek. `icecast.seek 30` on the running source answers `Seeked 0.00`:
the playlist sits behind `crossfade` and `mksafe`, and the result is not
seekable. Scrubbing would mean restructuring the audio graph, and crossfade is
the thing that would have to go.

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

`PUT /api/channels/{name}/visualization/opacity` with `{"opacity": 0.4}` changes
the layer alpha on the stable compositor through `lut@vizop`. It lands in one
frame and does not restart the visualizer child, compositor, or ingest session.

## Tuning a plugin

`PUT /api/channels/{name}/visualization/parameters` with
`{"plugin": "showfreqs-bars", "values": {"detail": 2048}}`.

Names and ranges are declared per plugin and reported by `GET /api/plugins`.
Values outside a declared range are **clamped, not refused**, and the clamped
value is what gets stored — because FFmpeg accepts an out-of-range filter option,
ignores it, and renders the branch wrong at exit 0, so refusing would be the only
way an operator learned about it and clamping is the safer failure. An
*undeclared* name is refused, since it can only be a mistake.

Substituted into the fragment at launch. Tuning the active plugin replaces only
the isolated visualizer child; tuning an inactive plugin is just saved. The
waveform and vectorscope plugins expose 1–4 pixel thickness through RGB-space
dilation passes in addition to their filter-native settings.

## Turning the visualization off

`PATCH /api/channels/{name}` with `{"visualization": {"enabled": false}}`.

Live child control: off stops only `<channel>-visualizer`; on starts it again.
The stable compositor keeps receiving transparent fallback, and `active` is
left alone so switching it back on restores the same look.

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

A make-before-break restart costs **~1.4 s** on the YouTube leg and a new ingest
session, measured on a live channel across 18 consecutive takeovers (1.32–1.59 s,
worst 1.59 s). That is down from 3.22–6.44 s before the Icecast burst and
`-fflags nobuffer` changes.

The handover runs two composers at once, so the host needs headroom for **two**
during a restart. On a host that does not have it, `speed` dips below
`min_speed` for the length of the takeover; the watchdog treats a channel whose
supervisor lock is held as off-limits so that dip cannot cascade into another
restart.

## Ingest settings

| Method | Path | Purpose |
|---|---|---|
| PUT | `/api/channels/{name}/delivery` | stream key, RTMP URL, encoder, fps, reach |

Every field is optional; only what is sent changes, so editing the fps can never
overwrite the stream key. `clear_encoder` and `clear_fps` fall back to the global
default, which is distinct from omitting the field.

```json
{"stream_key": "abcd-1234", "rtmp_url": "rtmp://a.rtmp.youtube.com/live2",
 "encoder": "libx264", "fps": 30, "clear_encoder": false, "clear_fps": false,
 "youtube": true, "local_height": 720, "local_fps": 30, "clear_local": false}
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

## Delivery targets

A channel delivers to one or more of three targets, set as a whole:

```json
{"targets": ["youtube", "video", "audio"]}
```

| Target | Relay path | What it is |
|---|---|---|
| `youtube` | `<ch>` | the public broadcast; the only path with a publisher hook |
| `video` | `<ch>/video` | internal HLS, full size, LAN only |
| `audio` | `<ch>/audio` | internal HLS, audio only — no video encode at all |

At least one is required; an empty list is `400`. The list is stored
deduplicated and in that order, so the rendered `.env` does not churn because a
UI sent the set in a different order.

The **operator preview** at `<ch>/preview` is not a target. It is always
published, always 640x360@15, and exists for the control plane's own player.

**A channel without `youtube` cannot reach YouTube.** This is structural, not
"leave the stream key blank": the composer never publishes the program path, so
that path never goes ready, and `runOnReady` in `docker/mediamtx.yml` — the only
thing in the system that ever talks to YouTube — hangs on that path alone.
Verified on a live channel with a **valid stream key still present in its
`.env`**: no publisher process, no publisher log line, no program path on the
relay.

Each target is a separate encode, and each one omitted is a scale and an encode
the composer never runs. `audio` adds no video branch at all, which is what
makes it the cheapest feed.

| | `rtmp` in status | Stream key |
|---|---|---|
| with `youtube` | `connected` / `disconnected` | required |
| without | `local-only` | not needed; one present is inert |

`local_height` and `local_fps` size the `video` feed; the width follows at 16:9,
rounded to an even number because yuv420p cannot encode an odd dimension. Both
default to the channel's own resolution and fps, because that feed is the
product rather than a preview. `clear_local` returns both to those defaults —
as a pair, since a half-cleared rendition leaves a stale fps on a feed that just
changed size.

### Migrating from `CHANNEL_PUBLISH_YOUTUBE`

`CHANNEL_DELIVERY` supersedes the old boolean. When it is absent:

- `CHANNEL_PUBLISH_YOUTUBE=false` resolves to `["video"]`, **not** to the
  default. A channel deliberately taken off YouTube must not be put back on air
  by an upgrade.
- anything else resolves to `["youtube"]`.

Setting `targets` writes `CHANNEL_DELIVERY` and clears the old key, so a stale
`false` cannot contradict an explicit list.

### Feed URLs

`GET /api/channels/{name}` returns a `feeds` array — only the renditions the
channel actually publishes, because a URL for a feed nobody started is a support
call rather than a convenience:

```json
{"rendition": "video", "label": "internal video", "detail": "1280x720@30",
 "url": "http://<host>:<AMBIENT_HLS_PUBLISH>/<channel>/video/index.m3u8"}
```

All three share the preview's shape,
`http://<host>:<port>/<channel>/<preview|video|audio>/index.m3u8`. `url` is
`null` when `AMBIENT_HLS_PUBLISH` is unset: an external player has no route to
the compose network, and offering an address that cannot resolve is worse than
offering none.

The backend also proxies all three behind its own token for the in-page player.
Those are **three literal routes**, not one with a `{rendition}` parameter — a
wildcard there matches any three-segment URL and was measured swallowing
`/api/channels/<x>` as channel `api`, rendition `channels`.

Measured on a live internal channel at 480p30: the HLS master reported
`RESOLUTION=854x480, FRAME-RATE=30.000` at 1.77 Mbps, holding 1.0x realtime.

### The public station directory

`GET /directory` (HTML) and `GET /directory.json` are the **only two tokenless
endpoints besides `/api/health`**. They list the internal feeds that are
currently on air, so a listener on the LAN can find them without an operator.

They are unauthenticated because they sit on the HLS port, which MediaMTX
already served without a token. Nothing on them is a control surface, and the
constraints are deliberate:

- **`active` comes from the relay**, not from configuration. A station that is
  configured but not publishing is not listed; a directory that lists a station
  you cannot tune to is worse than an empty one.
- **The program path is never listed.** It is the YouTube feed and the one path
  the publisher hook reads.
- **`preview` is never listed.** It is the operator's 360p feed — this is a
  directory of stations, not of tooling.
- **No `docker exec`, no config secrets.** The page is built from one relay
  listing plus channel names and genres, because an anonymous caller must not be
  able to amplify a page load into work on the host.
- Every value is HTML-escaped.

A relay that cannot be reached yields an empty directory rather than a claim
that everything is down.

The URLs are absolute and built from the `Host` header the listener used: the
relay's internal hostname resolves nowhere on their machine.

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
