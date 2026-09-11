# Docker deployment

How the containers are laid out, what is published where, how to pass a GPU through, and what
the Docker socket means for your host's security.

For a first install, follow [quickstart.md](quickstart.md). This document is the reference.

---

## Topology

Two containers per channel, plus three global containers. At the supported maximum of 8
channels that is **19 containers**, not 40.

| Scope | Container | Image | Role |
|-------|-----------|-------|------|
| Global | `ambient-icecast` | `libretime/icecast:2.4.4` | audio relay; one mount + one fallback mount per channel |
| Global | `ambient-mediamtx` | built from `docker/Dockerfile.mediamtx` | RTMP relay to YouTube, HLS preview server, and the per-channel YouTube publisher processes |
| Global | `ambient-backend` | built from `docker/Dockerfile.backend` | FastAPI control plane |
| Per channel | `<ch>-liquidsoap` | built from `docker/Dockerfile.liquidsoap` | playlist, crossfade, loudness; Icecast source client |
| Per channel | `<ch>-composer` | built from `docker/Dockerfile.composer` | slideshow producer + FFmpeg compositor and encoder |

| Channels | Per-channel | Global | Total |
|----------|-------------|--------|-------|
| 1 | 2 | 3 | 5 |
| 4 | 8 | 3 | 11 |
| 8 | 16 | 3 | 19 |

The YouTube publisher is **a process inside `ambient-mediamtx`**, spawned per channel by
`runOnReady`, not a container. The counts above hold whether or not a channel is live.

### Compose projects

| Project | File | Brought up by |
|---------|------|---------------|
| `ambient` | `docker-compose.yml` | `docker compose -p ambient up -d --build` |
| `ambient-<channel>` | `channels/<channel>/docker-compose.yml` (generated) | `scripts/channel.sh start <channel>` |
| `ambient-<channel>-next` | the same file, with a container-name override | the supervisor, during a make-before-break restart |

The `ambient` bridge network is created by the global project with a **fixed name**. Channel
projects join it as `external: true` and never create it — which is why the global stack must be
up before any channel starts.

### The two-env-file rule

`docker compose` resolves the implicit `.env` relative to the **Compose file's** directory. A
per-channel Compose file lives in `channels/<name>/`, so Compose finds only that channel's
`.env` and never the root one. Every `${ICECAST_SOURCE_PASSWORD}` then resolves to empty and the
channel comes up mute against a relay that rejects it.

Both files must be named explicitly, **root first** so the channel file wins on shared keys:

```bash
docker compose --project-name ambient-lofi \
  --env-file .env --env-file channels/lofi/.env \
  --file channels/lofi/docker-compose.yml up -d
```

`scripts/channel.sh` and the backend supervisor both do exactly this. Use one of them.

---

## Port allocation

**Only the backend is published to the host.** Everything else stays on the internal `ambient`
network — keeping the relay and the ZMQ control sockets off the LAN is the point.

| Port | Service | Published to host | Configurable via |
|------|---------|-------------------|------------------|
| **8090** | backend REST + SSE + UI | **yes**, bound to `AMBIENT_BIND_ADDRESS` (default `127.0.0.1`) | `AMBIENT_BACKEND_PORT` |
| 8081 | Icecast HTTP | no | `AMBIENT_ICECAST_PORT` |
| 1935 | MediaMTX RTMP ingest | no | `AMBIENT_RTMP_PORT` |
| 8888 | MediaMTX HLS | no | `AMBIENT_HLS_PORT` |
| 9997 | MediaMTX control API | no | fixed |
| 1234 | Liquidsoap telnet | no — container-internal only | `LIQ_TELNET_PORT` |
| 5555 | ZMQ filter control | no — bound to `127.0.0.1` **inside** the composer's namespace | `ZMQ_BIND_HOST` / `ZMQ_BIND_PORT` |

### Port conflicts are a real hazard

