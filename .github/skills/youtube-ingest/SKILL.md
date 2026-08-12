---
name: youtube-ingest
description: "Proven FFmpeg settings for a stable 24/7 YouTube RTMP stream. Use when: building or changing an FFmpeg output command, choosing bitrate/GOP/keyframe settings, picking an encoder (libx264/NVENC/QSV), normalizing audio for YouTube, debugging stream stalls, buffering, dropped frames, or 'stream unstable' warnings in YouTube Studio."
---

# YouTube RTMP Ingest Settings

Settings validated in production by the sibling `yt-azure-streamer` project
(`services/streamer/streamer.sh`), streaming continuously to YouTube Live. Do not
re-derive these from scratch, and do not deviate without a measured reason.

## Output flag set

The complete per-output block. `$RATE`, `$BUFSIZE`, `$AUDIO_BR` come from the ladder below;
`$FPS` is the channel's output frame rate; `$GOP` is always `FPS * 2`.

```
-c:v libx264 -preset veryfast -r $FPS -fps_mode cfr
-b:v $RATE -minrate $RATE -maxrate $RATE -bufsize $BUFSIZE
-g $GOP -keyint_min $GOP -sc_threshold 0
-x264-params "nal-hrd=cbr:force-cfr=1" -pix_fmt yuv420p
-c:a aac -b:a $AUDIO_BR -ar 44100
-f flv rtmp://a.rtmp.youtube.com/live2/$STREAM_KEY
```

Why each part matters:

- `-b:v` / `-minrate` / `-maxrate` all set to the **same** value, with `-bufsize` at **2×** —
  YouTube's ingest expects steady CBR, not VBR.
- `nal-hrd=cbr` pads frames with HRD filler to hold the target rate exactly. Without it the
  stream dips below target on static content and YouTube reports instability.
- `-g` / `-keyint_min` fixed at 2 seconds with `-sc_threshold 0` — scene-cut detection would
  otherwise emit keyframes at unpredictable intervals, which YouTube's segmenter dislikes.
- `-fps_mode cfr` — variable frame rate causes temporal artifacts on YouTube's transcode.
- `-pix_fmt yuv420p` — anything else is not universally decodable.
- `-ar 44100` — resample everything to a single clock.

## Bitrate ladder

| Resolution | Video bitrate | Bufsize | Audio bitrate |
|-----------|---------------|---------|---------------|
| 144p  | 400k   | 800k   | 96k  |
| 240p  | 700k   | 1400k  | 96k  |
| 360p  | 1000k  | 2000k  | 128k |
| 480p  | 1500k  | 3000k  | 128k |
| 720p  | 3000k  | 6000k  | 128k |
| 1080p | 5000k  | 10000k | 192k |
| 1440p | 8000k  | 16000k | 192k |
| 2160p | 16000k | 32000k | 256k |

Bufsize is always 2× video bitrate. These are tuned for 30 fps; at 60 fps raise video
bitrate by roughly 1.5×. Never upscale a source below the target resolution — scale with
`force_original_aspect_ratio=decrease` and cap on input height.

## Audio

Normalize to YouTube's loudness target:

```
[0:a:0]loudnorm=I=-14:TP=-1:LRA=11,aresample=44100:async=1000:first_pts=0[audio]
```

If the source has **no audio track**, YouTube will drop the stream. Inject silence:

```
-f lavfi -t $DURATION -i anullsrc=r=44100:cl=stereo
[1:a]aresample=44100:async=1000:first_pts=0[audio]
```

## Pacing low-frame-rate sources

A slideshow or static image produces frames far below the output rate. Upsampling alone is
not enough — the FLV muxer will release duplicated frames in bursts and YouTube will buffer
or stall. Always follow the `fps` filter with `realtime`:

```
fps=$FPS:start_time=0,realtime
```

This is the single most important filter for image-driven streams.

## Hardware encoders

Substitute for the `-c:v libx264 ... -x264-params` portion only; the rate-control intent
stays identical (CBR, fixed GOP, no scene-cut keyframes).

- **NVENC** — `-c:v h264_nvenc -preset p4 -tune ll -rc cbr -cbr 1`. Requires
  `--gpus all` plus nvidia-container-toolkit. Works on WSL2. Consumer GeForce cards cap
  concurrent encode sessions, so plan for overflow channels falling back to `libx264`.
- **QSV** — `-c:v h264_qsv -preset medium -rc_mode CBR`. Requires `/dev/dri` passthrough.
  **Not available on Docker Desktop / WSL2**; bare-metal Linux only.

Always verify the encoder actually exists at runtime (`ffmpeg -hide_banner -encoders`) and
fall back to `libx264` rather than failing to start a channel.

## Known traps

- **Escaping in `drawtext`** — complex expressions inside `text=` become an escaping
  nightmare through bash and the filtergraph parser. Use `textfile=` and write the string to
  a file instead. Add `reload=1` to pick up changes without restarting FFmpeg.
- **Apostrophes in file paths** break the concat demuxer. Escape `'` as `'\''`.
- **Never skip on failure.** If FFmpeg exits, retry the *same* item after a short delay.
  Advancing the playlist on error silently eats content.
- Capture the exit status deliberately (`set +e` around the call) so the wrapper can decide
  the retry policy instead of the shell aborting.

## Verifying a stream is actually healthy

1. YouTube Studio → stream health reads **Excellent**, no dropped-frame warnings.
2. FFmpeg progress output holds `speed=1.0x`. Sustained below 1.0 means the encoder cannot
   keep up and the stream will fall behind.
3. Bitrate in the progress line sits at the configured target, not below it.
4. Soak for at least 2 hours before calling it done — ingest problems often appear only
   after the first few keyframe intervals or on the first track transition.
