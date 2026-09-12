"""Compositor overlay visibility and isolated visualizer parameters."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import yaml

from ambient import plugins as plugin_registry
from ambient.supervisor import compose_context
from ambient.zmqctl import ZmqValidationError, build_message
from tests.conftest import AUTH, RUNNING_STATE, eventually

import pytest


def viz_of(repo, name: str = "lofi") -> dict:
    raw = yaml.safe_load((repo / "channels" / name / "config.yaml").read_text())
    return raw["visualization"]


def with_real_plugins(repo):
    """Clamping is only meaningful against the ranges the repo actually ships."""
    import shutil
    from pathlib import Path

    source = Path("plugins")
    assert source.is_dir(), "run pytest from the repo root"
    shutil.copytree(source, repo / "plugins", dirs_exist_ok=True)


# --------------------------------------------------------------------- standby


def test_visible_defaults_to_on(repo) -> None:
    assert viz_of(repo).get("visible", True) is True, "existing configs must not change"


def test_opacity_defaults_to_the_existing_filtergraph_value(api) -> None:
    _client, state = api
    assert state.channel("lofi").config.visualization.opacity == 0.65


def test_standby_persists_and_reaches_the_composer(api, repo) -> None:
    client, state = api
    response = client.put(
        "/api/channels/lofi/visualization/visible", headers=AUTH, json={"visible": False}
    )
    assert response.status_code == 202
    assert viz_of(repo)["visible"] is False

    channel = state.channel("lofi")
    assert compose_context(state.workspace, channel)["viz_visible"] == "off"


def test_standby_is_refused_when_there_are_no_branches(api, repo) -> None:
    """Nothing to reveal: the graph was built without the composite."""
    client, _state = api
    client.patch("/api/channels/lofi", headers=AUTH, json={"visualization": {"enabled": False}})
    response = client.put(
        "/api/channels/lofi/visualization/visible", headers=AUTH, json={"visible": True}
    )
    assert response.status_code == 409
    assert response.json()["error"] == "visualization_not_built"


def test_the_overlay_command_is_bounded() -> None:
    """A timeline switch, not a level. Out of range, FFmpeg answers 0 Success."""
    assert build_message("overlay@viz", "enable", "0").endswith(" 0")
    assert build_message("overlay@viz", "enable", "1").endswith(" 1")
    with pytest.raises(ZmqValidationError):
        build_message("overlay@viz", "enable", "2")
    with pytest.raises(ZmqValidationError):
        build_message("overlay@viz", "enable", "0.5")


def test_opacity_persists_and_reaches_the_composer(api, repo) -> None:
    client, state = api
    response = client.put(
        "/api/channels/lofi/visualization/opacity", headers=AUTH, json={"opacity": 0.4}
    )
    assert response.status_code == 202
    assert response.json()["opacity"] == 0.4
    assert viz_of(repo)["opacity"] == 0.4

    channel = state.channel("lofi")
    assert compose_context(state.workspace, channel)["viz_opacity"] == "0.4"


def test_opacity_range_is_rejected(api) -> None:
    client, _state = api
    for value in (-0.01, 1.01):
        response = client.put(
            "/api/channels/lofi/visualization/opacity",
            headers=AUTH,
            json={"opacity": value},
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_request"


def test_opacity_expression_is_safe_and_commandable() -> None:
    message = build_message("lut@vizop", "y", "(val-16)*1.16438356*0.65")
    assert message == "lut@vizop y (val-16)*1.16438356*0.65"


def test_running_generic_patch_rejects_runtime_owned_visual_fields(api, repo, docker) -> None:
    client, _state = api
    docker.states["lofi-composer"] = RUNNING_STATE

    for field, value in (("opacity", 0.4), ("visible", False)):
        response = client.patch(
            "/api/channels/lofi",
            headers=AUTH,
            json={"visualization": {field: value}},
        )
        assert response.status_code == 409
        assert response.json()["error"] == "visualization_runtime_endpoint"
    assert viz_of(repo).get("opacity", 0.65) == 0.65
    assert viz_of(repo).get("visible", True) is True


def test_failed_runtime_delivery_is_not_reported_live(api, docker, monkeypatch) -> None:
    from ambient.api import looks

    client, _state = api
    docker.states["lofi-composer"] = RUNNING_STATE

    async def fail_delivery(_state, _name, _messages):
        return False

    monkeypatch.setattr(looks, "_send", fail_delivery)
    response = client.put(
        "/api/channels/lofi/visualization/opacity",
        headers=AUTH,
        json={"opacity": 0.4},
    )

    assert response.status_code == 202
    assert response.json()["live"] is False


def test_concurrent_opacity_updates_deliver_in_persisted_order(
    api, repo, docker, monkeypatch
) -> None:
    from ambient.api import looks

    client, _state = api
    docker.states["lofi-composer"] = RUNNING_STATE
    first_entered = threading.Event()
    release_first = threading.Event()
    delivered: list[str] = []

    async def delayed_send(_state, _name, messages):
        if not delivered:
            first_entered.set()
            await asyncio.to_thread(release_first.wait)
        delivered.extend(messages)
        return True

    monkeypatch.setattr(looks, "_send", delayed_send)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            client.put,
            "/api/channels/lofi/visualization/opacity",
            headers=AUTH,
            json={"opacity": 0.2},
        )
        assert first_entered.wait(timeout=2)
        second = pool.submit(
            client.put,
            "/api/channels/lofi/visualization/opacity",
            headers=AUTH,
            json={"opacity": 0.8},
        )
        release_first.set()
        responses = [first.result(timeout=5), second.result(timeout=5)]

    assert [response.json()["live"] for response in responses] == [True, True]
    assert delivered == [
        "lut@vizop y (val-16)*1.16438356*0.2",
        "lut@vizop y (val-16)*1.16438356*0.8",
    ]
    assert viz_of(repo)["opacity"] == 0.8


def test_concurrent_visibility_updates_deliver_in_persisted_order(
    api, repo, docker, monkeypatch
) -> None:
    from ambient.api import looks

    client, _state = api
    docker.states["lofi-composer"] = RUNNING_STATE
    first_entered = threading.Event()
    release_first = threading.Event()
    delivered: list[str] = []

    async def delayed_send(_state, _name, messages):
        if not delivered:
            first_entered.set()
            await asyncio.to_thread(release_first.wait)
        delivered.extend(messages)
        return True

    monkeypatch.setattr(looks, "_send", delayed_send)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            client.put,
            "/api/channels/lofi/visualization/visible",
            headers=AUTH,
            json={"visible": False},
        )
        assert first_entered.wait(timeout=2)
        second = pool.submit(
            client.put,
            "/api/channels/lofi/visualization/visible",
            headers=AUTH,
            json={"visible": True},
        )
        release_first.set()
        responses = [first.result(timeout=5), second.result(timeout=5)]

    assert [response.json()["live"] for response in responses] == [True, True]
    assert delivered == ["overlay@viz enable 0", "overlay@viz enable 1"]
    assert viz_of(repo)["visible"] is True


# ------------------------------------------------------------------ parameters


def test_a_manifest_without_parameters_still_loads(tmp_path) -> None:
    directory = tmp_path / "plain"
    directory.mkdir()
    (directory / "config.json").write_text(json.dumps({"name": "plain"}), encoding="utf-8")
    assert plugin_registry.load_manifest(directory).parameters == ()


def test_values_are_clamped_not_rejected(api, repo, docker) -> None:
    """FFmpeg takes an out-of-range option, ignores it, and exits 0."""
    client, _state = api
    with_real_plugins(repo)
    response = client.put(
        "/api/channels/lofi/visualization/parameters", headers=AUTH,
        json={"plugin": "showfreqs-bars", "values": {"detail": 999999, "smoothing": -4}},
    )
    assert response.status_code == 200
    stored = viz_of(repo)["parameters"]["showfreqs-bars"]
    assert stored["detail"] == 8192, "clamped to the manifest maximum"
    assert stored["smoothing"] == 1, "clamped to the manifest minimum"
    assert not docker.compose_calls()


def test_an_undeclared_parameter_is_refused(api, repo) -> None:
    client, _state = api
    with_real_plugins(repo)
    response = client.put(
        "/api/channels/lofi/visualization/parameters", headers=AUTH,
        json={"plugin": "showfreqs-bars", "values": {"nonsense": 1}},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "unknown_parameter"
    assert "parameters" not in viz_of(repo) or not viz_of(repo)["parameters"]


def test_each_plugin_keeps_its_own_settings(api, repo) -> None:
    client, _state = api
    with_real_plugins(repo)
    client.put("/api/channels/lofi/visualization/parameters", headers=AUTH,
               json={"plugin": "showfreqs-bars", "values": {"detail": 2048}})
    client.put("/api/channels/lofi/visualization/parameters", headers=AUTH,
               json={"plugin": "showwaves-classic", "values": {"response": "log"}})
    stored = viz_of(repo)["parameters"]
    assert stored["showfreqs-bars"]["detail"] == 2048
    assert stored["showwaves-classic"]["response"] == "log"


def test_the_values_reach_the_composer_as_json(api, repo) -> None:
    client, state = api
    with_real_plugins(repo)
    client.put("/api/channels/lofi/visualization/parameters", headers=AUTH,
               json={"plugin": "showfreqs-bars", "values": {"shape": "dot"}})
    channel = state.channel("lofi")
    payload = json.loads(compose_context(state.workspace, channel)["plugin_parameters"])
    assert payload["showfreqs-bars"]["shape"] == "dot"


def test_tuning_the_active_plugin_recreates_only_visualizer(api, repo, docker) -> None:
    client, _state = api
    with_real_plugins(repo)
    docker.states["lofi-composer"] = RUNNING_STATE

    response = client.put(
        "/api/channels/lofi/visualization/parameters", headers=AUTH,
        json={"plugin": "showfreqs-bars", "values": {"detail": 2048}},
    )

    assert response.status_code == 202
    assert response.json()["restarted"] is False
    assert response.json()["visualizer_recreated"] is True
    call = eventually(
        lambda: docker.calls_matching("--force-recreate", "lofi-visualizer")
    )[-1]
    assert call[-1] == "lofi-visualizer"
    assert not docker.calls_matching("--force-recreate", "lofi-composer")


def test_every_shipped_plugin_declares_usable_parameters() -> None:
    """A token in a fragment with no matching parameter can never be substituted."""
    from pathlib import Path

    registry = plugin_registry.load_registry(Path("plugins"))
    assert registry, "the repo ships plugins"
    for manifest in registry.values():
        fragment = manifest.fragment_path.read_text(encoding="utf-8")
        declared = {p.token for p in manifest.parameters}
        used = set(__import__("re").findall(r"\$\{([A-Z_]+)\}", fragment))
        builtin = {"WIDTH", "HEIGHT", "FPS", "ACCENT", "OUT"}
        assert used - builtin <= declared, (
            f"{manifest.name}: fragment uses {sorted(used - builtin - declared)} "
            "which no parameter declares"
        )
        assert declared <= used, (
            f"{manifest.name}: declares {sorted(declared - used)} which the fragment never uses"
        )
