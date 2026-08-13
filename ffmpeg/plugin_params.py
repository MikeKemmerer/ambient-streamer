#!/usr/bin/env python3
"""Resolve one plugin's parameters into TOKEN=value lines for the entrypoint.

Kept out of bash because the clamping rules live in the manifest and an
out-of-range filter argument does not fail loudly: FFmpeg accepts the option,
ignores it, and the branch renders wrong at exit 0.

    plugin_params.py <config.json> <all-parameters-json>

`all-parameters-json` is the whole channel's `visualization.parameters` map,
keyed by plugin name, so the entrypoint passes the same string for every branch.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def choices_of(spec: dict) -> list[str]:
    return [str(c.get("value")) for c in spec.get("choices", []) if isinstance(c, dict)]


def coerce(spec: dict, value: object) -> str:
    kind = str(spec.get("type", "float"))
    if kind == "bool":
        if isinstance(value, str):
            truthy = value.strip().lower() in {"1", "true", "yes", "on"}
        else:
            truthy = bool(value)
        # showwaves and friends take 0/1, not the words.
        return "1" if truthy else "0"
    if kind == "enum":
        allowed = choices_of(spec)
        text = str(value)
        return text if text in allowed else str(spec.get("default", allowed[0] if allowed else ""))
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        number = float(spec.get("default", 0) or 0)
    low = float(spec.get("min", 0))
    high = float(spec.get("max", 1))
    number = max(low, min(high, number))
    if kind == "int":
        return str(int(round(number)))
    # %g so 1.0 becomes "1": some filters reject a trailing .0 in an int option.
    return f"{number:g}"


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: plugin_params.py <config.json> <parameters-json>", file=sys.stderr)
        return 2
    manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    name = str(manifest.get("name", ""))

    try:
        everything = json.loads(sys.argv[2] or "{}")
    except json.JSONDecodeError:
        everything = {}
    chosen = everything.get(name) or {}
    if not isinstance(chosen, dict):
        chosen = {}

    for spec in manifest.get("parameters", []):
        if not isinstance(spec, dict) or not spec.get("name"):
            continue
        key = str(spec["name"])
        token = str(spec.get("token", key.upper()))
        value = chosen.get(key, spec.get("default"))
        print(f"{token}={coerce(spec, value)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
