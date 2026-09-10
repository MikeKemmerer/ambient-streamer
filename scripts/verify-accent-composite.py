#!/usr/bin/env python3
"""Do the visualization's bars actually come out in the accent color?

The previous composite added chroma, so bars could only tint whatever was behind
them: green bars over orange artwork measured as brighter orange. The test is
therefore deliberately adversarial - a green accent over a strongly orange
background - and it measures the bar region, not the whole frame, because the
bars are a small fraction of the pixels and a frame average hides them.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ACCENT = "0x34C759"      # green
BACKGROUND = "0xE87A2A"  # orange


def region_rgb(png: Path, crop: str) -> tuple[int, int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-f", "lavfi",
         f"movie={png},crop={crop},signalstats", "-show_entries",
         "frame_tags=lavfi.signalstats.YAVG,lavfi.signalstats.UAVG,lavfi.signalstats.VAVG",
         "-of", "csv=p=0", "-read_intervals", "%+#1"],
        capture_output=True, text=True,
    ).stdout.strip().splitlines()
    if not out:
        return (-1, -1, -1)
    y, u, v = (float(x) for x in out[0].split(","))
    r = y + 1.402 * (v - 128)
    g = y - 0.344136 * (u - 128) - 0.714136 * (v - 128)
    b = y + 1.772 * (u - 128)
    return tuple(max(0, min(255, round(c))) for c in (r, g, b))  # type: ignore


def render(work: Path, composite: str, name: str) -> Path:
    """Render one frame using the given composite step."""
    graph = (
        f"color=c={BACKGROUND}:s=640x360:d=3,fps=30,format=yuv420p,setsar=1[base];"
        f"[0:a]showfreqs@viz=s=640x360:mode=bar:ascale=log:fscale=log:win_size=1024"
        f":averaging=2:colors={ACCENT}|{ACCENT},fps=30,format=yuv420p,setsar=1[v0];"
        f"[v0]hue@viz=h=0:s=1{composite},format=yuv420p[out]"
    )
    output = work / f"{name}.png"
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-y",
         "-f", "lavfi", "-i", "anoisesrc=color=pink:sample_rate=44100:duration=3",
         "-filter_complex", graph, "-map", "[out]",
         "-frames:v", "1", "-update", "1", str(output)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"  render failed ({name}):")
        print("   ", result.stderr.strip()[-500:])
        return Path()
    return output


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="accent-"))
    failures: list[str] = []

    def check(label: str, ok: bool, note: str) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<44} {note}")
        if not ok:
            failures.append(label)

    print(f"green bars ({ACCENT}) must survive an orange background ({BACKGROUND})")

    additive = ",split=2[c][m];[c][m]blend=c0_mode=screen:c1_mode=grainmerge:c2_mode=grainmerge"
    old = render(work, ",split=2[vc][vm];[vm]nullsink;[vc]null", "viz_only")
    if not old:
        return 1
    bar_colour = region_rgb(old, "iw:ih*0.25:0:ih*0.75")
    check("the plugin itself draws green", bar_colour[1] > bar_colour[0] and bar_colour[1] > bar_colour[2], f"rgb{bar_colour}")

    new_composite = (
        ",split=2[vizc][vizm];"
        "[vizm]format=gray,lut@vizop=y='val*0.65'[vizalpha];"
        "[vizc][vizalpha]alphamerge[vizrgba];"
        "[base][vizrgba]overlay=eof_action=pass:format=auto"
    )
    new = render(work, new_composite, "alpha")
    if not new:
        return 1
    bottom = region_rgb(new, "iw:ih*0.25:0:ih*0.75")
    check("alpha composite: bar band is green-dominant", bottom[1] > bottom[0] and bottom[1] > bottom[2], f"rgb{bottom}")

    old_composite = (
        ",split=2[vizc][vizm];[vizm]nullsink;"
        "[base][vizc]blend=c0_mode=screen:c1_mode=grainmerge:c2_mode=grainmerge:all_opacity=0.65"
    )
    previous = render(work, old_composite, "additive")
    if previous:
        was = region_rgb(previous, "iw:ih*0.25:0:ih*0.75")
        print(f"        for comparison, the additive composite gave rgb{was}")
        check("alpha beats additive on greenness", bottom[1] - max(bottom[0], bottom[2]) > was[1] - max(was[0], was[2]),
              f"{bottom[1] - max(bottom[0], bottom[2])} vs {was[1] - max(was[0], was[2])}")

    print(f"\n  frames: {work}")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("the accent survives the composite")
    return 0


if __name__ == "__main__":
    sys.exit(main())
