# Colour profiles

A colour profile is a small JSON file describing one image's palette. The slideshow producer
reads the incoming slide's profile and sends the compositor a colour change, so the
visualisation and the overall grade track the artwork.

The on-disk shape is fixed by [`contracts/on-disk.md`](contracts/on-disk.md); placement rules
are in [`contracts/media-selection.md`](contracts/media-selection.md). This document is how
extraction and application actually work.

---

## Where profiles live

**One profile per image, beside the image tree it belongs to.** A profile describes the
*image*, not the channel, so an image shared by four channels is analysed once.

```
common/
├── images/forest.jpg
└── profiles/forest.json

channels/lofi/
├── images/night/city.jpg
└── profiles/night/city.json
```

The mapping is `<tree>/images/<relative>.<ext>` → `<tree>/profiles/<relative>.json`.

Profiles are derived data. They are gitignored, and deleting them is always safe — the next
extraction run rebuilds them.

---

## Schema

```json
{
  "version": 1,
  "source": "common/images/forest.jpg",
  "extracted_at": "2026-08-11T22:04:00Z",
  "extractor": "pillow-kmeans-v1",
  "dominant": "#2E4A3B",
  "accent": "#8FD6A8",
  "palette": ["#2E4A3B", "#8FD6A8", "#0F1A14", "#C9E4D2", "#5A7A66"],
  "brightness": 0.34,
  "warmth": -0.42,
  "mood": "calm"
}
```

| Field | Type / range | Meaning |
|-------|--------------|---------|
| `version` | `1` | schema version. Only `1` exists |
| `source` | string | image path relative to the repository root, or the bare filename if it is outside |
| `extracted_at` | ISO-8601 UTC, seconds precision | when this profile was written |
| `extractor` | string | which implementation produced it. **Versioned on purpose** |
| `dominant` | `#RRGGBB` | the largest colour cluster |
| `accent` | `#RRGGBB` | the most visually prominent cluster — what the visualisation tracks |
| `palette` | 1–5 × `#RRGGBB` | clusters, largest first |
| `brightness` | 0.0–1.0 | mean perceived luminance (Rec. 709 weights) |
| `warmth` | −1.0–1.0 | negative cool, positive warm |
| `mood` | `calm` `warm` `cool` `energetic` | coarse label for UI grouping and preset selection |

Validation is strict: unknown keys are rejected, and hex colours must match `^#[0-9A-Fa-f]{6}$`.
A file that fails validation is treated as **absent** and re-extracted rather than half-used.

### Why `extractor` is versioned

A profile whose `extractor` value is unknown to the running backend is treated as absent and
re-extracted. That is the whole mechanism for rolling out a new algorithm: ship
`pillow-kmeans-v2`, and every `v1` profile regenerates on the next pass without anyone having
to work out which files are stale.

---

## The shipped extractor: `pillow-kmeans-v1`

`backend/ambient/colorprofile.py`, requires Pillow.

| Step | Detail |
|------|--------|
| Load | Pillow, converted to RGB |
| Downsample | `thumbnail((160, 160))` — analysis runs on at most 160 px on the long edge |
| Cluster | k-means++ seeding, k = 5, 12 iterations, **fixed seed `20260811`** |
| `palette` | cluster centres, ordered by pixel count, largest first |
| `dominant` | the largest cluster |
| `accent` | the cluster maximising `saturation × (0.35 + luma)`, plus a `0.15` bonus for not being the dominant one |
| `brightness` | mean per-pixel luma over the sampled image, weights `(0.2126, 0.7152, 0.0722)` |
| `warmth` | mean `(R − B) / 255`, doubled and clamped to ±1 |
| `mood` | `energetic` if saturation ≥ 0.55 and brightness ≥ 0.45; else `warm` if warmth ≥ 0.15; else `cool` if warmth ≤ −0.15; else `calm` |

**The seed is fixed deliberately.** The same image must not produce a different accent every
time the library is rescanned — a 24/7 stream that shifts colour because someone re-ran an
import is a bug, not a feature.

The 160 px sample is a speed/accuracy trade. It is enough for palette extraction and keeps a
few-thousand-image library to a job you can run in the foreground.

---

## Extracting profiles

### Through the API

```
POST /api/media/profiles
POST /api/media/profiles?channel=lofi
POST /api/media/profiles?force=true
```

Returns `202` with a per-tree summary:

```json
{
  "accepted": true,
  "trees": [
    { "tree": "/srv/ambient/common", "extracted": 12, "skipped": 40, "errors": [] },
    { "tree": "/srv/ambient/channels/lofi", "extracted": 3, "skipped": 0, "errors": [] }
  ]
}
```

