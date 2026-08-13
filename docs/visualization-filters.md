# Visualization filters

What each FFmpeg filter in the composer's graph does, how it behaves in a 24/7 stream, and
which of its parameters can be changed without a restart.

For writing a plugin, see [plugin-development.md](plugin-development.md). For the runtime
command protocol, see [`contracts/zmq-control.md`](contracts/zmq-control.md).

---

## The graph a channel actually runs

Read it out of the running container — it is written to a file at launch:

```bash
docker exec <channel>-composer cat /run/ambient/<channel>/filtergraph.txt
```

With a single hot plugin it is:

```
[1:v]fps=30:start_time=0,realtime,
     zmq@ctl=bind_address=tcp\://127.0.0.1\:5555,
     eq@eq=eval=frame:contrast=1:brightness=0:saturation=1,
     hue@hue=h=0,format=yuv420p,setsar=1[base];
[0:a]showfreqs@viz=s=1280x720:mode=bar:...:colors=0x4FC3F7,fps=30,format=yuv420p,setsar=1[viz0];
[viz0]null[viz];
[base][viz]blend=all_mode=screen:all_opacity=0.65,format=yuv420p[vfull];
[vfull]split=2[vmain][vpre];
[vpre]scale=640:360:flags=fast_bilinear,fps=15[vpreview];
[0:a]aresample=44100:async=1000:first_pts=0,loudnorm=I=-14:TP=-1:LRA=11,asplit=2[amain][apreview]
```

With two or more hot plugins, `[viz0]null[viz]` is replaced by
`[viz0][viz1]...streamselect@sel=inputs=N:map=<active>[viz]`.

Input **0 is audio** (Icecast), input **1 is video** (the slideshow producer on `image2pipe`).
That ordering is deliberate: it makes a plugin fragment's literal `[0:a]` correct as written.

---

## Audio visualisers

Four are used by shipped plugins. Three of them are verified present in the composer image —
the build fails if `showfreqs`, `showwaves` or `avectorscope` is missing. `showspectrum` is
not asserted.

### `showfreqs` — frequency spectrum

Used by `plugins/showfreqs-bars`, which is the compositor's built-in default.

| Option | Values | Notes |
|--------|--------|-------|
| `s` | `WxH` | **must** be the channel geometry |
| `mode` | `line` `bar` `dot` | `bar` reads well at a distance; `line` is quieter |
| `ascale` | `lin` `sqrt` `cbrt` `log` | amplitude scale. `log` keeps quiet passages visible |
| `fscale` | `lin` `log` `rlog` | frequency scale. `log` spreads bass out instead of crushing it into the left edge |
| `win_size` | power of two | FFT size. Larger = finer frequency resolution, slower response |
| `averaging` | integer | frames averaged. Higher = smoother, laggier. `2` is calm without feeling dead |
| `colors` | color list | launch-time only |

Ambient music is quiet and bass-heavy, which is why the shipped preset is
`ascale=log:fscale=log:win_size=1024:averaging=2` — linear scales leave most of the frame empty.

**Commandable: nothing.** Measured — `showfreqs` exposes no runtime-tunable parameters at all.
Its color cannot be changed live. Color comes from `eq`/`hue` downstream instead.

**Cost:** measured 0.24 cores at 720p30, scaling ~1.9× at 1080p.

### `showwaves` — waveform

Used by `plugins/showwaves-classic` (`mode=line`, `draw=scale`, `scale=sqrt`) and
`plugins/minimal-line` (`mode=p2p`, `draw=full`, `scale=lin`).

| Option | Values | Notes |
|--------|--------|-------|
| `s` | `WxH` | must be the channel geometry |
| `mode` | `point` `line` `p2p` `cline` | `cline` (centerd line) is the usual ambient choice |
| `n` | integer | samples per column; controls horizontal scroll rate |
| `rate` | fps | output rate. Set it to the channel fps, or let the required `fps` tail do it |
| `draw` | `scale` `full` | `full` draws every sample rather than a scaled envelope |

**Commandable: nothing.** Same measured result as `showfreqs`.

Cheaper than `showfreqs` — no FFT — but it renders as a thin band across the middle of the
frame, so it usually needs a background it can sit over rather than being the whole image.

### `avectorscope` — stereo field

