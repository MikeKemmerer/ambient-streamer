# ambient-streamer

Headless, multi-channel, 24/7 ambient music streaming to YouTube. Each channel pairs a
Liquidsoap audio engine with an FFmpeg compositor that renders a color-adaptive image
slideshow plus real-time audio visualization, publishes to YouTube over RTMP, and exposes a
low-resolution HLS preview.

Status: **under construction.** See `docs/` for the architecture overview once written.

## Quick orientation

| Path | Purpose |
|------|---------|
| `backend/ambient/` | FastAPI control plane: REST + SSE, supervisor, watchdog, scheduler |
| `frontend/` | Plain HTML/CSS/JS operator UI (no build step) |
| `channels/<name>/` | Per-channel config, media, color profiles |
| `liquidsoap/` | Parameterized Liquidsoap channel script |
| `ffmpeg/` | Slideshow producer and compositor entrypoint |
| `plugins/` | Visualization plugins (`viz.ffmpeg` + `config.json` + `preview.png`) |
| `presets/` | Preset packs (visualization + color + transition + crossfade) |
| `docker/` | Dockerfiles, MediaMTX config, per-channel compose template |
| `scripts/` | Installer and operator utilities |

## License

MIT
