# Contract — Audio transport

Liquidsoap → Icecast → compositor. Owner: lead. Consumers: media-pipeline, infra.

Everything marked **measured** was observed in spike S1 on real hardware.

## Why Icecast and not the obvious alternatives

**Measured**, one Liquidsoap restart, source down 9.41 s:

| Transport | FFmpeg process | Compositor output |
|---|---|---|
| named pipe / FIFO | **dies** on writer EOF | gone |
| `output.harbor` + FFmpeg `-reconnect` | survives | **stalls 18.19 s** |
| **Icecast + fallback mount** | survives | **0 s, 0.00 % silence** |

`output.harbor` is disqualified. FFmpeg's reconnect backoff ladder is
0, 1, 3, 7, 15, 31, 63 s, so a 9.4 s outage misses the 7 s step and waits for
the 15 s one — **the gap is roughly double the outage**, and the compositor
then runs that far behind wallclock permanently. No FFmpeg flag fixes this: the
stall happens in the demuxer before any filter sees a frame.
`-use_wallclock_as_timestamps`, `aresample=async=1` and `async=1000` were all
measured identical.

Icecast works because the compositor's HTTP connection terminates on Icecast,
which never restarts. Liquidsoap is merely a source client behind it.

One Icecast serves every channel. It is a global container, not per channel.

## Payload format — MP3, not Ogg

**Measured**, audio after a reconnect:

| Encoder | Result |
|---|---|
| `%mp3(bitrate=256)` | **clean** — 0.0 % silence, no fragments |
| `%vorbis(quality=0.6)` | broken — 67 % silence, 55 fragments, non-monotonic DTS |
| `%ogg(%flac(...))` | broken — 60 % silence, `CRC mismatch!` |
| `%wav` | usable — lossless fallback |

MP3 frames carry a sync word, so any resume point in the byte stream is
recoverable. **Ogg cannot resync**: a reconnect splices a fresh logical stream
with fresh headers into a demuxer that is mid-stream. This applies to the
fallback-mount switch too, which is exactly such a mid-connection change.

ICY metadata is not the cause — the same results were measured with `-icy 0`.

**Required:** `%mp3(bitrate=256, samplerate=44100, stereo=true)`

44.1 kHz is not negotiable; it is the single clock the whole pipeline shares.

## Liquidsoap side

```liquidsoap
pl       = playlist(id="playlist", mode="normal", reload_mode="watch", playlist_file)
requests = request.queue(id="queue")

# `playlist` has no "play this one" verb, so a request queue in front of it is
# the only way to honour a choice. track_sensitive=false so a push interrupts
# rather than waiting for the current track to end.
programme = fallback(id="programme", track_sensitive=false, [requests, pl])

radio = crossfade(duration=crossfade_s, programme)

# mksafe must be OUTERMOST. A fallible source reaching output.icecast drops the
# mount, which is the one failure Icecast cannot paper over.
radio = mksafe(radio)

output.icecast(
  %mp3(bitrate=256, samplerate=44100, stereo=true),
  host = "icecast", port = 8081,
  mount = getenv("CHANNEL_MOUNT"),
  password = getenv("ICECAST_SOURCE_PASSWORD"),
  radio
)
```

Requirements:

- `mksafe` outermost, always.
- Playlist comes from a **file**, not a directory — see
  [media-selection.md](media-selection.md).
- A telnet control socket on the channel's internal network for status queries
  (current track, next track, buffer state). Never published to the host.
- The source password comes from the environment. Never a literal.
- **`on_track` attaches to `programme`, below the crossfade, not to the
  output.** `crossfade` merges a mid-track switch into the track it is already
  playing, so a queue takeover produces no track mark at the output at all —
  measured, the audio changed and the reported track never moved. The price is
  that it fires up to `crossfade_seconds` early, because crossfade reads ahead.

## Icecast side

Each channel needs two mounts:

| Mount | Source | Purpose |
|---|---|---|
| `CHANNEL_MOUNT` e.g. `/lofi` | Liquidsoap | the live audio |
| `CHANNEL_FALLBACK_MOUNT` e.g. `/lofi-fallback` | a file on disk | covers Liquidsoap restarts |

The fallback mount must be configured with `fallback-override` so listeners
move back automatically when the real source returns. **Measured** behavior
across a kill and restart:

```
source_move_clients passing 1 listeners to "/live"           (source connects)
source_shutdown  Source ... at "/live" exiting               (kill)
source_move_clients passing 1 listeners to "/fallback.mp3"
Source logging in at mountpoint "/live"                      (restart)
source_move_clients passing 1 listeners to "/live"
```

FFmpeg logged **no reconnect lines at all** across that sequence.

The fallback file must be encoded identically to the live stream — MP3 256 kbps
44.1 kHz stereo — or the splice is a format change mid-demux, which is the Ogg
failure by another route.

## Compositor side

```
-probesize 32k -analyzeduration 500000
-reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1
-reconnect_delay_max 5
-i http://icecast:8081/<mount>
```

Two of these are mandatory for non-obvious reasons, both **measured**:

| Flag | Without it |
|---|---|
| `-probesize 32k -analyzeduration 500000` | **8.4 s** before FFmpeg emits its first sample. This is probe time, not transport latency |
| `-reconnect_on_network_error 1` | compositor **dies at startup**: `Error opening input files: Connection refused` |

The second is the dangerous one. Compose starts both containers at once, so the
compositor will always find nothing listening on its first attempt. Plain
`-reconnect` covers a disconnect *during* a stream, not the initial connect.
Without this flag every deployment fails, and it fails in a way that looks like
a configuration error rather than a race.

Audio filter chain, unchanged from the proven YouTube ingest settings:

```
aresample=44100:async=1000:first_pts=0,loudnorm=I=-14:TP=-1:LRA=11
```

## Startup and readiness

Ordering is: Icecast → Liquidsoap → compositor. Compose may start them in any
order; the flags above make that safe.

**Readiness for a channel is HTTP 200 on its mount, not a listening socket.**
Icecast accepts connections before any source has logged in, so a TCP check
reports healthy while the mount 404s.

## Failure modes

| Failure | Effect | Detection |
|---|---|---|
| Liquidsoap restarts | none — fallback covers it | Icecast log, `source_move_clients` |
| Liquidsoap dies permanently | fallback plays indefinitely | listener stays on fallback mount > threshold |
| Icecast dies | **compositor input dies** | mount unreachable; this is the single point of failure |
| Source password wrong | mount never appears, fallback plays forever | Icecast log, and the fallback-forever check above |

Icecast is a genuine single point of failure for audio and is accepted as such:
it is a small, stable, long-running process whose entire job is to not restart.
Liquidsoap — the process that actually changes when a playlist is edited — is
the one insulated behind it.
