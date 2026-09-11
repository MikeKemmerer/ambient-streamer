"""Digest-pinned release deployment never reaches a composer service."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

BACKEND = "ghcr.io/mikekemmerer/ambient-streamer-backend@sha256:" + "a" * 64
LIQUIDSOAP = "ghcr.io/mikekemmerer/ambient-streamer-liquidsoap@sha256:" + "b" * 64


def executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def deploy_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "repo"
    scripts = root / "scripts"
    fake_bin = root / "fake-bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    source = Path(__file__).resolve().parents[2] / "scripts" / "deploy-release.sh"
    shutil.copy2(source, scripts / "deploy-release.sh")
    (root / ".env").write_text(
        "AMBIENT_API_TOKEN=secret\nAMBIENT_BACKEND_IMAGE=ambient-backend:dev\n",
        encoding="utf-8",
        newline="\n",
    )
    (root / ".env").chmod(0o600)

    docker_log = root / "docker.log"
    channel_log = root / "channel.log"
    gh_log = root / "gh.log"
    executable(
        fake_bin / "docker",
        """#!/bin/sh
printf '%s\n' "$*" >> "$DOCKER_LOG"
case "$*" in
  *"compose"*"config --images"*) printf '%s\n' "$AMBIENT_BACKEND_IMAGE" ;;
    *".State.Running"*"-liquidsoap"*) printf '%s\n' "${FAKE_LIQ_RUNNING:-true}" ;;
    *".State.Running"*"-composer"*) printf '%s\n' "${FAKE_COMPOSER_RUNNING:-true}" ;;
    *".State.Running"*"ambient-backend"*) printf '%s\n' true ;;
  *".Id"*"-composer"*) printf '%s\n' composer-unchanged ;;
  *".Config.Image"*"ambient-backend"*) printf '%s\n' "$BACKEND_IMAGE" ;;
  *".Config.Image"*"-liquidsoap"*) printf '%s\n' "$LIQUIDSOAP_IMAGE" ;;
esac
exit 0
""",
    )
    executable(
        fake_bin / "gh",
        r"""#!/bin/sh
printf '%s\n' "$*" >> "$GH_LOG"
case "$*" in
    "attestation verify --help") exit 0 ;;
    attestation\ verify\ oci://ghcr.io/*\ --repo\ *\ --source-digest\ *)
        [ "${FAKE_ATTEST_FAIL:-false}" = true ] && exit 1
        exit 0
        ;;
  *) exit 1 ;;
esac
""",
    )
    executable(
        scripts / "channel.sh",
        """#!/bin/sh
printf '%s\n' "$*" >> "$CHANNEL_LOG"
case "$*" in
  "config "*" --images") printf '%s\n' "$AMBIENT_LIQUIDSOAP_IMAGE" ;;
