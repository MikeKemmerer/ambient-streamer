#!/usr/bin/env python3
"""Render the channel compose template and assert the composer really gets its
configuration.

This project has now shipped four separate cases of a value the backend computes
and the container never receives, so rendering and parsing the result is the
only check worth making.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

REPO = Path(__file__).resolve().parent.parent


def context(**overrides: str) -> dict[str, str]:
    base = {
        "channel": "demo",
        "repo_root": str(REPO),
        "common_dir": str(REPO / "common"),
        "channel_dir": str(REPO / "channels" / "demo"),
        "log_dir": "/var/log/ambient",
        "run_dir": str(REPO / ".run"),
        "crossfade_seconds": "4",
        "encoder": "libx264",
        "active_plugin": "showfreqs-bars",
        "hot_set": "showfreqs-bars,showwaves-classic",
        "width": "1920",
        "height": "1080",
        "color_mode": "manual",
        "color_accent": "#4FC3F7",
        "color_tint": "#0B2A3A",
        "color_transition_seconds": "4",
        "color_init_hue": "198.566",
        "color_init_saturation": "1.24",
        "color_init_brightness": "-0.048",
    }
    base.update(overrides)
    return base


def render(ctx: dict[str, str]) -> dict:
    env = Environment(
        loader=FileSystemLoader(str(REPO / "docker")),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    text = env.get_template("compose.channel.yml.j2").render(**ctx)
    return yaml.safe_load(text)


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, note: str) -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {label:<44} {note}")
        if not ok:
            failures.append(label)

    print("compose template delivers configuration to the composer")

    doc = render(context())
    services = doc["services"]
    composer = next(v for k, v in services.items() if "composer" in k)
    env = composer["environment"]

    for key, expected in (
        ("COLOR_MODE", "manual"),
        ("COLOR_ACCENT", "#4FC3F7"),
        ("COLOR_TINT", "#0B2A3A"),
        ("COLOR_TRANSITION_SECONDS", "4"),
        ("COLOR_INIT_HUE", "198.566"),
        ("COLOR_INIT_SATURATION", "1.24"),
        ("COLOR_INIT_BRIGHTNESS", "-0.048"),
        ("WIDTH", "1920"),
        ("HEIGHT", "1080"),
        ("ACCENT", "#4FC3F7"),
    ):
        got = env.get(key)
        check(f"manual: {key}", str(got) == expected, f"{got!r}")

    check(
        "hex survived YAML (not eaten as a comment)",
        env.get("COLOR_ACCENT", "").startswith("#"),
        repr(env.get("COLOR_ACCENT")),
    )

    auto = render(context(color_mode="auto", color_init_hue="0", color_init_saturation="1", color_init_brightness="0"))
    auto_env = next(v for k, v in auto["services"].items() if "composer" in k)["environment"]
    check("auto: COLOR_MODE", auto_env.get("COLOR_MODE") == "auto", repr(auto_env.get("COLOR_MODE")))
    check("auto: neutral hue", str(auto_env.get("COLOR_INIT_HUE")) == "0", repr(auto_env.get("COLOR_INIT_HUE")))
    check(
        "auto: no ACCENT override",
        "ACCENT" not in auto_env,
        "absent, entrypoint default applies" if "ACCENT" not in auto_env else repr(auto_env.get("ACCENT")),
    )

    liquidsoap = next(v for k, v in services.items() if "liquidsoap" in k)
    check(
        "color not leaked into liquidsoap",
        not any(k.startswith("COLOR_") for k in liquidsoap["environment"]),
        "clean",
    )

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("every computed value reaches the container")
    return 0


if __name__ == "__main__":
    sys.exit(main())
