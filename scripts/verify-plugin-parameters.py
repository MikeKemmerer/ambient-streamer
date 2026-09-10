#!/usr/bin/env python3
"""Build every plugin at its extremes and make FFmpeg accept it.

A filter option that is out of range or misspelled does not fail loudly: FFmpeg
takes it, ignores it, and the branch renders wrong at exit 0. So this does not
just substitute tokens - it runs each fragment and checks frames come out.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PLUGINS = Path("/plugins")
RESOLVER = Path("/opt/ambient/plugin_params.py")
WIDTH, HEIGHT, FPS = 640, 360, 30


def resolve(manifest: Path, chosen: dict) -> dict[str, str]:
    out = subprocess.run(
        [sys.executable, str(RESOLVER), str(manifest), json.dumps(chosen)],
        capture_output=True, text=True, check=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def build(fragment: Path, tokens: dict[str, str]) -> str:
    body = " ".join(fragment.read_text(encoding="utf-8").split())
    for token, value in tokens.items():
        body = body.replace(f"${{{token}}}", value)
    for token, value in (("WIDTH", WIDTH), ("HEIGHT", HEIGHT), ("FPS", FPS),
                         ("ACCENT", "0x4FC3F7"), ("OUT", "out")):
        body = body.replace(f"${{{token}}}", str(value))
    return body


def run(graph: str) -> tuple[bool, str]:
    script = Path("/tmp/frag.txt")
    script.write_text(graph, encoding="utf-8")
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration=1",
         "-filter_complex_script", str(script), "-map", "[out]",
         "-frames:v", "10", "-f", "null", "-"],
        capture_output=True, text=True)
    return proc.returncode == 0 and not proc.stderr.strip(), proc.stderr.strip()[:200]


def extremes(spec: dict) -> list[tuple[str, object]]:
    kind = spec.get("type", "float")
    if kind == "bool":
        return [("false", False), ("true", True)]
    if kind == "enum":
        return [(str(c["value"]), c["value"]) for c in spec.get("choices", [])]
    return [("min", spec.get("min")), ("max", spec.get("max")), ("default", spec.get("default"))]


def main() -> int:
    failures = 0
    for directory in sorted(p for p in PLUGINS.iterdir() if (p / "config.json").is_file()):
        manifest_path = directory / "config.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        name = manifest["name"]
        fragment = directory / "viz.ffmpeg"

        cases: list[tuple[str, dict]] = [("defaults", {})]
        for spec in manifest.get("parameters", []):
            for label, value in extremes(spec):
                cases.append((f"{spec['name']}={label}", {spec["name"]: value}))

        for label, chosen in cases:
            tokens = resolve(manifest_path, {name: chosen})
            body = build(fragment, tokens)
            if "${" in body:
                print(f"FAIL {name} [{label}]: unsubstituted token in {body[:120]}")
                failures += 1
                continue
            ok, err = run(body)
            if not ok:
                print(f"FAIL {name} [{label}]: {err}")
                failures += 1
            else:
                print(f"ok   {name} [{label}]")

    print()
    print("all fragments build and render" if not failures else f"{failures} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