esac
""",
    )

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    (root / "release.env").write_text(
        f"AMBIENT_RELEASE_TAG=v1.2.3\n"
        f"AMBIENT_RELEASE_COMMIT={commit}\n"
        f"AMBIENT_RELEASE_REPOSITORY=MikeKemmerer/ambient-streamer\n"
        f"AMBIENT_BACKEND_IMAGE={BACKEND}\n"
        f"AMBIENT_LIQUIDSOAP_IMAGE={LIQUIDSOAP}\n",
        encoding="utf-8",
        newline="\n",
    )
    return root, docker_log, channel_log


def run_deploy(
    fixture: tuple[Path, Path, Path],
    *args: str,
    running: bool = True,
    composer_running: bool = True,
    attest: bool = True,
) -> subprocess.CompletedProcess[str]:
    root, docker_log, channel_log = fixture
    env = {
        **os.environ,
        "PATH": f"{root / 'fake-bin'}:{os.environ['PATH']}",
        "DOCKER_LOG": str(docker_log),
        "CHANNEL_LOG": str(channel_log),
        "GH_LOG": str(root / "gh.log"),
        "BACKEND_IMAGE": BACKEND,
        "LIQUIDSOAP_IMAGE": LIQUIDSOAP,
        "AMBIENT_BACKEND_IMAGE": "ghcr.io/hostile/wrong-backend@sha256:" + "c" * 64,
        "AMBIENT_LIQUIDSOAP_IMAGE": "ghcr.io/hostile/wrong-liquidsoap@sha256:" + "d" * 64,
        "FAKE_LIQ_RUNNING": "true" if running else "false",
        "FAKE_COMPOSER_RUNNING": "true" if composer_running else "false",
        "FAKE_ATTEST_FAIL": "false" if attest else "true",
    }
    return subprocess.run(
        [str(root / "scripts" / "deploy-release.sh"), "release.env", *args],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_prepare_pulls_and_records_digests_without_recreating(deploy_repo) -> None:
    root, docker_log, channel_log = deploy_repo
    result = run_deploy(deploy_repo)

    assert result.returncode == 0, result.stderr
    env = (root / ".env").read_text(encoding="utf-8")
    assert f"AMBIENT_BACKEND_IMAGE={BACKEND}" in env
    assert f"AMBIENT_LIQUIDSOAP_IMAGE={LIQUIDSOAP}" in env
    assert stat.S_IMODE((root / ".env").stat().st_mode) == 0o600
    log = docker_log.read_text(encoding="utf-8")
    assert f"pull {BACKEND}" in log and f"pull {LIQUIDSOAP}" in log
    assert "compose" not in log
    assert not channel_log.exists()
    attestations = (root / "gh.log").read_text(encoding="utf-8")
    assert f"attestation verify oci://{BACKEND}" in attestations
    assert f"attestation verify oci://{LIQUIDSOAP}" in attestations
    assert "--signer-workflow MikeKemmerer/ambient-streamer/.github/workflows/release.yml" in attestations
    assert attestations.count("--source-digest ") == 2


def test_selected_rollout_never_recreates_the_composer(deploy_repo) -> None:
    _root, docker_log, channel_log = deploy_repo
    result = run_deploy(deploy_repo, "--backend", "--liquidsoap", "lofi")

    assert result.returncode == 0, result.stderr
    log = docker_log.read_text(encoding="utf-8")
    assert "up -d --no-deps --force-recreate --wait --wait-timeout 60 backend" in log
    assert "exec ambient-backend python -m ambient.compile lofi" in log
    assert "force-recreate lofi-composer" not in log
    channel_calls = channel_log.read_text(encoding="utf-8").splitlines()
    assert channel_calls == [
        "config lofi --images",
        "start lofi --no-deps --force-recreate lofi-liquidsoap",
    ]


def test_stopped_liquidsoap_is_refused(deploy_repo) -> None:
    root, docker_log, channel_log = deploy_repo
    result = run_deploy(deploy_repo, "--liquidsoap", "lofi", running=False)

    assert result.returncode != 0
    assert "refusing to start a stopped channel" in result.stderr
    assert "pull " not in docker_log.read_text(encoding="utf-8")
    assert "AMBIENT_LIQUIDSOAP_IMAGE=" not in (root / ".env").read_text(encoding="utf-8")
    assert not channel_log.exists()


def test_all_channel_names_are_checked_before_pulls_or_changes(deploy_repo) -> None:
    root, docker_log, channel_log = deploy_repo
    original_env = (root / ".env").read_bytes()
    result = run_deploy(
        deploy_repo,
        "--liquidsoap",
        "lofi",
        "--liquidsoap",
        "INVALID",
    )

    assert result.returncode != 0
    assert "invalid channel name" in result.stderr
    assert "pull " not in docker_log.read_text(encoding="utf-8")
    assert (root / ".env").read_bytes() == original_env
    assert not channel_log.exists()


def test_stopped_composer_is_refused_before_pulls_or_changes(deploy_repo) -> None:
    root, docker_log, channel_log = deploy_repo
    original_env = (root / ".env").read_bytes()
    result = run_deploy(
        deploy_repo,
        "--liquidsoap",
        "lofi",
        composer_running=False,
    )

    assert result.returncode != 0
    assert "composer is not running" in result.stderr
    assert "pull " not in docker_log.read_text(encoding="utf-8")
    assert (root / ".env").read_bytes() == original_env
    assert not channel_log.exists()


def test_failed_attestation_stops_before_env_or_container_changes(deploy_repo) -> None:
    root, docker_log, channel_log = deploy_repo
    original_env = (root / ".env").read_bytes()
    result = run_deploy(deploy_repo, "--backend", attest=False)

    assert result.returncode != 0
    assert (root / ".env").read_bytes() == original_env
    assert "compose" not in docker_log.read_text(encoding="utf-8")
    assert not channel_log.exists()


def test_wrong_ghcr_package_owner_is_rejected_before_docker(deploy_repo) -> None:
    root, docker_log, _channel_log = deploy_repo
    manifest = root / "release.env"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "ghcr.io/mikekemmerer/ambient-streamer-backend",
            "ghcr.io/attacker/ambient-streamer-backend",
        ),
        encoding="utf-8",
        newline="\n",
    )
    result = run_deploy(deploy_repo)

    assert result.returncode != 0
    assert "invalid backend image digest reference" in result.stderr
    assert not docker_log.exists()