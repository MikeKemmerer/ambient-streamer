# Plugin development

A visualisation plugin is a filtergraph fragment plus a manifest. Any plugin can occupy the
same position in a channel's graph, so switching between them at runtime is a single command
and never a restart.

The normative interface is [`contracts/plugin.md`](contracts/plugin.md). This guide is how to
write one against it.

---

## The rule FFmpeg will not enforce

> **A plugin's output width and height must equal the channel's output width and height.
> Exactly.**

Get this wrong and there is no error, no warning, and no non-zero exit code. The stream is
simply corrupt.

**Measured:** a 160×90 branch inside a 320×180 graph produced corrupted output with **exit
code 0**. FFmpeg logs `streamselect config output link 0 with settings from input link 0` and
does not insert a rescaler — the smaller buffer is read as the larger geometry.

`signalstats` on the two branches:

| | correct branch | size-mismatched branch |
|---|---|---|
| `YMIN` | 16 | **0** |
| `YMAX` | 145 | **255** |

`YMIN=0` / `YMAX=255` are outside the legal range for the stream's declared `yuv420p(tv)`.

Every other mismatch is handled for you:

| Mismatch | FFmpeg behaviour |
|---|---|
| pixel format | inserts `auto_scale`, renders correctly |
| frame rate | framesync resolves it |
| SAR | taken from input 0 |
| **size** | **silently corrupts** |

