# Contract — Visualization plugin

Owner: lead. Consumers: media-pipeline, backend-api.

Everything marked **measured** was observed in spike S4.

## Package

```
plugins/<name>/
├── viz.ffmpeg     # filtergraph fragment
├── config.json    # manifest
└── preview.png    # thumbnail for the UI
```

## The one rule FFmpeg will not enforce for you

**A plugin's output width and height must equal the channel's output width and
height. Exactly.**

**Measured:** a 160×90 branch in a 320×180 graph produced **corrupted output
with exit code 0 and no error message**. FFmpeg logs
`streamselect config output link 0 with settings from input link 0` and does
not insert a rescaler.

Evidence via `signalstats`:

| | correct branch | size-mismatched branch |
|---|---|---|
| YMIN | 16 | **0** |
| YMAX | 145 | **255** |

`YMIN=0` / `YMAX=255` are outside the legal range for the stream's declared
`yuv420p(tv)` — the smaller buffer was being read as the larger geometry.

Other mismatches are handled automatically and are not errors:

| Mismatch | FFmpeg behavior |
|---|---|
| pix_fmt | inserts `auto_scale`, renders correctly |
| frame rate | framesync resolves it |
| SAR | taken from input 0 |
| **size** | **silently corrupts** |

Because only size is unguarded, the backend validates it before launch.

## Required tail

Every branch ends:

```
,fps=$OUT_FPS,format=yuv420p,setsar=1
```

Not required for correctness — FFmpeg would negotiate all three — but it avoids
auto-inserted converters, and it makes the size assertion meaningful by
removing any doubt about what the branch actually emits.

## `viz.ffmpeg`

A fragment with exactly one audio input pad and one video output pad.
Placeholders are substituted at launch.

```
[0:a]showfreqs@viz=s=${WIDTH}x${HEIGHT}:mode=bar:colors=${ACCENT},
     fps=${FPS},format=yuv420p,setsar=1[${OUT}]
```

| Placeholder | Substituted with |
|---|---|
| `${WIDTH}` `${HEIGHT}` | channel output geometry |
| `${FPS}` | channel output frame rate |
| `${ACCENT}` | current accent color |
| `${OUT}` | the branch label the compositor assigns |

Every filter instance the backend may address at runtime carries an explicit
`@label` — targeting by class name is forbidden; see
[zmq-control.md](zmq-control.md).

## `config.json`

```json
{
  "name": "showfreqs-bars",
  "display_name": "Spectrum Bars",
  "description": "Frequency spectrum drawn as vertical bars.",
  "version": "1.0.0",
  "author": "ambient-streamer",
  "commandable": [
    { "target": "showfreqs@viz", "param": "colors", "type": "color" }
  ],
  "cost": {
    "cores_720p30": 0.24,
    "scale_1080p": 1.9
  },
  "requires_filters": ["showfreqs"]
}
```

| Field | Meaning |
|---|---|
| `commandable` | parameters this plugin genuinely exposes at runtime. **Declaring one it does not expose is a broken plugin** |
| `cost.cores_720p30` | measured idle cost of one instantiated branch |
| `cost.scale_1080p` | multiplier at 1080p |
| `requires_filters` | checked against `ffmpeg -filters` at load; missing filter disables the plugin rather than failing a channel |

**Measured:** `showwaves` and `showfreqs` expose **no** runtime-commandable
parameters at all. `avectorscope` exposes `mode`, `rc`, `gc`, `bc`, `zoom`,
`draw`, `scale`, `swap`, `mirror`. Color for the first two therefore has to
come from `eq`/`hue` downstream, not from the plugin itself.

## Switching

All plugins in a channel's `hot_set` are instantiated at launch and fed to
`streamselect`. Switching is `streamselect@sel map N`, where N is the plugin's
index in `hot_set`. The backend owns that mapping.

**Measured:** switching is frame-exact — six switches, zero frame error,
including two 200 ms apart. The cut is clean: frame N−1 is entirely the old
branch, frame N entirely the new one. No blended, torn or black frame,
`drop=0 dup=0`.

Invalid commands are safe: `map 9`, `map -1`, `map abc` and a bare `map` all
return `22 Invalid argument` and the graph keeps running.

`astreamselect` behaves identically for audio.

## Idle branches are not free

**Measured** at 1280×720, 30 fps, `-re` paced:

| Configuration | CPU |
|---|---|
| audio source only | 3 % |
| 1 branch | 20 % |
| 3 branches + `streamselect` | 73.5 % |
| 3 branches + `streamselect` + libx264 CBR | **95.6 %** |

Roughly **0.28 cores per idle branch at 720p**, scaling ~1.9× at 1080p.

`hot_set` is a CPU budget, not a preference list. The backend refuses a
`hot_set` whose projected cost would oversubscribe the host — see
[config.md](config.md).

### What a whole channel actually costs

The figures above are filtergraph benchmarks. A **running** channel measured
**~1.5 cores at 720p with one hot plugin** — higher than the benchmark implies,
because the channel also decodes MP3 from Icecast, decodes JPEG off the
producer pipe, and **encodes the HLS preview as a second output**. The preview
is not a free tap off the program encode.

Budget from the measured 1.5, not from the benchmark table, and add ~0.28
per additional hot plugin.

## Benchmarking caveat

**Do not benchmark a `streamselect` graph with `-f null -`.** A filter-only
run of three branches took **542 s of wall clock for 20 s of content**, while
the identical graph terminated by a real encoder took 19.67 s. The cause is
unresolved and suspected to be framesync stalling with no downstream
backpressure. Always measure with the encoder attached, which is what
production runs anyway.
