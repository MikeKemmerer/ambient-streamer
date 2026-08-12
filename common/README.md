# common/

Shared media library. Anything in here is available to **every** channel, so a
track or image used by several streams is stored once instead of copied per
channel.

```
common/
├── audio/     <- music available to every channel
├── images/    <- slideshow images available to every channel
└── profiles/  <- generated colour profiles, one JSON per common image
```

## How a channel uses it

A channel draws from the shared library, from its own
`channels/<name>/audio` and `images`, or from both. Selection is per channel —
being in `common/` makes a file *available*, not automatically used.

```
common/audio/rain-loop.mp3          available to every channel
channels/lofi/audio/lofi-only.mp3   available to lofi only
```

Both directories are mounted read-only into the channel's containers:

| Host | In container |
|---|---|
| `common/` | `/media/common` (read-only) |
| `channels/<name>/` | `/media/channel` (read-only) |

The backend then writes `channels/<name>/playlist.m3u` and `images.list` with
absolute in-container paths spanning both trees. Liquidsoap watches the
playlist file and the slideshow producer watches the image list, so changing a
channel's selection restarts nothing.

## Do not use symlinks for this

The obvious approach — symlinking `channels/lofi/audio/rain.mp3` to
`common/audio/rain.mp3` — **breaks inside the container**. A bind mount only
carries the directory it is given; a symlink whose target sits outside that
mount resolves to a path the container cannot see, and the file silently fails
to open. Selection lists are used instead precisely to avoid this.

## Colour profiles

Profiles for shared images are generated once into `common/profiles/` and
reused by every channel, since a profile describes the image rather than the
channel. Channel-specific images keep their profiles in
`channels/<name>/profiles/`.

## Storage

Everything here is gitignored except the `.gitkeep` placeholders. Keep this on
a local filesystem — a CIFS/NFS mount, or a Windows drive under `/mnt/c` on
Docker Desktop, adds a network or 9p round trip per read and will stall a 24/7
stream.
