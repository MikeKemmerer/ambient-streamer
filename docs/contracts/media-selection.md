# Contract — Media selection

How a channel draws from the shared library and its own media.
Owner: lead. Consumers: backend-api, media-pipeline.

## Two trees, mounted read-only

| Host | In container | Contents |
|---|---|---|
| `common/` | `/media/common` | shared by every channel |
| `channels/<name>/` | `/media/channel` | this channel only |

A channel may draw from either or both. Being in `common/` makes a file
**available**, not used — selection is per channel.

## Never use symlinks for this

A symlink from `channels/lofi/audio/rain.mp3` to `common/audio/rain.mp3`
**breaks inside the container**. A bind mount carries only the directory it is
given; a symlink whose target lies outside that mount resolves to a path the
container cannot see, and the file fails to open with no useful error.

Selection lists exist to avoid this. They are not a stylistic choice.

## Selection forms

`audio.tracks` and `images.slides` each accept three forms, which may be mixed
in one list.

### 1. Omitted or empty — use the channel's own folder

```yaml
audio:
  tracks: []          # or omit the key entirely
```

Resolves to everything in `channels/<name>/audio/`, recursively.

**It does not pull in `common/`.** The shared library is a library to select
*from*; a channel that silently played every shared track the moment its config
was blank would be a surprise, and would get worse as the library grew. To
include shared media, ask for it — see form 3.

### 2. Explicit paths — exactly these, in this order

```yaml
audio:
  tracks:
    - common/audio/intro.mp3
    - "channels/lofi/audio/01 - track.m4a"
```

Nothing is added or removed. Quote any path containing spaces.

### 3. Glob — everything matching, re-expanded as the folder changes

```yaml
images:
  slides:
    - common/images/*          # direct children only
    - channels/lofi/images/**  # recursive
```

`*` matches within one directory, `**` recurses. Both are relative to the repo
root and subject to the same two-tree rule as any other path.

### Mixing

Order is preserved as written; each glob expands in place.

```yaml
audio:
  tracks:
    - common/audio/station-open.mp3   # always first
    - channels/lofi/audio/**          # then everything else
```

## Expansion rules

| Rule | Behavior |
|---|---|
| Sort | natural sort, so `track2` precedes `track10` |
| Extension filter | only known media extensions; anything else is skipped |
| Duplicates | first occurrence wins, later ones dropped |
| Hidden files | `.gitkeep` and any dotfile are skipped |
| Empty glob | warning, not an error — a folder may legitimately be empty for now |
| Empty result overall | **hard error.** A channel with no audio cannot stream |

Recognized extensions:

| Kind | Extensions |
|---|---|
| audio | `.mp3` `.flac` `.ogg` `.opus` `.m4a` `.aac` `.wav` |
| image | `.jpg` `.jpeg` `.png` `.webp` `.bmp` |

The extension filter is what makes folder mode safe. Without it a stray
`.DS_Store`, `README`, or half-finished download would enter the playlist and
fail to open mid-stream.

## Directory watching

The selection form decides whether the backend watches a directory:

| Form | Watched | Effect of dropping a new file in |
|---|---|---|
| explicit paths | no | nothing until the config changes |
| glob | **yes** | list rewritten, picked up live |
| omitted/empty | **yes** | list rewritten, picked up live |

Rewrites go through the same atomic replace as any other list change, so
adding a file to a watched folder never interrupts the stream.

Explicit lists are deliberately not watched: the operator asked for exactly
those files, and quietly appending to a hand-curated playlist would be wrong.

## Generated lists

The backend compiles `config.yaml` selections into two files per channel.
Both contain **absolute in-container paths**.

### `channels/<name>/playlist.m3u`

```
/media/common/audio/rain-loop.mp3
/media/channel/audio/lofi-only.mp3
```

Consumed by Liquidsoap:

```liquidsoap
playlist(mode="normal", reload_mode="watch", "/media/channel/playlist.m3u")
```

`mode="normal"` preserves the order in the file. Shuffling, when a channel asks
for it, is applied by the backend when writing the file — so the order the
operator sees in the UI is the order that plays.

A playlist **file** rather than a watched directory is required for two
reasons: it is the only way to express explicit order, and it is the only way
to mix two source trees.

> **Unverified:** spike S1 proved `reload_mode="watch"` on a *directory*. The
> file case is documented by Liquidsoap but has not been measured here. Verify
> before relying on it; if it fails, fall back to `reload=<seconds>` polling.

### `channels/<name>/images.list`

```
/media/common/images/forest.jpg
/media/channel/images/city night.jpg
```

One absolute path per line, same as the playlist. Consumed by the slideshow
producer, which rescans **only between slides**; see
[slideshow.md](slideshow.md).

Hold and fade timings are **not** in this file. They are channel-level settings
in `config.yaml`, and putting them here would have made the format
whitespace-delimited — which silently breaks on any filename containing a
space. Media filenames routinely contain spaces, so the format carries paths
and nothing else.

## Writing the lists safely

Both files are read by a live process. Write to a temporary file in the same
directory and `rename()` over the target — an atomic replace. A partially
written playlist is a stream outage.

Do not write these to `/tmp` and `mv`: that is a cross-device move, which
degrades to copy-then-unlink and is not atomic.

## Path validation

Every entry in `config.yaml` must resolve, after normalization, to a location
under `common/` or that channel's own directory. Reject anything else.

This is a security boundary, not tidiness: these paths arrive from the control
plane's HTTP API, so `../` traversal would let a caller mount arbitrary host
files into a stream. Normalize first, then check the prefix — checking before
normalising is the classic way to get this wrong.

Symlinks inside either tree are resolved and re-validated after resolution, for
the same reason.

**Globs are validated on their expanded results, not on the pattern.** A
pattern is not a path and cannot be prefix-checked meaningfully — `**` in the
wrong position, or a symlinked subdirectory, can produce matches outside the
intended tree even when the pattern reads as though it could not. Expand first,
then validate every result individually and discard the ones that escape.

## Color profiles

Profiles are generated per image, not per channel — a profile describes the
image.

| Image location | Profile location |
|---|---|
| `common/images/x.jpg` | `common/profiles/x.json` |
| `channels/lofi/images/y.jpg` | `channels/lofi/profiles/y.json` |

A shared image used by four channels is analyzed once. See
[on-disk.md](on-disk.md) for the profile schema.

## Change semantics

| Change | Effect |
|---|---|
| add/remove/reorder tracks | `playlist.m3u` rewritten, Liquidsoap reloads. No restart |
| add/remove/reorder images | `images.list` rewritten, producer reloads between slides. No restart |
| new image added to `common/` | profile extracted, then available to every channel |
| file deleted while selected | Liquidsoap skips it; the producer holds the previous slide. The backend should prune the selection and warn |

Neither list interrupts the stream when rewritten.
