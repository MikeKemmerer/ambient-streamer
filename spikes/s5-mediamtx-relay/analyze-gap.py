#!/usr/bin/env python3
"""Summarise a poll-path.py timeline: publisher outages and RTMP session changes.

Usage: analyze-gap.py <csv> [label] [marks-file]
"""
import os
import sys


def main():
    path = sys.argv[1]
    label = sys.argv[2] if len(sys.argv) > 2 else path
    marks_file = sys.argv[3] if len(sys.argv) > 3 else None
    rows = []
    with open(path) as fh:
        next(fh)
        for line in fh:
            parts = line.rstrip("\n").split(",")
            if len(parts) < 5:
                continue
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3], int(parts[4])))
    if not rows:
        print(f"{label}: no samples")
        return

    t0 = rows[0][0]
    print(f"== {label} ==")
    print(f"samples={len(rows)} window={(rows[-1][0]-t0)/1000:.1f}s")
    if marks_file and os.path.exists(marks_file):
        for line in open(marks_file):
            name, ts = line.split()
            print(f"  MARK {name:<18} t+{(int(ts)-t0)/1000:6.2f}s")

    outages = []
    start = None
    for ts, ready, _n, _ids, _b in rows:
        if not ready and start is None:
            start = ts
        elif ready and start is not None:
            outages.append((start, ts))
            start = None
    if start is not None:
        outages.append((start, rows[-1][0]))

    # Ignore the leading not-ready window before the stream ever came up.
    first_ready = next((ts for ts, r, *_ in rows if r), None)
    if first_ready is None:
        print("  publisher NEVER became ready on this endpoint")
    else:
        print(f"  first ready at t+{(first_ready-t0)/1000:.2f}s")
    real = [o for o in outages if first_ready is not None and o[0] > first_ready]
    if not real:
        print("  OUTAGES: none after first ready  <-- uninterrupted")
    for s, e in real:
        print(f"  OUTAGE  t+{(s-t0)/1000:6.2f}s -> t+{(e-t0)/1000:6.2f}s   duration {(e-s)/1000:.2f}s")

    sessions = []
    prev = None
    for ts, _r, _n, ids, _b in rows:
        if ids != prev:
            sessions.append((ts, ids))
            prev = ids
    print("  RTMP publish-session timeline (id change == new ingest session):")
    for ts, ids in sessions:
        print(f"    t+{(ts-t0)/1000:6.2f}s  {ids or '(none)'}")
    changes = len([s for s in sessions if s[1]]) - 1
    print(f"  new publish sessions after the first: {max(changes, 0)}")

    # Data continuity: longest stall in bytesReceived while ready.
    stalls, last_b, last_ts, stall_start = [], None, None, None
    for ts, ready, _n, _ids, b in rows:
        if ready and last_b is not None and b == last_b:
            if stall_start is None:
                stall_start = last_ts
        elif stall_start is not None:
            stalls.append(ts - stall_start)
            stall_start = None
        last_b, last_ts = b, ts
    if stalls:
        print(f"  longest byte-flow stall while ready: {max(stalls)/1000:.2f}s")


if __name__ == "__main__":
    main()
