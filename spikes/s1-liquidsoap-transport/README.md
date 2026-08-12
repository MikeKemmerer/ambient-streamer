# Spike S1 — Liquidsoap → FFmpeg transport

**Question:** what carries audio from the Liquidsoap engine to the FFmpeg compositor, given
that the compositor must never restart and must survive a Liquidsoap restart?

**Answer:** a long-lived **Icecast relay**, carrying **MP3 256 kbps CBR 44.1 kHz stereo**.
`output.harbor` + FFmpeg reconnect keeps FFmpeg *alive* but stalls its **output** for
~18 s per restart, which is a sustained gap in RTMP data. A named pipe kills FFmpeg outright.

Ran on `kfamplex` (Ubuntu 24.04, 7 cores), FFmpeg 6.1.1, `savonet/liquidsoap:v2.4.2`,
`libretime/icecast:2.4.4`.

## Running it

```bash
./00-setup.sh            # pull the pinned image, synthesize test audio
./10-fifo.sh             # A: named pipe
./20-harbor.sh           # B: output.harbor + FFmpeg reconnect
./21-gapfill.sh          # B: aresample/timestamp variants
./22-timeline.sh         # B: metronome source, is the outage filled or dropped
./23-composer-restart.sh # B: does the *video* output stall too   <- the decisive test
./25-icecast.sh          # C: Icecast relay with a fallback mount
./30-formats.sh          # mp3 / ogg-vorbis / ogg-flac / wav
./40-drift.sh            # source clock drift over a long run
./50-playlist-watch.sh   # reload_mode="watch"
./60-startup-order.sh    # FFmpeg starts before Liquidsoap
```

Each script owns one port in 18090–18099 and cleans up only its own containers, so they can
run concurrently. `SPIKE_PORT=` overrides.

## Results

### 1. Does FFmpeg survive a Liquidsoap restart?

| Option | FFmpeg process | Composer output |
|---|---|---|
| A — named pipe | **dies** on writer EOF | gone |
| B — `output.harbor` + reconnect | survives | **stalls 18.19 s** |
| C — Icecast relay | survives | **never stalls** |

**A (`10-fifo.sh`).** FFmpeg ran at `speed=1.04x` for 20.5 s, then `docker kill` closed the
write end, FFmpeg took the EOF as end of input and exited cleanly at `out_time=22.52 s`.
Restarting Liquidsoap did not revive it; Liquidsoap then blocked forever in `open()` because
no reader was left. Two further warts: the FIFO needs mode `0666` because the Liquidsoap
image runs as its own uid (`Sys_error("...: Permission denied")` at 0644), and a leftover
reader from an aborted run silently steals bytes from the single-reader pipe and corrupts the
stream for everyone (`Header missing`, `invalid new backstep -1`). Rejected.

**B (`20-harbor.sh`, `23-composer-restart.sh`).** With the corrected flags FFmpeg survives a
`docker kill` + `docker start`: `out_time` went 19.17 s → 47.88 s across the outage. But the
composer test is what matters. Sampling the output file every 0.5 s during a **9.41 s** source
outage:

```
source_outage=9.41s
max_no_data_interval=18.19s          <- zero bytes written to the FLV output
video_packets=2174  max_pts_gap=0.034s   (one frame at 30 fps: no hole in the timeline)
audio_packets=3120  max_pts_gap=0.024s
```

The composer freezes — **video included** — for the whole reconnect window, then resumes and
stays ~18 s behind wallclock forever. A/V stay locked to each other (last video pts 72.39 s,
last audio pts 72.38 s), so there is no lip-sync-style skew; the problem is purely that
nothing reaches the relay for 18 s.

**C (`25-icecast.sh`).** Liquidsoap connects to Icecast as a *source client*; the composer's
HTTP connection terminates on Icecast, which does not restart. With a `fallback-mount`:

```
04:23:25  source_move_clients passing 1 listeners to "/live"          (source connects)
04:23:43  source_shutdown  Source ... at "/live" exiting              (docker kill)
04:23:43  source_move_clients passing 1 listeners to "/fallback.mp3"
04:23:53  Source logging in at mountpoint "/live"                     (docker start)
04:23:53  source_move_clients passing 1 listeners to "/live"
```

FFmpeg logged **no reconnect lines at all**; `out_time` ran 34.64 s → 86.75 s unbroken. The
92.1 s recording contains **0.00 % silence**: 16.6 s of the 100/150 Hz fallback tone before the
source ever connected, the music, then 19.9 s of fallback covering the outage.

### 2. How long is the gap?

Measured, per Liquidsoap restart (`docker kill` + `docker start`, source down 9.4 s):

