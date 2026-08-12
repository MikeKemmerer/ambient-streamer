"""CLI tests for `python -m ambient.compile`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ambient.compile import main
from tests.test_config import CHANNEL_ENV, CHANNEL_YAML, make_repo


def add_channel(root: Path, name: str, *, with_audio: bool = True, mount: str | None = None) -> None:
    channel = root / "channels" / name
    (channel / "audio").mkdir(parents=True)
    (channel / "images").mkdir(parents=True)
    if with_audio:
        (channel / "audio" / "a.mp3").write_bytes(b"x")
    (channel / "images" / "a.jpg").write_bytes(b"x")
    (channel / "config.yaml").write_text(
        CHANNEL_YAML.replace("name: lofi", f"name: {name}"), encoding="utf-8"
    )
    env = CHANNEL_ENV.replace("/lofi", mount or f"/{name}")
    (channel / ".env").write_text(env, encoding="utf-8")


def test_compiles_a_channel(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_repo(tmp_path)
    assert main(["lofi", "--repo-root", str(root)]) == 0
    playlist = root / "channels" / "lofi" / "playlist.m3u"
    images = root / "channels" / "lofi" / "images.list"
    assert playlist.read_text() == "/media/channel/audio/01 - a track.m4a\n"
    assert images.read_text() == "/media/channel/images/slide.jpeg\n"


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    assert main(["lofi", "--repo-root", str(root), "--dry-run"]) == 0
    assert not (root / "channels" / "lofi" / "playlist.m3u").exists()


def test_all_isolates_a_broken_channel(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(tmp_path)
    add_channel(root, "broken", with_audio=False)
    rc = main(["--all", "--repo-root", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert [c["channel"] for c in payload["channels"]] == ["lofi"]
    assert any("broken" in e and "no audio" in e for e in payload["errors"])


def test_duplicate_mount_across_channels_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_repo(tmp_path)
    add_channel(root, "twin", mount="/lofi")
    rc = main(["--all", "--repo-root", str(root), "--json", "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert any("unique" in e for e in payload["errors"])


def test_unknown_channel_name_is_an_error(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    assert main(["../etc", "--repo-root", str(root)]) == 1
