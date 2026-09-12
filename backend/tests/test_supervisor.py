"""Compose rendering: docker/compose.channel.yml.j2 -> channels/<name>/docker-compose.yml."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from ambient.config import ResolvedChannel, Workspace, load_channel, load_workspace
from ambient.presets import color_targets
from ambient.supervisor import ComposeError, compose_context, render_compose, write_compose
from tests.test_config import CHANNEL_ENV, CHANNEL_YAML, make_repo

MANUAL_COLOR = """\
color:
  mode: manual
  manual:
    accent: "#4FC3F7"
    tint: "#0B2A3A"
  transition_seconds: 4.0
"""


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
    assert set(document["services"]) == {
        "lofi-liquidsoap",
        "lofi-visualizer",
        "lofi-composer",
    }
    assert document["networks"]["ambient"]["external"] is True


def test_image_tag_falls_back_to_the_template_default(tmp_path: Path) -> None:
    workspace, channel = resolve(make_repo(tmp_path))
    document = yaml.safe_load(render_compose(workspace, channel))
    composer_image = "${AMBIENT_COMPOSER_IMAGE:-ambient-composer:dev}"
    assert document["services"]["lofi-composer"]["image"] == composer_image
    assert document["services"]["lofi-visualizer"]["image"] == composer_image
    assert document["services"]["lofi-liquidsoap"]["image"] == (
        "${AMBIENT_LIQUIDSOAP_IMAGE:-ambient-liquidsoap:dev}"
    )


def test_visualization_opacity_reaches_the_composer(tmp_path: Path) -> None:
    workspace, channel = resolve(make_repo(tmp_path))
    document = yaml.safe_load(render_compose(workspace, channel))
    assert document["services"]["lofi-composer"]["environment"]["VIZ_OPACITY"] == "0.65"


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


def test_manual_color_reaches_the_composer_with_its_initial_values(tmp_path: Path) -> None:
    """Without these the producer overwrites a manual color at the next slide."""
    config = CHANNEL_YAML.replace("color:\n  mode: automatic\n", MANUAL_COLOR)
    workspace, channel = resolve(make_repo(tmp_path, config=config))
    context = compose_context(workspace, channel)
    targets = color_targets("#4FC3F7", "#0B2A3A")

    assert context["color_mode"] == "manual"
    # The accent reaches the graph as ${ACCENT} inside the plugin fragments.
    # There is no COLOR_ACCENT/COLOR_TINT env any more: nothing in ffmpeg/ or
    # liquidsoap/ ever read them, and the producer takes its accent from the
    # image profile rather than the environment.
    assert context["color_accent"] == "#4FC3F7"
    assert "color_tint" not in context
    assert context["color_transition_seconds"] == "4"
    # The composite is never pre-rotated by the accent: the accent is the
    # visualization's, and rotating the finished frame by it too would take the
    # visualization straight back off the requested color.
    assert float(context["color_init_hue"]) == 0.0
    assert float(context["color_init_saturation"]) == targets.saturation
    assert float(context["color_init_brightness"]) == targets.brightness


def test_visualizer_recompile_can_preserve_the_running_base_accent(
    tmp_path: Path
) -> None:
    from ambient.api.deps import recompile
    from ambient.main import build_state

    config = CHANNEL_YAML.replace("color:\n  mode: automatic\n", MANUAL_COLOR).replace(
        "#4FC3F7", "#FF8800"
    )
    root = make_repo(tmp_path, config=config)
    env = root / ".env"
    env.write_text(
        env.read_text(encoding="utf-8") + f"AMBIENT_RUN_DIR={root / 'run'}\n",
        encoding="utf-8",
    )
    run_dir = load_workspace(root).run_dir / "lofi"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "viz-accent").write_text("#4FC3F7\n", encoding="utf-8")

    state = build_state(root)
    recompile(state, "lofi", preserve_visualizer_accent=True)
    document = yaml.safe_load(
        (root / "channels" / "lofi" / "docker-compose.yml").read_text(encoding="utf-8")
    )

    visualizer = document["services"]["lofi-visualizer"]
    assert visualizer["environment"]["ACCENT"] == "#4FC3F7"
    assert state.channel("lofi", resolve_media=False).config.color.manual.accent == "#FF8800"


def test_automatic_color_starts_the_graph_neutral(tmp_path: Path) -> None:
    workspace, channel = resolve(make_repo(tmp_path))
    context = compose_context(workspace, channel)
    assert context["color_mode"] == "auto"
    assert context["color_init_hue"] == "0"
    assert context["color_init_saturation"] == "1"
    assert context["color_init_brightness"] == "0"


def test_visualizer_context_is_isolated_and_capped(tmp_path: Path) -> None:
    env = CHANNEL_ENV.replace("CHANNEL_RESOLUTION=720p", "CHANNEL_RESOLUTION=1080p").replace(
        "CHANNEL_FPS=30", "CHANNEL_FPS=60"
    )
    workspace, channel = resolve(make_repo(tmp_path, env=env))
    context = compose_context(workspace, channel)

    assert context["visualizer_enabled"] == "on"
    assert context["active_plugin"] == "showfreqs-bars"
    assert context["visualizer_width"] == "1280"
    assert context["visualizer_height"] == "720"
    assert context["visualizer_fps"] == "30"
    assert json.loads(context["plugin_parameters"]) == {}


def test_visualizer_service_uses_shared_framekeeper_state(tmp_path: Path) -> None:
    workspace, channel = resolve(make_repo(tmp_path))
    document = yaml.safe_load(render_compose(workspace, channel))
    service = document["services"]["lofi-visualizer"]

    assert service["container_name"] == "lofi-visualizer"
    assert service["command"] == ["/opt/ambient/visualizer-entrypoint.sh"]
    assert service["environment"]["PLUGIN_NAME"] == "showfreqs-bars"
    assert service["environment"]["WIDTH"] == "1280"
    assert service["environment"]["HEIGHT"] == "720"
    assert service["environment"]["FPS"] == "30"
    assert f"{workspace.run_dir}:/run/ambient" in service["volumes"]
    assert f"{workspace.root}/plugins:/plugins:ro" in service["volumes"]
    assert "profiles" not in service


def test_disabled_visualizer_service_is_profile_gated(tmp_path: Path) -> None:
    config = CHANNEL_YAML.replace(
        "visualization:\n  active:", "visualization:\n  enabled: false\n  active:"
    )
    workspace, channel = resolve(make_repo(tmp_path, config=config))
    document = yaml.safe_load(render_compose(workspace, channel))

    assert document["services"]["lofi-visualizer"]["profiles"] == [
        "visualization-disabled"
    ]


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
