#!/usr/bin/env python3
"""Where does a channel's CPU actually go, and can the GPU take any of it?

Runs the real composer image against synthetic sources, one filtergraph stage at
a time, and reports CPU seconds per second of output. Synthetic sources keep the
live channels out of it and make the runs comparable.

The question this answers: the encoder is already on the GPU and the card is
nearly idle, so what is left on the CPU and is any of it movable?
"""
from __future__ import annotations

import re
import subprocess
import sys
import time

IMAGE = "ambient-composer:dev"
W, H, FPS, SECONDS = 1280, 720, 30, 20
ACCENT = "0x34C759"

AUDIO = "anoisesrc=color=pink:sample_rate=44100:duration=%d" % (SECONDS + 40)
# A moving source, so nothing can be optimized away as a static frame.
SLIDES = f"testsrc2=size={W}x{H}:rate=10:duration={SECONDS + 40}"

PLUGIN = (
    "[0:a]showfreqs@viz=s={w}x{h}:mode=bar:ascale=log:fscale=log:win_size=1024"
    ":averaging=2:colors={c}|{c},fps={fps},format=yuv420p,setsar=1"
)

ALPHA = (
    "split=2[vizc][vizm];"
    "[vizm]format=gray,lut=y='val*0.65'[viza];"
    "[vizc][viza]alphamerge[vizrgba];"
    "[base][vizrgba]overlay=eof_action=pass:format=auto"
)

GRADE = ("eq=eval=frame:contrast=1:brightness=0:saturation=1"
         ":gamma_r=1:gamma_g=1:gamma_b=1,hue=h=0,format=yuv420p,setsar=1")

BASE = f"[1:v]fps={FPS},format=yuv420p,setsar=1[base]"

ENC = ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll", "-rc", "cbr",
       "-cbr", "1", "-b:v", "4500k", "-maxrate", "4500k", "-minrate", "4500k",
       "-bufsize", "9000k", "-g", "60", "-no-scenecut", "1", "-pix_fmt", "yuv420p"]
PENC = ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll", "-rc", "cbr",
        "-cbr", "1", "-b:v", "1000k", "-maxrate", "1000k", "-minrate", "1000k",
        "-bufsize", "2000k", "-g", "30", "-no-scenecut", "1", "-pix_fmt", "yuv420p"]
PENC_X264 = ["-c:v", "libx264", "-preset", "veryfast", "-b:v", "1000k",
             "-maxrate", "1000k", "-minrate", "1000k", "-bufsize", "2000k",
             "-g", "30", "-sc_threshold", "0", "-pix_fmt", "yuv420p"]


def cpu_usec(name: str) -> int:
    """Cumulative CPU microseconds for the container, from its cgroup."""
    out = subprocess.run(
        ["docker", "exec", name, "cat", "/sys/fs/cgroup/cpu.stat"],
        capture_output=True, text=True,
    ).stdout
    match = re.search(r"usage_usec (\d+)", out)
    return int(match.group(1)) if match else -1


def run(label: str, graph: str, maps: list[str]) -> None:
    name = "ambient-bench"
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    argv = [
        "docker", "run", "-d", "--name", name, "--gpus", "all",
        "-e", "NVIDIA_DRIVER_CAPABILITIES=compute,video,utility",
        "--entrypoint", "ffmpeg", IMAGE,
        "-hide_banner", "-nostdin", "-v", "error",
        "-f", "lavfi", "-i", AUDIO,
        "-f", "lavfi", "-i", SLIDES,
        "-filter_complex", graph, *maps,
        "-t", str(SECONDS + 20), "-f", "null", "-",
    ]
    started = subprocess.run(argv, capture_output=True, text=True)
    if started.returncode != 0:
        print(f"  {label:<44} FAILED to start")
        return

    time.sleep(6)  # let the graph build and the encoder open
    first = cpu_usec(name)
    if first < 0:
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        print(f"  {label:<44} FAILED")
        for line in (logs.stdout + logs.stderr).strip().splitlines()[-3:]:
            print(f"      {line}")
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        return

    time.sleep(SECONDS)
    second = cpu_usec(name)
    logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    if second < 0:
        print(f"  {label:<44} FAILED (exited early)")
        for line in (logs.stdout + logs.stderr).strip().splitlines()[-3:]:
            print(f"      {line}")
        return

    cores = (second - first) / 1e6 / SECONDS
    print(f"  {label:<44} {cores:5.2f} cores")


def main() -> int:
    print(f"cost per stage, {W}x{H}@{FPS}, {SECONDS}s runs, encode on GPU\n")

    run("1. decode slides + encode only",
        f"{BASE};[base]{GRADE}[out]", ["-map", "[out]", *ENC])

    plugin_full = PLUGIN.format(w=W, h=H, c=ACCENT, fps=FPS)
    run("2. + visualization at full resolution",
        f"{BASE};{plugin_full}[viz];[viz]null[v2];[base][v2]overlay=format=auto,{GRADE}[out]",
        ["-map", "[out]", *ENC])

    run("3. + alpha composite (what runs today)",
        f"{BASE};{plugin_full}[viz];[viz]{ALPHA},{GRADE}[out]",
        ["-map", "[out]", *ENC])

    run("4. + 360p preview on libx264 (today's default)",
        f"{BASE};{plugin_full}[viz];[viz]{ALPHA},{GRADE}[full];"
        f"[full]split=2[main][pre];[pre]scale=640:360:flags=fast_bilinear,fps=15[pv]",
        ["-map", "[main]", *ENC, "-map", "[pv]", *PENC_X264])

    run("5. same, preview on NVENC",
        f"{BASE};{plugin_full}[viz];[viz]{ALPHA},{GRADE}[full];"
        f"[full]split=2[main][pre];[pre]scale=640:360:flags=fast_bilinear,fps=15[pv]",
        ["-map", "[main]", *ENC, "-map", "[pv]", *PENC])

    half = PLUGIN.format(w=W // 2, h=H // 2, c=ACCENT, fps=FPS)
    run("6. visualization at HALF res, upscaled",
        f"{BASE};{half},scale={W}:{H}:flags=fast_bilinear[viz];[viz]{ALPHA},{GRADE}[full];"
        f"[full]split=2[main][pre];[pre]scale=640:360:flags=fast_bilinear,fps=15[pv]",
        ["-map", "[main]", *ENC, "-map", "[pv]", *PENC])

    run("7. preview downscale on the GPU (scale_cuda)",
        f"{BASE};{plugin_full}[viz];[viz]{ALPHA},{GRADE}[full];"
        f"[full]split=2[main][pre];"
        f"[pre]fps=15,hwupload_cuda,scale_cuda=640:360,hwdownload,format=yuv420p[pv]",
        ["-map", "[main]", *ENC, "-map", "[pv]", *PENC])

    print("\n  differences between consecutive rows are that stage's cost")
    return 0


if __name__ == "__main__":
    sys.exit(main())