Used by `plugins/avectorscope-lissajous`.

| Option | Values | Notes |
|--------|--------|-------|
| `s` | `WxH` | must be the channel geometry |
| `mode` | `lissajous` `lissajous_xy` `polar` | `lissajous` is the familiar diamond |
| `zoom` | float | magnification; ambient material is often quiet enough to need > 1 |
| `draw` | `dot` `line` | `line` is denser and more visible on a slideshow |
| `scale` | `lin` `sqrt` `cbrt` `log` | amplitude scale |
| `rc` `gc` `bc` | 0–255 | per-channel color weights |
| `swap` `mirror` | flags | orientation |

**Commandable — and it is the only one of the three that is:** `mode`, `rc`, `gc`, `bc`, `zoom`,
`draw`, `scale`, `swap`, `mirror`. Measured against the target build.

That makes `avectorscope` the natural home for a plugin that declares real `commandable`
entries, and `avectorscope-lissajous` declares nine of them. A mono or near-mono source
collapses the display to a diagonal line, which is correct behavior, not a fault.

### `showspectrum` — scrolling spectrogram

Used by `plugins/neon-spectrum`
(`slide=scroll:mode=combined:color=intensity:scale=cbrt:fscale=log:saturation=5:win_func=hann:overlap=0.5:gain=2:legend=0`).

A time-frequency waterfall rather than an instantaneous bar display, so it reads as texture on
ambient material where `showfreqs` reads as movement. `legend=0` matters: the legend consumes
part of the frame, and the frame must be exactly the channel geometry.

**Commandable: nothing.** Color comes from `color=` at launch and from `eq`/`hue` downstream.

**Not asserted by the composer image build.** The build hard-fails on a missing `showfreqs`,
`showwaves` or `avectorscope`, but not on a missing `showspectrum` — it is present in the
distro FFmpeg but a base-image change could remove it, and the failure would then appear at
channel start rather than at build time.

### Others in the FFmpeg build

`showcqt`, `showvolume`, `ahistogram` and friends exist in a full FFmpeg build but are **not
asserted present** by the composer image build, and none has been measured in this pipeline.
Before writing a plugin around one:

```bash
docker run --rm ambient-composer:dev ffmpeg -hide_banner -filters | grep <name>
docker run --rm ambient-composer:dev ffmpeg -hide_banner -h filter=<name>
```

