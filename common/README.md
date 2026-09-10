# common/

Shared media library. Anything in here is available to **every** channel, so a
track or image used by several streams is stored once instead of copied per
channel.

```
common/
├── audio/          <- music available to every channel
├── images/         <- slideshow images available to every channel
├── bumpers/        <- station IDs / jingles available to every channel
│   └── beds/       <- short instrumental loops the voice is mixed over
└── profiles/       <- generated color profiles, one JSON per common image
```

## Bumpers

Radio-style station IDs, inserted periodically between tracks. A bumper is a
synthesised voice line mixed over a music bed, normalized to the same
**-14 LUFS** as the music so it does not jump out at listeners.

Voice is generated locally with [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx)
(MIT wrapper, Apache-2.0 weights) — local synthesis means no watermark, no API
key, and no network dependency in a system meant to run unattended. Generation
runs as a **one-shot container**: it produces the file and exits, so the TTS
model is never part of the 24/7 footprint.

Source text lives in `bumpers.yaml` and is version-controlled; the generated
audio is not. Regenerating is cheap, so the text is the artefact worth keeping.

Beds belong in `beds/` rather than `audio/` — a bed is a short loopable
instrumental written to sit *under* speech, not a track that plays on its own.

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

## Color profiles

Profiles for shared images are generated once into `common/profiles/` and
reused by every channel, since a profile describes the image rather than the
channel. Channel-specific images keep their profiles in
`channels/<name>/profiles/`.

## Storage

Everything here is gitignored except the `.gitkeep` placeholders. Keep this on
a local filesystem — a CIFS/NFS mount, or a Windows drive under `/mnt/c` on
Docker Desktop, adds a network or 9p round trip per read and will stall a 24/7
stream.
