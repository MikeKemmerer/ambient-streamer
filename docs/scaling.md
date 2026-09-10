# Scaling and capacity planning

How many channels a host will actually run, why the number is lower than a filtergraph
benchmark suggests, and what to check before adding one more.

---

## The headline number

> **Measured on a running 7-core host: about 1.5 cores per 720p channel with one hot
> visualization plugin.**
>
> Reserving about one core for the OS and the shared services, that host runs
> **about four channels, not five.**

Per-process, at steady state:

| Process | Scope | Measured |
|---------|-------|----------|
| `<ch>-composer` | per channel | **137–142 % of a core** |
| `<ch>-liquidsoap` | per channel | ~6 % of a core |
| `ambient-mediamtx` | global, includes every YouTube publisher | ~7 % of a core |
| `ambient-icecast` | global | ~0.2 % of a core |

Steady state on the reference channel, from FFmpeg `-progress`:

```
speed=0.996x   bitrate=3085kbits/s   drop_frames=0   dup_frames=0
```

That configuration is 1280×720 at 30 fps, `libx264`, one hot plugin (`showfreqs-bars`), plus
the 640×360 HLS preview.

---

## Why 1.5 and not 0.25

A filtergraph benchmark measures the visualization branch. A channel is much more than that.

| Cost | Counted by a filter benchmark? |
|------|-------------------------------|
| Visualization branch (one, idle or active) | yes — ~0.24 cores at 720p30 |
| Slideshow crossfade rendering and JPEG **encode**, in the producer | no |
| JPEG **decode** off the `image2pipe` input | no |
| MP3 decode from Icecast | no |
| `blend`, `split`, `scale`, `eq`, `hue`, `format` on every frame | no |
| **720p program encode** (libx264 CBR) | no |
| **360p preview encode — a second, complete encode chain** | no |
| AAC encode, twice | no |

**The preview is the big one.** MediaMTX does not transcode, so the low-resolution operator feed
is a second output from the compositor, not a free tap off the program encode. It is paid
continuously, whether or not anyone is watching.

**Any capacity estimate that does not count the preview encode is wrong.** Budget from the
measured 1.5, never from the 0.24 in a plugin manifest.

`GET /api/capacity` reports `projected_cores` alongside `measured_cores` for exactly this
reason. `projected_cores` is the sum of the hot plugins' declared costs and nothing else — on
the reference channel it reads 0.24 against a measured 1.49. **Use `measured_cores`.**

---

## Terms that scale the number

| Change | Effect |
|--------|--------|
| **Each additional hot plugin** | **+0.28 cores at 720p**, whether or not it is on screen. `hot_set` is a CPU budget, not a preference list |
| **1080p instead of 720p** | more on *both* encodes, ~1.9× on the visualization branch, and the bitrate ladder rises from 3000k to 5000k |
| **Higher frame rate** | roughly linear on the filter branches; ≥ 50 fps also multiplies the video bitrate by 1.5 in the ladder |
| **NVENC on the program encode** | moves the 720p encode off the CPU; the preview stays on `libx264` deliberately |
| **Longer crossfades / higher producer fps** | more JPEG encode in the producer. 10 fps with a 2.0 s fade costs about +14 % of a core at 720p |

A rough model for planning, all figures at 720p30 with `libx264`:

```
channel_cores ≈ 1.5 + 0.28 × (hot_plugins − 1)
host_channels ≈ floor((total_cores − reserved_cores) / channel_cores)
```

| Host | Reserved | 1 plugin | 3 plugins |
|------|----------|----------|-----------|
| 4 cores | 1.0 | 2 channels | 1 channel |
| 7 cores | 1.0 | **4 channels** *(measured)* | 3 channels |
| 12 cores | 1.0 | 7 channels | 5 channels |
| 16 cores | 1.5 | 8 channels *(hits `max_channels`)* | 7 channels |

`limits.max_channels` defaults to **8** and `limits.reserved_cores` to **1.0**, both in
`ambient.yaml`. The supported range is 1–8 channels. Nothing above 8 has been tested.

---

## Other resources

### Memory

Per-channel Compose limits default to `2g` for the composer and `512m` for Liquidsoap. Those are
ceilings, not observed usage. Budget conservatively:

| | Per channel | 8 channels |
|---|---|---|
| Compose limits | 2.5 GB | 20 GB |
| Global containers | — | ~1 GB |

The composer's `/run/ambient` tmpfs is 64 MB per channel and counts against host RAM. HLS
segments are held in memory too — `hlsDirectory: ''` is deliberate, because a 24/7 preview must
not grind on disk. Six 2-second segments per channel is a small, bounded amount.

### Network

Only the **YouTube leg** leaves the host. Composer → relay RTMP and relay → HLS are internal.

Computed from the shipped bitrate ladder, not measured:

| Channel | Video | Audio | Upstream |
|---------|-------|-------|----------|
| 480p | 1500 kbps | 128 kbps | ~1.6 Mbps |
| **720p** | **3000 kbps** | **128 kbps** | **~3.1 Mbps** |
| 1080p | 5000 kbps | 192 kbps | ~5.2 Mbps |

The reference channel's measured 3085–3137 kbits/s matches the 720p row.

Four 720p channels need about **12.5 Mbps sustained upstream**; eight need about **25 Mbps**.
CBR means sustained, not peak — size the link for the sum, with headroom. A saturated uplink
looks exactly like a starving encoder from YouTube's side.

### Disk

| Consumer | Size |
|----------|------|
| Docker `json-file` logs, per channel container | 10 MB × 5 = 50 MB, so 100 MB per channel |
| Host logs under `${AMBIENT_LOG_DIR}/<channel>/` | **unbounded — rotate them yourself** |
| Media | as large as your library |
| HLS segments | memory, not disk |
| Recording | none. MediaMTX has `record: no`; eight 24/7 channels would fill any disk |

