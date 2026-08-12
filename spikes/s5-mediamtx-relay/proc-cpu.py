#!/usr/bin/env python3
"""Measure CPU cores consumed and peak RSS of a process over a window.

Usage: proc-cpu.py <pid> <seconds> [label]
"""
import os
import sys
import time

HZ = os.sysconf("SC_CLK_TCK")


def cpu_ticks(pid):
    with open(f"/proc/{pid}/stat") as fh:
        fields = fh.read().rsplit(")", 1)[1].split()
    return int(fields[11]) + int(fields[12])  # utime + stime


def peak_rss_kb(pid):
    with open(f"/proc/{pid}/status") as fh:
        for line in fh:
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    return 0


def main():
    pid, secs = int(sys.argv[1]), float(sys.argv[2])
    label = sys.argv[3] if len(sys.argv) > 3 else f"pid {pid}"
    t0, c0 = time.time(), cpu_ticks(pid)
    time.sleep(secs)
    t1, c1 = time.time(), cpu_ticks(pid)
    cores = ((c1 - c0) / HZ) / (t1 - t0)
    print(f"{label}: {cores:.3f} cores ({cores*100:.1f}% of one core) over {t1-t0:.1f}s, "
          f"peakRSS {peak_rss_kb(pid)/1024:.0f} MiB")


if __name__ == "__main__":
    main()
