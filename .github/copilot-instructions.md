# Copilot Instructions — ambient-streamer

## Project Overview

Headless, multi-channel, 24/7 ambient music streaming to YouTube. Each channel pairs a
Liquidsoap audio engine with an FFmpeg compositor that renders a color-adaptive image
slideshow plus real-time audio visualization, publishes to YouTube over RTMP, and exposes a
low-resolution HLS preview. A FastAPI control plane orchestrates all channels.

## Ownership Boundary

This repository owns ambient/music channel streaming only. The sibling `yt-azure-streamer`
project owns church broadcast VOD streaming on Azure and is **reference material only** —
never write files there. Its `services/streamer/streamer.sh` is the proven source for
YouTube ingest settings; that knowledge is captured in the `youtube-ingest` skill.

## The One Rule That Shapes Everything

**A 24/7 stream must never restart FFmpeg.** YouTube can end a broadcast after a sustained
gap in data. But an FFmpeg filtergraph is fixed at launch, so every live-editable feature
needs a specific escape hatch:

| Live change | Mechanism | Measured gap |
|-------------|-----------|--------------|
| Playlist / track order | Liquidsoap owns audio in a separate process; FFmpeg never restarts | 0s |
| Liquidsoap restart | Icecast fallback mount absorbs it | 0s, 0.00% silence |
| Image set / order | Python producer feeds `-f image2pipe`; producer owns order + crossfade | 0s, same FFmpeg PID |
| Colors | `zmq` filter + runtime commands mutate filter params on the running graph | one frame |
| Visualization plugin | `streamselect` switches between pre-instantiated graphs | one frame, clean cut |
| Anything needing a real FFmpeg restart | MediaMTX relay makes the reconnect fast and deterministic | **~1s, and a new YouTube ingest session** |

The last row is the one to design away from. The relay does **not** hold the YouTube session
open across a composer restart — it was measured terminating reader connections even when the
relay path never dropped. It only turns an 18s backoff-driven gap into a ~1s deterministic one.

Before adding a feature that changes what the stream looks like, decide which mechanism it
uses. If the answer is "restart FFmpeg", it is the wrong design.

## Contracts

`docs/contracts/` is the source of truth for every interface between lanes, and it is frozen.
Read the relevant contract before writing code that crosses a lane boundary; never infer an
interface from another lane's implementation. Constraints marked **measured** were observed on
real hardware in Phase 0 — changing one means re-running the spike that produced it.

## Architecture

Two containers per channel plus three global containers. At the maximum of 8 channels that is
19 containers, not 40.

```
backend (global)  FastAPI: REST + SSE + static UI + supervisor + watchdog + scheduler
                  renders channels/<name>/docker-compose.yml, runs `docker compose -p <name>`
     |
per channel:
  <ch>-liquidsoap  --MP3 source client-->  icecast (global relay)
                                             |
                                             `--HTTP--> <ch>-composer (ffmpeg)
                                        image2pipe <- slideshow.py
                                        zmq        <- color / plugin commands
                                        v RTMP
                                   mediamtx (global relay)
                                     |-- RTMP --> YouTube (supervised publisher loop)
                                     `-- low-res HLS --> backend :8090
```

Icecast is global because one instance serves every channel's mount. It exists because
Liquidsoap restarts must not reach the compositor: `output.harbor` was measured stalling the
compositor's output for 18.19s on a 9.41s outage, while Icecast with a fallback mount measured
0s. Do not replace it with a direct HTTP or FIFO connection.

## Lane Ownership

Work is split into file-disjoint lanes so subagents can run in parallel. **Never edit
outside your lane.** If a change needs cross-lane edits, stop and report it instead.

| Lane | Owns |
|------|------|
| `media-pipeline` | `ffmpeg/`, `liquidsoap/`, `plugins/`, `presets/` |
| `backend-api` | `backend/` |
| `frontend` | `frontend/` |
| `infra` | `docker/`, `docker-compose.yml`, `scripts/`, `.github/workflows/` |
| `docs` | `docs/`, `README.md` |

Shared, cross-lane files (`ambient.yaml.example`, `.gitignore`, `docs/contracts/`) are edited
by the lead only.

## Contracts

`docs/contracts/` is the source of truth for every interface between lanes. Read the relevant
contract before writing code that crosses a lane boundary; never infer an interface from
another lane's implementation. If a contract is wrong or missing, report it — do not
unilaterally change it.

## Key Conventions

- **Python**: 3.10+, type hints throughout, PEP 8. Dependencies via `pyproject.toml` +
  pip (setuptools). **Not Poetry** — it is unused elsewhere in this workspace and adds
  weight to every container image.
- **Frontend**: plain HTML/CSS/JS. **No npm, no build step, no frameworks.** Third-party
  libraries are vendored as single files under `frontend/vendor/`.
- **Live updates**: SSE (in-memory queue + lock). **Not WebSockets.**
- **Bash**: `set -euo pipefail`, `log`/`ok`/`warn`/`die` colored output helpers, idempotent.
- **Config**: `*.example` files are tracked; the real file is gitignored. Never commit
  credentials, stream keys, or real hostnames.
- **Secrets**: per-channel `.env`, never a tracked file and never baked into an image. The
  RTMP stream key is read by the MediaMTX relay at path-ready time from a read-only
  `channels/` bind mount, so adding a channel needs no relay restart. Stream keys are
  created by hand in YouTube Studio, never through the YouTube API.
- **Comments**: only to explain what the code cannot show on its own. One short line.

## Encoder Support

Three encoder profiles: `libx264`, `h264_nvenc`, `h264_qsv`. Note that **QSV cannot work on
Docker Desktop / WSL2** because `/dev/dri` is not exposed there — it is a bare-metal-Linux
profile only. NVENC works on WSL2 via `--gpus all`. Consumer NVIDIA cards cap concurrent
NVENC sessions, so channels beyond the cap fall back to `libx264` automatically.

## Terminal Notes

- `rm`, `git branch -D`, and `curl` are blocked by policy — use the `fetch_webpage` tool
  instead of curl.
- Use `git --no-pager <cmd>` for any git output you need to read; otherwise the terminal
  gets stuck in `less`.
- WSL path: `/mnt/c/Users/michael.kemmerer/Desktop/ambient-streamer/`
- Git: `master` branch. Feature branch per unit of work.
- Media must live on WSL2 ext4 or a Docker named volume. Bind-mounting `/mnt/c/...` is
  9p-slow and unsuitable for 24/7 reads.

## Verification Expectations

Changes to the streaming path are not "done" until observed running. A channel is healthy
when YouTube Studio reports Excellent stream health with no dropped frames, and the HLS
preview plays in VLC.
