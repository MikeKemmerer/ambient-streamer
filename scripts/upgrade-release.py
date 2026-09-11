#!/usr/bin/env python3
"""Safely overlay a release source archive onto an existing non-Git install."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

REQUIRED_ENV = {
    "AMBIENT_RELEASE_TAG",
    "AMBIENT_RELEASE_COMMIT",
    "AMBIENT_RELEASE_REPOSITORY",
    "AMBIENT_BACKEND_IMAGE",
    "AMBIENT_LIQUIDSOAP_IMAGE",
}


def fail(message: str) -> None:
    raise SystemExit(f"fail {message}")


def read_release_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or key not in REQUIRED_ENV:
            fail(f"unexpected line in {path}: {line}")
        if key in values or not value:
            fail(f"invalid {key} in {path}")
        values[key] = value
    missing = REQUIRED_ENV - values.keys()
    if missing:
        fail(f"{path} is missing {', '.join(sorted(missing))}")
    if not re.fullmatch(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", values["AMBIENT_RELEASE_TAG"]):
        fail(f"invalid release tag: {values['AMBIENT_RELEASE_TAG']}")
    if not re.fullmatch(r"[0-9a-f]{40}", values["AMBIENT_RELEASE_COMMIT"]):
        fail(f"invalid release commit: {values['AMBIENT_RELEASE_COMMIT']}")
    return values


def validate_manifest(path: Path, release: dict[str, str]) -> None:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read release manifest: {exc}")
    expected = {
        "tag": release["AMBIENT_RELEASE_TAG"],
        "commit": release["AMBIENT_RELEASE_COMMIT"],
        "repository": release["AMBIENT_RELEASE_REPOSITORY"],
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            fail(f"RELEASE.json {key} does not match release.env")
    images = manifest.get("images")
    if not isinstance(images, dict):
        fail("RELEASE.json images are invalid")
    if images.get("backend") != release["AMBIENT_BACKEND_IMAGE"]:
        fail("RELEASE.json backend image does not match release.env")
    if images.get("liquidsoap") != release["AMBIENT_LIQUIDSOAP_IMAGE"]:
        fail("RELEASE.json Liquidsoap image does not match release.env")


def safe_members(archive: tarfile.TarFile, prefix: str) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not member.name.startswith(prefix):
            fail(f"unsafe archive path: {member.name}")
        if not (member.isdir() or member.isfile()):
            fail(f"unsupported archive entry: {member.name}")
    return members


def extract_staging(archive_path: Path, stage: Path, prefix: str) -> list[Path]:
    files: list[Path] = []
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in safe_members(archive, prefix):
            relative = PurePosixPath(member.name).relative_to(PurePosixPath(prefix))
            if not relative.parts:
                continue
            destination = stage.joinpath(*relative.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                fail(f"cannot extract {member.name}")
            with source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
            destination.chmod(member.mode & 0o777)
            files.append(destination)
    return files


def target_path(target: Path, relative: Path) -> Path:
    destination = target / relative
    target_root = target.resolve()
    if not destination.parent.resolve().is_relative_to(target_root):
        fail(f"target path escapes installation: {relative}")
    return destination


def atomic_copy(source: Path, destination: Path, mode: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".release-", dir=destination.parent)
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        shutil.copyfile(source, temporary_path)
        temporary_path.chmod(mode)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def install(archive_path: Path, release_path: Path, target: Path) -> Path:
    archive_path = archive_path.resolve()
    release_path = release_path.resolve()
    target = target.resolve()
    if not archive_path.is_file() or not release_path.is_file():
        fail("source archive and release.env must both exist")
    if not (target / ".env").is_file() or not (target / "ambient.yaml").is_file():
        fail(f"{target} is not an initialized ambient-streamer installation")

    configured_root = ""
    for line in (target / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("AMBIENT_REPO_ROOT="):
            configured_root = line.split("=", 1)[1]
    if not configured_root or Path(configured_root).resolve() != target:
        fail(f"AMBIENT_REPO_ROOT must resolve to {target}")

    release = read_release_env(release_path)
    prefix = f"ambient-streamer-{release['AMBIENT_RELEASE_TAG']}/"
    with tempfile.TemporaryDirectory(prefix=".ambient-release-", dir=target.parent) as temp:
        stage = Path(temp)
        files = extract_staging(archive_path, stage, prefix)
        validate_manifest(stage / "RELEASE.json", release)

        backups = target / ".release-backups"
        if backups.is_symlink():
            fail(f"backup directory must not be a symlink: {backups}")
        backups.mkdir(mode=0o700, exist_ok=True)
        if not backups.is_dir() or not backups.resolve().is_relative_to(target):
            fail(f"backup directory escapes installation: {backups}")
        backups.chmod(0o700)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        descriptor, backup_name = tempfile.mkstemp(
            prefix=f"source-before-{release['AMBIENT_RELEASE_TAG']}-{timestamp}-",
            suffix=".tar.gz",
            dir=backups,
        )
        backup = Path(backup_name)
        try:
            with os.fdopen(descriptor, "wb") as backup_file:
                with tarfile.open(fileobj=backup_file, mode="w:gz") as output:
                    for source in files:
                        relative = source.relative_to(stage)
                        existing = target_path(target, relative)
                        if existing.is_file():
                            output.add(existing, arcname=relative.as_posix(), recursive=False)
            backup.chmod(0o600)
        except BaseException:
            backup.unlink(missing_ok=True)
            raise

        for source in files:
            relative = source.relative_to(stage)
            destination = target_path(target, relative)
            atomic_copy(source, destination, source.stat().st_mode & 0o777)
        atomic_copy(release_path, target / "release.env", 0o600)
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="ambient-streamer-vX.Y.Z.tar.gz")
    parser.add_argument("release_env", type=Path, help="release.env from the same release")
    parser.add_argument("target", type=Path, help="existing installation directory")
    args = parser.parse_args()
    backup = install(args.archive, args.release_env, args.target)
    print(f"ok installed source; previous files saved in {backup}")


if __name__ == "__main__":
    main()