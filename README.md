# ambient-streamer

Headless, multi-channel, 24/7 ambient music streaming to YouTube. Each channel pairs a
Liquidsoap audio engine with an FFmpeg compositor that renders a color-adaptive image
slideshow plus real-time audio visualization, publishes to YouTube over RTMP, and exposes a
low-resolution HLS preview.

The rule the whole design serves: **a 24/7 stream must never restart FFmpeg.** A filtergraph is
fixed at launch, so every live-editable feature routes around that — images arrive on a pipe,
colours change over ZMQ, plugins switch with `streamselect`, and the audio engine restarts
behind an Icecast fallback mount. See [docs/architecture.md](docs/architecture.md).

## Status

**The streaming path is built and running on real hardware.** Liquidsoap → Icecast → composer →
MediaMTX → YouTube, plus the HLS preview, verified live. Measured steady state on a 720p
channel: `speed=0.996x`, 3085–3137 kbits/s, `drop_frames=0`, `dup_frames=0`.

| Works today | Not built yet |
|-------------|---------------|
| Liquidsoap → Icecast audio transport with fallback mounts | FastAPI control plane (REST + SSE) |
| Slideshow producer on `image2pipe` | Operator web UI |
| Composer filtergraph: slideshow + visualization plugin + preview split | Watchdog |
| MediaMTX relay, HLS preview, `runOnReady` YouTube publisher | Scheduler |
| Compose rendering and media resolution (`python -m ambient.compile`) | Colour-profile extraction and application |
| Per-channel start/stop (`scripts/channel.sh`) | Presets and bumpers |

There is no web UI and no API. Channels are compiled and started from the shell.

## Requirements

| | |
|---|---|
| OS | Linux with Docker Engine and the Compose v2 plugin. Docker Desktop / WSL2 works, with the caveats below |
| Python | 3.10+ on the host, to run the compiler |
| FFmpeg | on the host, only for `scripts/make-fallback.sh` |
| YouTube | one stream key per channel, created by hand in YouTube Studio (Go Live → Stream → reusable key) |

Platform constraints that bite in practice:

- **Media must not live on `/mnt/c/...`.** On Docker Desktop/WSL2 that path is 9p-backed and far
  too slow for continuous reads. A CIFS/NFS mount fails the same way. Use a WSL2 ext4 path or a
  Docker named volume.
- **`h264_qsv` cannot work on Docker Desktop/WSL2** — `/dev/dri` is not exposed there. It is a
  bare-metal-Linux profile only.
- **`h264_nvenc` works on WSL2** with nvidia-container-toolkit, but consumer NVIDIA cards cap
  concurrent encode sessions; channels beyond the cap fall back to `libx264`.

## Quick start

### 1. Configure the host

```bash
cp .env.example .env && chmod 600 .env
cp ambient.yaml.example ambient.yaml
```

Fill in `.env`. The three Icecast passwords and the API token have no defaults:

```bash
openssl rand -hex 16   # ICECAST_SOURCE_PASSWORD, ICECAST_ADMIN_PASSWORD, ICECAST_RELAY_PASSWORD
openssl rand -hex 32   # AMBIENT_API_TOKEN
```

Install the compiler:

```bash
pip install -e backend
```

### 2. Start the global stack

Three global containers serve every channel: Icecast, MediaMTX, and — once it exists — the
backend.

```bash
scripts/make-fallback.sh default          # 30s silent MP3, 256k/44.1k/stereo
docker compose -p ambient up -d --build
```

Neither relay publishes a host port. RTMP 1935, HLS 8888 and the MediaMTX API 9997 stay on the
internal Docker network.

`scripts/verify-stack.sh` brings the stack up, proves Icecast serves a fallback mount with no
source connected, and tears itself down again.

### 3. Create a channel

```bash
mkdir -p channels/lofi/{audio,images}
cp channels/example/.env.example channels/lofi/.env && chmod 600 channels/lofi/.env
cp channels/example/config.yaml  channels/lofi/config.yaml
```

Put that channel's YouTube stream key in `channels/lofi/.env`, then add media — to
`channels/lofi/audio` and `channels/lofi/images` for this channel only, or to `common/audio` and
`common/images` to make it available to every channel. Select what the channel uses in
`channels/lofi/config.yaml`; an empty list means "everything in this channel's own folder".

Register the channel's Icecast mount and give it a fallback file. Adding a mount is a SIGHUP,
never a restart — restarting Icecast would take every running channel's audio input with it:

```bash
echo lofi >> channels/mounts.list
scripts/make-fallback.sh lofi
docker kill -s HUP ambient-icecast
```

### 4. Compile and start

```bash
python -m ambient.compile lofi     # writes playlist.m3u, images.list, docker-compose.yml
scripts/channel.sh start lofi
scripts/channel.sh logs lofi
```

MediaMTX spawns the YouTube publisher when the channel's program path goes ready — measured at
207 ms. A channel whose stream key is still empty runs normally and stays off YouTube.

