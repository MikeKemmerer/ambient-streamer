# Operations

Running ambient-streamer 24/7: what healthy looks like, how the watchdog decides something is
wrong, where to look when it is, and what to do about it.

The rule underneath all of it: **a 24/7 stream must never restart FFmpeg.** Every recovery
below is chosen to avoid a restart where one can be avoided, and to make it cheap where it
cannot. See [architecture.md](architecture.md).

---

## What healthy looks like

Five things, in the order you should check them.

### 1. The compositor's `-progress` output

This is the primary signal. Everything else is corroboration.

```bash
docker exec lofi-composer tail -c 2000 /run/ambient/lofi/progress
```

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

| Field | Healthy | Meaning |
|-------|---------|---------|
| `speed` | **~1.0×**, sustained ≥ 0.97 | encoding at real time. This is the number that matters |
| `out_time` | climbing in step with wallclock | media actually published |
| `bitrate` | ~3085–3137 kbits/s at 720p | matches the ladder's 3000k video + 128k audio |
| `fps` | the channel frame rate | 30.0 at 720p30 |
| `drop_frames` / `dup_frames` | 0 | **and they stay 0 even when things are broken** |

### 2. Container state

```bash
scripts/channel.sh status lofi
docker compose -p ambient ps
```

### 3. The relay took the publish and started the YouTube leg

```bash
docker logs --tail 40 ambient-mediamtx
```

```
INF [path lofi] [RTMP] ... is publishing to path 'lofi'
INF [publish lofi] publishing to rtmp://a.rtmp.youtube.com/live2 (key withheld from argv)
```

### 4. The control plane agrees

```bash
TOKEN=$(grep '^AMBIENT_API_TOKEN=' .env | cut -d= -f2)
curl -sS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8090/api/channels/lofi
```

`state: running`, `health: healthy`, `rtmp: connected`, `hls: ok`,
`liquidsoap_buffer: ok`, `fault: null`.

### 5. YouTube Studio

**Excellent** stream health, no dropped frames, and the broadcast has not changed video ID.

---

## The two failure modes a liveness check cannot see

Both were measured. Both are why "is the process running?" is not a health check.

### A stalled producer kills nothing

FFmpeg stays alive. `speed` falls to **0.44×**. `out_time` advances, but far slower than
wallclock. YouTube starves and eventually reports the stream unhealthy.

Nothing exits. Docker's `restart: unless-stopped` never fires. A process check reports success
throughout.

**Detected by:** `out_time` failing to advance against wallclock, and `speed` sustained below
`min_speed`.

### A dead producer makes FFmpeg exit rc=0

The producer writes frames to a pipe. When it dies, the pipe closes, and FFmpeg treats a closed
input as **clean end of stream** — it exits **0**, indistinguishable from a successful finish.

**Therefore: any composer exit is a fault, whatever the exit code.** The watchdog makes no
exception for `rc=0`.

### `drop_frames` and `dup_frames` are useless

They stayed at **0** through the burst-pacing failure. The `fps` filter's duplication is
internal and never reaches the muxer's counters. Do not build an alert on them, and do not read
their being 0 as evidence of anything.

---

## The watchdog

Runs inside the backend, polling every channel on a timer. Configured under `watchdog:` in
`ambient.yaml`:

| Key | Default | Meaning |
|-----|---------|---------|
| `poll_seconds` | `5` | how often every channel is evaluated |
| `min_speed` | `0.97` | below this for `stall_seconds` is a fault |
| `stall_seconds` | `15` | how long a stall or a slow speed must persist |
| `restart_backoff_seconds` | `[5, 15, 45, 120, 300]` | ascending; must ascend |

Two timers are not configurable: a **45-second startup grace** (the producer is throttled to
~1.5 fps for 4–6 s while FFmpeg initializes, and that offset is benign) and a **30-second
settle window** after a restart, during which no new verdict can trigger another one.

### Faults it detects

| `fault` | State / health | Condition |
|---------|----------------|-----------|
| `composer_exit` | `failed` / `disconnected` | the composer container exists but is not running — **including exit 0** |
| `no_progress` | `degraded` / `stalled` | running past the startup grace, but no `-progress` block written at all |
| `out_time_stalled` | `degraded` / `stalled` | `out_time` unchanged for `stall_seconds` of wallclock |
| `speed_below_minimum` | `degraded` / `starving` | `speed` below `min_speed` for `stall_seconds` |
| *(none)* | `stopped` / `disconnected` | no composer container — **deliberately stopped, not a fault** |

