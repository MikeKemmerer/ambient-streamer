# Contract — Isolated visualization runtime

Owner: lead. Consumers: media-pipeline, backend-api, frontend.

The program compositor must not instantiate visualization plugins. It consumes
one fixed raw-video layer for its entire lifetime. A framekeeper produces that
layer continuously while restartable visualizer children connect and disconnect.

## Stable compositor input

- Format: raw `yuv420p`, limited-range black fallback (`Y=16`, `U=V=128`).
- Geometry: the channel geometry capped at `1280x720`; never upscale.
- Frame rate: the channel frame rate capped at 30 fps.
- Transport: framekeeper stdout into a named FIFO under
  `/run/ambient/<channel>/visualization.pipe`.
- Writer socket: `/run/ambient/<channel>/visualization.sock`.
- Stale timeout: 350 ms. Missing or stale frames become black; black is made
  transparent by the compositor's existing alpha derivation.

The framekeeper starts with the composer and dies with it. Its PID is not a
separate deployment unit. The program FFmpeg PID must remain unchanged when a
visualizer starts, stops, crashes, is tuned, or is replaced.

## Visualizer child

At most one `<channel>-visualizer` container runs. It reads audio from the
compositor's MediaMTX preview path, renders exactly one installed `viz.ffmpeg`
fragment at the capped layer geometry, and writes raw frames to the framekeeper
socket. The preview carries the same post-processed audio timeline as the
program output; a second Icecast listener is forbidden because independent
listener queues were measured putting visualization 2.4 seconds ahead of
published audio.

- Plugin switch: recreate only `<channel>-visualizer`.
- Parameter change: recreate only `<channel>-visualizer`.
- Layer opacity: command `lut@vizop y` on the stable compositor; no process restart.
- `visualization.enabled=false`: stop only `<channel>-visualizer`.
- Child crash or startup failure: the compositor continues with transparent
  fallback; the failure never enters the compositor watchdog restart path.
- Container restart policy provides bounded retry/backoff; the backend reports
  readiness from the framekeeper status file.

Line-based plugins may expose a 1–4 pixel thickness parameter. The child applies
conditional RGB dilation passes after rendering so color remains intact; changing
thickness follows the normal parameter path and recreates only the visualizer child.

## Compatibility

`visualization.hot_set` remains accepted for one release so existing channel
files and presets load. It is deprecated and ignored by runtime and capacity
planning. The backend may preserve it when rewriting config, but the operator UI
must not expose or mutate it.

## Measured basis

Spike S8 held one main FFmpeg PID through null/plugin/gap/plugin/null sequences,
20 live switches, and a forced visualizer `SIGKILL`: exact frame counts, zero
drops/duplicates, and 0.996-0.998x at 1080p output with a 720p layer.