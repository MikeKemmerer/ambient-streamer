# Contract — ZMQ runtime control

Backend → running FFmpeg filtergraph. Owner: lead.
Consumers: backend-api, media-pipeline.

Everything marked **measured** was observed in spike S3.

## Message format

```
TARGET COMMAND ARG
```

Exactly three whitespace-separated tokens. All three non-empty. No whitespace
inside any token.

## The validator is a security boundary, not a convenience

**Measured:** any message that does not parse into at least two tokens causes
heap corruption in FFmpeg 6.1.1's `f_zmq.c` and aborts the process with
SIGABRT, exit 134.

```
"eq@eq"        -> free(): invalid pointer        -> exit 134
""             -> free(): double free detected   -> exit 134
"   "          -> free(): invalid pointer        -> exit 134
"eq@eq "       -> free(): invalid pointer        -> exit 134
"nosuchfilter" -> munmap_chunk(): invalid ptr    -> exit 134
```

The `zmq` filter's default `bind_address` is `tcp://*:5555` — **all interfaces,
no authentication**. Combined with the above, that default is a one-packet
remote kill of a live encoder.

Therefore:

1. **Bind loopback or the channel's private network only.** Never
   `tcp://*:5555`. Never publish the port to the host.
2. **Validate every message client-side before sending.** Reject anything that
   is not exactly three non-empty whitespace-free tokens. This is the only
   thing standing between a malformed string and a dead stream.
3. Treat the validator as security-critical code. It gets tests.

Benign cases, for completeness: extra tokens are discarded
(`eq@eq brightness 0.2 0.3` → `0 Success`), and a quoted multi-value argument
returns `22 Invalid argument` without aborting.

### Escaping

`bind_address` needs **two** levels of escaping — the filtergraph tokenizer
strips one and the AVOption parser strips another:

```
zmq@ctl=bind_address=tcp\\://127.0.0.1\\:5555     # works
zmq@ctl=bind_address='tcp\://127.0.0.1\:5555'     # also works
zmq@ctl=bind_address=tcp\://127.0.0.1\:5555       # FAILS
```

## Replies

| Reply | Meaning |
|---|---|
| `0 Success` | applied |
| `38 Function not implemented` | unknown target, unknown command, or a non-commandable option |
| `22 Invalid argument` | unparseable or rejected value |

**Only `0 Success` is success.**

**Measured:** an out-of-range value returns `0 Success` and is silently
accepted — `eq@eq brightness 99` succeeds although the valid range is −1 to 1.
**The backend must range-check every value itself.** FFmpeg will not.

## Targeting

Every addressable filter instance carries an explicit `@label`.

| Target form | Behavior |
|---|---|
| `eq@eq` | the labelled instance — **the only permitted form** |
| `all` | every filter |
| `eq` | **every instance of that class — forbidden**, it broadcasts |
| `Parsed_eq_2` | does not work once a label is assigned |

Targeting by bare class name is forbidden because it silently fans out to
filters the caller did not intend to touch.

## Timing

**Measured:**

- Latency is **exactly one frame period**, deterministic. 15 commands spaced
  100 ms on a 30 fps graph produced 15 distinct plateaus with frame gaps
  `[4,3,3,3,3,3,3,3,3,3,3,3,3,3]`.
- Round trip 32.8 ms mean against a 33.3 ms frame period. The `zmq` filter only
  polls its socket when a frame passes through it, so the reply is gated on
  frame arrival.
- Throughput ceiling **~31.5 commands/s** — one per frame, on a single REQ
  socket. Each send blocks for a frame period.

## Color: use expressions, not command streams

The 31.5 commands/s ceiling would make per-frame color ramping impossible.
It does not need to be done that way.

**Measured:** a single command can install a self-animating expression.

```
eq@eq brightness 0.35*sin(2*PI*t/2)      -> 79.3 % of frames change
hue@hue h mod(t*120,360)                 -> 97.6 % of frames change
```

One command, then the value moves every frame with no further traffic. This is
how color transitions are implemented.

**`eq` must be instantiated with `eval=frame` at launch.** `eval` is not
commandable, so this cannot be fixed later — with the default `eval=init` the
same command changes only 0.7 % of frames and appears to do nothing. `hue`
re-evaluates per frame unconditionally.

## `drawbox` is not a live surface

**Measured:** one failed command permanently disables that `drawbox` instance
for the life of the process.

```
drawbox@box color red           -> 0 Success
drawbox@box box_source x        -> 22 Invalid argument   <- poison
drawbox@box color red           -> 22 Invalid argument   <- dead forever
eq@eq brightness 0.2            -> 0 Success             <- eq unaffected
```

`drawbox` re-runs `init()` on every command and never rolls back a bad value.
Recovery requires the restart we cannot do. `eq` and `hue` do not behave this
way.

Consequences:

- **Color-adaptive output rides on `eq` and `hue`, never `drawbox`.**
- `drawbox color` takes no expression, so it could only ever hard-cut anyway.
- If a `drawbox` must be addressed at all, only send values already proven
  valid by the range check.

## Commandable parameters

Verified present in the target build via the `T` flag in `ffmpeg -h filter=`:

| Filter | Commandable |
|---|---|
| `drawbox` | `x` `y` `w` `h` `color` `c` `t` `replace` |
| `eq` | `contrast` `brightness` `saturation` `gamma` `gamma_r/g/b` `gamma_weight` |
| `hue` | `h` `s` `H` `b` |
| `streamselect` / `astreamselect` | `map` |

Not commandable: `eq eval`, `streamselect inputs`.

A plugin declaring live-tunable parameters it does not actually expose is
broken; see [plugin.md](plugin.md).
