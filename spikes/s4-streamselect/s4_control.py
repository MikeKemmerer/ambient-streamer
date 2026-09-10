#!/usr/bin/env python3
"""Drive an S4 phase: switch streamselect / astreamselect on a running graph."""

from __future__ import annotations

import argparse
import json
import time

from ffzmq import TIMEOUT, FFZmq

VIDEO_PLAN: list[tuple[float, str, str]] = [
    (0.0, "streamselect@sel map 0", "t0 marker, stay on branch 0 (showwaves/red)"),
    (2.0, "streamselect@sel map 1", "switch to branch 1 (showfreqs/green)"),
    (4.0, "streamselect@sel map 2", "switch to branch 2 (avectorscope/blue)"),
    (6.0, "streamselect@sel map 0", "back to branch 0"),
    (8.0, "streamselect@sel map 2", "rapid switch A"),
    (8.2, "streamselect@sel map 1", "rapid switch B, 200 ms later"),
    (8.4, "streamselect@sel map 0", "rapid switch C, 200 ms later"),
]

AUDIO_PLAN: list[tuple[float, str, str]] = [
    (0.0, "astreamselect@asel map 0", "t0 marker, stay on 440 Hz"),
    (2.0, "astreamselect@asel map 1", "switch to 1500 Hz"),
    (4.0, "astreamselect@asel map 0", "switch back to 440 Hz"),
]

MISMATCH_PLAN: list[tuple[float, str, str]] = [
    (0.0, "streamselect@sel map 0", "t0 marker, branch 0 (the conforming one)"),
    (2.0, "streamselect@sel map 1", "route the mismatched branch to the output"),
]

ERROR_PROBES = [
    ("index out of range", "streamselect@sel map 9"),
    ("negative index", "streamselect@sel map -1"),
    ("non-numeric index", "streamselect@sel map abc"),
    ("empty arg", "streamselect@sel map"),
    ("two outputs requested from one output", "streamselect@sel map 0 1"),
    ("non-commandable option", "streamselect@sel inputs 2"),
    ("class-name target", "streamselect map 1"),
    ("valid recovery", "streamselect@sel map 0"),
]


def run_plan(z: FFZmq, plan, out_path: str) -> None:
    offset, msg, desc = plan[0]
    reply, t0 = z.wait_ready(msg)
    rows = [{"t_rel": 0.0, "msg": msg, "reply": reply, "rtt_ms": None, "desc": desc}]
    print(f"t0 established  reply={reply!r}  msg={msg!r}")
    for offset, msg, desc in plan[1:]:
        while time.monotonic() < t0 + offset:
            time.sleep(0.002)
        reply, rtt = z.send(msg)
        t_rel = time.monotonic() - t0
        rows.append({"t_rel": t_rel, "msg": msg, "reply": reply,
                     "rtt_ms": rtt * 1000.0, "desc": desc})
        print(f"t+{t_rel:6.3f}s  rtt={rtt * 1000:6.1f}ms  reply={reply!r}  {msg}")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2)


def phase_errors(z: FFZmq, out_path: str) -> None:
    z.wait_ready("streamselect@sel map 0")
    rows = []
    for desc, msg in ERROR_PROBES:
        reply, rtt = z.send(msg)
        rows.append({"desc": desc, "msg": msg, "reply": reply, "rtt_ms": rtt * 1000.0})
        print(f"{desc:38s} | send={msg!r}\n{'':38s} | reply={reply!r}")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["video", "audio", "errors", "mismatch"])
    ap.add_argument("--addr", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    z = FFZmq(args.addr)
    try:
        if args.phase == "video":
            run_plan(z, VIDEO_PLAN, args.out)
        elif args.phase == "audio":
            run_plan(z, AUDIO_PLAN, args.out)
        elif args.phase == "mismatch":
            run_plan(z, MISMATCH_PLAN, args.out)
        else:
            phase_errors(z, args.out)
    finally:
        z.close()


if __name__ == "__main__":
    main()
