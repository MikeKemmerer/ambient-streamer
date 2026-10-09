"""The limited AMBIENT_SHORTCUT_TOKEN: it reaches a short allowlist and nothing else."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from ambient.api.deps import SHORTCUT_ROUTES
from ambient.main import (
    MIN_SHORTCUT_TOKEN_LENGTH,
    StartupRefused,
    build_state,
    check_shortcut_token,
    create_app,
)
from tests.conftest import TOKEN

SHORTCUT = "d3b07384d113edec49eaa6238ad5ff00d3b07384d113edec"
SHORTCUT_AUTH = {"Authorization": f"Bearer {SHORTCUT}"}
MASTER_AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def shortcut_api(repo, docker, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AMBIENT_API_TOKEN", TOKEN)
    monkeypatch.setenv("AMBIENT_SHORTCUT_TOKEN", SHORTCUT)
    monkeypatch.setenv("AMBIENT_BIND_ADDRESS", "127.0.0.1")
    app = create_app(repo)
    with TestClient(app) as client:
        state = app.state.ambient
        state.watchdog.enabled = False
        state.scheduler.enabled = False
        state.supervisor.runner = docker
        state.watchdog.latest.clear()
        state.watchdog.channels.clear()
        yield client, state, app


# --------------------------------------------------------------------------
# What the limited token may do
# --------------------------------------------------------------------------


def test_the_limited_token_can_read_status_and_list_clips(shortcut_api) -> None:
    client, _state, _app = shortcut_api
    listing = client.get("/api/channels", headers=SHORTCUT_AUTH)
    assert listing.status_code == 200
    assert [c["name"] for c in listing.json()["channels"]] == ["lofi"]
    assert client.get("/api/channels/lofi/soundboard", headers=SHORTCUT_AUTH).status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/api/channels/lofi/skip",
        "/api/channels/lofi/soundboard/stop",
    ],
)
def test_the_limited_token_passes_authorization_on_its_actions(shortcut_api, path: str) -> None:
    client, _state, _app = shortcut_api
    response = client.post(path, headers=SHORTCUT_AUTH)
    # The channel is not running in this fixture, so the handler answers 409. What matters
    # is that authentication let the request through.
    assert response.status_code not in (401, 403), response.text


def test_the_limited_token_passes_authorization_on_playing_a_sound(shortcut_api) -> None:
    client, _state, _app = shortcut_api
    response = client.post(
        "/api/channels/lofi/soundboard/play",
        headers=SHORTCUT_AUTH,
        json={"clip": "/media/common/soundboard/air-horn.wav"},
    )
    assert response.status_code not in (401, 403), response.text


# --------------------------------------------------------------------------
# What it must never do
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/channels/lofi/start"),
        ("POST", "/api/channels/lofi/stop"),
        ("POST", "/api/channels/lofi/restart"),
        ("DELETE", "/api/channels/lofi"),
        ("POST", "/api/channels"),
        ("PATCH", "/api/channels/lofi"),
        ("GET", "/api/channels/lofi"),
        ("PUT", "/api/channels/lofi/playlist"),
        ("PUT", "/api/channels/lofi/delivery"),
        ("PUT", "/api/channels/lofi/resolution"),
        ("POST", "/api/channels/lofi/play"),
        ("GET", "/api/system"),
        ("GET", "/api/capacity"),
        ("GET", "/api/events"),
        ("GET", "/api/logs?channel=lofi"),
        ("GET", "/api/media/soundboard"),
        ("GET", "/api/channels/lofi/soundboard/preview?clip=x"),
    ],
)
def test_the_limited_token_is_refused_everywhere_else(shortcut_api, method: str, path: str) -> None:
    client, _state, _app = shortcut_api
    response = client.request(method, path, headers=SHORTCUT_AUTH)
    assert response.status_code == 403, (method, path, response.status_code, response.text)
    assert response.json() == {
        "error": "forbidden",
        "detail": "this token is limited to shortcut actions",
    }


def _concrete(path: str) -> str:
    return re.sub(r"\{([^}:]+)(?::[^}]*)?\}", lambda m: "lofi" if m.group(1) == "name" else "x", path)


def test_every_registered_route_outside_the_allowlist_refuses_the_limited_token(shortcut_api) -> None:
    """A future endpoint must not become reachable by accident."""
    client, _state, app = shortcut_api
    checked = 0
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api/") or path == "/api/health":
            continue
        for method in operations:
            method = method.upper()
            if method in {"HEAD", "OPTIONS"} or (method, path) in SHORTCUT_ROUTES:
                continue
            response = client.request(method, _concrete(path), headers=SHORTCUT_AUTH)
            assert response.status_code == 403, (method, path, response.status_code)
            checked += 1
    assert checked > 30  # the sweep really did cover the API


def test_the_allowlist_is_exactly_what_was_agreed() -> None:
    assert SHORTCUT_ROUTES == {
        ("GET", "/api/channels"),
        ("GET", "/api/channels/{name}/soundboard"),
        ("POST", "/api/channels/{name}/skip"),
        ("POST", "/api/channels/{name}/soundboard/play"),
        ("POST", "/api/channels/{name}/soundboard/stop"),
    }


# --------------------------------------------------------------------------
# The master token and bad tokens are unaffected
# --------------------------------------------------------------------------


def test_the_master_token_still_works_everywhere(shortcut_api) -> None:
    client, _state, _app = shortcut_api
    assert client.get("/api/channels/lofi", headers=MASTER_AUTH).status_code == 200
    assert client.get("/api/system", headers=MASTER_AUTH).status_code == 200
    assert client.get("/api/channels", headers=MASTER_AUTH).status_code == 200


@pytest.mark.parametrize(
    "header",
    [
        {},
        {"Authorization": "Bearer nope"},
        {"Authorization": f"Basic {SHORTCUT}"},
        {"Authorization": SHORTCUT},
        {"Authorization": "Bearer "},
        {"Authorization": f"Bearer {SHORTCUT}x"},
        {"Authorization": f"Bearer {SHORTCUT[:-1]}"},
    ],
)
def test_a_wrong_token_is_still_401_not_403(shortcut_api, header: dict) -> None:
    client, _state, _app = shortcut_api
    response = client.get("/api/channels", headers=header)
    assert response.status_code == 401
    assert response.json()["error"] == "unauthorized"


def test_with_the_feature_off_the_would_be_token_is_just_a_wrong_token(api) -> None:
    client, state = api
    assert state.shortcut_token == ""
    response = client.get("/api/channels", headers=SHORTCUT_AUTH)
    assert response.status_code == 401
    # An empty limited token must never match an empty bearer value either.
    assert client.get("/api/channels", headers={"Authorization": "Bearer "}).status_code == 401


# --------------------------------------------------------------------------
# Startup checks
# --------------------------------------------------------------------------


def test_an_unset_limited_token_is_fine() -> None:
    check_shortcut_token(TOKEN, "")


def test_a_limited_token_equal_to_the_master_is_refused() -> None:
    with pytest.raises(StartupRefused, match="same as AMBIENT_API_TOKEN"):
        check_shortcut_token(TOKEN, TOKEN)


def test_a_short_limited_token_is_refused() -> None:
    with pytest.raises(StartupRefused, match="shorter than"):
        check_shortcut_token(TOKEN, "x" * (MIN_SHORTCUT_TOKEN_LENGTH - 1))
    check_shortcut_token(TOKEN, "x" * MIN_SHORTCUT_TOKEN_LENGTH)


def test_the_app_refuses_to_start_with_a_weak_limited_token(
    repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMBIENT_API_TOKEN", TOKEN)
    monkeypatch.setenv("AMBIENT_SHORTCUT_TOKEN", TOKEN)
    monkeypatch.setenv("AMBIENT_BIND_ADDRESS", "127.0.0.1")
    with pytest.raises(StartupRefused, match="same as AMBIENT_API_TOKEN"):
        build_state(repo)
    monkeypatch.setenv("AMBIENT_SHORTCUT_TOKEN", SHORTCUT)
    assert build_state(repo).shortcut_token == SHORTCUT
