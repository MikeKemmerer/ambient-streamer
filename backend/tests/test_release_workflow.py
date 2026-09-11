"""The release workflow publishes immutable, verifiable runtime images."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def test_release_builds_only_backend_and_liquidsoap_for_both_platforms() -> None:
    assert WORKFLOW.count("platforms: linux/amd64,linux/arm64") == 2
    assert "file: docker/Dockerfile.backend" in WORKFLOW
    assert "file: docker/Dockerfile.liquidsoap" in WORKFLOW
    assert "file: docker/Dockerfile.composer" not in WORKFLOW
    assert "file: docker/Dockerfile.mediamtx" not in WORKFLOW


def test_release_pins_actions_and_attests_both_images() -> None:
    for action in (
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "docker/setup-qemu-action@1f40c72289eff860ee54a304f1438e3cff362e0a",
        "docker/setup-buildx-action@37fe631027851001ddb9b187196cc803df7f5f0e",
        "docker/login-action@dbcb813823bdd20940b903addbd779551569679f",
        "docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a",
        "actions/attest-build-provenance@8beda2b7ed98355c0e97c0a63bec38ae472e66c4",
        "softprops/action-gh-release@5113cdc90fd4d541c801c55356214017bf5ae34b",
    ):
        assert action in WORKFLOW
    assert WORKFLOW.count("push-to-registry: true") == 2
    assert "packages: write" in WORKFLOW
    assert "id-token: write" in WORKFLOW
    assert "attestations: write" in WORKFLOW


def test_release_never_publishes_a_floating_latest_tag() -> None:
    assert "ambient-streamer-backend }}:latest" not in WORKFLOW
    assert "ambient-streamer-liquidsoap }}:latest" not in WORKFLOW
    assert "AMBIENT_BACKEND_IMAGE=${BACKEND_IMAGE}@${BACKEND_DIGEST}" in WORKFLOW
    assert "AMBIENT_LIQUIDSOAP_IMAGE=${LIQUIDSOAP_IMAGE}@${LIQUIDSOAP_DIGEST}" in WORKFLOW
    assert WORKFLOW.count(":run-${{ github.run_id }}-${{ github.run_attempt }}") == 2
    assert ":${{ steps.release.outputs.tag }}" not in WORKFLOW


def test_release_identity_guards_are_present() -> None:
    assert 'manual releases must run from $DEFAULT_BRANCH' in WORKFLOW
    assert 'tag $tag points to $tagged_commit' in WORKFLOW
    assert 'release $tag already exists; refusing to republish' in WORKFLOW
    assert "AMBIENT_RELEASE_REPOSITORY=$GITHUB_REPOSITORY" in WORKFLOW
    assert "group: release-${{ inputs.version || github.ref_name }}" in WORKFLOW


def test_secret_guard_checks_files_without_matching_source_literals() -> None:
    assert "git ls-files | grep -E" in WORKFLOW
    assert "YOUTUBE_STREAM_KEY=[^[:space:]]" not in WORKFLOW


def test_published_image_bases_are_manifest_pinned() -> None:
    backend = (ROOT / "docker" / "Dockerfile.backend").read_text(encoding="utf-8")
    liquidsoap = (ROOT / "docker" / "Dockerfile.liquidsoap").read_text(encoding="utf-8")
    assert re.search(r"ARG BASE=python:[^\s]+@sha256:[0-9a-f]{64}", backend)
    assert re.search(r"ARG DOCKER_CLI_IMAGE=docker:[^\s]+@sha256:[0-9a-f]{64}", backend)
    assert re.search(r"FROM savonet/liquidsoap:[^\s]+@sha256:[0-9a-f]{64}", liquidsoap)