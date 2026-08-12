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

| Works today | Not wired up yet |
|-------------|------------------|
| Liquidsoap → Icecast audio transport with fallback mounts | Operator web UI — `frontend/` exists and is baked into the backend image, but nothing serves it |
| Slideshow producer on `image2pipe` | HLS preview proxy through the backend |
| Composer filtergraph: slideshow + visualization plugin + preview split | Resolution, fps, `hot_set` and slideshow timing reaching the compositor |
| MediaMTX relay, HLS preview, `runOnReady` YouTube publisher | Icecast mount registration when a channel is created |
| FastAPI control plane: REST, SSE, supervisor, watchdog, scheduler | Bumper synthesis |
| Five visualization plugins, six preset packs | |
| Colour-profile extraction and live `eq`/`hue` application | |
| `scripts/install.sh`, compile CLI, per-channel start/stop | |

Start with [docs/quickstart.md](docs/quickstart.md), which is explicit about what each gap
means in practice.

## Requirements

| | |
|---|---|
| OS | Linux with Docker Engine and the Compose v2 plugin. Docker Desktop / WSL2 works, with the caveats below |
| FFmpeg | `ffmpeg` and `ffprobe` on the host — the installer's encoder probe and the fallback encoder need them |
| Other | `openssl`; a `/var/run/docker.sock` that is **not** group `root` |
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
scripts/install.sh
```

Idempotent. Checks prerequisites, creates `.env` (mode 600) and `ambient.yaml`, generates the
three Icecast passwords and the API token **only where empty**, records `AMBIENT_REPO_ROOT`,
creates the media and log directories, warns about 9p/NFS/CIFS storage, refuses to continue on
a port conflict, probes every encoder with a real short encode, and encodes the Icecast
fallback MP3s.

`scripts/install.sh --check` probes without writing anything.

### 2. Build and start the global stack

```bash
docker compose -p ambient build
scripts/install.sh --check          # with images built, the encoder probe is authoritative
docker compose -p ambient up -d
```

Three global containers serve every channel: Icecast, MediaMTX and the backend. **Only the
backend publishes a host port** — `${AMBIENT_BIND_ADDRESS}:8090`, loopback by default. RTMP
1935, HLS 8888, Icecast 8081 and the MediaMTX API 9997 stay on the internal Docker network.

`scripts/verify-stack.sh` brings the stack up, proves Icecast serves a fallback mount with no
source connected, and tears itself down again.

### 3. Create a channel

```bash
mkdir -p channels/lofi/{audio,images,profiles,bumpers}
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
scripts/install.sh                        # encodes common/fallback/lofi.mp3
docker kill -s HUP ambient-icecast
```

### 4. Compile and start

```bash
docker exec ambient-backend python -m ambient.compile lofi
scripts/channel.sh start lofi
scripts/channel.sh logs lofi
```

Or through the API:

```bash
TOKEN=$(grep '^AMBIENT_API_TOKEN=' .env | cut -d= -f2)
curl -sS -X POST -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8090/api/channels/lofi/start
```

MediaMTX spawns the YouTube publisher when the channel's program path goes ready — measured at
207 ms. A channel whose stream key is still empty runs normally and stays off YouTube.

Full walkthrough, including verification and the current gaps:
[docs/quickstart.md](docs/quickstart.md).

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
| Add/remove/reorder tracks | edit `config.yaml` or `PUT /api/channels/<ch>/playlist`, recompile; Liquidsoap reloads | 0 s |
| Add/remove/reorder images | edit `config.yaml` or `PUT /api/channels/<ch>/images`, recompile; producer reloads between slides | 0 s, FFmpeg PID unchanged |
| Colour | `PUT /api/channels/<ch>/colour`, or an image's colour profile | one frame |
| Visualization plugin | `PUT /api/channels/<ch>/visualisation` | one frame, clean cut |
| Restart Liquidsoap | Icecast fallback mount absorbs it | 0 s, 0.00 % silence |
| Add a channel to Icecast | `mounts.list` + `docker kill -s HUP ambient-icecast` | no restart |
| Fill in a stream key | the relay reads it when the path goes ready | no relay restart |
| Restart the composer, make-before-break | `POST /api/channels/<ch>/restart` | **1.03 s** on the YouTube leg |
| Restart the composer, blunt | `scripts/channel.sh restart <ch>` | **13.7 s**, and a new ingest session |

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
does not count the preview encode is wrong. Supported range is 1–8 channels. Add ~0.2–0.25
cores per additional hot plugin; see [docs/scaling.md](docs/scaling.md).

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
| `backend/ambient/` | Control plane: config layer, media resolver, FFmpeg command builder, ZMQ validator, Compose renderer, REST + SSE app, supervisor, watchdog, scheduler, colour extraction |
| `frontend/` | Operator UI — plain HTML/CSS/JS, no build step, `hls.js` vendored as one file |
| `channels/<name>/` | Per-channel `.env`, `config.yaml`, media, colour profiles, generated files |
| `common/` | Shared media library — audio, images, bumpers, beds, fallbacks — mounted read-only into every channel |
| `liquidsoap/` | Parameterized Liquidsoap channel script |
| `ffmpeg/` | Slideshow producer and compositor entrypoint |
| `plugins/` | Visualization plugins (`viz.ffmpeg` + `config.json`) |
| `presets/` | Preset packs — look-and-feel bundles that can never require a restart |
| `docker/` | Dockerfiles, Icecast and MediaMTX config, the YouTube publisher, per-channel Compose template |
| `scripts/` | `install.sh`, `channel.sh`, `make-fallback.sh`, `verify-stack.sh` |
| `spikes/` | Phase 0 harnesses and raw output behind the measured claims |
| `docs/` | Guides, architecture, and the frozen lane contracts |

## Documentation

| Document | Covers |
|----------|--------|
| [docs/quickstart.md](docs/quickstart.md) | Install through first live channel. **Start here** |
| [docs/operations.md](docs/operations.md) | Running it 24/7 — health, the watchdog, logs, the runbook |
| [docs/architecture.md](docs/architecture.md) | Container topology, data flow, why FFmpeg never restarts |
| [docs/scaling.md](docs/scaling.md) | Capacity planning and encoder ceilings |
| [docs/docker-deployment.md](docs/docker-deployment.md) | Ports, GPU passthrough, resource limits, secrets, the Docker socket |
| [docs/api-reference.md](docs/api-reference.md) | Every REST endpoint and SSE event, with examples |
| [docs/plugin-development.md](docs/plugin-development.md) | Writing a visualization plugin |
| [docs/visualization-filters.md](docs/visualization-filters.md) | The FFmpeg filters available and how they behave here |
| [docs/color-profiles.md](docs/color-profiles.md) | Profile schema, extraction, adding an extractor |
| [docs/contracts/](docs/contracts/README.md) | Frozen interfaces between lanes — the source of truth |

## Security notes

- `.env`, `channels/*/.env`, `ambient.yaml` and all media are gitignored. Never commit a stream
  key, an Icecast password, or a real hostname.
- **The backend mounts the Docker socket, which is root-equivalent on the host.** A loopback
  bind and a bearer token are the only things between the network and your host; both are
  required, and the process refuses to start without a token when bound anywhere else. It also
  drops privileges, which needs `/var/run/docker.sock` to be group-owned by something other
  than `root`. See [docs/docker-deployment.md](docs/docker-deployment.md#the-docker-socket).
- A stream key is held only by the relay, read from `channels/<name>/.env` when a path goes
  ready, and passed to FFmpeg through a preloaded argv shim so it never reaches `ps` or
  `docker inspect`. No composer container is given one.
- The ZMQ control socket binds to `127.0.0.1` inside the composer container and is never
  published. A malformed ZMQ message kills FFmpeg outright, so that socket is a kill surface,
  not a convenience.

## License

MIT
