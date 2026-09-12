"""Capacity arithmetic for one isolated active visualization plugin."""

from __future__ import annotations

import json
from pathlib import Path

from ambient.plugins import (
    IDLE_BRANCH_CORES_720P30,
    PREVIEW_CORES,
    check_visualization,
    load_registry,
    pipeline_cores,
)

MEASURED_ONE_PLUGIN = 1.50
TOLERANCE = 0.06


def make_plugins(root: Path, count: int, cores: float = 0.24) -> Path:
    directory = root / "plugins"
    for index in range(count):
        plugin = directory / f"viz{index}"
        plugin.mkdir(parents=True)
        (plugin / "config.json").write_text(
            json.dumps(
                {
                    "name": f"viz{index}",
                    "output_size": "channel",
                    "cost": {"cores_720p30": cores, "scale_1080p": 1.9},
                }
            ),
            encoding="utf-8",
        )
        (plugin / "viz.ffmpeg").write_text("showfreqs=s=${WIDTH}x${HEIGHT}\n", encoding="utf-8")
    return directory


def project(tmp_path: Path, count: int, width: int = 1280, height: int = 720, fps: int = 30):
    registry = load_registry(make_plugins(tmp_path, count))
    return check_visualization("viz0", registry, width, height, fps)


def test_one_hot_plugin_lands_on_the_measured_one_and_a_half(tmp_path: Path) -> None:
    check = project(tmp_path, 1)
    assert abs(check.projected_cores - MEASURED_ONE_PLUGIN) < TOLERANCE


def test_legacy_hot_set_size_does_not_change_projection(tmp_path: Path) -> None:
    one = project(tmp_path / "one", 1)
    five = project(tmp_path / "five", 5)
    assert five.projected_cores == one.projected_cores


def test_only_one_active_branch_is_counted(tmp_path: Path) -> None:
    assert project(tmp_path, 5).branch_cores == IDLE_BRANCH_CORES_720P30


def test_projection_counts_the_preview_as_a_second_encode(tmp_path: Path) -> None:
    check = project(tmp_path, 1)
    assert check.preview_cores == PREVIEW_CORES
    assert check.projected_cores == check.pipeline_cores + check.preview_cores + check.branch_cores
    # The old model was the branch cost alone, which read 0.24 against 1.49 measured.
    assert check.projected_cores > check.branch_cores * 5


def test_a_cheap_manifest_cannot_project_below_the_measured_branch_cost(tmp_path: Path) -> None:
    check = project(tmp_path, 1)
    cheap = check_visualization(
        "viz0", load_registry(make_plugins(tmp_path / "cheap", 1, cores=0.05)), 1280, 720, 30
    )
    assert cheap.branch_cores == IDLE_BRANCH_CORES_720P30
    assert cheap.projected_cores == check.projected_cores


def test_1080p_compositor_costs_more_but_visualizer_is_capped(tmp_path: Path) -> None:
    at_720 = project(tmp_path / "a", 3).projected_cores
    high = project(tmp_path / "b", 3, width=1920, height=1080)
    at_1080 = high.projected_cores
    assert at_1080 > at_720
    assert high.branch_cores == IDLE_BRANCH_CORES_720P30


def test_the_preview_does_not_scale_with_the_channel_resolution(tmp_path: Path) -> None:
    """It is a fixed 640x360@15 output whatever the program encode is."""
    assert project(tmp_path / "b", 1, width=1920, height=1080).preview_cores == PREVIEW_CORES


def test_an_empty_registry_still_projects_the_known_pipeline_cost(tmp_path: Path) -> None:
    check = check_visualization("viz0", {}, 1280, 720, 30)
    assert check.projected_cores == check.pipeline_cores + check.preview_cores
    assert check.projected_cores > 0.0
    assert any("unverified" in w for w in check.warnings)


def test_pipeline_cost_is_paid_before_any_branch(tmp_path: Path) -> None:
    assert pipeline_cores(1280, 720, 30) > 1.0
    assert pipeline_cores(1280, 720, 60) > pipeline_cores(1280, 720, 30)
