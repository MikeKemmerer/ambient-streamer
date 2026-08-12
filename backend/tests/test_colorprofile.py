"""Colour profiles: the on-disk schema, where they land, and staleness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ambient.colorprofile import (
    EXTRACTORS,
    PILLOW_KMEANS_V1,
    ColourProfile,
    ProfileError,
    ensure_profile,
    extract,
    profile_path,
    read_profile,
    refresh_tree,
    summarise,
    write_profile,
)

PIL = pytest.importorskip("PIL")


def make_image(path: Path, colours: list[tuple[int, int, int]], size: int = 32) -> Path:
    from PIL import Image

    image = Image.new("RGB", (size, size))
    pixels = image.load()
    band = max(1, size // len(colours))
    for y in range(size):
        for x in range(size):
            pixels[x, y] = colours[min(x // band, len(colours) - 1)]
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return path


def test_the_profile_matches_the_on_disk_schema(tmp_path: Path) -> None:
    image = make_image(tmp_path / "common" / "images" / "forest.jpg", [(46, 74, 59), (143, 214, 168)])
    profile = extract(image, repo_root=tmp_path)

    assert profile.version == 1
    assert profile.source == "common/images/forest.jpg"
    assert profile.extracted_at.endswith("Z")
    assert profile.extractor == PILLOW_KMEANS_V1
    assert profile.dominant.startswith("#") and len(profile.dominant) == 7
    assert profile.accent.startswith("#") and len(profile.accent) == 7
    assert 1 <= len(profile.palette) <= 5
    assert 0.0 <= profile.brightness <= 1.0
    assert -1.0 <= profile.warmth <= 1.0
    assert profile.mood in {"calm", "warm", "cool", "energetic"}


def test_extraction_is_deterministic(tmp_path: Path) -> None:
    image = make_image(tmp_path / "common" / "images" / "a.png", [(10, 40, 90), (200, 30, 30)])
    first = extract(image, repo_root=tmp_path)
    second = extract(image, repo_root=tmp_path)
    assert first.palette == second.palette
    assert first.dominant == second.dominant


def test_warmth_separates_a_warm_image_from_a_cool_one(tmp_path: Path) -> None:
    warm = extract(make_image(tmp_path / "common" / "images" / "w.png", [(220, 120, 30)]), repo_root=tmp_path)
    cool = extract(make_image(tmp_path / "common" / "images" / "c.png", [(30, 90, 220)]), repo_root=tmp_path)
    assert warm.warmth > 0
    assert cool.warmth < 0
    assert cool.mood in {"cool", "energetic"}


def test_a_profile_lives_beside_the_tree_that_owns_the_image(tmp_path: Path) -> None:
    common = tmp_path / "common"
    channel = tmp_path / "channels" / "lofi"
    assert profile_path(common / "images" / "x.jpg", common) == common / "profiles" / "x.json"
    assert (
        profile_path(channel / "images" / "sub" / "y.png", channel)
        == channel / "profiles" / "sub" / "y.json"
    )


def test_a_shared_image_is_analysed_once(tmp_path: Path) -> None:
    tree = tmp_path / "common"
    make_image(tree / "images" / "shared.png", [(20, 60, 40)])
    _first, wrote_first = ensure_profile(
        tree / "images" / "shared.png", tree, repo_root=tmp_path
    )
    _second, wrote_second = ensure_profile(
        tree / "images" / "shared.png", tree, repo_root=tmp_path
    )
    assert wrote_first is True
    assert wrote_second is False


def test_an_unknown_extractor_is_treated_as_absent(tmp_path: Path) -> None:
    image = make_image(tmp_path / "common" / "images" / "x.png", [(10, 10, 10)])
    profile = extract(image, repo_root=tmp_path)
    target = profile_path(image, tmp_path / "common")
    write_profile(profile, target)
    assert read_profile(target) is not None

    stale = json.loads(target.read_text(encoding="utf-8"))
    stale["extractor"] = "some-future-thing-v9"
    target.write_text(json.dumps(stale), encoding="utf-8")
    assert read_profile(target) is None

    _profile, wrote = ensure_profile(image, tmp_path / "common", repo_root=tmp_path)
    assert wrote is True
    assert read_profile(target) is not None


def test_a_corrupt_profile_is_treated_as_absent(tmp_path: Path) -> None:
    path = tmp_path / "profiles" / "x.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert read_profile(path) is None


def test_the_extractor_registry_is_pluggable(tmp_path: Path) -> None:
    from ambient.colorprofile import Analysis

    def flat(_path: Path) -> Analysis:
        return Analysis("#112233", "#445566", ["#112233"], 0.5, 0.0, "calm")

    EXTRACTORS["test-flat-v1"] = flat
    try:
        image = make_image(tmp_path / "common" / "images" / "x.png", [(1, 2, 3)])
        profile = extract(image, repo_root=tmp_path, extractor="test-flat-v1")
        assert profile.extractor == "test-flat-v1"
        assert profile.dominant == "#112233"
    finally:
        EXTRACTORS.pop("test-flat-v1")


def test_an_unknown_extractor_name_is_an_error(tmp_path: Path) -> None:
    image = make_image(tmp_path / "common" / "images" / "x.png", [(1, 2, 3)])
    with pytest.raises(ProfileError, match="unknown extractor"):
        extract(image, repo_root=tmp_path, extractor="nope")


def test_refresh_walks_a_whole_tree(tmp_path: Path) -> None:
    tree = tmp_path / "common"
    for name in ("a.png", "b.jpg", "sub/c.png"):
        make_image(tree / "images" / name, [(30, 60, 90)])
    (tree / "images" / "notes.txt").write_text("skip me", encoding="utf-8")
    (tree / "images" / ".hidden.png").write_bytes(b"nope")

    result = refresh_tree(tree, repo_root=tmp_path)
    assert result["extracted"] == 3
    assert result["errors"] == []
    assert (tree / "profiles" / "sub" / "c.json").is_file()

    again = refresh_tree(tree, repo_root=tmp_path)
    assert again["extracted"] == 0 and again["skipped"] == 3


def test_summarise_is_none_for_a_missing_profile() -> None:
    assert summarise(None) is None


def test_the_schema_rejects_a_non_hex_colour() -> None:
    with pytest.raises(Exception):
        ColourProfile(
            source="x.jpg",
            extracted_at="2026-08-11T22:04:00Z",
            extractor=PILLOW_KMEANS_V1,
            dominant="green",
            accent="#8FD6A8",
            palette=["#8FD6A8"],
            brightness=0.3,
            warmth=0.0,
            mood="calm",
        )