List it in the manifest's `requires_filters`, and measure its cost with an encoder attached —
see [plugin-development.md](plugin-development.md#measuring-cost).

---

## Pipeline filters

These are the compositor's own, not a plugin's. A plugin should not duplicate them.

### `fps` and `realtime` — pacing

```
[1:v]fps=30:start_time=0,realtime,...
```

The slideshow producer emits ~10 fps; the output is 30 fps. `fps` duplicates frames to fill the
gap.

**`realtime` is mandatory, not an optimisation.** Without it the muxer was measured releasing
up to **53 frames — 1.77 s of media — inside a 0.5 s wall-clock window**. That burst is exactly
what makes YouTube buffer or stall, and it does not reproduce as a fault in local playback.

The duplication `fps` performs is internal and never reaches the muxer's counters, which is why
`dup_frames` in `-progress` stays at 0 even when pacing is broken. Do not use it as a health
signal. See [operations.md](operations.md).

### `zmq@ctl` — the control socket

```
zmq@ctl=bind_address=tcp\://127.0.0.1\:5555
```

Everything downstream of this instance in the same chain can be addressed at runtime.

Three things matter:

1. **Never `tcp://*:5555`.** That is the filter's default — all interfaces, no authentication —
   and a single malformed packet aborts FFmpeg with SIGABRT (exit 134). The composer binds
   loopback inside its own network namespace and the port is never published.
2. `bind_address` needs **two** levels of escaping. The filtergraph tokenizer strips one and
   the AVOption parser strips another. `tcp\\://...\\:5555` in a shell heredoc,
   `tcp\://...\:5555` in the written graph file.
3. The filter polls its socket **only when a frame passes through it**, so command latency is
   exactly one frame period and throughput ceilings at ~31.5 commands/s at 30 fps.

### `eq@eq` — brightness, contrast, saturation, gamma

```
eq@eq=eval=frame:contrast=1:brightness=0:saturation=1
```

**`eval=frame` must be set at launch.** `eval` is not itself commandable, so a graph built
without it can never be given a time expression later — the same command then changes only
0.7 % of frames and appears to do nothing.

| Commandable | Range |
|-------------|-------|
| `contrast` | −1000 to 1000 (useful: ~0.5–2) |
| `brightness` | −1 to 1 (useful: ±0.2) |
| `saturation` | 0 to 3 |
| `gamma`, `gamma_r`, `gamma_g`, `gamma_b`, `gamma_weight` | 0.1 to 10 |

**FFmpeg does not range-check.** `eq@eq brightness 99` returns `0 Success` and produces an
unwatchable frame. Every value must be checked by the caller. `backend/ambient/zmqctl.py` and
the Pydantic preset models exist for that.

### `hue@hue` — hue rotation

```
hue@hue=h=0
```

Commandable: `h` (degrees), `s`, `H` (radians), `b`. `hue` re-evaluates per frame
unconditionally, so it needs no `eval` equivalent.

Together, `eq` and `hue` are **the entire live color surface**. See
[color-profiles.md](color-profiles.md).

### `drawbox` — excluded on purpose

`drawbox` is commandable (`x` `y` `w` `h` `color` `c` `t` `replace`), and it is not used.

**Measured:** one failed command permanently disables that `drawbox` instance for the life of
the process. It re-runs `init()` on every runtime command and never rolls back a bad value, so
a single `22 Invalid argument` leaves it dead forever. Recovery needs the restart this system
exists to avoid. `eq` and `hue` do not behave this way.

`drawbox color` also takes no expression, so it could only ever hard-cut. Do not put a
`drawbox` on the live color path.

### `streamselect@sel` / `astreamselect` — the plugin switch

```
[viz0][viz1]streamselect@sel=inputs=2:map=0[viz]
```

Commandable: `map`. Not commandable: `inputs` — which is why `hot_set` is fixed at launch.

**Measured frame-exact:** frame *N−1* entirely the old branch, frame *N* entirely the new one.
No blended, torn or black frame, `drop=0 dup=0`, including switches 200 ms apart. Invalid
arguments (`map 9`, `map -1`, `map abc`, bare `map`) return `22 Invalid argument` and the graph
keeps running.

`inputs` has a minimum of 2, so a single hot plugin gets `[viz0]null[viz]` instead of a
selector.

`astreamselect` behaves identically for audio. It is not currently used.

### `blend` — the composite

```
[base][viz]blend=all_mode=screen:all_opacity=0.65
```

`screen` lightens: black in the visualization is transparent, bright areas add to the image.
That is what makes a visualiser readable over arbitrary photography without a mask.

Opacity comes from `VIZ_OPACITY` (default `0.65`) and is a **launch-time** value — `blend` is
not commandable, so changing it needs a new graph. To make a visualiser fade live, drive `eq`
on the composite instead.

`blend` requires both inputs at the same size. This is the second place a mis-sized plugin
branch does damage.

### `split` and `scale` — the preview

```
[vfull]split=2[vmain][vpre];
[vpre]scale=640:360:flags=fast_bilinear,fps=15[vpreview]
```

The preview is a **second encode**, not a free tap — MediaMTX does not transcode. `split` is
cheap; the second `libx264` pass behind it is not, and it is the single largest reason a
channel costs ~1.5 cores rather than what a filtergraph benchmark predicts.

`flags=fast_bilinear` and 15 fps are chosen to keep that second encode as cheap as possible.
The preview always uses `libx264` regardless of the channel encoder, because consumer NVENC
caps concurrent sessions and spending one on an operator preview costs a whole channel.

### `format` and `setsar` — hygiene

`format=yuv420p` and `setsar=1` appear at the end of every branch and after the blend. FFmpeg
would negotiate both, but stating them removes auto-inserted converters from the middle of the
graph and makes what each branch emits unambiguous.

### Audio chain

```
[0:a]aresample=44100:async=1000:first_pts=0,loudnorm=I=-14:TP=-1:LRA=11,asplit=2[amain][apreview]
```

| Filter | Why |
|--------|-----|
| `aresample=44100` | one clock for the whole stream. 44.1 kHz is what the ingest path mandates and what Liquidsoap encodes |
| `async=1000` | absorbs small drift between the audio clock and the video clock without a hard resync |
| `first_pts=0` | anchors the timeline so `out_time` starts at zero and is directly comparable to wallclock |
| `loudnorm=I=-14:TP=-1:LRA=11` | −14 LUFS is the level streaming platforms normalize to; `TP=-1` leaves a dB of true-peak headroom for the AAC encoder |
| `asplit=2` | one branch to the program encode, one to the preview |

`loudnorm` here is single-pass and operates on the composite stream. Per-track loudness
belongs to Liquidsoap, on the far side of Icecast, where it can change without touching the
compositor.

---

## What is and is not changeable at runtime

| Change | Mechanism | Cost |
|--------|-----------|------|
| Color (`eq`, `hue`) | ZMQ command, ideally a time expression | one frame |
| Active plugin, within the hot set | `streamselect@sel map N` | one frame, clean cut |
| Visualization on air / on standby | `overlay@viz enable 0\|1` | one frame |
| Slide set | producer rescans between slides | 0 s, FFmpeg PID unchanged |
| Playlist, track order | Liquidsoap, behind Icecast | 0 s |
| Slide timing, `enabled`, `hot_set`, plugin parameters, resolution, fps, encoder | **new filtergraph** | compositor restart |

A compositor restart costs ~13.7 s of YouTube outage as a plain `docker restart`, seconds as a
supervised make-before-break swap. Neither is zero. Design for the top five rows.

## Turning it off, and standby

Two different switches, and only one of them saves anything:

| | Branches render? | Cost | Changing it |
|---|---|---|---|
| `visualization.enabled: false` | no | the pipeline floor | a restart |
| `enabled: true, visible: false` | yes | same as on | one frame |
| `enabled: true, visible: true` | yes | same as on | — |

`enabled` is the lever. On a live 1080p30 channel it measured **0.999x realtime
with the visualization off against 0.415x with it on** — that channel could not
hold realtime at all until it was switched off.

`visible` is standby. It rides the composite overlay's timeline `enable`, so it
lands in one frame on the running graph, and the branches keep rendering — which
is exactly why it can be instant. Use it to take the visualization off air
without waiting for a restart, not to reclaim CPU.

```bash
# instant, no restart; refused with 409 if enabled is false
curl -X PUT .../api/channels/lofi/visualization/visible -d '{"visible": false}'
```

## Editing the hot set

Every plugin in `hot_set` renders continuously whether or not it is on screen —
that is what makes switching between them instant, and it is why membership is a
CPU budget rather than a preference. Switching to a plugin outside the set
appends it, so without this the cost could only ever grow.

```bash
curl -X PUT .../api/channels/lofi/hot-set \
  -d '{"hot_set": ["showfreqs-bars", "neon-spectrum"]}'
```

Dropping the branch that is on air needs a replacement named in `active`, or the
request is refused: `visualization.active` must stay inside `hot_set` or the
channel will not resolve. A channel whose visualization is off is not restarted
by this — its graph has no branches to rebuild.

## Tuning a plugin

Each plugin declares its own knobs in `plugins/<name>/config.json` under
`parameters`, and `GET /api/plugins` reports them with their ranges. They are
substituted into the fragment as `${TOKEN}` at launch.

```bash
curl -X PUT .../api/channels/lofi/visualization/parameters \
  -d '{"plugin": "showfreqs-bars", "values": {"detail": 2048, "shape": "line"}}'
```

Values outside a declared range are **clamped, not refused**, and the clamped
value is what gets stored. That is deliberate: FFmpeg accepts an out-of-range
filter option, ignores it, and renders the branch wrong at exit 0, so a silent
clamp is safer than a silent misrender. An *undeclared* name is refused.

Settings are kept per plugin, so switching away and back restores that plugin's
look. Applying them restarts the channel only if that plugin is being drawn.

---

## Related

| Document | Covers |
|----------|--------|
| [plugin-development.md](plugin-development.md) | writing a branch that fits this graph |
| [color-profiles.md](color-profiles.md) | how palettes become `eq`/`hue` expressions |
| [`contracts/zmq-control.md`](contracts/zmq-control.md) | message format, replies, targeting, escaping |
| [`contracts/plugin.md`](contracts/plugin.md) | the plugin package contract |
| [architecture.md](architecture.md) | why the graph is built this way |
