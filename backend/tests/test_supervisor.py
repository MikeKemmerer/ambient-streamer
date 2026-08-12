"""Compose rendering: docker/compose.channel.yml.j2 -> channels/<name>/docker-compose.yml."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from ambient.config import ResolvedChannel, Workspace, load_channel, load_workspace
from ambient.supervisor import ComposeError, render_compose, write_compose
from tests.test_config import CHANNEL_ENV, make_repo


def resolve(root: Path) -> tuple[Workspace, ResolvedChannel]:
    workspace = load_workspace(root)
    return workspace, load_channel(workspace, "lofi")


def test_every_placeholder_is_substituted(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    workspace, channel = resolve(root)
    rendered = render_compose(workspace, channel)
    assert "{{" not in rendered
    assert "{%" not in rendered
    assert "{#" not in rendered
    assert f"{workspace.common_dir}:/media/common:ro" in rendered
    assert f"{channel.directory}:/media/channel:ro" in rendered
    assert f"{workspace.root}/ffmpeg:/opt/ambient:ro" in rendered
    assert f"{workspace.log_dir / 'lofi'}:/var/log/ambient" in rendered


def test_rendered_yaml_parses(tmp_path: Path) -> None:
    workspace, channel = resolve(make_repo(tmp_path))
    document = yaml.safe_load(render_compose(workspace, channel))
    assert document["name"] == "ambient-lofi"
    assert set(document["services"]) == {"lofi-liquidsoap", "lofi-composer"}
    assert document["networks"]["ambient"]["external"] is True


def test_image_tag_falls_back_to_the_template_default(tmp_path: Path) -> None:
    workspace, channel = resolve(make_repo(tmp_path))
    document = yaml.safe_load(render_compose(workspace, channel))
    assert document["services"]["lofi-composer"]["image"] == "ambient-composer:dev"


def test_encoder_default_applies_when_the_channel_env_omits_it(tmp_path: Path) -> None:
    env = CHANNEL_ENV.replace("CHANNEL_ENCODER=\n", "")
    workspace, channel = resolve(make_repo(tmp_path, env=env))
    document = yaml.safe_load(render_compose(workspace, channel))
    composer = document["services"]["lofi-composer"]
    assert composer["environment"]["ENCODER"] == "${ENCODER:-libx264}"
    assert "devices" not in composer
    assert "reservations" not in composer["deploy"]["resources"]


def test_nvenc_encoder_renders_the_gpu_reservation(tmp_path: Path) -> None:
    env = CHANNEL_ENV.replace("CHANNEL_ENCODER=", "CHANNEL_ENCODER=h264_nvenc")
    workspace, channel = resolve(make_repo(tmp_path, env=env))
    document = yaml.safe_load(render_compose(workspace, channel))
    composer = document["services"]["lofi-composer"]
    assert composer["environment"]["ENCODER"] == "${ENCODER:-h264_nvenc}"
    device = composer["deploy"]["resources"]["reservations"]["devices"][0]
    assert device["driver"] == "nvidia"


def test_qsv_encoder_renders_the_render_node(tmp_path: Path) -> None:
    env = CHANNEL_ENV.replace("CHANNEL_ENCODER=", "CHANNEL_ENCODER=h264_qsv")
    workspace, channel = resolve(make_repo(tmp_path, env=env))
    document = yaml.safe_load(render_compose(workspace, channel))
    assert document["services"]["lofi-composer"]["devices"] == ["/dev/dri:/dev/dri"]


def test_stream_key_is_never_baked_into_the_file(tmp_path: Path) -> None:
    workspace, channel = resolve(make_repo(tmp_path))
    assert "aaaa-bbbb-cccc-dddd-eeee" not in render_compose(workspace, channel)


def test_missing_template_is_a_loud_error(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "docker" / "compose.channel.yml.j2").unlink()
    workspace, channel = resolve(root)
    with pytest.raises(ComposeError, match="missing"):
        render_compose(workspace, channel)


def test_template_that_renders_invalid_yaml_is_rejected(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "docker" / "compose.channel.yml.j2").write_text(
        "services:\n  {{ channel }}-composer: [unclosed\n", encoding="utf-8"
    )
    workspace, channel = resolve(root)
    with pytest.raises(ComposeError, match="invalid YAML"):
        render_compose(workspace, channel)
    assert not channel.compose_path.exists()


def test_template_with_no_services_is_rejected(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "docker" / "compose.channel.yml.j2").write_text(
        "name: ambient-{{ channel }}\n", encoding="utf-8"
    )
    workspace, channel = resolve(root)
    with pytest.raises(ComposeError, match="no services"):
        render_compose(workspace, channel)


def test_an_undeclared_variable_fails_rather_than_rendering_empty(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "docker" / "compose.channel.yml.j2").write_text(
        "services:\n  {{ channel }}-composer:\n    image: {{ nonesuch }}\n", encoding="utf-8"
    )
    workspace, channel = resolve(root)
    with pytest.raises(ComposeError, match="nonesuch"):
        render_compose(workspace, channel)


def test_write_goes_through_a_temp_file_in_the_target_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, channel = resolve(make_repo(tmp_path))
    renames: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def spy(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        renames.append((Path(src), Path(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    written = write_compose(workspace, channel)

    source, destination = renames[-1]
    # Same directory, so the rename is atomic; /tmp would be a cross-device copy.
    assert source.parent == destination.parent == channel.directory
    assert written == channel.compose_path
    assert yaml.safe_load(written.read_text(encoding="utf-8"))["name"] == "ambient-lofi"
    assert list(channel.directory.glob(".docker-compose.yml.*")) == []


def test_a_failed_rename_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, channel = resolve(make_repo(tmp_path))

    def boom(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_compose(workspace, channel)

    assert not channel.compose_path.exists()
    assert list(channel.directory.glob(".docker-compose.yml.*")) == []


def test_rewrite_replaces_the_previous_file_whole(tmp_path: Path) -> None:
    workspace, channel = resolve(make_repo(tmp_path))
    channel.compose_path.write_text("stale: true\n", encoding="utf-8")
    write_compose(workspace, channel)
    assert "stale" not in channel.compose_path.read_text(encoding="utf-8")
