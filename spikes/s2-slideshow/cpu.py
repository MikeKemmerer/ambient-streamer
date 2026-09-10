#!/usr/bin/env python3
"""Sample CPU cores consumed by a set of PIDs (with children) over a window.

Usage: cpu.py <seconds> <label:pid> [label:pid ...]
"""

from __future__ import annotations

import os
import sys
import time

HZ = os.sysconf("SC_CLK_TCK")


def children(pid: int) -> list[int]:
    out = [pid]
    try:
        for tgid in os.listdir(f"/proc/{pid}/task"):
            with open(f"/proc/{pid}/task/{tgid}/children") as fh:
                out += [int(x) for x in fh.read().split()]
    except OSError:
        pass
    return out


def ticks(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            f = fh.read().rsplit(")", 1)[1].split()
        return int(f[11]) + int(f[12])
    except OSError:
        return 0


def tree_ticks(pid: int) -> int:
    return sum(ticks(p) for p in children(pid))


def rss_mib(pid: int) -> float:
    try:
        with open(f"/proc/{pid}/status") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def main() -> None:
    secs = float(sys.argv[1])
    targets = []
    for arg in sys.argv[2:]:
        label, pid = arg.rsplit(":", 1)
        targets.append((label, int(pid)))
    t0 = time.time()
    base = {p: tree_ticks(p) for _, p in targets}
    time.sleep(secs)
    t1 = time.time()
    total = 0.0
    for label, pid in targets:
        cores = ((tree_ticks(pid) - base[pid]) / HZ) / (t1 - t0)
        total += cores
        print(f"  {label:<12} {cores*100:6.1f}% of one core   peakRSS {rss_mib(pid):5.0f} MiB")
    print(f"  {'TOTAL':<12} {total*100:6.1f}% of one core  ({total:.2f} cores)")


if __name__ == "__main__":
    main()
