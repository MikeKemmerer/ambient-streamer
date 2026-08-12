# Contracts

This directory is the source of truth for every interface between lanes. Read
the relevant contract before writing code that crosses a lane boundary. Never
infer an interface from another lane's implementation.

If a contract is wrong or missing, **report it** — do not unilaterally change
it. Contracts are lead-owned precisely so that two lanes cannot drift apart.

## Status

Frozen at the end of Phase 0. Every constraint marked **measured** was observed
on real hardware during the Phase 0 spikes; those are not opinions and changing
them requires re-running the spike that produced them.

| Contract | Governs | Lanes |
|---|---|---|
| [config.md](config.md) | `.env` / `ambient.yaml` / `channels/<n>/config.yaml` | all |
| [audio-transport.md](audio-transport.md) | Liquidsoap → Icecast → compositor | media-pipeline, infra |
| [slideshow.md](slideshow.md) | producer → compositor over `image2pipe` | media-pipeline, backend-api |
| [zmq-control.md](zmq-control.md) | runtime filter commands | backend-api, media-pipeline |
| [plugin.md](plugin.md) | visualization plugin package | media-pipeline, backend-api |
| [media-selection.md](media-selection.md) | `playlist.m3u`, `images.list`, shared vs per-channel | backend-api, media-pipeline |
| [on-disk.md](on-disk.md) | paths, color profiles, HLS, logs, runtime state | all |
| [rest-api.md](rest-api.md) | REST + SSE | backend-api, frontend |
| [preset.md](preset.md) | preset packs | media-pipeline, backend-api |
| [bumpers.md](bumpers.md) | station IDs and their insertion | media-pipeline, backend-api, frontend |

## The rule that shapes all of these

A 24/7 stream must never restart FFmpeg. An FFmpeg filtergraph is fixed at
launch, so every live-editable feature needs a specific escape hatch, and each
contract below exists to keep one of those hatches open.

Phase 0 measured what each hatch actually costs:

| Change | Mechanism | Measured gap |
|---|---|---|
| Track / playlist order | Liquidsoap owns audio in its own process | 0 s |
| Image set / order | producer feeds `image2pipe` | 0 s, FFmpeg PID unchanged |
| Colors | `zmq` runtime commands | one frame |
| Visualization plugin | `streamselect` between hot graphs | one frame, clean cut |
| Liquidsoap restart | Icecast fallback mount | 0 s, 0.00 % silence |
| Anything needing a real FFmpeg restart | MediaMTX relay | **~1 s, and a new YouTube ingest session** |

The last row is the one to design away from. It is not gap-free and no
configuration makes it so.
