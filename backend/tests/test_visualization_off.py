"""Starting and stopping the isolated visualization child."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

from ambient import plugins as plugin_registry
from ambient.models import ChannelConfig
from ambient.supervisor import compose_context
from tests.conftest import AUTH, EXITED_STATE, RUNNING_STATE


def config_of(repo, name: str = "lofi") -> dict:
    import yaml
    return yaml.safe_load((repo / "channels" / name / "config.yaml").read_text())


def test_visualization_defaults_to_on(repo) -> None:
    config = ChannelConfig.model_validate(config_of(repo))
    assert config.visualization.enabled is True, "existing configs must not change behavior"


def test_the_switch_reaches_generated_compose_context(api, repo) -> None:
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
    on = plugin_registry.check_visualization("showfreqs-bars", registry, 1280, 720, 30)
    off = plugin_registry.check_visualization(
        "showfreqs-bars", registry, 1280, 720, 30, enabled=False
    )
    assert off.branch_cores == 0.0, "no branches are instantiated"
    assert off.projected_cores < on.projected_cores
    assert off.projected_cores == off.pipeline_cores + off.preview_cores


def test_an_unknown_plugin_is_tolerated_when_off() -> None:
    registry = plugin_registry.load_registry(__import__("pathlib").Path("plugins"))
    off = plugin_registry.check_visualization(
        "does-not-exist", registry, 1280, 720, 30, enabled=False
    )
    assert off.errors == [], "nothing is instantiated, so nothing can be missing"


def test_it_is_reported_to_the_operator(api) -> None:
    client, _state = api
    client.patch("/api/channels/lofi", headers=AUTH, json={"visualization": {"enabled": False}})
    detail = client.get("/api/channels/lofi", headers=AUTH).json()
    assert detail["config"]["visualization"]["enabled"] is False


def test_the_channel_list_says_nothing_is_being_drawn(api) -> None:
    """The summary tile has no config, so the status has to carry it."""
    client, _state = api

    def lofi() -> dict:
        body = client.get("/api/channels", headers=AUTH).json()
        return next(c for c in body["channels"] if c["name"] == "lofi")

    assert lofi()["visualization_enabled"] is True

    client.patch("/api/channels/lofi", headers=AUTH, json={"visualization": {"enabled": False}})
    summary = lofi()
    assert summary["visualization_enabled"] is False
    assert summary["visualization"], "the selected plugin is still reported, just not drawn"


def test_switching_it_back_on_restores_the_projection() -> None:
    from pathlib import Path

    registry = plugin_registry.load_registry(Path("plugins"))
    off = plugin_registry.check_visualization(
        "showfreqs-bars", registry, 1280, 720, 30, enabled=False
    ).projected_cores
    on = plugin_registry.check_visualization(
        "showfreqs-bars", registry, 1280, 720, 30, enabled=True
    ).projected_cores
    assert on > off


def test_running_enabled_changes_target_only_the_visualizer(api, docker) -> None:
    from tests.conftest import RUNNING_STATE, eventually

    client, _state = api
    docker.states["lofi-composer"] = RUNNING_STATE
    response = client.patch(
        "/api/channels/lofi", headers=AUTH,
        json={"visualization": {"enabled": False}},
    )

    assert response.status_code == 202
    assert response.json()["visualizer_action"] == "stop"
    call = eventually(lambda: docker.calls_matching("stop", "lofi-visualizer"))[-1]
    assert call[-2:] == ["stop", "lofi-visualizer"]
    assert not docker.calls_matching("--force-recreate", "lofi-composer")


def test_stopped_enabled_change_is_saved_without_docker(api, docker) -> None:
    client, _state = api
    response = client.patch(
        "/api/channels/lofi", headers=AUTH,
        json={"visualization": {"enabled": False}},
    )

    assert response.status_code == 200
    assert response.json()["visualizer_action"] is None
    assert not docker.compose_calls()


def test_reenabling_a_running_channel_starts_only_visualizer(api, docker) -> None:
    from tests.conftest import RUNNING_STATE, eventually

    client, _state = api
    disabled = client.patch(
        "/api/channels/lofi", headers=AUTH,
        json={"visualization": {"enabled": False}},
    )
    assert disabled.status_code == 200
    docker.calls.clear()
    docker.states["lofi-composer"] = RUNNING_STATE

    response = client.patch(
        "/api/channels/lofi", headers=AUTH,
        json={"visualization": {"enabled": True}},
    )

    assert response.status_code == 202
    assert response.json()["visualizer_action"] == "start"
    call = eventually(lambda: docker.calls_matching("up", "--no-deps", "lofi-visualizer"))[-1]
    assert call[-4:] == ["up", "-d", "--no-deps", "lofi-visualizer"]
    assert not docker.calls_matching("--force-recreate", "lofi-composer")


def test_status_reports_visualizer_and_framekeeper_state(api, docker) -> None:
    client, _state = api
    docker.states["lofi-composer"] = RUNNING_STATE
    docker.states["lofi-visualizer"] = RUNNING_STATE
    docker.files[
        "lofi-composer:/run/ambient/lofi/visualization-status.json"
    ] = (
        '{"ready":true,"fallback":false,"emitted":40,"received":39,'
        '"fallbacks":1,"reconnects":1,"last_frame_age_seconds":0.01}'
    )

    body = client.get("/api/channels/lofi", headers=AUTH).json()
    assert body["visualizer_state"] == "running"
    assert body["visualizer_ready"] is True
    assert body["visualizer_fallback"] is False


def test_disabled_status_reports_an_existing_failed_visualizer(api, repo, docker) -> None:
    client, _state = api
    path = repo / "channels" / "lofi" / "config.yaml"
    config = config_of(repo)
    config["visualization"]["enabled"] = False
    path.write_text(__import__("yaml").safe_dump(config), encoding="utf-8")
    docker.states["lofi-composer"] = RUNNING_STATE
    docker.states["lofi-visualizer"] = EXITED_STATE

    body = client.get("/api/channels/lofi", headers=AUTH).json()

    assert body["visualization_enabled"] is False
    assert body["visualizer_state"] == "exited"


def test_concurrent_disable_then_enable_preserves_the_newest_intent(
    api, repo, docker, monkeypatch
) -> None:
    client, state = api
    docker.states["lofi-composer"] = RUNNING_STATE
    original = state.supervisor.containers
    first_entered = threading.Event()
    release_first = threading.Event()
    calls = 0

    async def delayed_containers(name):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_entered.set()
            await asyncio.to_thread(release_first.wait)
        return await original(name)

    monkeypatch.setattr(state.supervisor, "containers", delayed_containers)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            client.patch,
            "/api/channels/lofi",
            headers=AUTH,
            json={"visualization": {"enabled": False}},
        )
        assert first_entered.wait(timeout=2)
        second = pool.submit(
            client.patch,
            "/api/channels/lofi",
            headers=AUTH,
            json={"visualization": {"enabled": True}},
        )
        release_first.set()
        first_response = first.result(timeout=5)
        second_response = second.result(timeout=5)

    assert first_response.status_code == 202
    assert second_response.status_code == 202
    assert first_response.json()["generation"] < second_response.json()["generation"]
    assert config_of(repo)["visualization"]["enabled"] is True
