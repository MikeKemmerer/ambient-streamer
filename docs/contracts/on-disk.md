# Contract — On-disk layout and runtime state

Owner: lead. Consumers: all lanes.

## Host layout

```
ambient-streamer/
├── .env                          secrets + docker-time settings   (gitignored)
├── ambient.yaml                  global live config               (gitignored)
├── common/
│   ├── audio/  images/  bumpers/{,beds/}
│   └── profiles/<image>.json     color profiles for shared images
└── channels/<name>/
    ├── .env                      stream key + channel settings    (gitignored)
    ├── config.yaml               channel live config              (gitignored)
    ├── bumpers.yaml              bumper source text               (TRACKED)
    ├── audio/  images/  bumpers/
    ├── profiles/<image>.json     profiles for this channel's images
    ├── playlist.m3u              generated
    ├── images.list               generated
    └── docker-compose.yml        generated — never hand-edited
```

`bumpers.yaml` is the only tracked file in a channel directory: it is small,
human-written, and the generated audio can always be recreated from it.

## Container mounts

| Host | Container | Mode |
|---|---|---|
| `common/` | `/media/common` | ro |
| `channels/<name>/` | `/media/channel` | ro |
| `${AMBIENT_LOG_DIR}/<name>/` | `/var/log/ambient` | rw |
| `${AMBIENT_RUN_DIR}/` | `/run/ambient` | rw |

Media is mounted read-only. Nothing in the streaming path should ever write to
it, and enforcing that at the mount catches the mistake early.

**Media must be on a local filesystem.** A CIFS/NFS mount, or a Windows drive
under `/mnt/c` on Docker Desktop, adds a network or 9p round trip to every read
and will stall a 24/7 stream.

## Color profile

`<name>.json`, one per image, beside the image tree it belongs to.

```json
{
  "version": 1,
  "source": "common/images/forest.jpg",
  "extracted_at": "2026-08-11T22:04:00Z",
  "extractor": "pillow-kmeans-v1",
  "dominant": "#2E4A3B",
  "accent": "#8FD6A8",
  "palette": ["#2E4A3B", "#8FD6A8", "#0F1A14", "#C9E4D2", "#5A7A66"],
  "brightness": 0.34,
  "warmth": -0.42,
  "mood": "calm"
}
```

| Field | Range | Meaning |
|---|---|---|
| `brightness` | 0.0–1.0 | mean perceived luminance |
| `warmth` | −1.0–1.0 | negative cool, positive warm |
| `mood` | enum | `calm` `warm` `cool` `energetic` |
| `extractor` | string | which implementation produced this |

`extractor` is versioned so profiles can be regenerated when the algorithm
changes without guessing which are stale. A profile whose `extractor` is
unknown to the running backend is treated as absent and re-extracted.

Profiles are derived data and gitignored. Deleting them is always safe.

## HLS preview

MediaMTX does **not** transcode, so the low-resolution preview is a second
output from the compositor, published to a separate relay path.

| | Path |
|---|---|
| Program | `rtmp://mediamtx:1935/<channel>` |
| Preview | `rtmp://mediamtx:1935/<channel>/preview` |
| Preview HLS | `http://mediamtx:8888/<channel>/preview/index.m3u8` |
| Operator URL | `http://<host>:8090/<channel>/preview/index.m3u8` |
| External player URL | `http://<host>:<AMBIENT_HLS_PUBLISH>/<channel>/preview/index.m3u8` |

The backend proxies the operator URL, and that proxy requires the bearer token.
An external player such as VLC cannot send one, so it needs the relay's own port
instead — which exists only when `AMBIENT_HLS_PUBLISH` is set. By default
MediaMTX publishes no host ports: keeping the relay and the ZMQ sockets off the
LAN is the point, and publishing HLS is an explicit opt-in that puts an
unauthenticated preview on the network. `GET /api/channels/{name}` reports the
resulting address as `hls_url`, or `null` when the port is not published.

## Logs

On the **host**, one directory per channel:

```
${AMBIENT_LOG_DIR}/<channel>/{compositor,liquidsoap,producer,watchdog}.log
```

That directory is mounted at `/var/log/ambient` **inside** the channel's
containers, so a container writes `/var/log/ambient/compositor.log` with no
channel name in the path — the name is the mount point. Giving every channel
the same in-container path is what lets one image serve all of them; without
the per-channel host directory, every channel's containers would write to the
same file.

Every process writes to **both** its log file and stdout. Files give the UI
something to tail; stdout keeps `docker logs` and the container runtime's own
tooling working. Neither alone is sufficient.

Rotation is required — a 24/7 compositor at default FFmpeg verbosity will fill
a disk. Rotate by size, keep a bounded number of files.

