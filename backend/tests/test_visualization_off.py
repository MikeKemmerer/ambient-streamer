"""Turning the visualization off.

It is the largest lever a channel has - a live 1080p30 channel measured 0.999x
realtime without it and 0.415x with it - because it removes the plugin branches,
the selector and the alpha composite in one go. The plugin choice is kept either
way so switching it back on restores the same look.
"""

from __future__ import annotations

import pytest

from ambient import plugins as plugin_registry
from ambient.models import ChannelConfig
from ambient.supervisor import compose_context
from tests.conftest import AUTH


def config_of(repo, name: str = "lofi") -> dict:
    import yaml
    return yaml.safe_load((repo / "channels" / name / "config.yaml").read_text())


def test_visualization_defaults_to_on(repo) -> None:
    config = ChannelConfig.model_validate(config_of(repo))
    assert config.visualization.enabled is True, "existing configs must not change behavior"


def test_the_switch_reaches_the_composer(api, repo) -> None:
    client, state = api
    channel = state.channel("lofi")
    assert compose_context(state.workspace, channel)["visualization"] == "on"

    client.patch("/api/channels/lofi", headers=AUTH, json={"visualization": {"enabled": False}})
    channel = state.channel("lofi")
    assert compose_context(state.workspace, channel)["visualization"] == "off"


def test_turning_it_off_keeps_the_plugin_choice(api, repo) -> None:
    client, _state = api
    before = config_of(repo)["visualization"]
    client.patch("/api/channels/lofi", headers=AUTH, json={"visualization": {"enabled": False}})
    after = config_of(repo)["visualization"]
    assert after["enabled"] is False
    assert after["active"] == before["active"], "the look must survive being switched off"
    assert after["hot_set"] == before["hot_set"]


def test_capacity_drops_to_the_pipeline_floor() -> None:
    registry = plugin_registry.load_registry(__import__("pathlib").Path("plugins"))
    on = plugin_registry.check_hot_set(["showfreqs-bars"], registry, 1280, 720, 30)
    off = plugin_registry.check_hot_set(
        ["showfreqs-bars"], registry, 1280, 720, 30, enabled=False
    )
    assert off.branch_cores == 0.0, "no branches are instantiated"
    assert off.projected_cores < on.projected_cores
    assert off.projected_cores == off.pipeline_cores + off.preview_cores


def test_an_unknown_plugin_is_tolerated_when_off() -> None:
    registry = plugin_registry.load_registry(__import__("pathlib").Path("plugins"))
    off = plugin_registry.check_hot_set(
        ["does-not-exist"], registry, 1280, 720, 30, enabled=False
    )
    assert off.errors == [], "nothing is instantiated, so nothing can be missing"


def test_it_is_reported_to_the_operator(api) -> None:
    client, _state = api
    client.patch("/api/channels/lofi", headers=AUTH, json={"visualization": {"enabled": False}})
    detail = client.get("/api/channels/lofi", headers=AUTH).json()
    assert detail["config"]["visualization"]["enabled"] is False


def test_switching_it_back_on_restores_the_projection() -> None:
    from pathlib import Path

    registry = plugin_registry.load_registry(Path("plugins"))
    off = plugin_registry.check_hot_set(
        ["showfreqs-bars"], registry, 1280, 720, 30, enabled=False
    ).projected_cores
    on = plugin_registry.check_hot_set(
        ["showfreqs-bars"], registry, 1280, 720, 30, enabled=True
    ).projected_cores
    assert on > off