Because only size is unguarded, it is checked in two places before launch — see
[§Validation](#validation).

**The practical consequence: always derive your size from `${WIDTH}x${HEIGHT}`.** Never write a
literal resolution into a fragment. A literal is unverifiable at authoring time and the
compositor refuses it outright.

---

## Package layout

```
plugins/<name>/
├── viz.ffmpeg     # the filtergraph fragment
├── config.json    # the manifest
└── preview.png    # thumbnail (see the note below)
```

The directory name is not the plugin name — `config.json`'s `name` field is. Keep them the
same anyway; nothing else does.

`plugins/` is mounted read-only into every composer container at `/plugins`, and read by the
backend's registry at `<repo>/plugins`. Adding a directory is enough to install a plugin; there
is no registration step.

> `preview.png` is specified by the contract but nothing currently reads it — the registry does
> not load it and `GET /api/plugins` does not return it. Only `showfreqs-bars` ships one. Add
> one anyway; it is cheap and the UI will want it.

Five plugins ship today. They are the working references:

| Plugin | Filter | `commandable` | `cores_720p30` |
|--------|--------|---------------|----------------|
| `showfreqs-bars` | `showfreqs` | none | 0.24 |
| `showwaves-classic` | `showwaves` | none | 0.24 |
| `minimal-line` | `showwaves` | none | 0.22 |
| `neon-spectrum` | `showspectrum` | none | 0.25 |
| `avectorscope-lissajous` | `avectorscope` | 9 parameters | 0.25 |

`avectorscope-lissajous` is the one to read if you need a plugin with real runtime controls;
the other four correctly declare `"commandable": []`.

---

## `viz.ffmpeg`

A filtergraph fragment with **exactly one audio input pad and one video output pad**, on one
logical line. Newlines are collapsed to spaces before substitution, so you may wrap for
readability.

```
[0:a]showfreqs@viz=s=${WIDTH}x${HEIGHT}:mode=bar:ascale=log:fscale=log:win_size=1024:averaging=2:colors=${ACCENT},fps=${FPS},format=yuv420p,setsar=1[${OUT}]
```

### Input pad

Always `[0:a]`. Input 0 of the composer is the Icecast audio stream — the compositor orders its
inputs audio-first specifically so a fragment's literal `[0:a]` is correct as written.

### Placeholders

Substituted by `ffmpeg/entrypoint.sh` at launch, by literal string replacement.

| Placeholder | Substituted with | Example |
|-------------|------------------|---------|
| `${WIDTH}` | channel output width | `1280` |
| `${HEIGHT}` | channel output height | `720` |
| `${FPS}` | channel output frame rate | `30` |
| `${ACCENT}` | current accent colour, **as `0xRRGGBB`** | `0x4FC3F7` |
| `${OUT}` | the output label the compositor assigns | `viz0` |

`${ACCENT}` is converted from `#RRGGBB` to `0xRRGGBB` before substitution, because `#` is a
filtergraph escaping problem. Use it directly; do not add a `#`.

`${ACCENT}` is a **launch-time** value. It is not re-substituted later — live colour comes from
`eq` and `hue` downstream of your branch, not from re-writing your fragment. See
[color-profiles.md](color-profiles.md).

### The required tail

Every branch must end:

```
,fps=${FPS},format=yuv420p,setsar=1[${OUT}]
```

FFmpeg would negotiate all three on its own. They are required anyway for two reasons: they
stop auto-inserted converters appearing in the middle of a graph you are trying to reason
about, and they make the size assertion meaningful by removing any doubt about what the branch
actually emits.

### Labels

Every filter instance the backend may address at runtime carries an explicit `@label`
(`showfreqs@viz`, not `showfreqs`). Targeting a filter by bare class name is forbidden —
it broadcasts to every instance of that class, including ones in other branches.
See [`contracts/zmq-control.md`](contracts/zmq-control.md).

---

## `config.json`

```json
{
  "name": "showfreqs-bars",
  "display_name": "Spectrum Bars",
  "description": "Frequency spectrum drawn as vertical bars, log-scaled on both axes.",
  "version": "1.0.0",
  "author": "ambient-streamer",
  "commandable": [],
  "cost": {
    "cores_720p30": 0.24,
    "scale_1080p": 1.9
  },
  "requires_filters": ["showfreqs"]
}
```

| Field | Required | Meaning |
|-------|----------|---------|
| `name` | yes | registry key. Load fails without it |
| `display_name` | no | UI label. Defaults to `name` |
| `description` | no | one sentence, shown in the UI |
| `version` | no | plugin version, defaults `0.0.0` |
| `author` | no | free text |
| `commandable` | no | `[{ "target", "param", "type" }]` — parameters this plugin **genuinely** exposes at runtime |
| `cost.cores_720p30` | no | measured idle cost of one instantiated branch. Defaults `0.25` |
| `cost.scale_1080p` | no | multiplier at 1080p. Defaults `1.9` |
| `requires_filters` | no | filter names checked against the build at launch |
| `output_size` | no | `"channel"` or a literal `WxH`. Omit it unless you know why you need it |

### Declare `commandable` honestly

**Declaring a parameter the plugin does not expose is a broken plugin.** The backend will
happily build a command for it, FFmpeg will answer `38 Function not implemented`, and the
operator sees a control that silently does nothing.

**Measured against the target build:** `showwaves` and `showfreqs` expose **no**
runtime-commandable parameters at all. `avectorscope` exposes `mode`, `rc`, `gc`, `bc`, `zoom`,
`draw`, `scale`, `swap`, `mirror`.

Check your own filter before writing the field:

```bash
docker run --rm ambient-composer:dev ffmpeg -hide_banner -h filter=showfreqs
```

Only options carrying the `T` flag in that output are commandable. If your filter has none —
which is the common case — write `"commandable": []` and let colour ride on `eq`/`hue`
downstream. That is what `showfreqs-bars` does.

### `requires_filters`

Checked by `ffmpeg/entrypoint.sh` against `ffmpeg -filters` at composer launch. A missing
filter **kills the composer at startup** with a clear message, rather than producing a broken
graph.

> The contract says a missing filter should disable the plugin rather than fail a channel. The
> shipped entrypoint fails the channel. Do not rely on graceful degradation.

The composer image build asserts that `zmq`, `azmq`, `streamselect`, `astreamselect`,
`showfreqs`, `showwaves` and `avectorscope` are all present, so those seven are safe to
require. **`showspectrum` is not on that list**, even though `neon-spectrum` requires it — it
is present in the distro FFmpeg the image is built from, but a base-image change could remove
it without failing the build. Anything outside the asserted seven fails at channel start rather
than at image build.

### `cost`

`cores_720p30` is the measured cost of **one instantiated branch, idle** — a branch that is not
on screen still runs. See [§Measuring cost](#measuring-cost). The backend uses it to project a
channel's total and to refuse a `hot_set` the host cannot afford.

Scaling used by `plugins.cost_cores()`:

- at or below 1280×720: `cores_720p30 × (fps / 30)`
- between 720p and 1080p: linear interpolation from `1.0` to `scale_1080p`
- above 1080p: `scale_1080p × pixels / (1920×1080)`

A missing `cost` block defaults to `0.25` / `1.9`, which is roughly right for a `showfreqs`-class
filter and roughly wrong for anything heavier. Measure.

---

## Validation

Two independent checks, and **they do not agree**. Write to the stricter one.

| Checker | When | Accepts |
|---------|------|---------|
| `backend/ambient/plugins.py` → `check_output_size()` | at compile / channel load | `${WIDTH}x${HEIGHT}`, **or** a literal `s=WxH` that matches the channel, **or** `output_size` in the manifest matching the channel |
| `ffmpeg/entrypoint.sh` | at composer launch | **only** a fragment containing both `${WIDTH}` and `${HEIGHT}`. Anything else is a fatal error |

A literal-size fragment therefore passes `ambient.compile` with no warning and then kills the
composer at start. **Always use the placeholders.**

The backend additionally checks, per channel:

- every name in `visualisation.hot_set` exists in the registry;
- the projected core cost of the whole `hot_set`, surfaced through
  `GET /api/channels/{name}` as `projected_cores` and enforced on `POST .../start`.

Neither checker validates `requires_filters` — only the composer entrypoint does.

---

## How a plugin reaches the stream

At launch the compositor builds one branch per name in `HOT_SET` and wires them into
`streamselect`:

```
[0:a] ──┬─→ branch 0 (plugin A) ──┐
        ├─→ branch 1 (plugin B) ──┼─→ streamselect@sel ─→ [viz] ─┐
        └─→ branch 2 (plugin C) ──┘                              │
                                                                 ├─→ blend ─→ encode
[1:v] slideshow ─→ fps ─→ realtime ─→ zmq@ctl ─→ eq@eq ─→ hue@hue ┘
```

Switching is one command:

```
streamselect@sel map <index>
```

where `<index>` is the plugin's position in `hot_set`. The backend owns that mapping and sends
it for you via `PUT /api/channels/{name}/visualisation`.

**Measured:** the switch is frame-exact — six switches, zero frame error, including two 200 ms
apart. Frame *N−1* is entirely the old branch, frame *N* entirely the new one. No blended, torn
or black frame; `drop=0 dup=0`. Invalid commands are safe: `map 9`, `map -1`, `map abc` and a
bare `map` all return `22 Invalid argument` and the graph keeps running.

### Two structural constraints

**`streamselect` requires at least two inputs.** A channel with a single hot plugin has no
selector at all — the compositor substitutes `[viz0]null[viz]`. That is the shipped
configuration, so a lone plugin is a supported case, not a degenerate one, but there is nothing
to switch to.

**Changing `hot_set` requires a compositor restart.** The filtergraph is fixed at launch, so a
plugin that is not instantiated cannot be switched to. `PUT .../visualisation` with a name
outside `hot_set` returns `409 not_in_hot_set` rather than silently promoting it and forcing a
restart the caller did not ask for. A restart is not free — measured at ~13.7 s of YouTube
outage for a plain container restart, ~1.03 s for a supervised make-before-break swap.

---

## Idle branches are not free

**Measured** at 1280×720, 30 fps, `-re` paced:

| Configuration | CPU |
|---------------|-----|
| audio source only | 3 % |
| 1 branch | 20 % |
| 3 branches + `streamselect` | 73.5 % |
| 3 branches + `streamselect` + libx264 CBR | **95.6 %** |

Roughly **0.2–0.25 cores per idle branch at 720p**, scaling ~1.9× at 1080p.

`hot_set` is a CPU budget, not a preference list.

And the filtergraph benchmark is not the whole channel: a **running** channel measured
**~1.5 cores at 720p with one hot plugin**, because it also decodes MP3 from Icecast, decodes
JPEG off the producer pipe, and encodes the HLS preview as a second output. Budget from 1.5 and
add ~0.2–0.25 per additional hot plugin. See [scaling.md](scaling.md).

---

## Measuring cost

### The benchmarking trap

**Do not benchmark a `streamselect` graph with `-f null -`.** A filter-only run of three
branches took **542 s of wall clock for 20 s of content**; the identical graph terminated by a
real encoder took 19.67 s. The cause is unresolved and suspected to be framesync stalling with
no downstream backpressure.

**Always measure with an encoder attached**, which is what production runs anyway.

### A usable measurement

Run one branch against a synthetic audio source, `-re` paced, encoded, for 60 seconds, and
watch the process:

```bash
docker run --rm --name viz-bench ambient-composer:dev sh -c '
  ffmpeg -hide_banner -loglevel error -nostdin \
    -re -f lavfi -i "sine=frequency=220:sample_rate=44100:duration=60" \
    -filter_complex "[0:a]showfreqs@viz=s=1280x720:mode=bar,fps=30,format=yuv420p,setsar=1[v]" \
    -map "[v]" -c:v libx264 -preset veryfast -b:v 3000k -minrate 3000k -maxrate 3000k \
    -bufsize 6000k -g 60 -keyint_min 60 -sc_threshold 0 -pix_fmt yuv420p \
    -f null - '
```

In another terminal:

```bash
docker stats --no-stream viz-bench
```

Substitute your own fragment for the `[0:a]...` chain, with `${WIDTH}` etc. expanded by hand.
Subtract a baseline run with `nullsink` in place of your filter to isolate the branch from the
encoder, then divide the percentage by 100 to get cores. That is your `cores_720p30`.

Repeat at `1920x1080` to get `scale_1080p` as the ratio of the two.

---

## Installing and using a new plugin

```bash
mkdir -p plugins/my-viz
$EDITOR plugins/my-viz/viz.ffmpeg plugins/my-viz/config.json
```

Add it to a channel's `hot_set` in `channels/<name>/config.yaml`:

```yaml
visualisation:
  active: showfreqs-bars
  hot_set:
    - showfreqs-bars
    - my-viz
```

Then recompile and restart the compositor — a `hot_set` change needs a new filtergraph:

```bash
python -m ambient.compile <name>
scripts/channel.sh restart <name>
```

`ambient.compile` fails loudly if the plugin does not exist, or if its declared geometry cannot
be reconciled with the channel's.

> **Today this has no effect on the running stream.** The per-channel Compose template does not
> pass `HOT_SET` or `ACTIVE_PLUGIN` to the composer, so the compositor uses its own default
> (`showfreqs-bars`) regardless of `config.yaml` — which is why the other four shipped plugins
> are not on air anywhere. Your plugin will validate, project a cost, and appear in
> `GET /api/plugins`, but it will not reach the stream until that is wired. Verify with
> `docker exec <ch>-composer cat /run/ambient/<ch>/filtergraph.txt`.

---

## Checklist before you ship a plugin

- [ ] Fragment takes its size from `${WIDTH}x${HEIGHT}`, with no literal resolution anywhere.
- [ ] Fragment ends `,fps=${FPS},format=yuv420p,setsar=1[${OUT}]`.
- [ ] Exactly one input pad `[0:a]` and one output pad `[${OUT}]`.
- [ ] Every addressable filter instance carries an explicit `@label`.
- [ ] `commandable` lists only parameters confirmed with `ffmpeg -h filter=<name>` (`T` flag).
- [ ] `requires_filters` lists every non-core filter used.
- [ ] `cost.cores_720p30` measured with an encoder attached, not with `-f null -` on a
      `streamselect` graph.
- [ ] Rendered graph inspected in `/run/ambient/<channel>/filtergraph.txt`.
- [ ] Output eyeballed at full size for the mis-sized-branch signature: crushed blacks, blown
      whites, or a repeated block pattern.

---

## Related

| Document | Covers |
|----------|--------|
| [`contracts/plugin.md`](contracts/plugin.md) | the normative package contract |
| [`contracts/zmq-control.md`](contracts/zmq-control.md) | the runtime command protocol and targeting rules |
| [visualization-filters.md](visualization-filters.md) | what each FFmpeg visualiser does in this pipeline |
| [color-profiles.md](color-profiles.md) | why colour lives in `eq`/`hue`, not in the plugin |
| [scaling.md](scaling.md) | turning per-branch cost into a channel count |
