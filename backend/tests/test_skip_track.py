"""Skipping a track.

Liquidsoap owns audio in its own process and the compositor is a consumer of a
live Icecast mount, so a track change costs the video nothing - no restart, no
gap. That is the whole reason this is a separate control plane from zmq.

Which command to send was measured on a live channel, not read from the manual:
`icecast.skip` answers `Done` and the track changes; `playlist.skip` answers
`OK` and only advances the playlist cursor, leaving the audio where it was.
"""

from __future__ import annotations

import pytest

from ambient import liqctl
from tests.conftest import AUTH, RUNNING_STATE


def test_only_whitelisted_commands_are_sendable() -> None:
    """The same socket accepts `shutdown`."""
    assert liqctl.validate_command(liqctl.SKIP) == "icecast.skip"
    for refused in ["shutdown", "exit", "playlist.uri /etc/passwd", "icecast.stop"]:
        with pytest.raises(liqctl.LiquidsoapError):
            liqctl.validate_command(refused)


def test_playlist_skip_is_not_offered() -> None:
    """Measured: it answers OK and does not change what is playing."""
    with pytest.raises(liqctl.LiquidsoapError):
        liqctl.validate_command("playlist.skip")


def test_the_host_matches_the_container_the_template_pins() -> None:
    assert liqctl.liquidsoap_host("lofi") == "lofi-liquidsoap"


def test_skipping_a_stopped_channel_is_refused(api) -> None:
    client, _state = api
    response = client.post("/api/channels/lofi/skip", headers=AUTH)
    assert response.status_code == 409
    assert response.json()["error"] == "channel_not_running"


def test_skip_sends_the_command_that_moves_the_audio(api, docker, monkeypatch) -> None:
    client, _state = api
    docker.states["lofi-liquidsoap"] = RUNNING_STATE

    sent: list[tuple[str, str]] = []

    def fake_send(host: str, command: str, **_kwargs) -> str:
        sent.append((host, command))
        return "Done"

    monkeypatch.setattr(liqctl, "send", fake_send)
    response = client.post("/api/channels/lofi/skip", headers=AUTH)

    assert response.status_code == 202
    assert sent == [("lofi-liquidsoap", "icecast.skip")]
    assert response.json()["detail"] == "Done"


def test_an_unreachable_liquidsoap_is_reported_not_hung(api, docker, monkeypatch) -> None:
    client, _state = api
    docker.states["lofi-liquidsoap"] = RUNNING_STATE

    def refuse(host: str, command: str, **_kwargs) -> str:
        raise liqctl.LiquidsoapError("connection refused")

    monkeypatch.setattr(liqctl, "send", refuse)
    response = client.post("/api/channels/lofi/skip", headers=AUTH)

    assert response.status_code == 502
    assert response.json()["error"] == "liquidsoap_unreachable"


def test_skipping_requires_a_token(api) -> None:
    client, _state = api
    assert client.post("/api/channels/lofi/skip").status_code == 401