| Parameter | Effect |
|-----------|--------|
| *(none)* | walks `common/` **and every channel's** tree |
| `channel=<name>` | that channel's tree only |
| `force=true` | re-extract everything, ignoring existing profiles |

An image is re-extracted when its profile is missing, unparseable, from an unknown extractor,
or **older than the image file's mtime**. Otherwise it is skipped.

A per-image failure (unreadable file, not an image) is collected into `errors` and does not
stop the run.

> Despite the `202`, this endpoint runs **synchronously** before responding. A first pass over
> a large shared library will block the request. Run it once per import, not on a timer.

### Without the API

There is **no CLI for extraction** — `ambient.compile` does not extract profiles, and no script
wraps it. Use the module directly, in the virtualenv where `pip install -e backend` was run:

```bash
python - <<'PY'
from pathlib import Path
from ambient.colorprofile import refresh_tree

root = Path("/srv/ambient")
for tree in [root / "common", *sorted((root / "channels").iterdir())]:
    if (tree / "images").is_dir():
        print(refresh_tree(tree, repo_root=root))
PY
```

### Inspecting one image

```bash
python -c "
from pathlib import Path
from ambient.colorprofile import extract
print(extract(Path('common/images/forest.jpg'), repo_root=Path('.')).model_dump_json(indent=2))"
```

Profiles also appear in `GET /api/media/images`, summarised per image as `dominant`, `accent`,
`brightness`, `warmth`, `mood` and `extractor`, or `null` where no profile exists.

---

## How colour reaches the stream

Not by a per-frame command stream. By **one command that installs a self-animating expression**.

### Why expressions

The `zmq` filter polls its socket only when a frame passes through it. That makes command
latency exactly one frame period and caps throughput at about **31.5 commands per second** at
30 fps — one per frame, with each send blocking for a frame period. A per-frame colour ramp is
therefore impossible on the command channel.

It does not need to be. Measured:

```
eq@eq brightness 0.35*sin(2*PI*t/2)      -> 79.3 % of frames change
hue@hue h mod(t*120,360)                 -> 97.6 % of frames change
```

One command, then the value moves every frame with no further traffic.

The producer sends a clamped linear ramp of exactly this shape:

```
eq@eq brightness (0.0000+(0.0800)*min(max((t-412.400)/2.000,0),1))
```

`t` is **stream time**, not wallclock, which is why the ramp is anchored to the producer's
frame counter (or, on the backend path, to `out_time` from FFmpeg `-progress`).

### The launch-time requirement

**`eq` must be instantiated with `eval=frame`.** `eval` is not commandable, so a graph that
omits it can never be given a time expression later — the same command then changes only 0.7 %
of frames and appears to do nothing. `hue` re-evaluates per frame unconditionally.

The compositor builds `eq@eq=eval=frame:...` for exactly this reason.

### Why not `drawbox`

**Measured:** one failed `drawbox` command permanently disables that instance for the life of
the process — it re-runs `init()` on every command and never rolls back. Recovery requires the
restart this system exists to avoid. `drawbox color` also takes no expression, so it could only
hard-cut. Palette-driven colour rides on `eq` and `hue`, never `drawbox`.

### Range checking is the caller's job

**Measured:** `eq@eq brightness 99` returns `0 Success`. FFmpeg accepts out-of-range values
silently. Every value is clamped before it is sent —
`backend/ambient/zmqctl.py` and the `ColourSender` in `ffmpeg/slideshow.py` are security- and
correctness-critical for this reason, not convenience wrappers.

Message format is three whitespace-separated non-empty tokens, always. Anything else corrupts
the heap in FFmpeg's `f_zmq.c` and aborts the encoder with exit 134. See
[`contracts/zmq-control.md`](contracts/zmq-control.md).

---

## The two appliers

There are **two independent paths** that send colour, with different mappings. Know which one
you are looking at.

### 1. Automatic — the slideshow producer

`ffmpeg/slideshow.py`, inside the composer container. Runs on every slide advance, before the
crossfade begins.

| Command | Value |
|---------|-------|
| `eq@eq brightness` | ramp `0.0` → `clamp((brightness − 0.5) × 0.4, −1, 1)` |
| `eq@eq saturation` | ramp `1.0` → `clamp(1.0 + 0.25 × warmth, 0, 3)` |
| `hue@hue h` | hard set to the accent colour's hue in degrees, **not ramped** |