### What it does about them

| Fault state | Action | Why |
|-------------|--------|-----|
| `failed` | `supervisor.start()` | nothing is publishing, so there is no relay path to hand over |
| `degraded` | `supervisor.restart()` — make-before-break | something is still publishing; the replacement claims the path first |

Backoff climbs the ladder on each attempt and is reset only after **three consecutive healthy
polls** — a channel that flaps does not get its short backoff back. A restart that itself fails
is logged and emitted as `restart_failed`; it does not stop the loop or affect other channels.

**A tight restart loop against YouTube ingest looks like abuse.** That is what the ladder is
for.

### Watching it

```bash
curl -sS -N -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8090/api/events
```

```
event: watchdog.event
data: {"channel":"lofi","at":"2026-08-12T09:31:44Z","event":"restarting",
       "fault":"out_time_stalled","detail":"out_time held at 402.30s for 16.0s of wallclock",
       "attempt":1,"backoff_seconds":5.0}
```

`event` is `restarting`, `restarted` or `restart_failed`.

---

## Reading logs

### Container logs — the reliable source

```bash
scripts/channel.sh logs lofi          # both channel containers, followed
docker logs --tail 200 lofi-composer
docker logs --tail 200 lofi-liquidsoap
docker logs --tail 200 ambient-mediamtx
docker logs --tail 200 ambient-icecast
docker logs --tail 200 ambient-backend
```

Channel containers rotate at `max-size: 10m`, `max-file: 5`.

### Host log files

`${AMBIENT_LOG_DIR}/<channel>/{compositor,liquidsoap,producer,watchdog}.log` is the layout
[`contracts/on-disk.md`](contracts/on-disk.md) specifies, and it is what
`GET /api/logs?channel=&service=&lines=` reads.

> **The channel processes write to stderr only.** The compositor entrypoint and the slideshow
> producer log to stderr, which Docker captures — nothing writes those files. `GET /api/logs`
> returns an empty `lines` array on a healthy host, which looks like a fault and is not one.
> Use `docker logs` until the file sinks land.

### What each source is good for

| Question | Look at |
|----------|---------|
| Is the encoder keeping up? | `/run/ambient/<ch>/progress` |
| What graph is actually running? | `/run/ambient/<ch>/filtergraph.txt` |
| Did a plugin fail validation? | `docker logs <ch>-composer` — the entrypoint dies with a clear message |
| Is Liquidsoap connected? | `docker logs <ch>-liquidsoap`, `docker logs ambient-icecast` |
| Did the YouTube publisher start? | `docker logs ambient-mediamtx`, `[publish <ch>]` lines |
| Why did the watchdog restart something? | `docker logs ambient-backend`, or the SSE stream |

Useful one-liner across every channel:

```bash
for ch in $(ls channels | grep -v '^example$'); do
  printf '%-12s ' "$ch"
  docker exec "$ch-composer" tail -c 2000 "/run/ambient/$ch/progress" 2>/dev/null \
    | grep -E '^(speed|out_time)=' | tr '\n' ' '
  echo
done
```

---

## Runbook

### `speed` sustained below 1.0×

