"""The operator UI: served at `/`, unauthenticated, and never over the API."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ambient.main import IMAGE_FRONTEND_DIR, create_app, resolve_frontend_dir
from tests.conftest import AUTH, TOKEN

INDEX = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>ambient-streamer \u2014 operator</title>
<link rel="stylesheet" href="style.css">
<script type="module" src="app.js"></script>
</head><body><h1>ambient-streamer</h1></body></html>
"""


@pytest.fixture
def ui(repo: Path, docker, monkeypatch: pytest.MonkeyPatch):
    """The app wired to a throwaway frontend directory with a decoy above it."""
    frontend = repo / "ui-dist"
    (frontend / "vendor").mkdir(parents=True)
    (frontend / "index.html").write_text(INDEX, encoding="utf-8")
    (frontend / "style.css").write_text("body { color: #eee; }\n", encoding="utf-8")
    (frontend / "app.js").write_text("export const api = '/api';\n", encoding="utf-8")
    (frontend / "vendor" / "hls.js").write_text("// vendored\n", encoding="utf-8")
    (repo / "secret.txt").write_text("not part of the UI\n", encoding="utf-8")

    monkeypatch.setenv("AMBIENT_API_TOKEN", TOKEN)
    monkeypatch.setenv("AMBIENT_BIND_ADDRESS", "127.0.0.1")
    monkeypatch.setenv("AMBIENT_FRONTEND_DIR", str(frontend))
    app = create_app(repo)
    with TestClient(app) as client:
        state = app.state.ambient
        state.watchdog.enabled = False
        state.scheduler.enabled = False
        state.supervisor.runner = docker
        state.watchdog.latest.clear()
        state.watchdog.channels.clear()
        yield client, frontend


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def test_the_env_var_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AMBIENT_FRONTEND_DIR", str(tmp_path / "elsewhere"))
    assert resolve_frontend_dir(tmp_path) == (tmp_path / "elsewhere").resolve()


def test_a_source_checkout_falls_back_to_its_own_frontend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AMBIENT_FRONTEND_DIR", raising=False)
    (tmp_path / "frontend").mkdir()
    expected = IMAGE_FRONTEND_DIR if IMAGE_FRONTEND_DIR.is_dir() else tmp_path / "frontend"
    assert resolve_frontend_dir(tmp_path) == expected.resolve()


def test_a_blank_env_var_is_treated_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMBIENT_FRONTEND_DIR", "   ")
    assert resolve_frontend_dir(tmp_path) != Path("   ").resolve()


# --------------------------------------------------------------------------
# Serving
# --------------------------------------------------------------------------


def test_the_root_serves_index_html_without_a_token(ui) -> None:
    client, _frontend = ui
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<!DOCTYPE html>" in response.text
    assert "ambient-streamer" in response.text


def test_static_assets_carry_a_sane_content_type(ui) -> None:
    client, _frontend = ui
    css = client.get("/style.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert "#eee" in css.text

    js = client.get("/app.js")
    assert js.status_code == 200
    assert js.headers["content-type"].split(";")[0] in {
        "text/javascript",
        "application/javascript",
    }

    nested = client.get("/vendor/hls.js")
    assert nested.status_code == 200
    assert "vendored" in nested.text


def test_a_missing_asset_is_a_contract_shaped_404(ui) -> None:
    client, _frontend = ui
    response = client.get("/nope.js")
    assert response.status_code == 404
    assert response.json() == {"error": "not_found", "detail": "Not Found"}


def test_no_asset_response_carries_the_token(ui) -> None:
    client, _frontend = ui
    for path in ("/", "/style.css", "/app.js"):
        assert TOKEN not in client.get(path).text, path


# --------------------------------------------------------------------------
# The mount must not weaken or shadow the API
# --------------------------------------------------------------------------


def test_the_api_is_unchanged_behind_the_mount(ui) -> None:
    client, _frontend = ui
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/channels").status_code == 401
    authed = client.get("/api/channels", headers=AUTH)
    assert authed.status_code == 200
    assert "lofi" in {entry["name"] for entry in authed.json()["channels"]}
    assert client.get("/api/channels/lofi", headers=AUTH).status_code == 200
    assert client.get("/api/channels/nope", headers=AUTH).json()["error"] == "unknown_channel"


def test_an_unknown_api_path_stays_json_not_html(ui) -> None:
    client, _frontend = ui
    response = client.get("/api/does-not-exist", headers=AUTH)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"] == "not_found"


def test_a_file_cannot_shadow_an_api_route(ui) -> None:
    client, frontend = ui
    decoy = frontend / "api"
    decoy.mkdir()
    (decoy / "health").write_text("shadowed\n", encoding="utf-8")
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "shadowed" not in response.text


# --------------------------------------------------------------------------
# Nothing outside the directory
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/%2e%2e/secret.txt",
        "/%2e%2e%2fsecret.txt",
        "/vendor/../../secret.txt",
        "/....//secret.txt",
        "/%2f%2fetc%2fpasswd",
    ],
)
def test_a_traversal_never_escapes_the_frontend_directory(ui, path: str) -> None:
    client, _frontend = ui
    response = client.get(path)
    assert response.status_code == 404, path
    assert "not part of the UI" not in response.text, path


def test_an_absolute_host_path_is_not_served(ui) -> None:
    client, _frontend = ui
    assert "root:" not in client.get("/etc/passwd").text


# --------------------------------------------------------------------------
# A missing UI must not take the control plane down
# --------------------------------------------------------------------------


def test_a_missing_directory_warns_and_leaves_the_api_up(
    repo: Path, docker, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    missing = repo / "no-such-frontend"
    monkeypatch.setenv("AMBIENT_API_TOKEN", TOKEN)
    monkeypatch.setenv("AMBIENT_BIND_ADDRESS", "127.0.0.1")
    monkeypatch.setenv("AMBIENT_FRONTEND_DIR", str(missing))

    with caplog.at_level(logging.WARNING, logger="ambient.main"):
        app = create_app(repo)
    assert any(str(missing) in record.getMessage() for record in caplog.records)

    with TestClient(app) as client:
        app.state.ambient.watchdog.enabled = False
        app.state.ambient.scheduler.enabled = False
        app.state.ambient.supervisor.runner = docker
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/channels", headers=AUTH).status_code == 200
        root = client.get("/")
        assert root.status_code == 404
        assert root.json()["error"] == "not_found"


def test_a_file_where_the_directory_should_be_is_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decoy = repo / "frontend.txt"
    decoy.write_text("not a directory\n", encoding="utf-8")
    monkeypatch.setenv("AMBIENT_API_TOKEN", TOKEN)
    monkeypatch.setenv("AMBIENT_BIND_ADDRESS", "127.0.0.1")
    monkeypatch.setenv("AMBIENT_FRONTEND_DIR", str(decoy))
    app = create_app(repo)
    assert not any(getattr(route, "name", "") == "frontend" for route in app.routes)
