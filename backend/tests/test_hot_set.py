"""Editing which branches the graph instantiates.

Switching to a plugin can only ever grow `hot_set`, so without this endpoint the
per-channel CPU cost is a one-way ratchet: an operator can add a 0.28-core idle
branch by accident and has no way to give it back.
"""

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
    """The fixture ships one branch; removal needs something to remove."""
    wanted = [*viz_of(repo)["hot_set"], extra]
    response = client.put("/api/channels/lofi/hot-set", headers=AUTH, json={"hot_set": wanted})
    assert response.status_code == 202, response.text
    return wanted


def test_a_branch_can_be_removed(api, repo) -> None:
    client, _state = api
    grow(client, repo, "showwaves-classic")
    before = viz_of(repo)
    assert len(before["hot_set"]) > 1

    keep = [before["active"]]
    response = client.put("/api/channels/lofi/hot-set", headers=AUTH, json={"hot_set": keep})

    assert response.status_code == 202
    assert viz_of(repo)["hot_set"] == keep
    assert viz_of(repo)["active"] == before["active"], "the look must not change"


def test_removing_the_branch_on_air_needs_a_replacement_named(api, repo) -> None:
    client, _state = api
    grow(client, repo, "showwaves-classic")
    before = viz_of(repo)
    others = [p for p in before["hot_set"] if p != before["active"]]

    refused = client.put("/api/channels/lofi/hot-set", headers=AUTH, json={"hot_set": others})
    assert refused.status_code == 409
    assert refused.json()["error"] == "active_not_in_hot_set"
    assert viz_of(repo)["hot_set"] == before["hot_set"], "a refused edit must not persist"

    accepted = client.put(
        "/api/channels/lofi/hot-set", headers=AUTH,
        json={"hot_set": others, "active": others[0]},
    )
    assert accepted.status_code == 202
    assert viz_of(repo)["active"] == others[0]


def test_an_empty_hot_set_is_refused(api, repo) -> None:
    """A graph with no branches is what `visualization.enabled=false` is for."""
    client, _state = api
    before = viz_of(repo)
    response = client.put("/api/channels/lofi/hot-set", headers=AUTH, json={"hot_set": []})
    assert response.status_code == 400
    assert viz_of(repo)["hot_set"] == before["hot_set"]


def test_an_uninstalled_plugin_is_refused(api, repo) -> None:
    client, _state = api
    seed_registry(repo, "showfreqs-bars", "showwaves-classic")
    before = viz_of(repo)
    response = client.put(
        "/api/channels/lofi/hot-set", headers=AUTH,
        json={"hot_set": [*before["hot_set"], "no-such-plugin"]},
    )
    assert response.status_code == 404
    assert viz_of(repo)["hot_set"] == before["hot_set"]


def test_membership_is_unverified_when_no_manifests_are_installed(api, repo) -> None:
    """Matches check_hot_set: an absent registry is not evidence of absence."""
    client, _state = api
    before = viz_of(repo)
    response = client.put(
        "/api/channels/lofi/hot-set", headers=AUTH,
        json={"hot_set": [*before["hot_set"], "unverifiable"]},
    )
    assert response.status_code == 202


def test_duplicates_collapse(api, repo) -> None:
    """A branch instantiated twice costs twice and renders the same thing."""
    client, _state = api
    active = viz_of(repo)["active"]
    response = client.put(
        "/api/channels/lofi/hot-set", headers=AUTH, json={"hot_set": [active, active]}
    )
    assert response.status_code == 202
    assert viz_of(repo)["hot_set"] == [active]


def test_an_unchanged_set_does_not_restart_the_channel(api, repo) -> None:
    client, _state = api
    before = viz_of(repo)
    response = client.put(
        "/api/channels/lofi/hot-set", headers=AUTH, json={"hot_set": before["hot_set"]}
    )
    assert response.json()["accepted"] is False
    assert response.json()["restarted"] is False


def test_the_new_set_reaches_the_composer(api, repo) -> None:
    from ambient.supervisor import compose_context

    client, state = api
    keep = [viz_of(repo)["active"]]
    client.put("/api/channels/lofi/hot-set", headers=AUTH, json={"hot_set": keep})

    channel = state.channel("lofi")
    assert compose_context(state.workspace, channel)["hot_set"] == ",".join(keep)
