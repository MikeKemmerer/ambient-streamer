"""Resolver tests: the three selection forms, expansion rules, and the path
validation boundary. Traversal attempts are the point of this file."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ambient.media import (
    MediaError,
    MediaKind,
    MediaRoots,
    atomic_write_lines,
    natural_key,
    resolve_selection,
    write_images_list,
    write_playlist,
)


@pytest.fixture()
def repo(tmp_path: Path) -> MediaRoots:
    (tmp_path / "common" / "audio").mkdir(parents=True)
    (tmp_path / "common" / "images").mkdir(parents=True)
    (tmp_path / "channels" / "lofi" / "audio" / "deep").mkdir(parents=True)
    (tmp_path / "channels" / "lofi" / "images").mkdir(parents=True)
    (tmp_path / "channels" / "other" / "audio").mkdir(parents=True)
    (tmp_path / "outside").mkdir()

    (tmp_path / "common" / "audio" / "intro.mp3").write_bytes(b"x")
    (tmp_path / "common" / "images" / "forest.jpg").write_bytes(b"x")
    (tmp_path / "channels" / "lofi" / "audio" / "track2.mp3").write_bytes(b"x")
    (tmp_path / "channels" / "lofi" / "audio" / "track10.mp3").write_bytes(b"x")
    (tmp_path / "channels" / "lofi" / "audio" / "01 - has spaces.m4a").write_bytes(b"x")
    (tmp_path / "channels" / "lofi" / "audio" / "README").write_bytes(b"x")
    (tmp_path / "channels" / "lofi" / "audio" / ".hidden.mp3").write_bytes(b"x")
    (tmp_path / "channels" / "lofi" / "audio" / "deep" / "nested.flac").write_bytes(b"x")
    (tmp_path / "channels" / "lofi" / "images" / "city night.jpeg").write_bytes(b"x")
    (tmp_path / "channels" / "other" / "audio" / "notmine.mp3").write_bytes(b"x")
    (tmp_path / "outside" / "secret.mp3").write_bytes(b"x")

    return MediaRoots.create(tmp_path, tmp_path / "common", tmp_path / "channels" / "lofi")


def test_folder_mode_uses_channel_folder_only(repo: MediaRoots) -> None:
    result = resolve_selection([], MediaKind.AUDIO, repo)
    assert result.container_paths == [
        "/media/channel/audio/01 - has spaces.m4a",
        "/media/channel/audio/deep/nested.flac",
        "/media/channel/audio/track2.mp3",
        "/media/channel/audio/track10.mp3",
    ]
    assert all("/media/common" not in p for p in result.container_paths)


def test_folder_mode_skips_dotfiles_and_unknown_extensions(repo: MediaRoots) -> None:
    result = resolve_selection([], MediaKind.AUDIO, repo)
    assert not any("hidden" in p or "README" in p for p in result.container_paths)


def test_natural_sort_orders_track2_before_track10() -> None:
    assert sorted(["track10", "track2"], key=natural_key) == ["track2", "track10"]


def test_explicit_paths_preserve_order(repo: MediaRoots) -> None:
    entries = [
        "channels/lofi/audio/track10.mp3",
        "common/audio/intro.mp3",
        "channels/lofi/audio/track2.mp3",
    ]
    result = resolve_selection(entries, MediaKind.AUDIO, repo)
    assert result.container_paths == [
        "/media/channel/audio/track10.mp3",
        "/media/common/audio/intro.mp3",
        "/media/channel/audio/track2.mp3",
    ]
    assert result.watched_dirs == []


def test_glob_expands_in_place_and_dedupes(repo: MediaRoots) -> None:
    entries = [
        "common/audio/intro.mp3",
        "channels/lofi/audio/**",
        "common/audio/intro.mp3",
    ]
    result = resolve_selection(entries, MediaKind.AUDIO, repo)
    assert result.container_paths[0] == "/media/common/audio/intro.mp3"
    assert result.container_paths.count("/media/common/audio/intro.mp3") == 1
    assert "/media/channel/audio/deep/nested.flac" in result.container_paths


def test_single_star_is_one_level_only(repo: MediaRoots) -> None:
    result = resolve_selection(["channels/lofi/audio/*"], MediaKind.AUDIO, repo)
    assert "/media/channel/audio/deep/nested.flac" not in result.container_paths
    assert "/media/channel/audio/track2.mp3" in result.container_paths


def test_globs_are_watched_explicit_paths_are_not(repo: MediaRoots) -> None:
    glob_result = resolve_selection(["channels/lofi/images/*"], MediaKind.IMAGE, repo)
    assert glob_result.watched_dirs == [repo.channel_dir / "images"]
    folder_result = resolve_selection([], MediaKind.IMAGE, repo)
    assert folder_result.watched_dirs == [repo.channel_dir / "images"]


def test_empty_glob_is_a_warning_not_an_error(repo: MediaRoots) -> None:
    result = resolve_selection(["common/audio/nothing-here/*"], MediaKind.AUDIO, repo)
    assert result.files == []
    assert any("matched no" in w for w in result.warnings)


@pytest.mark.parametrize(
    "entry",
    [
        "../outside/secret.mp3",
        "common/../../outside/secret.mp3",
        "channels/lofi/../other/audio/notmine.mp3",
        "/etc/passwd",
        "~/secret.mp3",
        "common/audio/../../outside/secret.mp3",
    ],
)
def test_traversal_is_rejected(repo: MediaRoots, entry: str) -> None:
    with pytest.raises(MediaError):
        resolve_selection([entry], MediaKind.AUDIO, repo)


def test_symlink_escape_is_rejected_after_resolution(repo: MediaRoots) -> None:
    link = repo.channel_dir / "audio" / "escape.mp3"
    link.symlink_to(repo.repo_root / "outside" / "secret.mp3")
    with pytest.raises(MediaError):
        resolve_selection(["channels/lofi/audio/escape.mp3"], MediaKind.AUDIO, repo)


def test_glob_matching_a_symlinked_directory_discards_the_escapes(repo: MediaRoots) -> None:
    (repo.channel_dir / "audio" / "linked").symlink_to(repo.repo_root / "outside")
    result = resolve_selection(["channels/lofi/audio/**"], MediaKind.AUDIO, repo)
    assert all("secret" not in p for p in result.container_paths)
    assert any("discarded" in w for w in result.warnings)


def test_folder_mode_does_not_follow_symlinked_directories(repo: MediaRoots) -> None:
    (repo.channel_dir / "audio" / "linked").symlink_to(repo.repo_root / "outside")
    result = resolve_selection([], MediaKind.AUDIO, repo)
    assert all("secret" not in p for p in result.container_paths)


def test_newline_in_entry_is_rejected(repo: MediaRoots) -> None:
    with pytest.raises(MediaError):
        resolve_selection(["common/audio/intro.mp3\n/etc/passwd"], MediaKind.AUDIO, repo)


def test_missing_explicit_path_is_fatal(repo: MediaRoots) -> None:
    with pytest.raises(MediaError):
        resolve_selection(["common/audio/nope.mp3"], MediaKind.AUDIO, repo)


def test_explicit_non_media_file_is_rejected(repo: MediaRoots) -> None:
    with pytest.raises(MediaError):
        resolve_selection(["channels/lofi/audio/README"], MediaKind.AUDIO, repo)


def test_image_kind_filters_extensions(repo: MediaRoots) -> None:
    result = resolve_selection(["channels/lofi/**"], MediaKind.IMAGE, repo)
    assert result.container_paths == ["/media/channel/images/city night.jpeg"]


def test_lists_are_written_atomically(tmp_path: Path, repo: MediaRoots) -> None:
    target = tmp_path / "channels" / "lofi" / "playlist.m3u"
    result = resolve_selection([], MediaKind.AUDIO, repo)
    write_playlist(target, result)
    assert target.read_text().splitlines() == result.container_paths
    # No temp file survives, and the replace happened in the target's directory.
    assert [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_atomic_write_leaves_previous_content_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "playlist.m3u"
    atomic_write_lines(target, ["/media/channel/audio/a.mp3"])

    def exploding():
        yield "/media/channel/audio/b.mp3"
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError):
        atomic_write_lines(target, exploding())
    assert target.read_text() == "/media/channel/audio/a.mp3\n"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_images_list_carries_paths_and_nothing_else(tmp_path: Path, repo: MediaRoots) -> None:
    target = tmp_path / "images.list"
    result = resolve_selection([], MediaKind.IMAGE, repo)
    write_images_list(target, result)
    lines = target.read_text().splitlines()
    assert lines == ["/media/channel/images/city night.jpeg"]
    assert " " in lines[0]  # a filename with spaces must survive intact


def test_shuffle_is_applied_at_write_time(tmp_path: Path, repo: MediaRoots) -> None:
    from random import Random

    target = tmp_path / "playlist.m3u"
    result = resolve_selection([], MediaKind.AUDIO, repo)
    written = write_playlist(target, result, shuffle=True, rng=Random(1))
    assert sorted(written) == sorted(result.container_paths)
    assert target.read_text().splitlines() == written


def test_file_mode_is_readable(tmp_path: Path) -> None:
    target = tmp_path / "playlist.m3u"
    atomic_write_lines(target, ["/media/channel/audio/a.mp3"])
    assert oct(os.stat(target).st_mode & 0o777) == "0o644"
