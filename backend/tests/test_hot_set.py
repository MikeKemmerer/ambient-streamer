"""Compatibility behavior for the deprecated visualization.hot_set field."""

from __future__ import annotations

import json

import yaml

from tests.conftest import AUTH


def seed_registry(repo, *names: str) -> None:
    """Without manifests the backend deliberately skips membership checks."""
    for plugin in names:
        directory = repo / "plugins" / plugin
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "config.json").write_text(json.dumps({
            "name": plugin,
            "display_name": plugin,
            "description": "test fixture",
            "version": "1.0.0",
            "author": "tests",
            "commandable": [],
            "cost": {"cores_720p30": 0.2, "scale_1080p": 1.9},
            "requires_filters": [],
        }), encoding="utf-8")
        (directory / "viz.ffmpeg").write_text(
            "[0:a]showvolume@viz=s=${WIDTH}x${HEIGHT},fps=${FPS},"
            "format=yuv420p,setsar=1[${OUT}]\n", encoding="utf-8")


def viz_of(repo, name: str = "lofi") -> dict:
    raw = yaml.safe_load((repo / "channels" / name / "config.yaml").read_text())
    return raw["visualization"]


def grow(client, repo, extra: str) -> list[str]:
    wanted = [*viz_of(repo)["hot_set"], extra]
    response = client.put("/api/channels/lofi/hot-set", headers=AUTH, json={"hot_set": wanted})
    assert response.status_code == 200, response.text
    return wanted


def test_a_branch_can_be_removed(api, repo) -> None:
    client, _state = api
    grow(client, repo, "showwaves-classic")
    before = viz_of(repo)
    assert len(before["hot_set"]) > 1

    keep = [before["active"]]
    response = client.put("/api/channels/lofi/hot-set", headers=AUTH, json={"hot_set": keep})

    assert response.status_code == 200
    assert viz_of(repo)["hot_set"] == keep
    assert viz_of(repo)["active"] == before["active"], "the look must not change"


def test_active_need_not_remain_in_the_legacy_set(api, repo) -> None:
    client, _state = api
    seed_registry(repo, "showfreqs-bars", "showwaves-classic")
    grow(client, repo, "showwaves-classic")
    before = viz_of(repo)
    others = [p for p in before["hot_set"] if p != before["active"]]

    accepted = client.put("/api/channels/lofi/hot-set", headers=AUTH, json={"hot_set": others})
    assert accepted.status_code == 200
    assert viz_of(repo)["hot_set"] == others
    assert viz_of(repo)["active"] == before["active"]

    switched = client.put(
        "/api/channels/lofi/hot-set", headers=AUTH,
        json={"hot_set": [], "active": others[0]},
    )
    assert switched.status_code == 200
    assert viz_of(repo)["active"] == others[0]


def test_an_empty_hot_set_is_accepted(api, repo) -> None:
    client, _state = api
    response = client.put("/api/channels/lofi/hot-set", headers=AUTH, json={"hot_set": []})
    assert response.status_code == 200
    assert viz_of(repo)["hot_set"] == []


def test_uninstalled_legacy_entries_are_preserved_and_ignored(api, repo) -> None:
    client, _state = api
    seed_registry(repo, "showfreqs-bars", "showwaves-classic")
    before = viz_of(repo)
    response = client.put(
        "/api/channels/lofi/hot-set", headers=AUTH,
        json={"hot_set": [*before["hot_set"], "no-such-plugin"]},
    )
    assert response.status_code == 200
    assert viz_of(repo)["hot_set"] == [*before["hot_set"], "no-such-plugin"]


def test_any_installed_plugin_can_become_active(api, repo) -> None:
    client, _state = api
    seed_registry(repo, "showfreqs-bars", "showwaves-classic")
    response = client.put(
        "/api/channels/lofi/visualization", headers=AUTH,
        json={"active": "showwaves-classic"},
    )
    assert response.status_code == 200
    assert viz_of(repo)["active"] == "showwaves-classic"
    assert viz_of(repo)["hot_set"] == ["showfreqs-bars"]


def test_an_uninstalled_active_plugin_is_refused(api, repo) -> None:
    client, _state = api
    seed_registry(repo, "showfreqs-bars")
    response = client.put(
        "/api/channels/lofi/visualization", headers=AUTH,
        json={"active": "no-such-plugin"},
    )
    assert response.status_code == 404
    assert viz_of(repo)["active"] == "showfreqs-bars"


def test_membership_is_unverified_when_no_manifests_are_installed(api, repo) -> None:
    """An absent registry is not evidence that compatibility data is invalid."""
    client, _state = api
    before = viz_of(repo)
    response = client.put(
        "/api/channels/lofi/hot-set", headers=AUTH,
        json={"hot_set": [*before["hot_set"], "unverifiable"]},
    )
    assert response.status_code == 200


def test_duplicates_collapse(api, repo) -> None:
    """The compatibility endpoint continues its prior normalization."""
    client, _state = api
    active = viz_of(repo)["active"]
    response = client.put(
        "/api/channels/lofi/hot-set", headers=AUTH, json={"hot_set": [active, active]}
    )
    assert response.status_code == 200
    assert viz_of(repo)["hot_set"] == [active]


def test_an_unchanged_set_does_not_restart_the_channel(api, repo) -> None:
    client, _state = api
    before = viz_of(repo)
    response = client.put(
        "/api/channels/lofi/hot-set", headers=AUTH, json={"hot_set": before["hot_set"]}
    )
    assert response.json()["accepted"] is False
    assert response.json()["restarted"] is False


def test_the_new_set_does_not_change_capacity(api, repo) -> None:
    client, state = api
    seed_registry(repo, "showfreqs-bars")
    before = state.channel("lofi").projected_cores
    assert before > 0
    keep = [viz_of(repo)["active"]]
    keep.extend(["obsolete-a", "obsolete-b"])
    client.put("/api/channels/lofi/hot-set", headers=AUTH, json={"hot_set": keep})
    assert state.channel("lofi").projected_cores == before