Container log rotation is configured. Host-side rotation is not — add a `logrotate` rule for
`${AMBIENT_LOG_DIR}/*/*.log` before you leave a host unattended for months.

---

## Encoder ceilings

| Encoder | Ceiling | Behavior past it |
|---------|---------|-------------------|
| `libx264` | CPU only — the model above | encoder speed drops below 1.0×, YouTube starves |
| `h264_nvenc` | **consumer GeForce cards cap concurrent encode sessions** | channels past the cap fall back to `libx264` automatically and report the substitution |
| `h264_qsv` | needs `/dev/dri`; **impossible on Docker Desktop/WSL2** | probe fails, channel starts on `libx264` |

Three consequences for planning:

1. **A host with more channels than NVENC sessions is a normal configuration.** It just needs
   the CPU headroom for the overflow — size for `libx264` on the channels that will not get a
   session.
2. **The preview always uses `libx264`**, regardless of the channel encoder, so NVENC removes
   the program encode from the CPU budget but not the preview encode.
3. **Availability is probed with a real encode**, not read from `ffmpeg -encoders`. Check
   `GET /api/system` for what this host can actually do before planning around a hardware
   encoder.

---

## Measuring your own host

`scripts/install.sh` prints a first estimate in its encoder step, from `nproc`,
`limits.reserved_cores` and the measured 1.5 cores per channel:

```
 ok  7 cores, 1.0 reserved — room for about 4 concurrent 720p channels
```

Before adding a channel, look at all three of these:

```bash
# 1. Per-container cores, right now
docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'

# 2. The control plane's view
curl -s -H "Authorization: Bearer $AMBIENT_API_TOKEN" http://127.0.0.1:8090/api/capacity

# 3. Every channel is actually keeping up
for ch in $(ls channels | grep -v '^example$'); do
  printf '%s: ' "$ch"
  docker exec "$ch-composer" tail -c 2000 "/run/ambient/$ch/progress" 2>/dev/null \
    | grep -E '^speed=' | tail -1
done
```

**`speed` is the capacity signal, not CPU percentage.** A host at 95 % CPU with every channel at
1.0× is fine. A host at 70 % CPU with one channel at 0.94× is not — something is contending, and
YouTube is being starved.

The watchdog treats sustained `speed` below `min_speed` (default 0.97) for `stall_seconds`
(default 15 s) as a fault. See [operations.md](operations.md).

---

## Adding a channel

1. **Check headroom.** Measured cores in use, plus ~1.5, must stay under
   `cores − reserved_cores`.
2. Create the channel directory, `.env` and `config.yaml`
   ([quickstart.md §4](quickstart.md#4-create-a-channel)).
3. **Register the Icecast mount and generate its fallback**, then `SIGHUP` Icecast. Never
   restart Icecast — that takes every running channel's audio input with it.
4. Add media and extract color profiles.
5. `python -m ambient.compile <name>` — it warns about anything that resolved oddly and refuses
   a `hot_set` the host cannot afford.
6. `scripts/channel.sh start <name>`.
7. **Watch every other channel's `speed` for a few minutes.** A new channel that pushes an
   existing one below 1.0× is a capacity failure, and it shows up on the *old* channel, not the
   new one.

`POST /api/channels/{name}/start` performs a version of step 1 automatically and returns
`409 insufficient_capacity` when it fails — but it compares against `projected_cores`, which
counts only visualization branches, so it is generous. It will not save you from
oversubscribing. Step 7 is the real check.

---

## What oversubscription looks like

| Symptom | Where |
|---------|-------|
| `speed` settles below 1.0× and stays there | `-progress`, `GET /api/channels`, `ambient_channel_speed` |
| YouTube Studio reports buffering or poor stream health | Studio |
| `out_time` falls progressively behind wallclock | `-progress` |
| The watchdog restarts a channel, and it degrades again | `watchdog.event` SSE, `ambient_watchdog_restarts_total` |

`drop_frames` and `dup_frames` stay at **0** through this. They are not usable as signals — the
`fps` filter's duplication is internal and never reaches the muxer's counters.

**A watchdog restart does not fix a CPU shortage.** It restarts a channel into the same
oversubscribed host, costs a YouTube ingest session each time, and the exponential backoff
(5, 15, 45, 120, 300 s) exists precisely so that a doomed retry loop does not look like abuse to
YouTube ingest. If restarts are climbing across several channels, stop a channel rather than
letting the watchdog keep trying.

---

## Scaling beyond one host

There is **no multi-host support**. The backend drives one Docker daemon, the Compose network is
local, and Icecast and MediaMTX are single instances.

Channels are fully independent, so running a second host with its own complete stack works —
separate `.env`, separate channels, separate control plane. There is no shared state, no
cross-host scheduling, and no aggregated view. Split by channel, not by component: putting
Icecast on one host and the compositors on another would put a network hop inside the audio path
that the fallback-mount design depends on.

---

## Related

| Document | Covers |
|----------|--------|
| [architecture.md](architecture.md#72-capacity) | the measurement and the reasoning behind it |
| [`contracts/plugin.md`](contracts/plugin.md) | per-branch costs and the benchmarking caveat |
| [plugin-development.md](plugin-development.md#measuring-cost) | measuring a new plugin's cost correctly |
| [docker-deployment.md](docker-deployment.md) | resource limits, GPU passthrough, encoder profiles |
| [operations.md](operations.md) | reading `speed` and reacting to a degraded channel |
