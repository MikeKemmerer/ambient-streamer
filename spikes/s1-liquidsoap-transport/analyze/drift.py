#!/usr/bin/env python3
"""Clock-drift numbers for spike S1.

--samples is (epoch,bytes) rows sampled from an uncompensated PCM capture of the
harbor stream. Fitting bytes against wallclock gives the Liquidsoap source clock
rate directly; using a fit rather than total/elapsed cancels the fixed startup
buffer out of the answer.
--container is the composer's muxed output, checked for A/V pts divergence.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
from typing import Any


def pts_times(path: str, stream: str) -> list[float]:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", stream,
            "-show_entries", "packet=pts_time", "-of", "csv=p=0", path,
        ],
        capture_output=True, text=True, check=True,
    ).stdout
    vals = []
    for line in out.splitlines():
        line = line.strip().rstrip(",")
        if line and line != "N/A":
            try:
                vals.append(float(line))
            except ValueError:
                pass
    return sorted(vals)


def fit_rate(rows: list[tuple[float, int]], bytes_per_sample: int) -> dict[str, Any]:
    t0 = rows[0][0]
    xs = [r[0] - t0 for r in rows]
    ys = [r[1] / bytes_per_sample for r in rows]
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    denom = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
    return {
        "span_s": round(xs[-1] - xs[0], 1),
        "points": n,
        "measured_sample_rate": round(slope, 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples")
    ap.add_argument("--rate", type=int, default=44100)
    ap.add_argument("--channels", type=int, default=2)
    ap.add_argument("--container")
    args = ap.parse_args()

    result: dict[str, Any] = {}

    if args.samples:
        rows: list[tuple[float, int]] = []
        with open(args.samples, newline="") as fh:
            for row in csv.reader(fh):
                if len(row) == 2 and int(row[1]) > 0:
                    rows.append((float(row[0]), int(row[1])))
        # drop the first point: it still contains the connect/probe buffer
        rows = rows[1:]
        if len(rows) >= 3:
            fit = fit_rate(rows, 2 * args.channels)
            ratio = fit["measured_sample_rate"] / args.rate
            fit["ppm"] = round((ratio - 1.0) * 1e6, 1)
            fit["drift_s_per_24h"] = round((ratio - 1.0) * 86400.0, 2)
            result["source_clock"] = fit
        else:
            result["source_clock"] = {"error": "not enough samples", "points": len(rows)}

    if args.container:
        v = pts_times(args.container, "v")
        a = pts_times(args.container, "a")
        buckets = []
        if v and a:
            end = min(v[-1], a[-1])
            step = 60.0
            t = step
            while t <= end:
                lv = max((x for x in v if x <= t), default=0.0)
                la = max((x for x in a if x <= t), default=0.0)
                buckets.append({"t_s": t, "audio_minus_video_s": round(la - lv, 4)})
                t += step
        result["container"] = {
            "video_packets": len(v),
            "audio_packets": len(a),
            "last_video_pts_s": round(v[-1], 3) if v else None,
            "last_audio_pts_s": round(a[-1], 3) if a else None,
            "end_skew_s": round(a[-1] - v[-1], 3) if v and a else None,
            "per_minute": buckets,
        }

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