## Runtime state

`/run/ambient/<channel>/` — a **host directory bind-mounted into every container
of the channel**, not a per-container tmpfs. It was a tmpfs once, and that made
the watchdog permanently blind to `progress`: it could never read the file, so it
restarted a perfectly healthy composer roughly every 75 seconds. Sharing it is
load-bearing, not incidental.

State here is not persisted across a host reboot and is a cache of what the
process is doing now, never a source of truth.

| File | Written by | Contents |
|---|---|---|
| `now.json` | composer | current track, next track, current slide, started_at |
| `progress` | composer | FFmpeg `-progress` output — **the watchdog's primary input** |
| `zmq.sock` | composer | control socket address for this channel |
| `health.json` | backend | last watchdog verdict and timestamp |
| `color-mode` | backend | `manual` or `auto`, read live by the slideshow producer |
| `slide-position` | composer | the slide on screen, so a rebuilt graph resumes there |
| `visualization.pipe` | composer | framekeeper raw-video output into the stable FFmpeg graph |
| `visualization.sock` | framekeeper | Unix socket for the current visualizer writer |
| `visualization-status.json` | framekeeper | emitted/received/fallback/reconnect counters and readiness |

`slide-position` exists because applying most settings replaces the compositor,
and the producer goes with it. Audio survives that — Liquidsoap is a separate
process feeding Icecast and `restart` uses `--no-deps` — but the producer's slide
index is in-process, so without this the images jumped back to the top of the
order every time an operator changed the resolution. The producer records the
slide as it goes up and resumes on **that** slide, not the next one: it was on
screen when the graph was rebuilt, so it never finished its hold. A recorded
slide that has since left `images.list` falls back to the top rather than
stalling. Writing it is best-effort — losing continuity is survivable, and a
producer that dies EOFs the pipe and ends the broadcast.

`color-mode` exists because the producer receives the mode as a launch-time
environment variable, and a filtergraph is fixed at launch. Without a live
signal, switching to manual would not stop the producer re-coloring from the
image until the composer restarted — the one operation the design avoids. The
backend replaces the file atomically; the producer polls a `stat()` token and
re-opens it, so the switch lands in well under a second with no restart.

`now.json` deliberately does **not** carry the active plugin. The isolated
visualizer changes without restarting the composer, so the channel config and
framekeeper status are the runtime sources of truth.

### Why `progress` is the primary signal

Both dangerous failure modes are invisible to a process check:

- A stalled producer leaves FFmpeg **alive** at 0.44x while YouTube starves.
- A dead producer makes FFmpeg exit **rc=0**, which looks like success.

So: health is `out_time` advancing in step with wallclock and `speed` sustained
at ≥0.97. **Any** compositor exit is a fault regardless of exit code. `drop` and
`dup` are not usable — both stay at 0 through the burst-pacing failure.

## Restarts

Visualization plugin, enabled state, and plugin parameters never restart the
compositor. They start, stop, or replace only `<channel>-visualizer`. Resolution
and anything in `.env` still require a compositor replacement.

`visualization.visible` is the exception. It rides the composite overlay's
timeline `enable` over zmq, lands in one frame, and was confirmed on a running
channel with the composer's container start time unchanged either side.

### What a restart actually costs

Measure it from the relay, not from the composer. `runOnReady` runs the YouTube
publisher only while the path has a publisher, so the window between
`runOnReady command stopped` and `runOnReady command started` is exactly the
dead air — and each restart of it is a new YouTube ingest session.
`scripts/measure-restart-gap.py` reports it.

| | Dead air |
|---|---|
| Before the per-slot progress fix | **3.22 s and 6.44 s**, sometimes several gaps per restart |
| After it | one transition per restart |

Older revisions of these docs claimed **1.03 s**. That figure came from a
Phase-0 spike measuring the publisher reattach in isolation, not an end-to-end
composer replacement, and nothing in this tree substantiates it. Treat any
surviving "~1 s" as wrong.

Two things were fixed after that measurement, both verified:

- **Each slot writes its own progress file.** Both composers mount the same run
  directory, so a single `progress` had two writers and the takeover check could
  not tell whose frames it was watching — make-before-break was not holding.
- **The replacement's cold start went from ~9 s to ~0.9 s.** FFmpeg needs several
  seconds of MP3 content before it emits a packet; the same content from a file
  reached first packet in 0 s and from a burst-less mount in 9 s. Icecast now
  bursts on connect, and the composer adds `-fflags nobuffer` (0.46 s).

The end-to-end figure after both fixes has **not** been re-measured on a channel
that actually publishes to YouTube. The component measurements are sound; the
whole-path number is not yet confirmed.
