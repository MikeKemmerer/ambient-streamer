# Contract — Bumpers

Radio-style station IDs and their insertion.
Owner: lead. Consumers: media-pipeline, backend-api, frontend.

> **Not built.** This is a design, not a description. The model, the API surface
> and this document all exist; `grep -ri bumper ffmpeg/ liquidsoap/ docker/`
> returns nothing. No Liquidsoap operator inserts a bumper, and
> `POST .../bumpers/generate` synthesises nothing — it emits an SSE event and
> returns. Setting `bumpers.enabled` with sources that resolve to no files will
> fail the channel's config load for a feature that cannot run.

## Source text

`channels/<name>/bumpers.yaml` — tracked in git. The generated audio is not:
it can always be recreated from this file, which is small and human-written.

```yaml
version: 1
voice: af_heart            # a Kokoro voice id
speed: 1.0
bed: common/bumpers/beds/warm-pad.mp3
bed_gain_db: -18           # level of the bed under the voice
lead_in_seconds: 1.5       # bed alone before the voice starts
lead_out_seconds: 2.5      # bed alone after the voice ends

bumpers:
  - id: station-id
    text: "You're listening to lo-fi beats. Stay a while."
  - id: night
    text: "Late night sounds, all night long."
```

`id` is stable and is the filename stem of the generated audio. Changing `text`
regenerates in place; changing `id` creates a new file.

## Generation

Local synthesis with [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx)
— MIT wrapper, Apache-2.0 weights. Local means no watermark, no API key, and no
network dependency in a system meant to run unattended for months.

**Generation runs as a one-shot container.** It produces the file and exits.
The TTS model must never be part of the 24/7 footprint.

Pipeline:

1. text → kokoro-onnx → voice WAV
2. **resample the voice to 44.1 kHz**
3. mix over the bed, ducking the bed with `sidechaincompress` keyed on the voice
4. pad with `lead_in_seconds` / `lead_out_seconds` of bed alone
5. **normalize the finished bumper to `I=-14:TP=-1:LRA=11`**
6. encode MP3 44.1 kHz stereo → `channels/<name>/bumpers/<id>.mp3`

Two steps are easy to omit and both produce bugs that only show up on air:

- **Kokoro emits 24 kHz.** Everything downstream is locked to 44.1 kHz. Skipping
  the resample yields wrong-pitch audio or forces a mid-stream rate change.
- **Loudness must match the music.** An un-normalized bumper jumps out at
  listeners. `-14 LUFS` is the same target the stream already uses.

`espeak-ng` is an **apt** package required by the misaki G2P for
out-of-dictionary words. No `pip install` provides it; it belongs in the
Dockerfile.

## Insertion

Configured in the channel's `config.yaml`:

```yaml
bumpers:
  enabled: true
  mode: tracks             # tracks | time | both
  every_tracks: 4
  every_minutes: 20
  sources:
    - common/bumpers/station-id.mp3
    - channels/lofi/bumpers/night.mp3
```

Both interval modes are native Liquidsoap operators.

### `mode: tracks`

```liquidsoap
radio = rotate(weights=[1, every_tracks], [bumpers, music])
```

### `mode: time`

```liquidsoap
timed = delay(every_minutes * 60., bumpers)
radio = fallback([timed, music])
```

`delay` makes the source unavailable until the interval has elapsed, so the
fallback plays music until a bumper is due. This is "at least every N minutes",
not "exactly on the minute".

### `mode: both`

The track-count rotation wrapped in the time-based fallback, so a bumper plays
after N tracks **or** N minutes, whichever comes first. This combination is
ours; Liquidsoap documents each operator separately.

## Bumpers must not crossfade like music

A 5-second smart crossfade into a station ID sounds broken. Liquidsoap's
cookbook documents the fix: tag each source, then branch the transition.

```liquidsoap
def source_tag(s, tag) =
  metadata.map(id=tag, insert_missing=true, fun(_) -> [("source_tag", tag)], s)
end

music   = source_tag(music,   "music")
bumpers = source_tag(bumpers, "bumpers")

radio = rotate(weights=[1, 4], [bumpers, music])

def transition(a, b) =
  if a.metadata["source_tag"] != "music" or b.metadata["source_tag"] != "music" then
    sequence([a.source, b.source])          # hard cut
  else
    cross.smart(a, b)
  end
end

radio = cross(duration=5., transition, radio)
```

> **The upstream cookbook example has a bug — do not copy it verbatim.** It
> tests `a.metadata["source_tag"]` on both sides of the `or` instead of `a`
> then `b`, making the condition always true and disabling crossfading
> entirely, music included. The version above is corrected.

## Validation

| Rule | Why |
|---|---|
| `sources` non-empty when `enabled` | `rotate` starves on an empty branch |
| every source exists and is under `common/` or this channel | same two-tree rule as all media |
| `every_tracks` ≥ 1, `every_minutes` ≥ 1 | zero means every track is a bumper |
| `bed` exists when set | a missing bed silently yields voice-only |
| generated audio is 44.1 kHz | catches a skipped resample before it reaches air |

## Change semantics

Editing text, interval or sources reconfigures Liquidsoap only. **The
compositor is never restarted for a bumper change** — bumpers are audio, and
audio lives behind Icecast.
