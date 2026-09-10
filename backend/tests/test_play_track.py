"""Playing one specific track.

`playlist` has no "play this one" verb - measured on a live channel,
`playlist.skip` answers OK and only advances an internal cursor, leaving the
audio where it was - so the track goes onto a `request.queue` that sits in
front of the playlist in a `fallback(track_sensitive=false, ...)`. Measured on
a live channel: the push interrupts the current track, and after `queue.skip`
the playlist resumes.

`queue.push` resolves whatever it is handed, including an absolute path
anywhere on the Liquidsoap container or an `http://` URL, so the track must be
one this channel already resolved.
"""

from __future__ import annotations

import pytest

from ambient import liqctl
from tests.conftest import AUTH, RUNNING_STATE

TRACK = "/media/channel/audio/01 - a track.m4a"


def test_only_a_track_on_this_channel_may_be_pushed() -> None:
    assert liqctl.validate_uri(TRACK, [TRACK]) == TRACK
    for refused in [
        "/etc/passwd",
        "http://example.invalid/x.mp3",
        "/media/channel/audio/not mine.mp3",
        "",
    ]:
        with pytest.raises(liqctl.LiquidsoapError):
            liqctl.validate_uri(refused, [TRACK])


def test_a_second_command_cannot_ride_along_on_the_line() -> None:
    """The socket also accepts `shutdown`."""
    with pytest.raises(liqctl.LiquidsoapError):
        liqctl.validate_uri(f"{TRACK}\nshutdown", [TRACK, f"{TRACK}\nshutdown"])


def test_playing_on_a_stopped_channel_is_refused(api) -> None:
    client, _state = api
    response = client.post("/api/channels/lofi/play", json={"track": TRACK}, headers=AUTH)
    assert response.status_code == 409
    assert response.json()["error"] == "channel_not_running"


def test_play_pushes_the_track_onto_the_request_queue(api, docker, monkeypatch) -> None:
    client, _state = api
    docker.states["lofi-liquidsoap"] = RUNNING_STATE

    sent: list[tuple[str, str, tuple[str, ...]]] = []

    def fake_push(host: str, uri: str, *, allowed, **_kwargs) -> str:
        sent.append((host, uri, tuple(allowed)))
        return "4"

    monkeypatch.setattr(liqctl, "push", fake_push)
    response = client.post("/api/channels/lofi/play", json={"track": TRACK}, headers=AUTH)

    assert response.status_code == 202
    assert response.json()["track"] == TRACK
    assert response.json()["detail"] == "4"
    host, uri, allowed = sent[0]
    assert (host, uri) == ("lofi-liquidsoap", TRACK)
    assert TRACK in allowed


def test_a_track_this_channel_does_not_have_is_a_404(api, docker) -> None:
    client, _state = api
    docker.states["lofi-liquidsoap"] = RUNNING_STATE
    response = client.post(
        "/api/channels/lofi/play", json={"track": "/etc/passwd"}, headers=AUTH
    )
    assert response.status_code == 404
    assert response.json()["error"] == "unknown_track"


def test_an_unreachable_liquidsoap_is_reported_not_hung(api, docker, monkeypatch) -> None:
    client, _state = api
    docker.states["lofi-liquidsoap"] = RUNNING_STATE

    def refuse(host: str, uri: str, **_kwargs) -> str:
        raise liqctl.LiquidsoapError("connection refused")

    monkeypatch.setattr(liqctl, "push", refuse)
    response = client.post("/api/channels/lofi/play", json={"track": TRACK}, headers=AUTH)

    assert response.status_code == 502
    assert response.json()["error"] == "liquidsoap_unreachable"


def test_playing_requires_a_token(api) -> None:
    client, _state = api
    assert client.post("/api/channels/lofi/play", json={"track": TRACK}).status_code == 401