| | Option B (harbor) | Option C (icecast) |
|---|---|---|
| composer emits no data | **18.19 s** | **0 s** |
| audible interruption | 18.19 s of frozen output | 0 s (fallback bed) |
| FFmpeg reconnect backoff | 0, 1, 3, 7, 15, 31, 63 s | n/a — never disconnects |

The backoff is the reason the 9.4 s outage costs 18 s: FFmpeg retries on a doubling ladder and
lands on whichever step first falls after the mount returns. A 9.4 s outage misses the 7 s step
and waits for the 15 s one. **The gap is roughly double the outage, not equal to it.**

`21-gapfill.sh` and `22-timeline.sh` looked for a cheaper fix on the FFmpeg side.
`-use_wallclock_as_timestamps`, `aresample=async=1` and `aresample=async=1000` all behave
identically — none of them shortens the stall, because the stall happens in the demuxer before
any filter sees a frame. There is no flag that fixes this; only removing the disconnect does.

### 3. Which audio format?

Same conclusions with FFmpeg's ICY request on (`-icy 1`, the default) and off (`-icy 0`), so
ICY metadata interleaving is **not** the cause of the ogg failures.

| Liquidsoap encoder | survived | first sample | Liquidsoap CPU | audio after a reconnect |
|---|---|---|---|---|
| `%mp3(bitrate=256)` | yes | 8.4 s | 5.6–6.4 % | **clean** — 0.0 % silence, no fragments |
| `%vorbis(quality=0.6)` | yes | 2.8 s | 3.4 % | **broken** — 67 % silence, 55 fragments, non-monotonic DTS |
| `%ogg(%flac(...))` | yes | 2.0 s | 2.3 % | **broken** — 60 % silence, `CRC mismatch!` |
| `%wav` | yes | 2.1 s | 3.7–5.5 % | usable — one clean 10.9 s silence (the outage) |

**Recommend `%mp3(bitrate=256, samplerate=44100, stereo=true)`.** MP3 frames carry a sync word,
so any resume point in the byte stream is recoverable; Ogg cannot resync because the reconnect
splices a fresh logical stream with fresh headers into a demuxer that is mid-stream. This
matters for Icecast too, not just for reconnect: the fallback-mount switch is exactly such a
mid-connection stream change, and the Icecast run proves MP3 crosses it twice with zero
silence.

44.1 kHz matches the single clock the `youtube-ingest` skill mandates. At 256 kbps into a
128–192 kbps AAC ingest the second-generation loss is not the limiting factor; `%wav` is the
lossless fallback if that judgement ever changes — 1.4 Mbps on an internal Docker network is
free, and it also survived.

The one MP3 cost: **8.4 s before FFmpeg emits its first sample**, which is probe time, not
transport latency. `-probesize 32k -analyzeduration 500000` removes it (`22-timeline.sh` starts
in well under a second with those set). They are mandatory in the composer command.

### 4. Drift

_(pending — `40-drift.sh`, 25 min run)_

### 5. `playlist(reload_mode="watch")` (`50-playlist-watch.sh`)

Works, with no restart and no break in the stream. A fifth file was copied into the media
directory 20 s into a run:

```
liquidsoap pid: before=2328053 after=2328053     -> same process
04:16:37 Prepared "/media/t1-a.mp3"
04:17:19 Prepared "/media/t2-b.mp3"
04:18:01 Prepared "/media/t3-c.mp3"
04:18:43 Prepared "/media/t4-d.mp3"
04:19:25 Prepared "/media/t9-new.mp3"            <- picked up, in normal rotation
```

234.8 s recorded, **0.0 % silence**, FFmpeg alive throughout.

### 6. Startup ordering (`60-startup-order.sh`)

Compose starts both containers at once, so the composer *will* find nothing listening.

| FFmpeg input flags | outcome |
|---|---|
| `-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 -reconnect_delay_max 120` | **dies immediately**: `Error opening input files: Connection refused` |
| … plus `-reconnect_on_network_error 1` | waits, retries, connects; first audio 19.0 s after `docker run` returned |

`-reconnect` covers a disconnect *during* a stream. Only `-reconnect_on_network_error 1` covers
the initial connect. Without it the composer is dead before it starts.

Two related traps found while building the harness:

- **`-reconnect_delay_max` is the give-up threshold, not a per-attempt cap.** FFmpeg 6.1's own
  help: *"max reconnect delay in seconds after which to give up (default 120)"*. The widely
  copy-pasted `-reconnect_delay_max 5` therefore *shortens* the retry window to about 4 s
  (`0 + 1 + 3`), which is less than any container restart. That is exactly how the first run of
  `20-harbor.sh` failed. Use the default 120, explicitly.