The host is oversubscribed, or something else on it is. Check
[scaling.md](scaling.md#what-oversubscription-looks-like).

**A watchdog restart does not fix this** — it restarts the channel into the same oversubscribed
host and costs a YouTube ingest session each time. Stop a channel instead, or lower a
resolution.

### Composer restart-looping

```bash
docker logs --tail 100 lofi-composer
```

The entrypoint fails fast and says why. Common causes:

| Message | Cause |
|---------|-------|
| `plugin 'x' has no viz.ffmpeg` | the plugin directory is incomplete |
| `plugin 'x' does not derive its size from ${WIDTH}x${HEIGHT}` | literal size in a fragment; see [plugin-development.md](plugin-development.md) |
| `plugin 'x' requires filter 'y', absent from this build` | the composer image lacks that filter |
| `HOT_SET is empty` | an environment override set to the empty string |
| `Error opening input files: Connection refused` | Icecast has no mount for this channel |

Stop the channel while you fix it; a channel that cannot start should not keep trying.

### No audio, or the fallback loop is on air

The composer is reading Icecast's fallback mount, which means Liquidsoap is not connected.

```bash
docker logs --tail 50 lofi-liquidsoap
docker logs --tail 50 ambient-icecast
grep -c . channels/mounts.list
```

Usual causes, in order of likelihood:

1. The channel is missing from `channels/mounts.list` — add it and `docker kill -s HUP
   ambient-icecast`.
2. The root `.env` was not passed to Compose, so `ICECAST_SOURCE_PASSWORD` resolved to empty.
   Use `scripts/channel.sh` or the API, never bare `docker compose`.
3. The playlist is empty or every file failed to open.

**Do not restart Icecast to fix this.** A restart takes every running channel's audio input
with it. Adding a mount is always a `SIGHUP`.

The fallback covering an outage is the design working, not a failure: a full Liquidsoap kill and
restart was measured at **0 s** of composer output loss and **0.00 %** silence.

### YouTube says no data, or the broadcast ended

```bash
docker logs --tail 60 ambient-mediamtx
```

| What you see | Meaning |
|--------------|---------|
| `[publish lofi] staying down; will re-check every 60s` | `YOUTUBE_STREAM_KEY` is empty. Paste it in; no restart needed |
| `[publish lofi] refusing to publish: MTX_PATH is not a channel name` | path name does not match the channel-name pattern |
| `argv shim missing` | the relay image was built wrong; rebuild it |
| publisher starts, YouTube drops the stream ~15 s later | the publisher's probe is shorter than the composer's 2-second GOP, so `-c copy` forwarded a stream with no SPS/PPS. Leave `AMBIENT_PUBLISH_ANALYZEDURATION` at its 3 s default |

**A composer restart ends the ingest session.** MediaMTX does not hold the YouTube session open
across a publisher change — measured terminating reader connections even when the relay path
never dropped. The relay bounds the gap; it does not remove it.

### A channel is `degraded` but nothing obvious is wrong

```bash
curl -sS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8090/api/channels/lofi \
  | python -m json.tool
```

`fault` and `fault_detail` carry the watchdog's reasoning verbatim. If `rtmp` or `hls` reads
`unknown`, the backend could not reach the MediaMTX control API — after a failed probe it stops
asking for 30 s, so `unknown` can persist briefly after the relay recovers.

### The backend will not start

| Message | Fix |
|---------|-----|
| `AMBIENT_API_TOKEN is unset while bound to ...` | set a token, or bind to `127.0.0.1` |
| `AMBIENT_ROOT is not present in this container` | `AMBIENT_REPO_ROOT` does not match the repository's host path. Re-run `scripts/install.sh`, then `docker compose -p ambient up -d backend` |
| `/var/run/docker.sock is group root` | the entrypoint cannot drop privileges and still reach the daemon. Give the socket a non-root group |
| `the Docker daemon did not answer as uid ...` | the unprivileged user is not in the socket's group |

### Disk filling

Container logs are capped. **Host logs under `${AMBIENT_LOG_DIR}` are not** — add a `logrotate`
rule. Nothing records video: MediaMTX has `record: no` and HLS segments are held in memory.

---

## Routine changes, and what they cost

| Change | How | Cost |
|--------|-----|------|
| Add or reorder tracks | edit `config.yaml` / `PUT .../playlist`, recompile | **0 s** — Liquidsoap reloads a watched playlist |
| Add or reorder images | edit `config.yaml` / `PUT .../images`, recompile | **0 s** — producer rescans between slides, FFmpeg PID unchanged |
| Drop a file into a watched folder | copy it in | **0 s** — globs and empty selections are watched; explicit lists are not |
| Change color | `PUT .../color`, or a preset | one frame |
| Switch plugin | `PUT .../visualization` | one frame, clean cut |
| Apply a preset | `POST .../preset` | one frame |
| Restart Liquidsoap | `docker restart <ch>-liquidsoap` | **0 s** on the stream — Icecast's fallback absorbs it |
| Add a channel to Icecast | `mounts.list` + `SIGHUP` | none |
| Fill in a stream key | edit `channels/<ch>/.env` | none — the relay reads it at path-ready time |
| Change `hot_set`, resolution, fps, encoder | `POST .../restart` | **seconds** on the YouTube leg, and a new ingest session; see on-disk.md |
| Blunt restart | `scripts/channel.sh restart <ch>` | **~13.7 s**, and a new ingest session |

Only the last two rows touch the YouTube broadcast. Prefer everything above them.

### Scheduled changes

`schedule:` in a channel's `config.yaml` applies presets on a timer. Resolution is most
specific first — `date`, then `days` + `when`, then `when`, then the channel default — and the
first match wins. Times are the channel's `timezone`, compared as wall clock, so a window inside
a spring-forward hour simply never fires and one inside a fall-back hour matches twice
harmlessly (applying a preset is idempotent, and the scheduler applies only on a change).

The scheduler evaluates every 20 s. A rule that cannot be applied — for example a preset naming
a plugin outside the channel's `hot_set` — is logged and retried on the next tick, never raised.

---

## Alerting

`GET /api/metrics` is Prometheus text and requires the bearer token.

| Alert on | Expression, roughly |
|----------|---------------------|
| Channel starving | `ambient_channel_speed < 0.97` for 1m |
| Channel stalled | `rate(ambient_channel_out_time_seconds[2m]) < 0.9` |
| Channel down | `ambient_channel_up == 0` while it should be running |
| Restart storm | `increase(ambient_watchdog_restarts_total[30m]) > 3` |
| Host oversubscribed | `sum(ambient_channel_cpu_cores)` approaching `cores − reserved_cores` |
| UI clients being dropped | `increase(ambient_sse_dropped_subscribers_total[15m]) > 0` |

Do **not** alert on `drop_frames` or `dup_frames`. They are always 0.

`GET /api/health` is unauthenticated and suitable for an uptime monitor; the backend container
already uses it as its Docker healthcheck.

---

## What to back up

| Back up | Why |
|---------|-----|
| `.env` | Icecast credentials, API token, `AMBIENT_REPO_ROOT` |
| `ambient.yaml` | global operational settings |
| `channels/*/.env` | stream keys and per-channel settings |
| `channels/*/config.yaml` | selection, visualization, color, schedule |
| `channels/*/bumpers.yaml` | hand-written station-ID text |
| `channels/mounts.list` | the Icecast mount registry — nothing regenerates it |
| media under `common/` and `channels/*/` | your library |

**Do not bother backing up** `playlist.m3u`, `images.list`, `docker-compose.yml` (regenerated by
`ambient.compile`), `profiles/` (regenerated by extraction), `common/fallback/*.mp3`
(regenerated by `scripts/install.sh`), or anything under `/run/ambient` (tmpfs, and a cache of
what a process is doing now — never a source of truth).

Everything in the first list except the media is small and text. Treat `.env` files like
password files: mode 600, never committed, encrypted at rest wherever the backup lands.

---

## Restart discipline

1. **Prefer a mechanism that is not a restart.** Playlist, images, color and plugin switching
   are all gap-free.
2. **If a restart is genuinely required, use `POST /api/channels/{name}/restart`.** It is
   make-before-break: the replacement composer starts in a second Compose project, claims the
   relay path, and only then is the outgoing one removed. Liquidsoap is left alone.
3. **Never restart Icecast.** `SIGHUP` covers every configuration change it needs.
4. **Restart MediaMTX only deliberately.** It briefly interrupts every channel at once.
5. **Restart the backend freely.** It holds no media state; channels keep running without it,
   and which composer slot is live is derived from Docker rather than stored, so a backend
   crash cannot lose track of it. You lose the watchdog and the scheduler while it is down.

---

## Related

| Document | Covers |
|----------|--------|
| [quickstart.md](quickstart.md) | first install and first channel |
| [architecture.md](architecture.md) | why the escape hatches exist and what each measured |
| [scaling.md](scaling.md) | capacity, and what oversubscription looks like |
| [docker-deployment.md](docker-deployment.md) | ports, limits, secrets, the Docker socket |
| [api-reference.md](api-reference.md) | endpoints, SSE events, metrics |
| [`contracts/on-disk.md`](contracts/on-disk.md) | runtime state, logs, and why `progress` is the primary signal |
