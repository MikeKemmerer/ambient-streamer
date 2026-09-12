"""Release archives safely update copied, non-Git installations."""

from __future__ import annotations

import importlib.util
import io
import json
import stat
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "upgrade-release.py"
SPEC = importlib.util.spec_from_file_location("upgrade_release", SCRIPT)
assert SPEC and SPEC.loader
upgrade_release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade_release)

COMMIT = "a" * 40
BACKEND = "ghcr.io/mikekemmerer/ambient-streamer-backend@sha256:" + "b" * 64
LIQUIDSOAP = "ghcr.io/mikekemmerer/ambient-streamer-liquidsoap@sha256:" + "c" * 64
COMPOSER = "ghcr.io/mikekemmerer/ambient-streamer-composer@sha256:" + "d" * 64


def add_file(archive: tarfile.TarFile, name: str, content: bytes, mode: int = 0o644) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(content)
    member.mode = mode
    archive.addfile(member, io.BytesIO(content))


def release_files(tmp_path: Path) -> tuple[Path, Path]:
    release_env = tmp_path / "release.env"
    release_env.write_text(
        "AMBIENT_RELEASE_TAG=v1.2.3\n"
        f"AMBIENT_RELEASE_COMMIT={COMMIT}\n"
        "AMBIENT_RELEASE_REPOSITORY=MikeKemmerer/ambient-streamer\n"
        f"AMBIENT_BACKEND_IMAGE={BACKEND}\n"
        f"AMBIENT_LIQUIDSOAP_IMAGE={LIQUIDSOAP}\n"
        f"AMBIENT_COMPOSER_IMAGE={COMPOSER}\n",
        encoding="utf-8",
    )
    manifest = json.dumps(
        {
            "tag": "v1.2.3",
            "commit": COMMIT,
            "repository": "MikeKemmerer/ambient-streamer",
            "images": {"backend": BACKEND, "liquidsoap": LIQUIDSOAP, "composer": COMPOSER},
        }
    ).encode()
    archive_path = tmp_path / "ambient-streamer-v1.2.3.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        root = tarfile.TarInfo("ambient-streamer-v1.2.3")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        add_file(archive, "ambient-streamer-v1.2.3/README.md", b"new source\n")
        add_file(
            archive,
            "ambient-streamer-v1.2.3/scripts/deploy-release.sh",
            b"#!/bin/sh\n",
            0o755,
        )
        add_file(archive, "ambient-streamer-v1.2.3/RELEASE.json", manifest)
    return archive_path, release_env


def existing_install(tmp_path: Path) -> Path:
    target = tmp_path / "install"
    (target / "channels" / "live" / "audio").mkdir(parents=True)
    (target / ".env").write_text(
        f"AMBIENT_REPO_ROOT={target.resolve()}\nAMBIENT_API_TOKEN=secret\n",
        encoding="utf-8",
    )
    (target / "ambient.yaml").write_text("channels: {}\n", encoding="utf-8")
    (target / "README.md").write_text("old source\n", encoding="utf-8")
    (target / "channels" / "live" / "audio" / "track.mp3").write_bytes(b"media")
    return target


def test_upgrade_overlays_source_and_preserves_runtime_state(tmp_path: Path) -> None:
    archive, release_env = release_files(tmp_path)
    target = existing_install(tmp_path)

    backup = upgrade_release.install(archive, release_env, target)

    assert (target / "README.md").read_text(encoding="utf-8") == "new source\n"
    assert (target / ".env").read_text(encoding="utf-8").endswith("AMBIENT_API_TOKEN=secret\n")
    assert (target / "ambient.yaml").read_text(encoding="utf-8") == "channels: {}\n"
    assert (target / "channels" / "live" / "audio" / "track.mp3").read_bytes() == b"media"
    assert stat.S_IMODE((target / "scripts" / "deploy-release.sh").stat().st_mode) == 0o755
    assert json.loads((target / "RELEASE.json").read_text(encoding="utf-8"))["commit"] == COMMIT
    assert (target / "release.env").read_text(encoding="utf-8") == release_env.read_text(
        encoding="utf-8"
    )
    assert stat.S_IMODE((target / "release.env").stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    with tarfile.open(backup, "r:gz") as saved:
        assert saved.extractfile("README.md").read() == b"old source\n"


def test_upgrade_rejects_manifest_mismatch_before_writing(tmp_path: Path) -> None:
    archive, release_env = release_files(tmp_path)
    target = existing_install(tmp_path)
    release_env.write_text(
        release_env.read_text(encoding="utf-8").replace(COMMIT, "d" * 40),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="RELEASE.json commit does not match"):
        upgrade_release.install(archive, release_env, target)

    assert (target / "README.md").read_text(encoding="utf-8") == "old source\n"
    assert not (target / ".release-backups").exists()


def test_upgrade_rejects_archive_path_traversal(tmp_path: Path) -> None:
    archive, release_env = release_files(tmp_path)
    target = existing_install(tmp_path)
    with tarfile.open(archive, "w:gz") as output:
        add_file(output, "ambient-streamer-v1.2.3/../../escaped", b"bad")

    with pytest.raises(SystemExit, match="unsafe archive path"):
        upgrade_release.install(archive, release_env, target)

    assert not (tmp_path / "escaped").exists()


def test_upgrade_rejects_symlinked_backup_directory(tmp_path: Path) -> None:
    archive, release_env = release_files(tmp_path)
    target = existing_install(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (target / ".release-backups").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SystemExit, match="backup directory must not be a symlink"):
        upgrade_release.install(archive, release_env, target)

    assert not list(outside.iterdir())
    assert (target / "README.md").read_text(encoding="utf-8") == "old source\n"


def test_upgrade_does_not_follow_a_preplanted_backup_file_symlink(tmp_path: Path) -> None:
    archive, release_env = release_files(tmp_path)
    target = existing_install(tmp_path)
    backups = target / ".release-backups"
    backups.mkdir()
    outside = tmp_path / "outside.tar.gz"
    outside.write_bytes(b"do not overwrite")
    predictable = backups / "source-before-v1.2.3-20200101T000000Z.tar.gz"
    predictable.symlink_to(outside)

    backup = upgrade_release.install(archive, release_env, target)

    assert outside.read_bytes() == b"do not overwrite"
    assert backup != predictable
    assert backup.parent == backups
    assert backup.is_file()