- **Docker publishes the port before Liquidsoap registers the mount.** A TCP-connect readiness
  check passes while the harbor still answers `Connection reset by peer`. The only valid probe
  is an HTTP GET on the mount returning `200` (`harbor_ready()` in `lib.sh`).

## Recommended configuration

### Liquidsoap (`liq/icecast.liq`)

```liquidsoap
pl = playlist(id="pl", mode="normal", reload_mode="watch", music_dir)

# crossfade re-introduces fallibility, so mksafe goes OUTERMOST — a bad file must
# never take the mount down, because that is what the composer is connected to.
s = mksafe(crossfade(duration=3., pl))

output.icecast(
  %mp3(bitrate=256, samplerate=44100, stereo=true),
  id="ice", host=ice_host, port=ice_port,
  mount=ice_mount, password=ice_password,
  s
)
```

Liquidsoap 2.4 notes, all found the hard way:

- `mksafe(crossfade(...))`, not `crossfade(mksafe(...))` — the latter fails with
  `Error 7: Invalid value: That source is fallible.`
- `s.on_track` requires `synchronous=` in 2.4: `s.on_track(synchronous=false, f)`.
- `output.harbor` has **no** `icy_metadata` parameter in 2.4.
- Liquidsoap itself logs, on every `output.harbor` start:
  *"`output.harbor` code has not been update in a long while … we suggest using `icecast`!"*
  Another reason not to build the product on harbor.
- String interpolation cannot contain nested double quotes: bind `m["filename"]` to a variable
  first.

### FFmpeg audio input block

```
-probesize 32k -analyzeduration 500000
-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1
-reconnect_on_network_error 1 -reconnect_delay_max 120
-i http://<relay>:8000/<channel>
```

and in the filtergraph, per the `youtube-ingest` skill:

```
[1:a]aresample=44100:async=1000:first_pts=0[a]
```

`-max_interleave_delta 0` on the output, so the muxer never withholds video waiting for audio.

### Compose

- Global `icecast` service; per-channel mount named after the channel.
- Liquidsoap healthcheck = **HTTP GET on the mount**, never a TCP probe.
- The composer still needs `-reconnect_on_network_error 1`: `depends_on` orders *starts*, not
  readiness, and the relay is what the composer waits for.
- A `fallback.mp3` must exist in the Icecast webroot before any composer starts. Silence works;
  a short ambient bed is better.
- The Icecast source password is a secret — per-channel `.env` or a Docker secret.
  `25-icecast.sh` generates a throwaway one per run; `icecast/icecast.xml` is gitignored.

## Contract points this imposes

1. Transport is HTTP; the composer reads `http://<relay>:<port>/<mount>` and nothing else.
2. Payload is **MP3 256 kbps CBR, 44100 Hz, stereo**. Fixed, not negotiated — the composer
   sets `-probesize`/`-analyzeduration` low precisely so it does *not* have to probe.
3. The reconnect flag set above is **mandatory and complete**; dropping
   `-reconnect_on_network_error` or lowering `-reconnect_delay_max` breaks startup and restart
   respectively.
4. Readiness is an HTTP 200 on the mount. A listening socket is not readiness.
5. Liquidsoap's output must be infallible (`mksafe` outermost).
6. Now-playing metadata comes from the Liquidsoap telnet/HTTP API, **not** from ICY in the
   audio stream. 2.4's `output.harbor` cannot even configure ICY.
7. A stalled audio input stalls the **entire composer**, video included. So "process is alive"
   is not a health signal — the watchdog must check that the composer's output is *advancing*.

## Cross-lane items I did not act on

- **infra** — `docker/compose.channel.yml.j2` needs the icecast service, the HTTP healthcheck,
  and the fallback-file mount. I did not touch `docker/`.
- **backend-api** — `backend/ambient/ffmpeg_cmd.py` must emit the input flag block verbatim;
  `watchdog.py` needs an output-is-advancing check rather than process liveness.
- **lead / contracts** — `docs/contracts/` has no liquidsoap↔composer audio transport contract
  yet. Points 1–7 above are the content for it.
- **docs** — `docs/architecture.md` §2.2 says a Liquidsoap blip "is a reconnect rather than a
  process death". True, but it understates it: with harbor the composer stops emitting for
  ~18 s. §3's "two global containers" becomes three if Icecast is adopted (19 containers at 8
  channels, not 18).
- **Ports.** The brief listed 8090/8888/9000/8081 as free; all four were taken by the
  concurrently running `s5-mediamtx-relay` spike. I moved to 18090–18099. Spike port ranges
  need an owner.
