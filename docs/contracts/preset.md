# Contract — Presets

Owner: lead. Consumers: media-pipeline, backend-api.

A preset is a named bundle of look-and-feel settings that can be applied to a
channel, or switched to on a schedule.

## Package

`presets/<name>.yaml`

```yaml
version: 1
name: calm-ocean
display_name: "Calm Ocean"
description: "Cool blues, slow fades, gentle spectrum."

visualisation:
  active: showfreqs-bars

colour:
  mode: manual
  manual:
    accent: "#4FC3F7"
    tint: "#0B2A3A"
  transition_seconds: 4.0

slideshow:
  hold_seconds: 30.0
  fade_seconds: 3.0
  order: sequential

audio:
  crossfade_seconds: 6.0

effects:
  eq:
    brightness: -0.05
    saturation: 0.9
  hue:
    h: 190
```

## What a preset may and may not set

| May set | May not set |
|---|---|
| active visualisation | `hot_set` |
| colour mode and values | resolution, fps, encoder |
| slideshow hold/fade/order | which tracks or images are selected |
| audio crossfade | stream key, mounts, limits |
| `eq` / `hue` effect values | anything in `.env` |

The exclusions are the point: **a preset must never require a restart.** It may
only touch things reachable through the escape hatches — zmq commands,
`streamselect`, and the generated lists.

`hot_set` is excluded specifically because changing it means a new filtergraph.
A preset whose `visualisation.active` is not in the channel's current `hot_set`
is rejected with `409`, not silently promoted.

## Applying

1. Validate against the channel's current `hot_set` and plugin manifests.
2. Merge over the channel's `config.yaml`. The preset wins on the fields it
   sets; everything else is untouched.
3. Emit the resulting zmq commands and `streamselect` switch.
4. Rewrite `images.list` if slideshow timing changed.
5. Reconfigure Liquidsoap if crossfade changed.

None of these interrupt the stream.

Colour changes are applied as `eq`/`hue` time expressions rather than stepped
commands, so `transition_seconds` is honoured smoothly with a single message
per filter — see [zmq-control.md](zmq-control.md).

## Scheduling

Schedules live in the channel's `config.yaml`, not in the preset. A preset does
not know when it applies.

```yaml
schedule:
  timezone: UTC
  rules:
    - name: morning
      when: "06:00-11:00"
      days: [Mon, Tue, Wed, Thu, Fri]
      preset: warm-sunrise
    - name: evening
      when: "18:00-23:00"
      preset: calm-ocean
    - name: sunday
      when: "09:00-12:00"
      days: [Sun]
      preset: orthodox-chant
    - name: christmas
      date: "2026-12-25"
      preset: winter-quiet
```

Resolution order, most specific first: `date` → `days` + `when` → `when` →
channel default. First match wins.

Times are local to `timezone` and resolved with `zoneinfo`. Two consequences
that must be handled rather than discovered:

- **A rule may occur twice or not at all on DST transition days.** Applying a
  preset twice is harmless — it is idempotent. Skipping one is also acceptable.
  Neither may raise.
- A rule whose window is entirely skipped by a DST jump simply does not fire.

The scheduler evaluates on a timer and on config change, and applies a preset
only when the resolved preset differs from the one currently applied. It does
not re-apply on every tick.

## Built-in presets

`calm-ocean`, `warm-sunset`, `deep-space`, `orthodox-chant`,
`minimalist-line-art`, `neon-spectrum`.

Each must be applicable to a channel whose `hot_set` contains only the default
plugin, so that presets work on a freshly created channel without editing the
hot set first.
