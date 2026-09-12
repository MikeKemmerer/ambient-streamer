# Contract — Configuration

Owner: lead. Consumers: every lane.

## The split rule

Four config surfaces exist. Which one a setting belongs in is decided by a
single question:

> **Can the backend apply this change to a running channel?**
> If no — it needs a container recreated — it belongs in `.env`.
> If yes, it belongs in YAML.

Secrets always live in `.env`, never in YAML, because YAML is served to the
browser by the control plane.

| File | Holds | Scope | Live-editable | Tracked |
|---|---|---|---|---|
| `.env` | ports, host paths, uid/gid, Icecast credentials, backend auth | install | no | no (`.env.example` is) |
| `ambient.yaml` | global defaults, limits, registries | install | **yes** | no (`ambient.yaml.example` is) |
| `channels/<n>/.env` | stream key, resolution, fps, encoder, CPU/memory limits, mounts | channel | no | no |
| `channels/<n>/config.yaml` | media selection, visualization, color, preset, schedule, bumpers | channel | **yes** | no |

Resolution and encoder sit in `.env` rather than YAML because changing either
means a new FFmpeg filtergraph, which means recreating the compositor
container. They are deliberately not live-editable — see the rule above.

## `ambient.yaml`

```yaml
version: 1

defaults:
  resolution: 720p          # 480p | 720p | 1080p | 1440p | 2160p
  fps: 30
  encoder: libx264          # libx264 | h264_nvenc | h264_qsv
  crossfade_seconds: 5.0
  slideshow:
    producer_fps: 10        # see slideshow.md before changing
    hold_seconds: 20.0
    fade_seconds: 2.0
    jpeg_quality: 88

limits:
  max_channels: 8
  # Refuse to start a channel when the projected core cost of all running
  # channels exceeds this. Measured on a running channel: ~1.5 cores per 720p
  # channel with ONE hot plugin. See plugin.md for the per-plugin figures.
  reserved_cores: 1.0

encoders:
  # Probe order. A profile is only offered if a real test encode succeeds —
  # `ffmpeg -encoders` lists encoders whose hardware is absent, so the encoder
  # list alone is not evidence. See the note below.
  probe_order: [h264_nvenc, h264_qsv, libx264]
  fallback: libx264

paths:
  common: ./common
  channels: ./channels
  logs: /var/log/ambient

relay:
  rtmp: rtmp://mediamtx:1935
  hls: http://mediamtx:8888
  icecast: http://icecast:8081
```

### Encoder probing is not optional

**Measured:** on both candidate hosts `ffmpeg -encoders` listed `h264_qsv`
while `/dev/dri` held no Intel device, and on one host `h264_nvenc` was listed
while the driver was mismatched and every encode failed with
`OpenEncodeSessionEx failed: unsupported device (2)`.

An encoder profile is available only if a short real encode succeeds. A channel
whose configured encoder fails its probe starts on `fallback` and reports the
substitution; it does not fail to start.

## `channels/<name>/config.yaml`

