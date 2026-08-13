# Quickstart

Zero to one channel streaming to YouTube Live. Every command here is complete and
copy-pasteable from the repository root.

At the end you will have three global containers, one channel with two containers, a continuous
RTMP push to YouTube, and an HLS preview.

**Read [§Before you start](#before-you-start) first.** Two of the constraints there will cost
you an afternoon if you find them later.

---

## What is built, and what is not

| | Status |
|---|---|
| Streaming path — Liquidsoap → Icecast → composer → MediaMTX → YouTube, plus HLS preview | **built, verified live** |
| `scripts/install.sh` — prerequisites, secrets, paths, port check, encoder probe, fallbacks | **built** |
| Control plane — FastAPI REST + SSE, supervisor, watchdog, scheduler, in its own container | **built** |
| Five visualization plugins, six preset packs | **built** |
| Operator web UI | files exist under `frontend/` and are baked into the backend image, but **the backend serves no static files** — see [§10](#10-the-operator-ui) |
| HLS preview proxy through the backend | **not implemented** — see [§9](#9-verify) |
| Icecast mount registration when a channel is created | **manual** — see [§7](#7-register-the-channels-icecast-mount-and-fallback) |

Several `config.yaml` settings do not reach the running compositor yet. See
[§11](#11-known-gaps-that-will-surprise-you) before you spend time tuning them.

---

## Before you start

### Host requirements

| | |
|---|---|
| OS | Linux with Docker Engine and the Compose **v2 plugin**. The standalone `docker-compose` v1 binary is not supported |
| CPU | ~1.5 cores free per 720p channel. See [scaling.md](scaling.md) |
| FFmpeg | `ffmpeg` **and** `ffprobe` on the host — the installer's encoder probe and the fallback encoder both need them |
| Other | `openssl`; and `ss`, if you want the installer to name whatever is holding a port |
| Docker socket | must **not** be group `root` — the backend drops privileges and reaches the daemon through the socket's own group |
| YouTube | one reusable stream key per channel, created by hand in YouTube Studio |

### Constraints that bite

- **Media must not live on `/mnt/c/...`.** On Docker Desktop/WSL2 that path is 9p-backed and
  far too slow for continuous reads. A CIFS or NFS mount fails the same way. Use a WSL2 ext4
  path or a Docker named volume.
- **`h264_qsv` cannot work on Docker Desktop or WSL2.** `/dev/dri` is not exposed there. It is
  a bare-metal-Linux profile only.
- **`h264_nvenc` works on WSL2** with nvidia-container-toolkit, but consumer NVIDIA cards cap
  concurrent encode sessions. Channels past the cap must run `libx264`.
- **Port 8090 is the only port published to the host.** If something already owns it, change
  `AMBIENT_BACKEND_PORT` before you start. On the reference host 8000, 8080 and 32400 were
  already taken by unrelated services — check first, do not assume. The installer refuses to
  continue on a conflict, and names the container or process holding the port.

### Media licensing — read this once

This is a public, continuous broadcast. Both halves of the media matter legally.

- **Music must be royalty-free or owned.** Anything else attracts Content ID claims, and on a
  24/7 stream a claim can mute or end the broadcast. Keep the license or purchase record for
  every track.
- **Images must be licensed for this use**, and any license requiring attribution needs that
  attribution where a viewer can see it — the video description at minimum, on screen if the
  license says so. CC-BY is not "free"; it is "free with a condition".

Nothing in this repository checks either. It is the operator's responsibility.

---

## 1. Run the installer

```bash
cd /path/to/ambient-streamer
scripts/install.sh
```

It is **idempotent** — every step checks for its own result first, and an existing secret is
never regenerated. Re-running it is the supported way to re-check a host after changing
something.

Seven steps, in order:

| Step | What it does |
|------|--------------|
| 1 prerequisites | `docker`, `docker compose`, `ffmpeg`, `ffprobe`, `openssl`; daemon reachable; warns if `/var/run/docker.sock` is group `root` |
| 2 configuration | creates `.env` (mode 600) and `ambient.yaml` from the examples; strips CRLF, which would otherwise put a trailing `\r` inside every password |
| 3 secrets | generates the three Icecast passwords and `AMBIENT_API_TOKEN` **only where empty**; records `AMBIENT_REPO_ROOT`; `chmod 600` on every `channels/*/.env` |
| 4 paths | creates `channels/`, `common/{audio,images,bumpers,fallback,profiles}` and the log directory; warns on 9p, NFS and CIFS |
| 5 ports | **fails** if the backend port is taken, naming the container or process holding it |
| 6 encoders | runs a **real short encode** per encoder and reports how many channels this host has room for |
| 7 fallback | encodes `common/fallback/default.mp3`, plus one per existing channel |

Useful flags:

```bash
scripts/install.sh --check            # probe only, write nothing
scripts/install.sh --skip-fallback    # skip the MP3 encoding
```

Secrets are never printed, never passed through `argv`, and never regenerated over an existing
value — rotating one means recreating the containers that hold it.

After it runs, review `.env` for the things it cannot decide for you: `PUID`/`PGID` (your own
`id -u` / `id -g`), `TZ`, and `AMBIENT_DEFAULT_ENCODER`. Full key-by-key reference:
[`contracts/config.md`](contracts/config.md).

## 2. Build the images, then re-probe

```bash
docker compose -p ambient build
scripts/install.sh --check
```

The second run matters. Until the composer image exists, the encoder probe uses the **host's**
FFmpeg, which is only an approximation. With the image built it probes inside
`ambient-composer:dev` with the device flags a channel will actually get — and it will tell you
when an encoder works on the host but not in the container, which is the case that matters.

## 3. Start the global stack

```bash
docker compose -p ambient up -d
docker compose -p ambient ps
```

Three global containers serve every channel:

| Container | Role |
|-----------|------|
| `ambient-icecast` | audio relay; one mount plus one fallback mount per channel |
| `ambient-mediamtx` | RTMP relay to YouTube, HLS preview server, and the per-channel YouTube publishers |
| `ambient-backend` | FastAPI control plane on `${AMBIENT_BIND_ADDRESS}:8090` |

Only the backend publishes a host port. RTMP 1935, HLS 8888, Icecast 8081 and the MediaMTX API
9997 stay on the internal `ambient` Docker network by design.

The fallback MP3s the installer encoded are **256 kbps / 44.1 kHz / stereo**, and those three
values are fixed by [`contracts/audio-transport.md`](contracts/audio-transport.md) — Icecast
splices a fallback into an already-open HTTP connection, so a different rate or channel count is
a format change mid-demux. `scripts/make-fallback.sh <channel> [source.wav]` encodes one by
hand, verifying the result with `ffprobe`.

Prove the audio transport before adding a channel. This brings the stack up, shows Icecast
serving a fallback mount with no source connected, and **always tears itself down again**:

```bash
scripts/verify-stack.sh
```

## 4. Create a channel

Channel names must match `^[a-z0-9][a-z0-9_-]{0,30}[a-z0-9]?$` — they become a directory name,
a Compose project name and an Icecast mount.

```bash
mkdir -p channels/lofi/{audio,images,profiles,bumpers}
cp channels/example/.env.example channels/lofi/.env && chmod 600 channels/lofi/.env
cp channels/example/config.yaml  channels/lofi/config.yaml
```

Edit `channels/lofi/config.yaml` and set `name: lofi`. Edit `channels/lofi/.env` and set the
mounts, which must be unique across every channel:

```
CHANNEL_MOUNT=/lofi
CHANNEL_FALLBACK_MOUNT=/lofi-fallback
```

Leave `YOUTUBE_STREAM_KEY` empty for now. A channel with an empty key runs normally and stays
off YouTube — the relay parks and re-checks every 60 s rather than hot-looping.

## 5. Add media

Two read-only trees feed a channel:

| Tree | Mounted at | Use it for |
|------|------------|------------|
| `common/audio`, `common/images` | `/media/common` | anything shared by several channels — stored once |
| `channels/lofi/audio`, `channels/lofi/images` | `/media/channel` | this channel only |

```bash
cp ~/music/*.mp3  channels/lofi/audio/
cp ~/pictures/*.jpg channels/lofi/images/
```

Being in `common/` makes a file *available* to a channel, not used by it. Selection is
per-channel, in `config.yaml`, and takes three mixable forms:

| Form | Meaning | Watched |
|------|---------|---------|
| `tracks: []` (or omitted) | everything under **this channel's** `audio/`, recursively. Does **not** pull in `common/` | yes |
| `- common/audio/intro.mp3` | exactly that file, in that position | no |
| `- common/audio/**` | glob; `*` is one level, `**` recurses, re-expanded as the folder changes | yes |

Explicit lists are deliberately not watched: you asked for exactly those files, and quietly
appending to a hand-curated playlist would be wrong. The watched forms are what make "drop a
file in and it appears" work with no restart.

Symlinking a channel folder into `common/` does not work — a bind mount carries only the
directory it is given, so the link resolves to a path the container cannot see. The selection
lists exist to avoid that. Full rules:
[`contracts/media-selection.md`](contracts/media-selection.md).

Optionally extract color profiles now, so the visualization tracks the artwork from the first
slide:

```bash
TOKEN=$(grep '^AMBIENT_API_TOKEN=' .env | cut -d= -f2)
curl -sS -X POST -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8090/api/media/profiles?channel=lofi"
```

Keep images **one directory level deep** under `images/`. Nested folders get a profile written
but the producer will not find it — see
[color-profiles.md](color-profiles.md#known-limitations).

## 6. Get a YouTube stream key

Per channel, by hand. This project never touches the YouTube Data API.

1. YouTube Studio → **Go Live**.
2. **Stream** tab.
3. **Select stream key** → create or pick a **reusable** key. A reusable key lets a restart
   resume the same broadcast instead of needing a new one.
4. Copy the key (format `xxxx-xxxx-xxxx-xxxx-xxxx`).

Paste it into `channels/lofi/.env`:

```
YOUTUBE_STREAM_KEY=xxxx-xxxx-xxxx-xxxx-xxxx
YOUTUBE_RTMP_URL=rtmp://a.rtmp.youtube.com/live2
```

Each channel needs its own key on its own YouTube channel. Two streams pushed to one key fight
over the broadcast.

That file is `chmod 600` and gitignored. The key is read only by the MediaMTX relay, only when
the channel's path goes ready, and reaches FFmpeg through a preloaded argv shim so it never
appears in `ps`, in `docker inspect`, or in a log line. No composer container is given it.

## 7. Register the channel's Icecast mount and fallback

Icecast needs a `<mount>` block per channel and its own fallback file.

**Registering the mount is manual and no tooling does it** — neither `install.sh` nor
`POST /api/channels` writes `channels/mounts.list`. The fallback MP3, on the other hand, is
encoded for you by a plain re-run of the installer, which walks every channel directory that
has a `.env`.

```bash
echo lofi >> channels/mounts.list
scripts/install.sh                     # encodes common/fallback/lofi.mp3
docker kill -s HUP ambient-icecast
```

`SIGHUP` re-renders the config and reloads Icecast in place. **Never restart Icecast** — that
takes every running channel's audio input with it.

Check it took:

```bash
docker logs --tail 5 ambient-icecast
```

Look for `rendered /run/icecast/icecast.xml with N channel mounts`.

Missing the per-channel fallback is survivable: the entrypoint falls back to `default.mp3` and
logs a warning. Missing the `mounts.list` line is not — Liquidsoap will be refused by Icecast
and the compositor will find nothing to read.

## 8. Compile and start

```bash
docker exec ambient-backend python -m ambient.compile lofi
scripts/channel.sh start lofi
```

The API does both in one call, and is the normal path once the stack is up:

```bash
curl -sS -X POST -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:8090/api/channels/lofi/start
```

To run the compiler on the host instead, `pip install -e backend` in a virtualenv gives you
`ambient-compile` and `python -m ambient.compile`.

`compile` writes three files into `channels/lofi/`:

| File | Contents |
|------|----------|
| `playlist.m3u` | resolved, ordered absolute in-container audio paths |
| `images.list` | resolved, ordered absolute in-container image paths |
| `docker-compose.yml` | rendered from `docker/compose.channel.yml.j2` — generated, never hand-edited |

It prints a warning for anything that resolved oddly — an empty selection, a missing stream
key, a plugin geometry problem — and exits non-zero if a channel could not be compiled.

**Always use `scripts/channel.sh` or the API, never bare `docker compose`.** Compose resolves
the implicit `.env` relative to the *Compose file's* directory, so a per-channel file in
`channels/lofi/` never sees the root `.env` — every `${ICECAST_SOURCE_PASSWORD}` resolves to
empty and the channel comes up mute against a relay that rejects it. Both files must be named,
root first, which is exactly what the script and the supervisor do:

```bash
docker compose --project-name ambient-lofi \
  --env-file .env --env-file channels/lofi/.env \
  --file channels/lofi/docker-compose.yml up -d
```

The first start builds `ambient-liquidsoap:dev` and `ambient-composer:dev` if
`docker compose -p ambient build` has not already done so.

## 9. Verify

Work down this list. Stop at the first thing that is wrong.

**Containers are up:**

```bash
scripts/channel.sh status lofi
```

Expect `lofi-liquidsoap` and `lofi-composer`, both `Up`.

**Liquidsoap reached Icecast:**

```bash
docker logs --tail 30 lofi-liquidsoap
```

**The compositor is encoding at real time.** This is the signal that matters:

```bash
docker exec lofi-composer tail -c 2000 /run/ambient/lofi/progress
```

A healthy 720p channel looks like this:

```
frame=12043
fps=30.0
bitrate=3085.4kbits/s
out_time=00:06:41.400000
drop_frames=0
dup_frames=0
speed=0.996x
progress=continue
```

`speed` at ~1.0x and `out_time` climbing in step with wallclock is health. See
[operations.md](operations.md) for what each failure looks like.

**The relay accepted the publish and spawned the YouTube leg:**

```bash
docker logs --tail 40 ambient-mediamtx
```

Expect `[path lofi] ... is publishing` and a `[publish lofi] publishing to
rtmp://a.rtmp.youtube.com/live2 (key withheld from argv)` line. If the key is still empty you
get `staying down; will re-check every 60s` instead — that is correct behavior, not a fault.

**YouTube Studio** reports **Excellent** stream health with no dropped frames. Path-ready to
publishing is measured at **207 ms**, so the ingest indicator should turn over quickly.

**The control plane agrees:**

```bash
curl -sS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8090/api/channels/lofi
```

**The HLS preview plays.** MediaMTX publishes no host port and the backend does not proxy it
yet, so check it from inside the network:

```bash
docker run --rm --network ambient ambient-composer:dev \
  ffprobe -v error -show_entries format=format_name -of default=nw=1 \
  http://mediamtx:8888/lofi/preview/index.m3u8
```

To open it in VLC from another machine, publish the relay's HLS port by setting
`AMBIENT_HLS_PUBLISH` in `.env`. `docker-compose.yml` maps
`${AMBIENT_HLS_BIND:-127.0.0.1}:${AMBIENT_HLS_PUBLISH:-8888}:8888`, so no override file is
needed — one existed in an older version of this guide and now collides with the real mapping.

```bash
# .env
AMBIENT_HLS_PUBLISH=8888
AMBIENT_HLS_BIND=127.0.0.1     # 0.0.0.0 to reach it from the LAN
```

```bash
docker compose -p ambient up -d mediamtx
```

Then open `http://127.0.0.1:8888/lofi/preview/index.m3u8`, reaching it over an SSH tunnel from
another machine. The operator UI shows this address under the preview pane, and the channel
detail returns it as `hls_url` — `null` when the port is not published.

Keep it on loopback unless you mean otherwise: the relay's authentication is an IP allow-list
with an empty password, not a real credential, and the preview is unauthenticated.

## 10. The operator UI

`frontend/` holds a complete plain-HTML/CSS/JS operator UI — no npm, no build step, `hls.js`
vendored as a single file — and the backend image bakes it in at `/opt/ambient/frontend`, with
`AMBIENT_FRONTEND_DIR` pointing at it.

The backend serves it: open `http://127.0.0.1:8090/`. It also proxies each channel's preview at
`/<channel>/preview/index.m3u8`, so the preview pane works without a second web server.

That proxy requires the bearer token, which an external player cannot send. For VLC, use the
relay's own port instead — the address is shown under the preview pane, and the channel detail
returns it as `hls_url`. It exists only when `AMBIENT_HLS_PUBLISH` is set; see §9.

The SSE stream is useful on its own:

```bash
curl -sS -N -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8090/api/events
```

The backend refuses to start if `AMBIENT_API_TOKEN` is unset while bound to anything other than
loopback, because it drives Docker and is therefore root-equivalent on the host. Read the token
with `grep '^AMBIENT_API_TOKEN=' .env`.

## 11. Known gaps that will surprise you

These are real and current. Everything listed here was verified against the tree; the rest of
the configuration does reach the compositor.

| You set | What actually happens |
|---------|-----------------------|
| `CHANNEL_FALLBACK_MOUNT` | Validated, then unused. Icecast derives the fallback from the channel name in `mounts.list` |
| `POST /api/channels` | Creates the directory, `config.yaml` and `.env`, but does **not** add the Icecast mount or generate a fallback file. Step 7 is still manual |
| `bumpers.*` and `POST .../bumpers/generate` | The model, the API and `docs/contracts/bumpers.md` all exist; **nothing in the media pipeline reads any of it**. No Liquidsoap operator, no TTS container. Setting `bumpers.enabled` with unresolvable sources will fail the channel's config load for a feature that cannot run |
| `visualization.visible: false` | Takes the visualization off air in one frame, but the branches keep rendering and keep costing. Only `visualization.enabled: false` gives the cores back, and that needs a restart |
| `images.hold_seconds`, `images.fade_seconds` | Reach the producer as launch-time environment. Changing them on a running channel does nothing until it restarts |
| `VIZ_OPACITY` | A real knob in the filtergraph (`lut@vizop`), pinned to 0.65. No config field and no API reach it |

`audio.tracks`, `images.slides`, `audio.shuffle`, `audio.crossfade_seconds` and the encoder
selection **do** take effect — they travel through the generated `playlist.m3u`, `images.list`
and the rendered Compose file.

Verify what a channel is really running:

```bash
docker exec lofi-composer cat /run/ambient/lofi/filtergraph.txt
```

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| Installer: `docker.sock is group root` | the backend cannot drop privileges and still reach the daemon | give the socket a non-root group, or run the backend as root and accept that the API handler shares the socket's uid |
| Installer: `refusing to continue with a port conflict` | something else owns 8090 | free it, or change `AMBIENT_BACKEND_PORT` and re-run |
| Backend exits: `AMBIENT_ROOT is not present in this container` | `AMBIENT_REPO_ROOT` does not match where the repository actually is | re-run `scripts/install.sh`, then `docker compose -p ambient up -d backend` |
| Channels start with empty media directories | same cause — the daemon resolves bind sources on the host | as above |
| `the 'ambient' network is missing` | global stack is down | `docker compose -p ambient up -d` |
| Channel starts, stream is silent | root `.env` was not passed to Compose | use `scripts/channel.sh` or the API, never bare `docker compose` |
| `Error opening input files: Connection refused` at composer start | Icecast mount not registered | step 7, then `docker kill -s HUP ambient-icecast` |
| Composer exits immediately, `HOT_SET is empty` | environment override set to an empty string | unset it; the default is `showfreqs-bars` |
| Composer dies with `plugin 'x' does not derive its size from ${WIDTH}x${HEIGHT}` | plugin fragment uses a literal size | see [plugin-development.md](plugin-development.md) |
| Composer dies with `requires filter 'x', absent from this build` | plugin needs a filter this FFmpeg lacks | rebuild the composer image, or drop the plugin from `hot_set` |
| `[publish lofi] staying down` | `YOUTUBE_STREAM_KEY` empty | paste the key; the relay re-checks within 60 s, no restart needed |
| YouTube shows the stream then drops it after ~15 s | publisher probe shorter than the composer GOP | leave `AMBIENT_PUBLISH_ANALYZEDURATION` at its 3 s default |
| `speed` well below 1.0x | host is oversubscribed | [scaling.md](scaling.md) |
| Everything looks right, YouTube says no data | channel key belongs to a different YouTube channel | re-copy from Studio |
| `http://127.0.0.1:8090/` returns 404 | the UI is not served yet | [§10](#10-the-operator-ui) |

## Where to go next

| Document | Covers |
|----------|--------|
| [operations.md](operations.md) | running it 24/7, the watchdog, logs, what a healthy stream looks like |
| [architecture.md](architecture.md) | why FFmpeg never restarts, and what each escape hatch costs |
| [scaling.md](scaling.md) | how many channels this host will hold |
| [docker-deployment.md](docker-deployment.md) | ports, GPU passthrough, resource limits, the Docker socket |
| [api-reference.md](api-reference.md) | every REST endpoint and SSE event |
| [plugin-development.md](plugin-development.md) | writing a visualization plugin |
| [visualization-filters.md](visualization-filters.md) | the filters available and how they behave here |
| [color-profiles.md](color-profiles.md) | palettes, extraction, and how color reaches the stream |
| [`contracts/`](contracts/README.md) | the frozen interfaces — the source of truth |
