"""Config-layer tests: the validation table in docs/contracts/config.md."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ambient.config import (
    ConfigError,
    channel_directory,
    check_mount_uniqueness,
    discover_channels,
    ignored_channel_names,
    load_channel,
    load_workspace,
    parse_env_file,
)

CHANNEL_ENV = """\
YOUTUBE_STREAM_KEY=aaaa-bbbb-cccc-dddd-eeee
CHANNEL_RESOLUTION=720p
CHANNEL_FPS=30
CHANNEL_ENCODER=
CHANNEL_CPU_LIMIT=2.0
CHANNEL_MEMORY_LIMIT=2g
CHANNEL_MOUNT=/lofi
CHANNEL_FALLBACK_MOUNT=/lofi-fallback
"""

COMPOSE_TEMPLATE = Path(__file__).resolve().parents[2] / "docker" / "compose.channel.yml.j2"

CHANNEL_YAML = """\
version: 1
name: lofi
genre: "lo-fi"
audio:
  tracks: []
  shuffle: false
images:
  slides: []
  order: sequential
visualization:
  active: showfreqs-bars
  hot_set: [showfreqs-bars]
color:
  mode: automatic
preset: null
bumpers:
  enabled: false
schedule:
  timezone: America/Los_Angeles
  rules: []
"""


def make_repo(tmp_path: Path, *, name: str = "lofi", config: str = CHANNEL_YAML, env: str = CHANNEL_ENV) -> Path:
    (tmp_path / "common" / "audio").mkdir(parents=True)
    (tmp_path / "common" / "images").mkdir(parents=True)
    channel = tmp_path / "channels" / name
    (channel / "audio").mkdir(parents=True)
    (channel / "images").mkdir(parents=True)
    (channel / "audio" / "01 - a track.m4a").write_bytes(b"x")
    (channel / "images" / "slide.jpeg").write_bytes(b"x")
    (channel / "config.yaml").write_text(config, encoding="utf-8")
    (channel / ".env").write_text(env, encoding="utf-8")
    (tmp_path / ".env").write_text("AMBIENT_DEFAULT_ENCODER=libx264\n", encoding="utf-8")
    # The real infra template, so compose rendering is tested against the frozen file.
    docker = tmp_path / "docker"
    docker.mkdir(exist_ok=True)
    (docker / COMPOSE_TEMPLATE.name).write_text(
        COMPOSE_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    return tmp_path


def test_loads_a_valid_channel(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    workspace = load_workspace(root)
    channel = load_channel(workspace, "lofi")
    assert channel.resolution.value == "720p"
    assert channel.encoder.value == "libx264"
    assert channel.audio.container_paths == ["/media/channel/audio/01 - a track.m4a"]
    assert channel.images.container_paths == ["/media/channel/images/slide.jpeg"]


def test_missing_ambient_yaml_falls_back_to_defaults(tmp_path: Path) -> None:
    workspace = load_workspace(make_repo(tmp_path))
    assert workspace.ambient.defaults.resolution.value == "720p"
    assert any("ambient.yaml" in w for w in workspace.warnings)


def test_active_must_be_in_hot_set(tmp_path: Path) -> None:
    bad = CHANNEL_YAML.replace("active: showfreqs-bars", "active: showwaves-classic")
    workspace = load_workspace(make_repo(tmp_path, config=bad))
    with pytest.raises(ConfigError, match="hot_set"):
        load_channel(workspace, "lofi")


def test_empty_audio_result_is_fatal(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "channels" / "lofi" / "audio" / "01 - a track.m4a").unlink()
    workspace = load_workspace(root)
    with pytest.raises(ConfigError, match="no audio"):
        load_channel(workspace, "lofi")


def test_empty_images_result_is_a_warning(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "channels" / "lofi" / "images" / "slide.jpeg").unlink()
    workspace = load_workspace(root)
    channel = load_channel(workspace, "lofi")
    assert any("nothing to show" in w for w in channel.warnings)


def test_traversal_in_a_track_entry_is_fatal(tmp_path: Path) -> None:
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.mp3").write_bytes(b"x")
    bad = CHANNEL_YAML.replace("tracks: []", 'tracks: ["../outside/secret.mp3"]')
    workspace = load_workspace(make_repo(tmp_path, config=bad))
    with pytest.raises(ConfigError):
        load_channel(workspace, "lofi")


def test_config_name_must_match_directory(tmp_path: Path) -> None:
    bad = CHANNEL_YAML.replace("name: lofi", "name: other")
    workspace = load_workspace(make_repo(tmp_path, config=bad))
    with pytest.raises(ConfigError, match="directory"):
        load_channel(workspace, "lofi")


def test_unknown_key_in_channel_config_is_rejected(tmp_path: Path) -> None:
    bad = CHANNEL_YAML + "\nresolution: 1080p\n"
    workspace = load_workspace(make_repo(tmp_path, config=bad))
    with pytest.raises(ConfigError, match="Extra inputs"):
        load_channel(workspace, "lofi")


def test_bumpers_enabled_without_sources_is_rejected(tmp_path: Path) -> None:
    bad = CHANNEL_YAML.replace("bumpers:\n  enabled: false", "bumpers:\n  enabled: true")
    workspace = load_workspace(make_repo(tmp_path, config=bad))
    with pytest.raises(ConfigError, match="sources"):
        load_channel(workspace, "lofi")


@pytest.mark.parametrize(
    "name",
    ["../etc", "lofi/../../etc", "LOFI", "lo fi", "", ".", "-lofi", "a" * 40, "lofi;rm -rf /"],
)
def test_hostile_channel_names_are_rejected(tmp_path: Path, name: str) -> None:
    workspace = load_workspace(make_repo(tmp_path))
    with pytest.raises(ConfigError):
        channel_directory(workspace, name)


def test_channel_symlinked_outside_the_tree_is_rejected(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "elsewhere").mkdir()
    (root / "channels" / "sneaky").symlink_to(root / "elsewhere")
    workspace = load_workspace(root)
    with pytest.raises(ConfigError, match="symlink"):
        channel_directory(workspace, "sneaky")


def test_duplicate_mounts_are_rejected(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    second = root / "channels" / "lofi2"
    (second / "audio").mkdir(parents=True)
    (second / "images").mkdir(parents=True)
    (second / "audio" / "b.mp3").write_bytes(b"x")
    (second / "images" / "b.jpg").write_bytes(b"x")
    (second / "config.yaml").write_text(CHANNEL_YAML.replace("name: lofi", "name: lofi2"), encoding="utf-8")
    (second / ".env").write_text(CHANNEL_ENV, encoding="utf-8")

    workspace = load_workspace(root)
    channels = [load_channel(workspace, "lofi"), load_channel(workspace, "lofi2")]
    with pytest.raises(ConfigError, match="unique"):
        check_mount_uniqueness(channels)


def test_env_parser_handles_quotes_comments_and_export(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        textwrap.dedent(
            """\
            # comment
            export FOO=bar
            QUOTED="a value"
            EMPTY=
            NOEQUALS
            """
        ),
        encoding="utf-8",
    )
    assert parse_env_file(path) == {"FOO": "bar", "QUOTED": "a value", "EMPTY": ""}


def test_empty_env_values_fall_back_to_defaults(tmp_path: Path) -> None:
    workspace = load_workspace(make_repo(tmp_path))
    channel = load_channel(workspace, "lofi")
    assert channel.env.encoder is None  # CHANNEL_ENCODER= is unset, not an error
    assert channel.encoder.value == "libx264"


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def test_a_directory_without_an_env_is_not_a_channel(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "channels" / "notes").mkdir()
    (root / "channels" / "notes" / "README.md").write_text("x", encoding="utf-8")
    assert discover_channels(load_workspace(root)) == ["lofi"]


def test_the_example_template_is_ignored_by_default(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    example = root / "channels" / "example"
    example.mkdir()
    (example / ".env").write_text(CHANNEL_ENV, encoding="utf-8")
    workspace = load_workspace(root)
    assert ignored_channel_names(workspace) == ["example"]
    assert discover_channels(workspace) == ["lofi"]


def test_the_ignore_list_is_configurable(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    (root / "ambient.yaml").write_text(
        "version: 1\npaths:\n  ignore_channels: [lofi]\n", encoding="utf-8"
    )
    workspace = load_workspace(root)
    assert ignored_channel_names(workspace) == ["lofi"]
    assert discover_channels(workspace) == []


def test_an_empty_ignore_list_hides_nothing(tmp_path: Path) -> None:
    root = make_repo(tmp_path)
    example = root / "channels" / "example"
    example.mkdir()
    (example / ".env").write_text(CHANNEL_ENV, encoding="utf-8")
    (root / "ambient.yaml").write_text(
        "version: 1\npaths:\n  ignore_channels: []\n", encoding="utf-8"
    )
    assert discover_channels(load_workspace(root)) == ["example", "lofi"]


def test_stream_key_is_not_printed(tmp_path: Path) -> None:
    workspace = load_workspace(make_repo(tmp_path))
    channel = load_channel(workspace, "lofi")
    assert "aaaa-bbbb" not in repr(channel.env)
    assert channel.env.stream_key.get_secret_value() == "aaaa-bbbb-cccc-dddd-eeee"