```yaml
version: 1
name: lofi
genre: "lo-fi hip hop"

audio:
  # Three forms, mixable: explicit paths, globs, or omitted/empty to use the
  # channel's own folder. See media-selection.md.
  #   tracks: []                        -> everything in channels/<name>/audio/
  #   - channels/lofi/audio/**          -> glob, re-expanded as the folder changes
  #   - common/audio/intro.mp3          -> exactly this file
  # Order is preserved as written; each glob expands in place.
  tracks:
    - common/audio/rain-loop.mp3
    - channels/lofi/audio/**
  shuffle: false
  crossfade_seconds: 5.0

images:
  # Same three forms as audio.
  slides:
    - common/images/forest.jpg
    - channels/lofi/images/*
  order: sequential          # sequential | shuffle
  hold_seconds: 20.0
  fade_seconds: 2.0

visualization:
  # Off stops only the restartable visualizer child. The stable compositor keeps
  # receiving transparent fallback from its framekeeper.
  enabled: true
  # Standby, and the only visualization on/off that is live. `enabled` decides
  # whether the branches exist and needs the graph rebuilt; this rides the
  # overlay's timeline `enable` and was measured landing in one frame on a
  # running channel without replacing the composer. The branches keep rendering
  # either way, so standby costs what on costs.
  visible: true
  # Alpha multiplier for the visualization layer. Live: one command to the
  # stable compositor, with no visualizer or compositor restart.
  opacity: 0.65
  active: showfreqs-bars
  # Deprecated compatibility field. Accepted for one release, but ignored by
  # the isolated visualization runtime and capacity planning.
  hot_set: [showfreqs-bars, showwaves-classic, avectorscope-lissajous]
  # Per plugin, so each keeps its own look across a switch. Names and ranges come
  # from the plugin's config.json; values outside a declared range are clamped
  # rather than refused, because FFmpeg accepts an out-of-range filter option,
  # ignores it, and renders the branch wrong at exit 0.
  parameters:
    showfreqs-bars: {detail: 2048, smoothing: 6, shape: line}

color:
  mode: automatic            # automatic | manual
  # automatic: derived per-slide from the image's color profile
  # manual: the fixed values below
  manual:
    accent: "#4FC3F7"
    tint: "#101820"
  transition_seconds: 2.0

preset: calm-ocean           # null to use the values above verbatim

bumpers:
  enabled: false
  # see bumpers.md
  mode: tracks               # tracks | time | both
  every_tracks: 4
  every_minutes: 20
  sources:
    - common/bumpers/station-id.mp3

schedule:
  timezone: UTC
  rules: []                  # see preset.md
```

## Validation

The backend validates on load and refuses to start a channel that fails. All of
these are hard errors, not warnings:

| Rule | Why |
|---|---|
| every path in `audio.tracks` / `images.slides` exists and is under `common/` or this channel's directory | a path outside both trees is not mounted into the container and will silently fail to open |
| globs are validated on their **expanded results**, not the pattern | a pattern is not a path; `**` or a symlinked subdirectory can match outside the intended tree |
| `audio.tracks` resolves to at least one file | a channel with no audio cannot stream. An empty *glob* is only a warning; an empty *result* is fatal |
| `visualization.active` names an installed plugin | the restartable visualizer must have a valid fragment to launch |
| `bumpers.sources` non-empty when `bumpers.enabled` | otherwise the rotate operator starves |
| projected core cost + running channels ≤ cores − `reserved_cores` | prevents oversubscribing the host into a stream that cannot hold 1.0x |
| `CHANNEL_MOUNT` unique across channels | two channels sharing an Icecast mount would fight |

## Reload semantics

`ambient.yaml` and `config.yaml` are re-read on change. What happens next
depends on the field:

| Change | Effect |
|---|---|
| `audio.tracks` | `playlist.m3u` rewritten; Liquidsoap picks it up. No restart |
| `images.slides` | `images.list` rewritten; the producer stats it and re-reads. No restart |
| **file added to a watched folder** | list rewritten automatically. No restart, no config edit |
| `visualization.active` | replace only the isolated visualizer child; compositor unchanged |
| `visualization.visible` | `overlay@viz enable` command. One frame. Saves nothing — the branches keep rendering |
| `visualization.opacity` | `lut@vizop y` command. One frame. No process restart |
| `color.*` | zmq commands. One frame |
| `images.hold_seconds`, `fade_seconds` | launch-time environment, converted to frame counts once at producer start. **Applies on the next start** |
| `visualization.enabled` | start or stop only the isolated visualizer child |
| `visualization.hot_set` | deprecated compatibility field; preserved and ignored |
| `visualization.parameters` | replace only the isolated visualizer child when active; otherwise just saved |
| `resolution` | **requires a compositor restart** |
| anything in `.env` | requires the container to be recreated |

`bumpers.*` is deliberately absent from this table. It used to claim "Liquidsoap
reconfigured. No compositor restart"; nothing in `liquidsoap/` or `ffmpeg/` reads
any bumper field, so the change has no effect at all. See bumpers.md.

A folder is watched when its channel selected it by glob or by leaving the list
empty. Explicit lists are not watched — see media-selection.md.

Only fields explicitly naming a compositor restart interrupt the stream. The
isolated visualizer operations above leave the compositor and ingest session
unchanged; see [visualization-runtime.md](visualization-runtime.md).
