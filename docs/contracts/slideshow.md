# Contract — Slideshow producer

Python producer → compositor over `image2pipe`. Owner: lead.
Consumers: media-pipeline, backend-api.

Everything marked **measured** was observed in spike S2.

## Why a producer process

An FFmpeg input list is fixed at launch, so images cannot be an FFmpeg input if
the set is ever to change. A separate producer writes encoded frames to the
compositor's stdin and owns slideshow order, hold and crossfade itself.

**Measured:** images were added and removed mid-run with the compositor PID
unchanged (2363420 throughout), 780 frames covering exactly 26.0 s, no gap.

## Wire format

| Property | Value |
|---|---|
| Container | `-f image2pipe` on FFmpeg's stdin |
| Codec | **JPEG** |
| Quality | 88 |
| Chroma | 4:4:4 (`subsampling=0`) |
| Colour | RGB; the compositor converts to `yuv420p` |
| Geometry | exactly the channel's output size, every frame |
| Rate | `producer_fps`, default 10 |

There is no signalling channel. FFmpeg probes the first frame and everything
after must match it.

### JPEG, not PNG

**Measured** at 720p, 10 fps:

| Format | Producer CPU | FFmpeg CPU | speed |
|---|---|---|---|
| JPEG | 2.2 % | 38.0 % | 0.992x |
| PNG | 4.7 % | 39.7 % | **0.941x** |

PNG doubled producer CPU and **broke real-time pacing**. Worth noting that the
test images were flat colour, which is PNG's best case for size — real
photographs would make PNG considerably larger. PNG loses on content that
flatters it.

## Producer frame rate

**Measured** marginal cost over an encode-only baseline (720p 25.9 %,
1080p 49.6 % of one core):

| `producer_fps` | 720p | 1080p |
|---|---|---|
| 1 | +3.1 % | +7.1 % |
| 5 | +10.5 % | +21.8 % |
| **10 (default)** | **+14.3 %** | **+32.6 %** |
| 15 | +19.9 % | +42.8 % |

The producer itself is nearly free (1–6 %); the cost is FFmpeg's JPEG decode on
the pipe.

Crossfade smoothness is `fade_seconds × producer_fps + 1` steps:

| Steps | Appearance |
|---|---|
| < 10 | visibly stepped |
| ~20 | acceptable |
| ~30 | effectively smooth |

10 fps with a 2 s fade gives 20 steps for +14.3 % of a core, which is the
default. At 1080p, prefer 5 fps with a 4 s fade — same 21 steps, roughly a
third less CPU.

## Compositor side

```
-f image2pipe -framerate $PRODUCER_FPS -i pipe:0
```

then, in the filtergraph, before anything else:

```
fps=$OUT_FPS:start_time=0,realtime
```

### `realtime` is mandatory

**Measured**, 5 fps producer → 30 fps output, 20 s to a real FLV muxer:

| Graph | speed | Frames per 0.5 s progress block |
|---|---|---|
| `fps=30,realtime` | 0.996x | min 11 / max 25 |
| `fps=30` alone | **1.31x** | min 12 / **max 53** |

Without `realtime` the muxer released up to **53 frames — 1.77 s of media — in
a 0.5 s wall window**. That is the burst that makes YouTube buffer, and it will
not reproduce in local file-output testing.

`-framerate` **must** equal the producer's rate. PTS is derived from this value,
not from frame arrival time, so the producer cannot vary its rate to save CPU.

## Producer obligations

1. Emit every frame at exactly the channel output geometry. A mid-stream size
   change breaks the graph.
2. **Do not treat being late as an error, and never drop frames to catch up.**
   The producer is flow-controlled by the pipe; FFmpeg's read rate is
   authoritative. **Measured:** during FFmpeg initialisation the producer is
   throttled to ~1.5 fps for 4–6 s, which permanently offsets the slideshow
   timeline. That is benign and must not be corrected.
3. Rescan the image list **only between slides**, never mid-fade.
4. Exit 0 on `BrokenPipeError` — that is the compositor shutting down normally.
5. Read its slide list from `images.list`; see
   [media-selection.md](media-selection.md).
6. Emit colour transitions as zmq commands derived from the upcoming slide's
   profile; see [zmq-control.md](zmq-control.md).

## Lifecycle

**The producer and the compositor are one supervised unit.** Producer death
means compositor death. Do not restart one without the other.

## Failure modes the watchdog must detect

These are the reason the watchdog cannot be a liveness check.

| Failure | What happens | Detection |
|---|---|---|
| **producer stalls** | **nothing dies.** FFmpeg stays alive, speed falls to **0.44x**, YouTube starves | `out_time` from `-progress` stops advancing; sustained `speed < 0.97` |
| producer dies | FFmpeg exits **rc=0**, indistinguishable from clean completion | **any** compositor exit is a fault regardless of exit code |
| burst pacing (`realtime` missing) | YouTube buffers | progress blocks with a wide frame spread |

**`drop` and `dup` counters are useless here.** Both stayed at 0 through the
bursty failure, because the `fps` filter's duplication is internal and never
reaches the muxer's counters.