Ramp duration is `COLOUR_TRANSITION_SECONDS`, default `2.0`. Sends run on a daemon thread with
a depth-1 queue, so a wedged endpoint can never stall frame production — a backlogged colour
change is dropped and logged as `zmq_backlogged`.

If the profile file does not exist, nothing is sent and the previous colour stays. That is the
normal state for a library that has never been extracted.

### 2. Manual — the control plane

`backend/ambient/presets.py`, driven by `PUT /api/channels/{name}/colour` and
`POST /api/channels/{name}/preset`. Takes an `accent` + `tint` pair rather than a profile.

| Command | Derivation |
|---------|------------|
| `hue@hue h` | accent hue in HLS space, mapped to −180…+180 |
| `eq@eq saturation` | `0.8 + accent_saturation × 0.8` |
| `eq@eq brightness` | `(tint_luma − 0.5) × 2 × 0.2` |

All three are ramped over `colour.transition_seconds`, anchored to the channel's current
`out_time`.

### Where they collide

`config.yaml` has `colour.mode: automatic | manual`, and the backend honours it — manual mode
sends the manual pair, automatic mode sends nothing.

**The producer does not read `colour.mode`.** It applies the incoming slide's profile on every
slide change regardless. So on a channel set to `manual`, a manual colour holds only until the
next slide, then the profile wins. If you want manual colour to stick today, delete the
profiles for that channel's images.

---

## Adding an extractor

Register a callable in `EXTRACTORS`. Nothing else in the module knows which implementation
produced a profile.

```python
# backend/ambient/colorprofile.py

PILLOW_HISTOGRAM_V1 = "pillow-histogram-v1"


def extract_pillow_histogram_v1(path: Path) -> Analysis:
    ...
    return Analysis(
        dominant="#2E4A3B",
        accent="#8FD6A8",
        palette=["#2E4A3B", "#8FD6A8"],
        brightness=0.34,   # 0.0 - 1.0
        warmth=-0.42,      # -1.0 - 1.0
        mood="calm",       # calm | warm | cool | energetic
    )


EXTRACTORS = {
    PILLOW_KMEANS_V1: extract_pillow_kmeans_v1,
    PILLOW_HISTOGRAM_V1: extract_pillow_histogram_v1,
}
```

Rules an extractor must follow:

1. **Return an `Analysis`**, with every field inside its documented range. `ColourProfile`
   validation rejects anything else and the profile is then treated as absent forever.
2. **Be deterministic.** Seed any randomness with a constant. A profile that changes on
   re-extraction makes a live stream shift colour for no operator-visible reason.
3. **Version the name.** `-v1`, `-v2`. Changing behaviour without changing the name leaves
   stale profiles indistinguishable from fresh ones.
4. **Raise `ProfileError`** for an unreadable or unparseable image. `refresh_tree` collects it
   and carries on; any other exception aborts the whole run.
5. **Be reasonably fast.** It runs once per image over the whole library.

To make it the default, change `DEFAULT_EXTRACTOR`. To roll it out, run extraction once —
profiles from the now-unknown old extractor are re-extracted automatically. There is no
migration to write.

`extract()`, `ensure_profile()` and `refresh_tree()` all take an `extractor=` argument, so a new
implementation can be exercised against one tree before it becomes the default. Note that
`POST /api/media/profiles` does **not** expose that argument — it always uses the default.

---

## Known limitations

| Limitation | Effect |
|------------|--------|
| The producer's profile lookup only handles images **directly** under `images/` | An image at `images/night/city.jpg` gets a profile written at `profiles/night/city.json` by the extractor, but the producer looks for `images/night/profiles/city.json`, finds nothing, and applies no colour. **Keep images one level deep if you want automatic colour** |
| The producer ignores `colour.mode` | See [§Where they collide](#where-they-collide) |
| `hue` is set, not ramped, by the producer | Slide changes with very different accents show a hue snap while brightness and saturation glide |
| `POST /api/media/profiles` blocks | It answers `202` after doing the work, not before |
| No extraction CLI | Use the Python snippet above |

---

## Related

| Document | Covers |
|----------|--------|
| [`contracts/on-disk.md`](contracts/on-disk.md) | the normative profile schema and placement |
| [`contracts/zmq-control.md`](contracts/zmq-control.md) | message format, replies, targeting, the expression technique |
| [`contracts/media-selection.md`](contracts/media-selection.md) | how images are selected and where profiles sit relative to them |
| [visualization-filters.md](visualization-filters.md) | `eq` and `hue` as the live colour surface |
| [api-reference.md](api-reference.md) | the colour and preset endpoints |
