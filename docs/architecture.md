# Architecture

**Status: design document.** Nothing described here is implemented yet. The repository
currently contains project instructions, agent definitions, the Phase 0 spike harnesses, and
this documentation. This document describes the system the lanes are building toward, so that
every lane builds the same system. Where a decision is deliberately still open, it is marked as
such.

**Phase 0 spikes have been run on real hardware.** Their measured results are folded in below;
where a claim rests on a spike it says so, and the numbers are the measured ones. The harnesses
and raw output live in `spikes/s1-liquidsoap-transport/` through `spikes/s5-mediamtx-relay/`.
Two claims in the first draft of this document were disproved by measurement and have been
corrected:

| Was claimed | Measured |
|-------------|----------|
| Liquidsoap serves the composer over `output.harbor`, so a blip is just a reconnect | The composer's **output stalls 18.19 s** on a 9.41 s outage and never catches up. Icecast replaces harbor — see [§2.2](#22-audio-a-separate-process-behind-an-icecast-relay). |
| MediaMTX holds the YouTube session open across a composer restart | MediaMTX **terminates readers** when the publisher changes. The relay bounds the gap at ~1 s; it does not remove it — see [§2.6](#26-the-relay-a-bounded-restart-gap-not-a-restart-proof-session). |

## 1. What the system is

ambient-streamer is a headless, multi-channel, 24/7 ambient music streaming system for
YouTube Live. It runs unattended on a single Linux host and is operated entirely through a
browser.

Each **channel** is one continuous YouTube broadcast. A channel pairs:

| Piece | Role |
|-------|------|
| Liquidsoap audio engine | Owns the playlist, track order, crossfades, and loudness. Publishes a continuous MP3 stream to the shared Icecast relay, which is what the compositor reads. |
| FFmpeg compositor | Renders a color-adaptive image slideshow plus real-time audio visualization, encodes, and publishes over RTMP. |
| Color profile | Per-image palette data that drives the visualization and background colors so they track the current image. |
| HLS preview | A second, low-resolution feed the composer publishes alongside the program, which an operator can watch without touching the YouTube broadcast. |

A **FastAPI control plane** orchestrates all channels: it renders each channel's Compose file,
starts and stops channels, watches their health, restarts what dies, applies scheduled
changes, and streams live state to the operator UI.

The system has no interactive console, no desktop session, and no manual step in normal
operation. An operator's only interface is the web UI and, at install time, a shell script.

## 2. The central constraint

> **A 24/7 stream must never restart FFmpeg.**

This single rule explains most of the design. It follows from two facts that pull against each
other.

**Fact one: YouTube ends a broadcast after a sustained gap in data.** A live broadcast is a
session, not a file. If the RTMP publisher disconnects and stays gone, YouTube marks the
stream unhealthy, then ends the broadcast. Ending a broadcast is not recoverable in place —
the video ID changes, watch-page continuity is lost, and any accumulated concurrent viewers
are gone. A 24/7 channel that restarts its encoder to change a color is not a 24/7 channel.

**Fact two: an FFmpeg filtergraph is fixed at launch.** The graph is parsed once, from
`-filter_complex`, before the first frame. Filters cannot be added, removed, or rewired while
the process runs. Inputs cannot be added. The output URL cannot change. Only a narrow set of
filter *parameters* can be mutated at runtime.

So every feature an operator can change live must be designed around one of a small number of
escape hatches. The design question is never "how do I restart FFmpeg cleanly?" — it is
"which escape hatch does this feature use?"

### 2.1 The escape hatches

| Live change | Mechanism | Why it works | Measured in Phase 0 |
|-------------|-----------|--------------|---------------------|
| Playlist, track order, crossfade | Liquidsoap owns audio in a separate process, behind an Icecast relay | FFmpeg sees one never-ending HTTP audio input that terminates on Icecast, not on Liquidsoap. Liquidsoap can reload its playlist, or restart entirely, without FFmpeg noticing. | S1: `reload_mode="watch"` picked up a new file mid-run with the Liquidsoap PID unchanged, 0.0 % silence over 234.8 s. A full Liquidsoap kill and restart cost **0 s** of composer output through the Icecast fallback mount. |
| Image set, image order, transitions | Python producer feeds `-f image2pipe` | FFmpeg sees one never-ending stream of frames on stdin. The producer decides which image, in what order, with what crossfade. | S2: images added and removed mid-run with the FFmpeg PID unchanged and **no gap**. |
| Colors | `zmq` filter plus runtime commands | Mutates parameters on filter instances in the running graph. No re-parse, no restart. | S3: latency is exactly **one frame**, deterministic. Ceiling is one command per frame, **~31.5 commands/s**. |
| Visualization plugin | `streamselect` between pre-instantiated graphs | Every plugin's branch exists in the graph from launch. Switching changes which branch is routed to the output. | S4: **frame-exact** — a clean single-frame cut, no dropped frames. `astreamselect` behaves identically for audio. |
| Anything that genuinely needs a restart | MediaMTX relay plus a supervised, make-before-break composer swap | The relay bounds the gap and keeps the stream key off the composer. It does **not** hold the YouTube session open. | S5: best measured floor **1.03 s** on the YouTube leg. Kill-and-restart costs 5.14 s; an unsupervised publisher dies permanently on the first swap. |

**The decision rule:** before adding a feature that changes what the stream looks or sounds
like, decide which row it lands in. If the answer is "restart FFmpeg", the design is wrong —
change the design, not the rule. Only the first four rows are gap-free. The last row costs about
a second even when everything is done correctly, which is why it is the last resort and not a
general-purpose mechanism.

### 2.2 Audio: a separate process, behind an Icecast relay

Liquidsoap runs in its own container. It does **not** serve the composer directly. It connects
to a global Icecast container as a *source client*, and the composer reads
`http://icecast:8000/<channel>` over HTTP.

The obvious design — `output.harbor` plus FFmpeg reconnect flags — was measured in spike S1 and
**rejected**. Three transports were tested against a `docker kill` plus `docker start` of
Liquidsoap, source down 9.41 s:

| Transport | FFmpeg process | Composer output |
|-----------|----------------|-----------------|
| Named pipe / FIFO | **dies** on writer EOF | gone; restarting Liquidsoap does not revive it |
| `output.harbor` + `-reconnect` | survives | **stalls 18.19 s**, then runs ~18 s behind wallclock permanently |
| Icecast relay + `fallback-mount` | survives | **0 s gap, 0.00 % silence** |

Harbor fails because FFmpeg's reconnect backoff ladder is 0, 1, 3, 7, 15, 31, 63 s and lands on
whichever step first falls after the mount returns. A 9.4 s outage misses the 7 s step and waits
for the 15 s one: **the gap is roughly double the outage, not equal to it.** No FFmpeg-side flag
fixes it — `-use_wallclock_as_timestamps`, `aresample=async=1`, and `aresample=async=1000` all
behave identically, because the stall is in the demuxer before any filter sees a frame. And it is
not only an audio failure: the composer freezes its *video* output too, which is exactly the
sustained gap in RTMP data that §2 exists to prevent.

Icecast works because the composer's HTTP connection terminates on Icecast, which never
restarts. When Liquidsoap disconnects, Icecast moves the listener to a `fallback-mount` and
moves it back when the source returns. FFmpeg logged **no reconnect lines at all** across a full
kill-and-restart cycle.

**The payload is MP3 256 kbps CBR, 44.1 kHz, stereo** — `%mp3(bitrate=256, samplerate=44100,
stereo=true)`. This is fixed, not negotiated. S1 measured four encoders across a reconnect:

| Encoder | Audio after a reconnect |
|---------|-------------------------|
| `%mp3(bitrate=256)` | **clean** — 0.0 % silence, no fragments |
| `%vorbis(quality=0.6)` | broken — 67 % silence, 55 fragments, non-monotonic DTS |
| `%ogg(%flac(...))` | broken — 60 % silence, `CRC mismatch!` |
| `%wav` | usable — one clean silence covering the outage; the lossless fallback if ever needed |

MP3 frames carry a sync word, so any resume point in the byte stream is recoverable. Ogg cannot
resync, because a reconnect splices a fresh logical stream with fresh headers into a demuxer that
is already mid-stream. This matters for Icecast specifically and not just for reconnect: the
fallback-mount switch *is* such a mid-connection stream change, and the Icecast run crossed it
twice with zero silence. 44.1 kHz is also the single clock the `youtube-ingest` skill mandates.

Two flag groups on the composer's audio input are **mandatory**, both measured:

| Flags | Without them |
|-------|--------------|
| `-probesize 32k -analyzeduration 500000` | MP3 probing costs **8.4 s** before FFmpeg emits its first sample |
| `-reconnect_on_network_error 1` | The composer dies at startup with `Error opening input files: Connection refused` |

The second is a startup-ordering fix, not a resilience one. Compose starts both containers at
once, so the composer *will* find nothing listening; plain `-reconnect` covers a disconnect
during a stream and not the initial connect. Related trap from the same spike:
`-reconnect_delay_max` is the give-up threshold, not a per-attempt cap, so the widely copied
`-reconnect_delay_max 5` shortens the whole retry window to about 4 s — less than any container
restart.

Playlists use `playlist(reload_mode="watch")`, so adding or removing a track on disk is picked
up by Liquidsoap on its own schedule. S1 confirmed this: a file copied into the media directory
mid-run appeared in normal rotation with the Liquidsoap PID unchanged and no break in the
stream. Nothing downstream restarts. Crossfade, replaygain, and loudness normalization all live
on this side of the boundary — they are audio concerns, and audio concerns belong to the process
that can change them live.

The tradeoff is one extra container per channel plus one global container, and two hops instead
of none. That is accepted: it is what makes the playlist editable, and the audio engine
restartable, without the encoder ever noticing.

### 2.3 Images: a producer on a pipe

The slideshow is not built from FFmpeg's concat demuxer or a `glob` pattern, because both fix
the image set at launch. Instead a Python producer writes encoded frames to stdout and FFmpeg
reads them with `-f image2pipe`.

This inverts the ownership: the producer, not the filtergraph, owns image order, dwell time,
crossfade rendering, and the response to a changed media directory. Adding images to a channel
becomes a filesystem operation plus a producer-side rescan, with no effect on the encoder.
Spike S2 measured exactly that: images were added to and removed from the directory mid-run with
the FFmpeg PID unchanged and no gap in output. The producer encodes JPEG at quality 88, 4:4:4
chroma, at a 10 fps producer rate.

Because a slideshow produces frames far below the output frame rate, the graph pairs `fps`
with `realtime`. **`realtime` is mandatory, not an optimization.** Without it, S2 measured the
muxer releasing up to **53 frames — 1.77 s of media — inside a 0.5 s wall-clock window**. That
burst is precisely what makes YouTube buffer or stall, and it does not reproduce as a fault in
local playback. See the `youtube-ingest` skill for the full rationale.

### 2.4 Colors: runtime commands over ZMQ

A `zmq` filter instance in the graph listens on a socket. The backend sends messages that
target a named filter instance and set one of its parameters. Filters are therefore
instantiated with explicit labels (`filtername@label`) so they can be addressed later.

Color profiles are extracted from the images ahead of time and stored as JSON per channel. As
the slideshow advances, the backend applies the incoming image's palette by sending commands
for the affected parameters. The visualization and background track the artwork without a
graph change.

Spike S3 measured the mechanism. Command latency is **exactly one frame** and deterministic — a
command lands on the next frame boundary, never later, never smeared. The ceiling is therefore
one command per frame, about **31.5 commands/s** at the output frame rate.

That ceiling never binds, because smooth transitions are **not** built by streaming a command
per frame. They are built with `eq` and `hue` **time expressions**: a single command installs a
self-animating ramp that the filter evaluates per frame on its own. A fade is one command, not a
command stream. This imposes one launch-time requirement: `eq` must be instantiated with
`eval=frame`, because `eval` is not itself commandable, so a graph that omits it cannot be given
a time expression later.

Only parameters whose filters implement a command interface are addressable this way. That
list is part of the media-pipeline contract, not something a caller may assume. `drawbox` is
excluded from the live colour surface for a separate reason — see [§2.7](#27-two-defects-the-escape-hatches-carry).

### 2.5 Visualization plugins: pre-instantiated branches

A plugin is a filtergraph fragment with a fixed pad contract, so any plugin can occupy the
same position in the graph. All configured plugin branches are instantiated at launch and run
concurrently; `streamselect` chooses which one reaches the encoder.

The honest cost: every instantiated branch consumes CPU whether or not it is on screen. The
number of plugins loaded per channel is therefore a capacity decision, not a free one. The
benefit is that switching visualization is a single runtime command instead of a broadcast
restart.

Spike S4 measured the switch as **frame-exact**: a clean single-frame cut with no dropped
frames and no restart. `astreamselect` behaves identically for audio. This is worth stating
plainly because it settles a question the relay was once thought to answer — a plugin swap is
gap-free on its own and never needed a restart domain around it.

### 2.6 The relay: a bounded restart gap, not a restart-proof session

Some changes cannot be made live — a resolution change, a frame-rate change, an encoder swap,
or a crash. For those, the composer must be relaunched, and relaunching a publisher that talks
directly to YouTube ends the broadcast.

MediaMTX sits between them. The composer publishes two RTMP streams per channel — the program
feed on `<channel>` and a low-resolution operator feed on `<channel>/preview`. MediaMTX relays
the program feed on to YouTube and serves the preview path as HLS. It does not transcode.

**The relay does not hold the YouTube session open.** Spike S5 measured MediaMTX 1.9.3
terminating *reader* connections whenever the publisher on a path changes:

```
INF [path spike01] closing existing publisher
INF [RTMP] [conn ...60384] closed: terminated    <- this is the reader
```

Four variants were measured:

| Variant | Composer swap | Relay path outage | YouTube-leg outage | YouTube publisher |
|---------|---------------|-------------------|--------------------|-------------------|
| A — bare ffmpeg publisher | kill + 2 s restart | 2.26 s | ≥ 11.01 s, never recovered | **dead** |
| B — supervised loop | kill + 2 s restart | 2.26 s | 5.14 s | relaunched |
| C — bare ffmpeg publisher | make-before-break | **0 s — path never dropped** | ≥ 10.80 s, never recovered | **dead** |
| D — fast-probe supervised loop | make-before-break | **0 s** | **1.03 s** | relaunched |

Variant C is the decisive one: the relay path never went not-ready, and the reader was killed
anyway. **Path continuity at the relay does not imply reader continuity.** MediaMTX has a
`fallback:` key, but it is a read-time redirect — "if the stream is not available, send readers
to this path" — not a splice. It cannot rescue an already-connected reader. There is no
Icecast-style fallback mount in MediaMTX, so the trick that makes §2.2 gap-free is not available
here.

Two consequences follow.

**The YouTube publisher must be an external supervised loop.** FFmpeg's `-reconnect*` flags do
not apply to the RTMP demuxer, so a bare `ffmpeg -i rtmp://relay/<ch> -f flv <youtube-url>` dies
permanently on the first composer swap — variants A and C. The loop needs fast-probe flags
(`-fflags nobuffer -analyzeduration 500000 -probesize 250000`) and sub-second retry; that
combination is what buys variant D's 1.03 s.

**Composer swaps must be make-before-break.** Start the replacement composer, let it publish,
then stop the old one. Kill-then-restart costs 5.14 s on the YouTube leg even when the publisher
is supervised; make-before-break with a fast-probe loop costs 1.03 s.

#### Then why keep the relay?

Because a second is not eighteen, and because the relay buys three things that are still worth
one global container and one extra hop:

| What it buys | Detail |
|--------------|--------|
| A deterministic gap instead of a backoff-driven one | A publisher pointed straight at YouTube is back on FFmpeg's doubling ladder — the same mechanism that turned a 9.4 s audio outage into an 18.19 s stall in §2.2. Behind the relay, a supervised make-before-break swap lands at ~1 s, every time. |
| Independent legs | Program and preview are separate paths on the relay. An operator opening a preview reads a different path entirely and never touches the broadcast leg. |
| Credential isolation | The stream key lives in one global service. Composer containers are rendered, restarted, and scaled per channel and never hold it. |

**And the rule in §2 is now more important, not less.** The relay makes a restart cost about a
second; it does not make one free. The only mechanisms measured at *zero* are the ones that
avoid the restart entirely — `streamselect` switching pre-instantiated graphs frame-exactly
(§2.5), the producer changing images with the FFmpeg PID unchanged (§2.3), and Icecast's
fallback mount covering a full audio-engine restart (§2.2). Plugin swaps in particular never
needed the relay at all. Every feature that can be built on an escape hatch must be; the relay
is what is left over for the cases that genuinely cannot.

### 2.7 Two defects the escape hatches carry

Both were found in Phase 0. Both constrain the backend rather than the media pipeline, so they
are recorded here rather than left in a lane document.

**1. A malformed ZMQ message aborts FFmpeg.** Any message that does not parse into at least two
whitespace-separated tokens causes heap corruption and terminates the process with exit 134.
The `zmq` filter's default `bind_address` is `tcp://*:5555` — every interface, no authentication
— so in the default configuration a single malformed packet is a remote kill of the encoder.
Three requirements follow: bind the socket to loopback or a private Docker network only, never
publish the port to the host, and treat the client that composes commands as a security
boundary that validates every message before it is sent.

**2. A failed `drawbox` command disables that filter instance permanently.** `drawbox` re-runs
its `init()` on every runtime command and does not roll back on failure, so one rejected value
leaves that instance broken for the remaining life of the process. `eq` and `hue` do not have
this flaw. `drawbox` is therefore not usable as the live colour surface; palette-driven colour
goes through `eq` and `hue`.

## 3. Container topology

Two containers per channel, plus three global containers.

| Scope | Container | Responsibility |
|-------|-----------|----------------|
| Global | `backend` | FastAPI control plane: REST, SSE, static operator UI, supervisor, watchdog, scheduler |
| Global | `icecast` | Audio relay. One instance serves every channel; each channel gets its own mount plus its own fallback mount (§2.2) |
| Global | `mediamtx` | RTMP relay of the program feed to YouTube; HLS server for the per-channel preview path (§2.6) |
| Per channel | `<ch>-liquidsoap` | Playlist, crossfade, loudness; publishes to Icecast as a source client |
| Per channel | `<ch>-composer` | Slideshow producer plus FFmpeg compositor and encoder |

At the supported maximum of 8 channels that is **19 containers**:

| Channels | Per-channel containers | Global containers | Total |
|----------|------------------------|-------------------|-------|
| 1 | 2 | 3 | 5 |
| 4 | 8 | 3 | 11 |
| 8 | 16 | 3 | 19 |

The third global container is a Phase 0 correction: the design assumed Liquidsoap would serve
the composer directly and needed no relay. It cannot (§2.2), so Icecast is now load-bearing.

Where the supervised YouTube publisher loop (§2.6) is hosted — a MediaMTX `runOnReady` hook or
a separate supervised process — is **still open**. It is not a per-channel container in either
case, so it does not change the count above.

A container-per-concern decomposition — separate containers for the slideshow producer, the
color applier, the encoder, the relay, and the metrics exporter — would put the same workload
in roughly 40 containers at 8 channels. That is rejected. The slideshow producer is a pipe
away from FFmpeg and gains nothing from a process boundary, and every extra container is
another supervised object, another restart policy, and another failure mode on a host that
must run unattended for months.

The splits that do exist each buy something specific: Liquidsoap is separate because it is the
audio escape hatch, Icecast is separate because it is what makes that hatch gap-free, and
MediaMTX is separate because it bounds the cost of a composer restart and keeps the stream key
out of the per-channel containers.

### 3.1 Data flow

```mermaid
flowchart LR
    subgraph host["Single Linux host"]
        subgraph globals["Global"]
            backend["backend<br/>FastAPI control plane"]
            ice["icecast<br/>audio relay + fallback mounts"]
            relay["mediamtx<br/>RTMP relay + HLS"]
        end

        subgraph chan["Per channel (1..8)"]
            liq["&lt;ch&gt;-liquidsoap<br/>playlist, crossfade"]
            slides["slideshow.py<br/>image2pipe producer"]
            comp["&lt;ch&gt;-composer<br/>ffmpeg filtergraph"]
        end
    end

    media[("channels/&lt;ch&gt;/<br/>music, images,<br/>color profiles")]

    media --> liq
    media --> slides
    slides -->|"image2pipe (stdout to stdin)"| comp
    liq -->|"source client, MP3 256k"| ice
    ice -->|"HTTP audio, mount + fallback"| comp
    backend -.->|"docker compose -p &lt;ch&gt;"| chan
    backend -.->|"zmq: colors, plugin switch"| comp
    comp -->|"RTMP: program + preview"| relay
    relay -->|"RTMP via supervised publisher loop"| yt["YouTube Live"]
    relay -->|"HLS preview path"| backend
    backend -->|"REST + SSE + HLS preview"| ui["Operator browser"]
```

Solid arrows carry media. Dashed arrows carry control. The two relays are there for different
reasons: Icecast makes an audio-engine restart invisible (§2.2), MediaMTX bounds the cost of a
composer restart (§2.6). Only the first is gap-free.

## 4. End-to-end data flow

1. **Audio origin.** Liquidsoap reads the channel's music directory with a watched playlist,
   applies crossfade and loudness normalization, and connects to the global Icecast container
   as a source client on a mount named for the channel, encoding MP3 256 kbps CBR / 44.1 kHz /
   stereo. Its output must be infallible (`mksafe` outermost) — a bad file must never take down
   the mount the composer is attached to.
2. **Audio ingest.** The composer takes `http://icecast:8000/<channel>` as an FFmpeg input with
   `-probesize 32k -analyzeduration 500000` and the full reconnect set — `-reconnect`,
   `-reconnect_at_eof`, `-reconnect_streamed`, `-reconnect_on_network_error`,
   `-reconnect_delay_max 120`. Those flags are for startup ordering and for an Icecast outage,
   not for a Liquidsoap restart; a Liquidsoap restart is absorbed by the fallback mount and the
   composer never disconnects (§2.2). Audio is resampled to a single clock, per the
   `youtube-ingest` skill.
3. **Image origin.** The slideshow producer scans the channel's image directory, orders the
   images, renders crossfades, and writes encoded frames to stdout.
4. **Image ingest.** FFmpeg reads those frames with `-f image2pipe`, then paces them with
   `fps=<rate>` followed by `realtime`.
5. **Color application.** Color profile JSON for the current image is read by the backend,
   which sends ZMQ runtime commands to the named filter instances that carry palette-driven
   parameters. The graph is not re-parsed.
6. **Visualization.** The audio stream also feeds the visualization branches. All configured
   plugin branches run; `streamselect` routes the active one into the composite.
7. **Composite and encode.** Slideshow, visualization, and any overlay are composited, then
   encoded with the CBR, fixed-GOP, no-scene-cut settings the `youtube-ingest` skill
   specifies, muxed to FLV.
8. **Publish.** The composer pushes two RTMP streams to `mediamtx` on the internal Docker
   network: the program feed on `<channel>` and a low-resolution operator feed on
   `<channel>/preview`. It never holds the YouTube stream key.
9. **Fan-out.** A supervised publisher loop reads the program path and pushes to YouTube's
   ingest endpoint with the channel's stream key; MediaMTX serves the preview path as HLS. The
   loop must be supervised and fast-probing: FFmpeg's reconnect flags do not apply to the RTMP
   demuxer, so a bare publisher dies permanently the first time the composer is swapped (§2.6).
10. **Preview.** The backend re-exposes that HLS rendition to the operator UI. Watching a
    channel costs composer-side encoding of a second, smaller feed — paid continuously whether
    or not anyone is watching — and never interferes with the broadcast leg.

Two properties of this chain matter more than the individual steps:

- **No control path touches the media path.** Control reaches the composer only as ZMQ
  parameter commands, and reaches Liquidsoap only through files on disk it already watches.
- **The stages do *not* fail independently — the audio input gates the whole composer.** S1
  measured an audio-source outage freezing the composer's video output too, for the entire
  reconnect window. That is why the audio path is built on Icecast rather than on reconnect, and
  it means "the process is alive" is not a health signal: the watchdog must verify that the
  composer's output is *advancing*. A dead composer, by contrast, is bounded rather than
  absorbed — about 1 s on the YouTube leg if the swap is make-before-break and the publisher is
  supervised, and an ended broadcast if it is not.

## 5. Control plane

The backend is a single FastAPI application. It is the only component that talks to Docker.

| Module | Responsibility |
|--------|----------------|
| `main.py` | Application assembly, router mounting, static UI |
| `config.py` / `models.py` | Load and validate configuration; Pydantic models are the schema |
| `supervisor.py` | Render per-channel Compose files, start/stop/restart channels |
| `watchdog.py` | Detect unhealthy channels, restart with exponential backoff. Health is "output is advancing", not "process is alive" (§4) |
| `scheduler.py` | Time-based changes (playlist, plugin, preset) |
| `colorprofile.py` | Extract palettes from images into profile JSON |
| `plugins.py` / `presets.py` | Discover and validate plugin and preset packs |
| `events.py` | SSE event hub — in-memory queue plus a lock, no broker |
| `metrics.py` | Health and throughput data for the UI |
| `ffmpeg_cmd.py` | Assemble the composer command from lane-supplied fragments |
| `zmqctl.py` | Send runtime commands to a running graph. Validates every message before sending; a malformed one kills FFmpeg (§2.7) |

These modules are the planned decomposition, owned by the `backend-api` lane.

### 5.1 Channel lifecycle

The backend does not run FFmpeg or Liquidsoap directly. It generates Compose files and lets
Docker own process supervision.

1. Operator creates or edits a channel through the UI.
2. The supervisor renders `docker/compose.channel.yml.j2` into
   `channels/<name>/docker-compose.yml`. Generated Compose files are gitignored.
3. The supervisor runs `docker compose -p <name>` against that file, giving each channel its
   own Compose project namespace.
4. Docker restarts crashed containers (`restart: unless-stopped`); the watchdog handles the
   cases Docker cannot see, such as a process that is running but no longer producing frames.

A replacement composer is started **before** the outgoing one is stopped whenever the restart is
planned rather than a crash — make-before-break is what holds the YouTube-leg gap at ~1 s
instead of 5 s (§2.6).
Rendering a file rather than constructing containers through the API is deliberate. The
generated Compose file is readable, diffable, and an operator can run it by hand when the
backend is down.

### 5.2 Live updates to the browser

Live state reaches the UI over **Server-Sent Events**, backed by an in-memory queue and a
lock. There is no message broker and no WebSocket upgrade. Channel state changes, health
transitions, and log lines are all events on that stream; the browser applies them to the DOM
without a reload.

### 5.3 Frontend

The operator UI is plain HTML, CSS, and JavaScript served directly by FastAPI. There is no
npm, no bundler, and no framework. Third-party libraries — for example an HLS playback
library for the preview — are vendored as single files under `frontend/vendor/` and committed.

Server-supplied strings (channel names, track titles, file names) are rendered with
`textContent`, never `innerHTML`. Those strings come from disk and from operator input, and
this is the one place in the project where an XSS bug is realistically reachable.

### 5.4 Security posture

| Property | Consequence |
|----------|-------------|
| The backend mounts the Docker socket | The backend is root-equivalent on the host. It binds to localhost by default and requires authentication. |
| Channel names reach Docker and the filesystem | Every Docker-touching endpoint treats its input as hostile and sanitizes before interpolation. |
| A malformed ZMQ message kills FFmpeg | Measured: a message that does not parse into two whitespace-separated tokens aborts the encoder with exit 134, and the filter's default bind is all interfaces with no auth. The socket binds to loopback or a private Docker network only and is never published; `zmqctl.py` validates every command before sending it and is a security boundary, not a convenience wrapper (§2.7). |
| Stream keys are secrets | Supplied via Docker secrets or a per-channel `.env`. Never baked into an image, never in a tracked file, never logged, never placed in the DOM. |
| The Icecast source password is a secret | Same handling as a stream key. It is what authorizes a source client to take over a channel's mount. |
| The watchdog restarts channels | Backoff is exponential and retries the same channel. A tight restart loop against YouTube ingest looks like abuse. |

## 6. Lanes and contracts

Work is split into file-disjoint lanes so that agents and contributors can work in parallel
without colliding.

| Lane | Owns |
|------|------|
| `media-pipeline` | `ffmpeg/`, `liquidsoap/`, `plugins/`, `presets/` |
| `backend-api` | `backend/` |
| `frontend` | `frontend/` |
| `infra` | `docker/`, `docker-compose.yml`, `scripts/`, `.github/workflows/` |
| `docs` | `docs/`, `README.md` |

Shared, cross-lane files — `ambient.yaml.example`, `.gitignore`, and everything under
`docs/contracts/` — are edited by the lead only.

`docs/contracts/` is the source of truth for every interface that crosses a lane boundary:

| Contract | Binds |
|----------|-------|
| REST + SSE shapes | `backend-api` ↔ `frontend` |
| Plugin filtergraph pad contract | `media-pipeline` ↔ `backend-api` |
| ZMQ runtime command protocol | `media-pipeline` ↔ `backend-api` |
| Preset and color profile schemas | `media-pipeline` ↔ `backend-api` ↔ `frontend` |
| Per-channel Compose template inputs | `infra` ↔ `backend-api` |

An interface is read from its contract, never inferred from another lane's implementation. A
contract that is wrong or missing is reported to the lead, not changed unilaterally.

One boundary is worth calling out because it looks misplaced: `backend/ambient/ffmpeg_cmd.py`
builds an FFmpeg command line but belongs to `backend-api`. The `media-pipeline` lane supplies
the filtergraph fragments and the contract that describes them; the backend assembles the
final command.

## 7. Encoders

Three encoder profiles are supported. The rate-control intent is identical across all three —
CBR, fixed GOP, no scene-cut keyframes — because that is what YouTube ingest expects. See the
`youtube-ingest` skill for the exact flags and the bitrate ladder.

| Profile | Requires | Docker Desktop / WSL2 | Notes |
|---------|----------|-----------------------|-------|
| `libx264` | CPU only | Yes | Always available. The fallback for everything. |
| `h264_nvenc` | NVIDIA GPU, nvidia-container-toolkit, `--gpus all` | Yes | Consumer GeForce cards cap concurrent encode sessions. |
| `h264_qsv` | Intel GPU, `/dev/dri` passthrough | **No** | Bare-metal Linux only. |

**QSV cannot work on Docker Desktop or WSL2.** `/dev/dri` is not exposed there, so the device
the encoder needs does not exist inside the container. The profile is valid on bare-metal
Linux; the installer detects the platform and does not offer it otherwise. This is a hard
platform limitation, not a configuration problem to work around.

**NVENC works on WSL2** through `--gpus all` plus nvidia-container-toolkit, but consumer
NVIDIA cards limit how many simultaneous encode sessions the driver will grant. Channels
beyond that cap fall back to `libx264` automatically rather than failing to start. A host with
more channels than NVENC sessions is a normal, supported configuration — it just needs the CPU
headroom for the overflow.

Encoder availability is verified at runtime (`ffmpeg -hide_banner -encoders`) rather than
assumed from configuration. A channel never fails to start because a configured encoder is
absent; it degrades to `libx264`.

### 7.1 Other host constraints

| Constraint | Why |
|------------|-----|
| Media must not live on `/mnt/c/...` | On Docker Desktop/WSL2 that path is 9p-backed. It is far too slow for continuous reads and will starve a channel. Use a WSL2 ext4 path or a Docker named volume. |
| Per-channel CPU quota, memory limit, and GPU assignment are set | One misbehaving channel must not take down the other seven. |
| Base image versions are pinned | A 24/7 service whose FFmpeg build changes silently is a liability. |

## 8. Why not X

Every item here is a deliberate deviation from a common default. Each was chosen for a reason
specific to this system.

| Choice | Common alternative | Why not the alternative |
|--------|--------------------|--------------------------|
| SSE | WebSockets | Traffic is one-way: server to browser. SSE reconnects on its own, works through ordinary HTTP proxies, and needs no broker — an in-memory queue and a lock. WebSockets would add a bidirectional protocol and its failure modes for a channel that only ever flows one way. |
| Plain HTML/CSS/JS | SvelteKit or React | The UI is one page with progressive disclosure. A build step would mean npm in the image, a toolchain to keep current, and a compile between editing a file and seeing the result. The operator UI is not the hard part of this system, and it should not be the part with the most dependencies. |
| pip + `pyproject.toml` (setuptools) | Poetry | Poetry is unused everywhere else in this workspace and adds weight to every container image for no gain here. Standard `pyproject.toml` with setuptools installs with the pip that is already in the base image. |
| 2 containers per channel + 3 global | ~5 containers per channel | 19 containers at 8 channels versus roughly 40. The three splits that exist each buy something specific: Liquidsoap is the audio escape hatch, Icecast is what makes that hatch gap-free (measured: 0 s versus 18.19 s), and MediaMTX bounds the cost of a composer restart while keeping the stream key out of the per-channel containers. Splitting the slideshow producer from FFmpeg would replace a pipe with a network hop and add a supervised object per channel for nothing. |
| Stream keys created by hand in YouTube Studio | YouTube Data API broadcast management | API-managed broadcasts require OAuth credentials with broad account scope, carry quota limits, and add an external dependency to channel startup. A stream key is a long-lived string created once per channel. Manual creation keeps the system free of Google API credentials entirely, and there is no automation win when channels are created a handful of times per year. |

## 9. Related documents

These companion documents are planned in this lane and are not all written yet.

| Document | Covers |
|----------|--------|
| `quickstart.md` | Ubuntu Server install through first live channel |
| `plugin-development.md` | Writing a `viz.ffmpeg`, the pad contract, testing a plugin |
| `visualization-filters.md` | `showwaves` / `showfreqs` / `avectorscope` reference |
| `color-profiles.md` | Profile JSON schema, extraction, adding an extractor |
| `api-reference.md` | Every REST endpoint and SSE event |
| `docker-deployment.md` | Compose topology, GPU passthrough, resource limits, secrets |
| `scaling.md` | Adding channels, capacity planning, encoder ceilings |

The `youtube-ingest` skill under `.github/skills/` holds the proven encoder settings and is
the authority for anything on the ingest path.

The Phase 0 spikes are the evidence behind the measured claims in this document. Each directory
holds its harness and its raw output.

| Spike | Question it answered |
|-------|----------------------|
| `spikes/s1-liquidsoap-transport/` | What carries audio from Liquidsoap to the composer (§2.2) |
| `spikes/s2-slideshow/` | Does `image2pipe` support a live media directory, and what pacing does it need (§2.3) |
| `spikes/s3-zmq-color/` | What runtime commands can actually do, at what latency and what rate (§2.4, §2.7) |
| `spikes/s4-streamselect/` | How clean is a plugin switch (§2.5) |
| `spikes/s5-mediamtx-relay/` | What does the relay protect against, and what does it not (§2.6) |
