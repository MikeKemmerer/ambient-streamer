#!/usr/bin/env python3
"""Analyse an S3 recording: prove from pixels that ZMQ commands changed the graph."""

from __future__ import annotations

import argparse
import json

from frameprobe import Region, change_points, deltas, fmt, sample

FPS = 30.0

# 320x180 source; drawbox sits at x=20 y=20 w=100(->160) h=60.
STEP_REGIONS = [
    Region("box", 60, 40, 25, 25),      # always inside the box
    Region("bg", 60, 60, 240, 100),     # never inside the box
    Region("edge", 20, 40, 130, 30),    # inside the box only once w >= 130
]
FULL_REGION = [Region("frame", 320, 180, 0, 0)]


def group_events(points: list[tuple[int, list[int]]]) -> list[tuple[int, int]]:
    """Collapse runs of consecutive changed frames into (start, end) events."""
    events: list[tuple[int, int]] = []
    for idx, _ in points:
        if events and idx == events[-1][1] + 1:
            events[-1] = (events[-1][0], idx)
        else:
            events.append((idx, idx))
    return events


def load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def cmd_steps(video: str, commands: str) -> None:
    frames = sample(video, STEP_REGIONS)
    rows = load(commands)
    pts = change_points(frames, threshold=3)
    events = group_events(pts)
    names = [r.name for r in STEP_REGIONS]

    print(f"frames decoded          : {len(frames)}")
    print(f"commands sent           : {len(rows)}")
    print(f"pixel change events     : {len(events)}")
    print()
    print("event  frame  regions-that-moved            before -> after")
    for n, (start, end) in enumerate(events):
        moved = deltas(frames[end], frames[start - 1])
        which = ",".join(nm for nm, d in zip(names, moved) if d > 3)
        before = " ".join(f"{nm}={fmt(p)}" for nm, p in zip(names, frames[start - 1]))
        after = " ".join(f"{nm}={fmt(p)}" for nm, p in zip(names, frames[end]))
        print(f"{n:5d}  {start:5d}  {which:28s}  {before}  ->  {after}")

    if not events:
        print("\nNO PIXEL CHANGE DETECTED -- spike FAILS")
        return

    f0 = events[0][0]
    print()
    print("command correlation (expected frame = f0 + t_rel * fps)")
    print("  t_rel     expected  observed  err  reply                 command")
    for i, row in enumerate(rows):
        expected = f0 + round(row["t_rel"] * FPS)
        observed = events[i][0] if i < len(events) else None
        err = "n/a" if observed is None else f"{observed - expected:+d}"
        obs = "MISSING" if observed is None else str(observed)
        print(f"  {row['t_rel']:7.3f}  {expected:8d}  {obs:>8s}  {err:>4s}  "
              f"{str(row['reply']):20s}  {row['msg']}")


def cmd_latency(video: str, commands: str) -> None:
    frames = sample(video, FULL_REGION)
    rows = load(commands)
    events = group_events(change_points(frames, threshold=3))
    interval_frames = []
    for a, b in zip(events, events[1:]):
        interval_frames.append(b[0] - a[0])

    print(f"frames decoded          : {len(frames)}")
    print(f"commands sent           : {len(rows)}")
    print(f"distinct pixel steps    : {len(events)}")
    print(f"frame gap between steps : {interval_frames}")
    print()
    print("step  frame  mean RGB       commanded brightness  send t_rel")
    for i, (start, _) in enumerate(events):
        val = f"{rows[i]['value']:+.2f}" if i < len(rows) else "-"
        t = f"{rows[i]['t_rel']:7.3f}" if i < len(rows) else "-"
        print(f"{i:4d}  {start:5d}  {fmt(frames[start][0]):>12s}   {val:>19s}  {t:>9s}")
    rtts = [r["rtt_ms"] for r in rows if r.get("rtt_ms")]
    if rtts:
        print(f"\nrtt ms  min={min(rtts):.1f}  max={max(rtts):.1f}  "
              f"mean={sum(rtts) / len(rtts):.1f}   frame period={1000 / FPS:.1f} ms")


def cmd_ramp(video: str, commands: str) -> None:
    frames = sample(video, FULL_REGION)
    rows = load(commands)
    events = group_events(change_points(frames, threshold=3))
    if not events:
        print("NO PIXEL CHANGE AT ALL -- graph never responded")
        return

    # Command 0 is a static step, so its change frame anchors the timeline.
    f0 = events[0][0]
    print(f"frames decoded : {len(frames)}")
    print(f"anchor frame f0: {f0}")
    for r in rows:
        print(f"  t+{r['t_rel']:6.3f}s -> frame {f0 + round(r['t_rel'] * FPS)}  {r['msg']}")

    marks = [f0 + round(r["t_rel"] * FPS) for r in rows]
    per_frame = [deltas(frames[i], frames[i - 1])[0] for i in range(1, len(frames))]

    print()
    print("window                      frames  changed  %changed  mean|d|  max|d|")
    bounds = marks + [len(frames)]
    for i in range(len(bounds) - 1):
        lo = max(bounds[i], 1)
        hi = bounds[i + 1]
        seg = per_frame[lo - 1:hi - 1]
        if not seg:
            continue
        changed = sum(1 for d in seg if d > 0)
        label = rows[i]["msg"] if i < len(rows) else "tail"
        print(f"{label[:26]:26s}  {len(seg):6d}  {changed:7d}  "
              f"{100.0 * changed / len(seg):7.1f}%  {sum(seg) / len(seg):7.2f}  {max(seg):6d}")

    print()
    print("per-frame delta and value for 40 frames after the final command:")
    start = marks[-1]
    for i in range(start, min(start + 40, len(frames))):
        print(f"  frame {i:4d}  {fmt(frames[i][0])}  delta={per_frame[i - 1]:3d}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["steps", "latency", "ramp"])
    ap.add_argument("video")
    ap.add_argument("commands")
    args = ap.parse_args()
    {"steps": cmd_steps, "latency": cmd_latency, "ramp": cmd_ramp}[args.mode](
        args.video, args.commands
    )


if __name__ == "__main__":
    main()
