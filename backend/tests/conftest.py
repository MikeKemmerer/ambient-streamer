"""Shared fixtures: a throwaway repo and an app wired to a fake Docker."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Sequence

import pytest
from fastapi.testclient import TestClient

from ambient.main import create_app
from ambient.supervisor import CommandResult
from tests.test_config import make_repo

TOKEN = "b1946ac92492d2347c6235b4d2611184b1946ac92492d2347c6235b4d2611184"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

PRESET_YAML = """\
version: 1
name: calm-ocean
display_name: "Calm Ocean"
description: "Cool blues, slow fades."
visualization:
  active: showfreqs-bars
color:
  mode: manual
  manual:
    accent: "#4FC3F7"
    tint: "#0B2A3A"
  transition_seconds: 4.0
slideshow:
  hold_seconds: 30.0
  fade_seconds: 3.0
  order: sequential
audio:
  crossfade_seconds: 6.0
effects:
  eq:
    brightness: -0.05
    saturation: 0.9
  hue:
    h: 190
"""

RUNNING_STATE = json.dumps(
    {"Running": True, "Status": "running", "ExitCode": 0, "StartedAt": "2026-08-11T00:00:00Z"}
)
EXITED_STATE = json.dumps(
    {"Running": False, "Status": "exited", "ExitCode": 0, "FinishedAt": "2026-08-11T00:10:00Z"}
)


class FakeDocker:
    """Records every argv and answers from a scripted table."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.states: dict[str, str] = {}
        self.files: dict[str, str] = {}
        self.logs: dict[str, str] = {}
        # Keyed by a substring of the argv, e.g. an encoder name for a probe.
        self.runs: dict[str, CommandResult] = {}
        self.fail: set[str] = set()

    async def __call__(
        self, argv: Sequence[str], timeout: float | None = None
    ) -> CommandResult:
        args = list(argv)
        self.calls.append(args)
        joined = " ".join(args)
        for marker in self.fail:
            if marker in joined:
                return CommandResult(tuple(args), 1, "", f"scripted failure: {marker}")
        if args[1:2] == ["inspect"]:
            container = args[-1]
            state = self.states.get(container)
            if state is None:
                return CommandResult(tuple(args), 1, "", "No such object")
            return CommandResult(tuple(args), 0, state, "")
        if args[1:2] == ["exec"]:
            container = args[2]
            payload = self.files.get(f"{container}:{args[-1]}")
            if payload is None:
                return CommandResult(tuple(args), 1, "", "no such file")
            return CommandResult(tuple(args), 0, payload, "")
        if args[1:2] == ["logs"]:
            payload = self.logs.get(args[-1])
            if payload is None:
                return CommandResult(tuple(args), 1, "", "No such container")
            return CommandResult(tuple(args), 0, payload, "")
        if args[1:2] == ["run"]:
            for marker, result in self.runs.items():
                if marker in joined:
                    return result
        return CommandResult(tuple(args), 0, "", "")

    def compose_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c[1:2] == ["compose"]]

    def calls_matching(self, *tokens: str) -> list[list[str]]:
        return [c for c in self.calls if all(t in c for t in tokens)]


def eventually(predicate: Callable[[], object], timeout: float = 5.0):
    """A 202 endpoint does its work in a background task; give it a moment."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = make_repo(tmp_path)
    (root / "presets").mkdir(exist_ok=True)
    (root / "presets" / "calm-ocean.yaml").write_text(PRESET_YAML, encoding="utf-8")
    (root / "channels" / "lofi" / "docker-compose.yml").write_text("services: {}\n", "utf-8")
    # Keep logs inside the sandbox; the default is /var/log/ambient.
    env = root / ".env"
    env.write_text(
        env.read_text(encoding="utf-8")
        + f"AMBIENT_LOG_DIR={root / 'logs'}\n"
        + f"AMBIENT_RUN_DIR={root / 'run'}\n",
        "utf-8",
    )
    return root


@pytest.fixture
def docker() -> FakeDocker:
    return FakeDocker()


@pytest.fixture
def api(repo: Path, docker: FakeDocker, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AMBIENT_API_TOKEN", TOKEN)
    monkeypatch.setenv("AMBIENT_BIND_ADDRESS", "127.0.0.1")
    app = create_app(repo)
    with TestClient(app) as client:
        state = app.state.ambient
        state.watchdog.enabled = False
        state.scheduler.enabled = False
        state.supervisor.runner = docker
        # The first watchdog cycle ran against the real `docker` before the
        # fake was installed; its verdict would mask what a test sets up.
        state.watchdog.latest.clear()
        state.watchdog.channels.clear()
        yield client, state