8090 is the one port you must have free. Check before you install, and do not assume the
obvious ones are available — on the reference host **8000, 8080 and 32400 were already taken**
by unrelated services, which is why 8090 was chosen in the first place.

```bash
ss -ltnp | grep -E ':(8090|8081|1935|8888|9997)\b'
```

The internal ports only need to be free *inside* the Compose network, so a host process on 1935
does not conflict. Change them only if they collide with something else on that network.

### Exposing the HLS preview

`AMBIENT_HLS_PUBLISH` is checked for conflicts by `scripts/install.sh` when it is set, but
**nothing publishes it** — `docker-compose.yml` maps no MediaMTX ports at all. To reach the
preview from another machine, add an override file:

```yaml
# docker-compose.hls.yml
services:
  mediamtx:
    ports:
      - "127.0.0.1:8888:8888"
```

```bash
docker compose -p ambient -f docker-compose.yml -f docker-compose.hls.yml up -d
```

Keep it on loopback and reach it over SSH port-forwarding. MediaMTX's authentication is an IP
allow-list covering loopback and RFC1918 ranges with an empty password — it is a second layer,
not a first.

---

## Volumes and mounts

| Host | Container | Mode | Notes |
|------|-----------|------|-------|
| `common/` | `/media/common` | ro | shared media library |
| `channels/<name>/` | `/media/channel` | ro | that channel only |
| `${AMBIENT_LOG_DIR}/<name>/` | `/var/log/ambient` | rw | the channel name **is** the mount point, which is what lets one image serve every channel |
| `ffmpeg/` | `/opt/ambient` | ro | composer entrypoint + producer |
| `liquidsoap/` | `/opt/ambient` | ro | Liquidsoap script |
| `plugins/` | `/plugins` | ro | mounted at the root, not under `/opt/ambient` — a read-only mount cannot create a nested mountpoint |
| tmpfs | `/run/ambient` | rw | runtime state, 64 MB composer / 16 MB Liquidsoap |
| `channels/` | `/etc/ambient/channels` | ro | into **Icecast** (`mounts.list`) and **MediaMTX** (stream keys) |
| `common/fallback/` | `/usr/share/icecast/web/fallback` | ro | fallback MP3s |
| `/var/run/docker.sock` | `/var/run/docker.sock` | rw | **backend only** — see [§Docker socket](#the-docker-socket) |
| `${AMBIENT_REPO_ROOT}` | **the same absolute path** | rw | backend only — see below |

**The backend mounts the repository at its own host path, not at `/app`.** It renders
`channels/<name>/docker-compose.yml` with bind sources in it, and the Docker daemon resolves a
bind source **on the host** — so the path the backend writes and the path the daemon reads have
to be the same string. `scripts/install.sh` records `AMBIENT_REPO_ROOT` in `.env` for this.
A mismatch does not fail loudly; it produces channels that start with empty media directories.
The entrypoint checks for the directory and for `docker/compose.channel.yml.j2` inside it, and
refuses to start if either is missing.

That mount is read-write because the supervisor writes generated Compose files and the media
resolver writes `playlist.m3u` and `images.list`.

**Media is mounted read-only on purpose.** Nothing in the streaming path should ever write to
it, and enforcing that at the mount catches the mistake early.

**Media must be on a local filesystem.** A CIFS or NFS mount, or a Windows drive under
`/mnt/c` on Docker Desktop, adds a network or 9p round trip to every read and will stall a 24/7
stream. Use a WSL2 ext4 path or a Docker named volume.

Set `PUID`/`PGID` to your own uid/gid. The channel containers start as root only long enough to
align the container user, then drop privileges with `gosu`, so files written to the log mount
stay editable on the host. Icecast runs as `PUID:PGID` from the start.

---

## Images and pinning

| Image | Source | Pinning |
|-------|--------|---------|
| `libretime/icecast:2.4.4` | pulled | tag-pinned |
| `ambient-mediamtx:dev` | built `FROM bluenviron/mediamtx:1.9.3-ffmpeg` | **version-pinned and load-bearing** |
| `ambient-backend:dev` | built from digest-pinned `python:3.12-slim-bookworm`, Docker CLI copied from digest-pinned `docker:29.8.0-cli` | GHCR release digest in production |
| `ambient-composer:dev` | built `FROM ubuntu:24.04` | pin by digest in production |
| `ambient-liquidsoap:dev` | built from digest-pinned `savonet/liquidsoap:v2.4.5` | GHCR release digest; revalidate S1 failover before deploying |

**MediaMTX refuses to start on an unknown config key**, so bumping the relay image without
re-reading its release notes turns a routine upgrade into an outage. 1.9.3 is what
`docker/mediamtx.yml` was written against.

The composer base is Ubuntu 24.04 because its distro FFmpeg 6.1.1 is built with
`--enable-libzmq`, which is the entire live-control mechanism. The build **hard-fails** if the
base ever ships an FFmpeg without libzmq, libx264, or any of `zmq`, `azmq`, `streamselect`,
`astreamselect`, `showfreqs`, `showwaves`, `avectorscope`. Silent loss of libzmq would present
as "color commands stopped working" months later.

`ubuntu:24.04` is a moving tag. For a 24/7 service, pin the digest:

```bash
docker build --build-arg BASE=ubuntu:24.04@sha256:<digest> \
  -f docker/Dockerfile.composer -t ambient-composer:dev .
```

Override image names with `AMBIENT_MEDIAMTX_IMAGE`, `AMBIENT_BACKEND_IMAGE`,
`AMBIENT_LIQUIDSOAP_IMAGE`, or the per-channel template's local-development `image_tag`.
Tagged releases publish immutable backend and Liquidsoap image digests; see
[releases.md](releases.md).

### Pending relay migrations

MediaMTX 1.21.0 and Icecast 2.5.0 are intentionally not routine dependency bumps. Replacing
either global relay disconnects active clients, and the current measurements belong to
MediaMTX 1.9.3 and Icecast 2.4.4.

Before moving MediaMTX to 1.21.0:

- migrate `hlsAllowOrigin` to `hlsAllowOrigins` and `runOnReady*` to `runOnAvailable*`;
- make backend status handling accept `online`/`available` without depending on deprecated
  `ready` fields, and update restart-gap log parsing;
- validate the checked-in config with the target image, then exercise RTMP publishing, HLS
  playback, the YouTube publisher hook, API status, and publisher override;
- prove whether replacing the relay leaves every composer FFmpeg PID running, and measure the
  resulting YouTube ingest-session gap before production deployment.

Before moving Icecast to `libretime/icecast:2.5.0-debian`:

- remove the rejected `icecast -n` option and replace numeric `fallback-override` with `all`;
- smoke-test the rendered config, arbitrary PUID/PGID startup, source authentication, fallback
  MP3 format, and SIGHUP mount reload against the target image;
- repeat the S1 source-stop/restart test under representative channel load and require 0 s
  compositor-output interruption, 0.00% captured silence, and unchanged compositor PIDs;
- measure the separate outage caused by replacing the single global Icecast container. The
  current topology cannot describe that image cutover itself as zero-gap.

---

## Resource limits

Set per channel in `channels/<name>/.env`, applied through `deploy.resources.limits` in the
generated Compose file.

| Key | Default | Applies to |
|-----|---------|------------|
| `CHANNEL_CPU_LIMIT` | `2.0` | composer |
| `CHANNEL_MEMORY_LIMIT` | `2g` | composer |
| `LIQUIDSOAP_CPU_LIMIT` | `0.5` | Liquidsoap |
| `LIQUIDSOAP_MEMORY_LIMIT` | `512m` | Liquidsoap |
| `BACKEND_CPU_LIMIT` | `1.0` | the control plane — root `.env` |
| `BACKEND_MEMORY_LIMIT` | `1g` | the control plane — root `.env` |

The control plane is capped because it forks `docker compose`, which is the memory-hungry part,
and it must never be what starves an encoder.

These are real requirements, not decoration. Eight channels on one host means the host must
refuse to oversubscribe itself into a sub-1.0× stream, and one misbehaving channel must not
take down the other seven.

A composer measured at ~1.4 cores against a 2.0 limit has headroom for a transient; against a
1.5 limit it does not. Size the limit above the measured steady state, not at it — see
[scaling.md](scaling.md).

Channel containers use `restart: unless-stopped` and `json-file` logging capped at
`max-size: 10m`, `max-file: 5` — 50 MB per container. A 24/7 compositor at default FFmpeg
verbosity will otherwise fill a disk.

---

## GPU passthrough

### NVENC

Requires an NVIDIA GPU, the NVIDIA driver, and `nvidia-container-toolkit` on the host. Works on
WSL2.

Set the channel's encoder to `h264_nvenc`. The template then renders:

```yaml
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              device_ids: ["${NVIDIA_DEVICE_ID:-0}"]
              capabilities: [gpu, video]
```

`capabilities: [gpu, video]` — **`video` is required for NVENC**; `gpu` alone gives you CUDA
without the encoder. Pin `NVIDIA_DEVICE_ID` per channel by index or UUID so channels do not all
land on card 0:

```bash
nvidia-smi --query-gpu=index,uuid,name --format=csv
```

Verify the toolkit before configuring a channel:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

**Consumer GeForce cards cap concurrent NVENC sessions** (historically 3–8 depending on driver
generation). Channels beyond the cap fall back to `libx264` automatically rather than failing to
start, and report the substitution as `encoder` ≠ `encoder_requested`. A host with more channels
than NVENC sessions is a normal, supported configuration — it just needs CPU headroom for the
overflow.

The **HLS preview always uses `libx264`**, whatever the channel encoder. Spending an NVENC
session on an operator preview costs a whole channel, and mixing encoders inside one FFmpeg
process is legal.

### QSV

**`h264_qsv` cannot work on Docker Desktop or WSL2.** `/dev/dri` is not exposed there, so the
device the encoder needs does not exist inside the container. This is a hard platform
limitation, not a configuration problem to work around.

On bare-metal Linux with an Intel iGPU, the template renders:

```yaml
    devices:
      - /dev/dri:/dev/dri
```

The container user must be able to read the render node — add `PGID` to the host's `render`
group, or `chgrp` the device appropriately.

### Encoder selection is probed, never assumed

Availability is decided by running a **short real encode** (15 frames of black), not by reading
`ffmpeg -encoders`. The list is not evidence: on both hosts tested it advertised `h264_qsv`
with no Intel device present.

A channel never fails to start because its configured encoder is absent. It degrades to the
fallback and reports the substitution through `GET /api/system` and `encoder_requested`.

---

## Secrets

| Secret | Lives in | Reaches |
|--------|----------|---------|
| YouTube stream key | `channels/<name>/.env`, `0600`, gitignored | **only** `ambient-mediamtx`, read at path-ready time |
| Icecast source password | root `.env` | Liquidsoap containers as an environment variable — a source client needs it at connect time |
| Icecast admin/relay passwords | root `.env` | the Icecast entrypoint only |
| `AMBIENT_API_TOKEN` | root `.env` | the backend only |

All four are generated by `scripts/install.sh`, **only where empty** — an existing secret is
never regenerated, which is what makes the installer safe to re-run on a live host. Rotating
one means recreating the containers that hold it.

The installer also strips CRLF from `.env`. A file edited on Windows otherwise carries a
trailing `\r` into every value, and Compose interpolates that verbatim into a password.

### How a stream key stays out of sight

1. The per-channel Compose template deliberately has **no `env_file`**. Loading
   `channels/<name>/.env` wholesale would put the key in `docker inspect` for both channel
   containers. It is passed with `--env-file` for *interpolation* only, and just the named
   values reach a container.
2. **No composer container is given a key.** The composer publishes to the relay and never to
   YouTube.
3. The relay reads `YOUTUBE_STREAM_KEY` out of the read-only `channels/` bind mount when a path
   goes ready, without sourcing the file — a `.env` is data, not code.
4. The key is staged in a `0700` tmpfs directory as a `0600` file and reaches FFmpeg through
   `docker/argv-shim.c`, preloaded with `LD_PRELOAD`, so it never appears in `argv`, `ps`,
   `docker inspect`, or a log line. The publisher refuses to run if the shim is missing.

A consequence worth knowing: **adding a channel, or filling in a key that was left blank, needs
no relay restart.** A channel with an empty key parks and re-checks every 60 s rather than
hot-looping.

### The ZMQ socket is a kill surface

A malformed ZMQ message causes heap corruption in FFmpeg's `f_zmq.c` and aborts the encoder
with exit 134. The filter's default `bind_address` is `tcp://*:5555` — every interface, no
authentication.

The composer therefore binds `127.0.0.1:5555` **inside its own network namespace**, and the port
is never published. The backend cannot reach it directly; it delivers commands with
`docker exec`. Every message is validated before it is sent. That validator is a security
boundary, not a convenience wrapper.

### Never commit

`.env`, `channels/*/.env`, `ambient.yaml`, `channels/*/config.yaml`, all media, and all
generated files are gitignored. A stream key grants publish rights to a YouTube channel.

---

## The Docker socket

> **The backend mounts `/var/run/docker.sock`. That makes it root-equivalent on the host.**

Anyone who can reach the API can start a container, and a container can be started with the
host filesystem mounted. There is no sandbox between "reached the API" and "root on the box".

Two things stand between the network and your host, and **both** are required:

| Control | Enforcement |
|---------|-------------|
| **Loopback bind** | `AMBIENT_BIND_ADDRESS` defaults to `127.0.0.1` and is the **host** side of the port mapping. The process inside the container listens on `0.0.0.0`; the mapping is what decides who can reach it |
| **Bearer token** | every endpoint except `GET /api/health`; constant-time comparison; the process **refuses to start** if the token is unset while bound to anything but loopback |

A third control reduces the blast radius rather than the reachability: **the backend drops
privileges.** Its entrypoint runs as root only long enough to align the container user with
`PUID`/`PGID`, join the Docker socket's group, and prove the daemon answers *as that
unprivileged user* — then `exec`s the app under it.

That has a hard prerequisite: **`/var/run/docker.sock` must not be group `root`.** If it is,
the entrypoint refuses to start rather than silently running the API handler as root.
`scripts/install.sh` warns about this in step 1. Your options are to give the socket a
non-root group, or to run the container with `user: root` and accept that a bug in a request
handler shares the socket's uid.

Note that group membership on the Docker socket is itself root-equivalent. Dropping privileges
means a compromised handler is not *directly* root; it does not mean it cannot become root.

Guidance:

- **Do not bind to `0.0.0.0`** to "make it reachable". Use an SSH tunnel:
  `ssh -N -L 8090:127.0.0.1:8090 user@host`.
- If you must expose it, put a TLS-terminating reverse proxy with its own authentication in
  front, keep the backend on loopback, and keep the bearer token as well.
- Treat the token like a root password. Rotate it by editing `.env` and restarting the backend.
- Channel names reach both Docker and the filesystem. Every Docker-touching endpoint validates
  its input against `^[a-z0-9](?:[a-z0-9_-]{0,30}[a-z0-9])?$` and resolves the directory before
  use. Nothing is passed through a shell — every invocation is argv.

If your threat model does not tolerate a root-equivalent HTTP service, do not run the backend.
The shell path (`ambient.compile` + `scripts/channel.sh`) does everything the API does, under
your own credentials.

---

## Running the backend

The backend is a normal member of the `ambient` project — `docker compose -p ambient up -d`
starts it alongside Icecast and MediaMTX.

`docker/Dockerfile.backend` is built from digest-pinned `python:3.12-slim-bookworm` and copies the Docker CLI
and the Compose plugin out of a pinned `docker:*-cli` image, because the supervisor shells out
to `docker compose` and does not speak the socket API. `docker compose version` is run **at
build time**, so a broken CLI is a build failure rather than a channel that will not start at
3am. The image also bakes `frontend/` in at `/opt/ambient/frontend`.

It has a healthcheck that polls `/api/health` with Python's `urllib` — there is no `curl` in
the image, and adding one for a healthcheck is a poor trade.

Three environment values are mandatory and the stack will not start without them:

| Key | Why |
|-----|-----|
| `AMBIENT_API_TOKEN` | root-equivalent surface; the app refuses to start unset off loopback |
| `AMBIENT_REPO_ROOT` | the repository's absolute **host** path; mounted at the identical path inside |
| `AMBIENT_LOG_DIR` | must be absolute; bind-mounted at the same path on both sides |

All three are written by `scripts/install.sh`.

### Running it on the host instead

Supported, and useful for development:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r backend/requirements.lock
pip install --no-build-isolation --no-deps -e backend
export AMBIENT_API_TOKEN=<from .env>
ambient-backend --repo-root /srv/ambient
```

The same requirement applies: the `docker` CLI with the Compose v2 plugin must be on its
`PATH`. Installing the local package also provides `ambient-compile`.

A systemd unit, which this repository does not ship:

```ini
# /etc/systemd/system/ambient-backend.service
[Unit]
Description=ambient-streamer control plane
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=ambient
WorkingDirectory=/srv/ambient
EnvironmentFile=/srv/ambient/.env
ExecStart=/srv/ambient/.venv/bin/ambient-backend --repo-root /srv/ambient
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

The `ambient` user must be in the `docker` group, which is itself root-equivalent. Keep that
account's credentials as tightly held as root's.

### The UI is not served

The image ships `frontend/` and sets `AMBIENT_FRONTEND_DIR`, but **the application mounts no
static files** and does not proxy `/<channel>/preview/index.m3u8`. `http://<host>:8090/`
returns a `404`. Until both land, the UI is inert; drive the system through the API or the
shell.

---

## Day-two operations

| Action | Command | Cost |
|--------|---------|------|
| Re-check the host after any change | `scripts/install.sh --check` | none — writes nothing |
| Add an Icecast mount | `echo <ch> >> channels/mounts.list && docker kill -s HUP ambient-icecast` | none — **never restart Icecast**, it takes every channel's audio with it |
| Fill in a stream key | edit `channels/<ch>/.env` | none — the relay reads it at path-ready time |
| Change media or playlist | recompile the channel | none — watched paths update live |
| Change color or plugin | API, or a ZMQ command | one frame |
| Change `hot_set`, resolution, fps, encoder | `POST /api/channels/<ch>/restart` | seconds, make-before-break; see on-disk.md |
| Blunt restart | `scripts/channel.sh restart <ch>` (stop-then-start) | ~13.7 s of YouTube outage, and a new ingest session |
| Upgrade a channel image | rebuild, then `POST .../restart` per channel, one at a time | as above, per channel |
| Upgrade MediaMTX | read the release notes for renamed config keys **first** | brief outage on every channel |

`scripts/channel.sh` scopes every command to that channel's own Compose project. It cannot
reach the global stack or another channel. `scripts/verify-stack.sh` brings the global stack up,
proves Icecast serves a fallback mount with no source connected, and always tears itself down
again — its cleanup trap is not optional on a shared host.

---

## Related

| Document | Covers |
|----------|--------|
| [quickstart.md](quickstart.md) | first install, step by step |
| [scaling.md](scaling.md) | how many channels this host holds |
| [operations.md](operations.md) | health, logs, and failure recovery |
| [architecture.md](architecture.md) | why the topology is what it is |
| [`contracts/config.md`](contracts/config.md) | every `.env` and YAML key |
| [`contracts/on-disk.md`](contracts/on-disk.md) | mounts, runtime state, log paths |
