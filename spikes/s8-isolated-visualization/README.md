# S8 - isolated visualization layer

This spike proves that visualization plugins can be moved out of the stable
program FFmpeg graph without interrupting its output.

```text
Icecast --> restartable visualization FFmpeg --> Unix socket
                                                  |
                                                  v
slideshow --> stable program FFmpeg <--FIFO-- framekeeper (black fallback)
                      |
                      +--> scale layer --> alpha overlay --> final output
```

The framekeeper owns the Unix socket, accepts replacement visualization
writers, and continuously sends either the newest complete frame or black.
Black becomes transparent in the existing alpha-overlay graph. A null
visualization therefore means no child process and base video only.

## Resolution cap

The visualization layer is capped at `1280x720` by default. Only that layer is
scaled before compositing. A 1080p channel keeps native 1920x1080 slideshow
images and final video; it does not downscale the base picture.

This bounds visualization filter cost and raw layer traffic independently of
the final stream resolution.

## Deterministic lifecycle test

`run.sh` checks null, plugin A, child gap, plugin B, and final null pixels. It
also checks exact frame count, drop/duplicate counters, writer reconnection,
real-time speed, and unchanged main PID.

Measured locally:

| Final output | Viz layer | Frames | Speed | Drop/dup | Main PID |
|---|---|---:|---:|---:|---|
| 1280x720@30 | 1280x720@30 | 600/600 | 0.996x | 0/0 | unchanged |
| 1920x1080@30 | 1280x720@30 | 600/600 | 0.996x | 0/0 | unchanged |

```bash
WIDTH=1920 HEIGHT=1080 PY=backend/.venv/bin/python \
  bash spikes/s8-isolated-visualization/run.sh
```

## Live-switch stress test

`stress.sh` keeps one 1080p main compositor running while repeatedly switching
between two visualization children and null. It also kills a child forcibly
and checks compositor liveness and advancing output after every transition.

Measured locally: 20 A/B/null switches, 14 accepted visualization writers, one
forced child kill, 1,350/1,350 output frames at `0.998x`, zero drops or
duplicates, and one unchanged main FFmpeg PID.

```bash
PY=backend/.venv/bin/python bash spikes/s8-isolated-visualization/stress.sh
```

## Production plan

1. Keep one framekeeper and layer input alive with the program compositor.
2. Add a controller that starts at most one plugin child and reconnects it to
   the keeper socket.
3. Render children at the configured layer cap from a second Icecast listener.
4. On switch, stop the old child, fall back to transparent black, and start the
   replacement. Null stops the child only.
5. Keep opacity and automatic color grading after the stable layer input.
6. Give visualizer lifecycle its own generation, readiness, timeout, and
   backoff. It must never enter the compositor watchdog restart path.
7. Count one active capped plugin in capacity planning, or zero for null.

Production implementation requires coordinated revisions to the frozen plugin,
config, REST, preset, ZMQ, and on-disk contracts before replacing `hot_set`.