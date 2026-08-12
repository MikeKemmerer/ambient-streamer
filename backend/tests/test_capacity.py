"""Capacity arithmetic against the numbers measured on the 7-core reference host.

    1 hot plugin   ~1.50 cores, 1.0x
    3 hot plugins   2.03 cores, 0.9936x
    5 hot plugins   2.64 cores, 0.97x  (below realtime)
"""

from __future__ import annotations

import json
from pathlib import Path

from ambient.plugins import (
    IDLE_BRANCH_CORES_720P30,
    PREVIEW_CORES,
    check_hot_set,
    load_registry,
    pipeline_cores,
)

MEASURED = {1: 1.50, 3: 2.03, 5: 2.64}
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
    return check_hot_set([f"viz{i}" for i in range(count)], registry, width, height, fps)


def test_one_hot_plugin_lands_on_the_measured_one_and_a_half(tmp_path: Path) -> None:
    check = project(tmp_path, 1)
    assert abs(check.projected_cores - MEASURED[1]) < TOLERANCE


def test_three_and_five_hot_plugins_match_the_measured_run(tmp_path: Path) -> None:
    for count in (3, 5):
        check = project(tmp_path / str(count), count)
        assert abs(check.projected_cores - MEASURED[count]) < TOLERANCE


def test_the_slope_is_the_measured_zero_point_two_eight(tmp_path: Path) -> None:
    """0.22 was the old, optimistic figure; five hot plugins fell below realtime."""
    three = project(tmp_path / "a", 3).projected_cores
    five = project(tmp_path / "b", 5).projected_cores
    assert abs((five - three) / 2 - IDLE_BRANCH_CORES_720P30) < 0.001


def test_projection_counts_the_preview_as_a_second_encode(tmp_path: Path) -> None:
    check = project(tmp_path, 1)
    assert check.preview_cores == PREVIEW_CORES
    assert check.projected_cores == check.pipeline_cores + check.preview_cores + check.branch_cores
    # The old model was the branch cost alone, which read 0.24 against 1.49 measured.
    assert check.projected_cores > check.branch_cores * 5


def test_a_cheap_manifest_cannot_project_below_the_measured_branch_cost(tmp_path: Path) -> None:
    check = project(tmp_path, 1)
    cheap = check_hot_set(
        ["viz0"], load_registry(make_plugins(tmp_path / "cheap", 1, cores=0.05)), 1280, 720, 30
    )
    assert cheap.branch_cores == IDLE_BRANCH_CORES_720P30
    assert cheap.projected_cores == check.projected_cores


def test_1080p_costs_more_than_720p(tmp_path: Path) -> None:
    at_720 = project(tmp_path / "a", 3).projected_cores
    at_1080 = project(tmp_path / "b", 3, width=1920, height=1080).projected_cores
    assert at_1080 > at_720


def test_the_preview_does_not_scale_with_the_channel_resolution(tmp_path: Path) -> None:
    """It is a fixed 640x360@15 output whatever the program encode is."""
    assert project(tmp_path / "b", 1, width=1920, height=1080).preview_cores == PREVIEW_CORES


def test_an_empty_registry_projects_nothing_and_says_so(tmp_path: Path) -> None:
    check = check_hot_set(["viz0"], {}, 1280, 720, 30)
    assert check.projected_cores == 0.0
    assert any("unverified" in w for w in check.warnings)


def test_pipeline_cost_is_paid_before_any_branch(tmp_path: Path) -> None:
    assert pipeline_cores(1280, 720, 30) > 1.0
    assert pipeline_cores(1280, 720, 60) > pipeline_cores(1280, 720, 30)