## Operating a channel

```bash
scripts/channel.sh start|stop|restart|status|logs|config <channel>
```

Every command is scoped to that channel's own Compose project. It cannot reach the global stack
or any other channel.

**Use this script rather than `docker compose` directly.** Compose resolves the implicit `.env`
relative to the *Compose file's* directory, so a per-channel file in `channels/<name>/` never
sees the root `.env` — Icecast credentials resolve to empty and the channel comes up mute
against a relay that rejects it. Both files have to be named, root first:

```bash
docker compose --project-name ambient-lofi \
  --env-file .env --env-file channels/lofi/.env \
  --file channels/lofi/docker-compose.yml up -d
```

### What can change without restarting anything

| Change | How | Measured cost |
|--------|-----|---------------|
| Add/remove/reorder tracks | edit `config.yaml`, recompile; Liquidsoap reloads | 0 s |
| Add/remove/reorder images | edit `config.yaml`, recompile; producer reloads between slides | 0 s, FFmpeg PID unchanged |
| Restart Liquidsoap | Icecast fallback mount absorbs it | 0 s, 0.00 % silence |
| Add a channel to Icecast | `mounts.list` + `docker kill -s HUP ambient-icecast` | no restart |
| Fill in a stream key | the relay reads it when the path goes ready | no relay restart |
| Restart the composer | last resort | **13.7 s** on the YouTube leg, and a new ingest session |

Globs and empty selection lists are watched, so dropping a file into a watched folder updates
the stream. Explicit path lists are deliberately not watched — the operator asked for exactly
those files.

## Capacity

**Measured on a 7-core host: about 1.5 cores per 720p channel with one hot visualization
plugin** — composer 137–142 % of a core, Liquidsoap ~6 %, plus a small share of the global
MediaMTX (~7 %, which includes the publisher) and Icecast (~0.2 %). Reserving about a core for
the OS and shared services, that host runs **about four channels**.

That is higher than a filtergraph benchmark predicts, because **the HLS preview is a second
encode**, not a free tap off the program encode — MediaMTX does not transcode. Any estimate that
does not count the preview encode is wrong. Supported range is 1–8 channels.

## Media licensing

This is a 24/7 public YouTube broadcast, so both halves of the media matter legally.

- **Music must be royalty-free or owned.** Anything else attracts Content ID claims, and on a
  continuous stream a claim can mute or end the broadcast. Keep the licence or purchase record
  for every track.
- **Images must be licensed for the use**, and any licence requiring attribution needs that
  attribution somewhere the viewer can see it — the video description at minimum, on-screen if
  the licence says so. CC-BY is not "free"; it is "free with a condition".

Nothing in this repository checks either of these. It is the operator's responsibility.

## Repository layout

| Path | Purpose |
|------|---------|
| `backend/ambient/` | Config layer, media resolver, FFmpeg command builder, ZMQ validator, Compose renderer, `compile` CLI |
| `channels/<name>/` | Per-channel `.env`, `config.yaml`, media, colour profiles, generated files |
| `common/` | Shared media library — audio, images, bumpers, beds, fallbacks — mounted read-only into every channel |
| `liquidsoap/` | Parameterized Liquidsoap channel script |
| `ffmpeg/` | Slideshow producer and compositor entrypoint |
| `plugins/` | Visualization plugins (`viz.ffmpeg` + `config.json`) |
| `docker/` | Dockerfiles, Icecast and MediaMTX config, the YouTube publisher, per-channel Compose template |
| `scripts/` | `channel.sh`, `make-fallback.sh`, `verify-stack.sh` |
| `spikes/` | Phase 0 harnesses and raw output behind the measured claims |
| `docs/` | Architecture and the frozen lane contracts |

`frontend/` and `presets/` do not exist yet.

## Documentation

| Document | Covers |
|----------|--------|
| [docs/architecture.md](docs/architecture.md) | Container topology, data flow, why FFmpeg never restarts, capacity |
| [docs/contracts/](docs/contracts/README.md) | Frozen interfaces between lanes — the source of truth |

The remaining planned documents — quickstart, plugin development, colour profiles, API
reference, deployment, scaling — are listed in
[docs/architecture.md §9](docs/architecture.md#9-related-documents) and are not written yet.

## Security notes

- `.env`, `channels/*/.env`, `ambient.yaml` and all media are gitignored. Never commit a stream
  key, an Icecast password, or a real hostname.
- A stream key is held only by the relay, read from `channels/<name>/.env` when a path goes
  ready, and passed to FFmpeg through a preloaded argv shim so it never reaches `ps` or
  `docker inspect`. No composer container is given one.
- The ZMQ control socket binds to `127.0.0.1` inside the composer container and is never
  published. A malformed ZMQ message kills FFmpeg outright, so that socket is a kill surface,
  not a convenience.

## License

MIT
