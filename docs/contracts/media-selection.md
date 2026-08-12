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
/media/common/images/forest.jpg      20.0  2.0
/media/channel/images/city-night.jpg 20.0  2.0
```

`<path> <hold_seconds> <fade_seconds>`, whitespace-separated. Consumed by the
slideshow producer, which rescans **only between slides**; see
[slideshow.md](slideshow.md).

## Writing the lists safely

Both files are read by a live process. Write to a temporary file in the same
directory and `rename()` over the target — an atomic replace. A partially
written playlist is a stream outage.

Do not write these to `/tmp` and `mv`: that is a cross-device move, which
degrades to copy-then-unlink and is not atomic.

## Path validation

Every entry in `config.yaml` must resolve, after normalisation, to a location
under `common/` or that channel's own directory. Reject anything else.

This is a security boundary, not tidiness: these paths arrive from the control
plane's HTTP API, so `../` traversal would let a caller mount arbitrary host
files into a stream. Normalise first, then check the prefix — checking before
normalising is the classic way to get this wrong.

Symlinks inside either tree are resolved and re-validated after resolution, for
the same reason.

## Colour profiles

Profiles are generated per image, not per channel — a profile describes the
image.

| Image location | Profile location |
|---|---|
| `common/images/x.jpg` | `common/profiles/x.json` |
| `channels/lofi/images/y.jpg` | `channels/lofi/profiles/y.json` |

A shared image used by four channels is analysed once. See
[on-disk.md](on-disk.md) for the profile schema.

## Change semantics

| Change | Effect |
|---|---|
| add/remove/reorder tracks | `playlist.m3u` rewritten, Liquidsoap reloads. No restart |
| add/remove/reorder images | `images.list` rewritten, producer reloads between slides. No restart |
| new image added to `common/` | profile extracted, then available to every channel |
| file deleted while selected | Liquidsoap skips it; the producer holds the previous slide. The backend should prune the selection and warn |

Neither list interrupts the stream when rewritten.
