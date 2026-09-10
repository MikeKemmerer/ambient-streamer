"""The slideshow picks up where the last compositor left off.

Applying most settings replaces the compositor, and the producer goes with it.
Audio survives because Liquidsoap is a separate process feeding Icecast, but the
producer is in-process state, so without this the images jumped back to the top
of the order every time an operator changed the resolution.

slideshow.py ships in the composer image and is not importable as part of the
backend package, so it is loaded from the repo by path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / "ffmpeg" / "slideshow.py"


@pytest.fixture(scope="module")
def slideshow():
    if not SOURCE.is_file():
        pytest.skip(f"{SOURCE} not present")
    # slideshow.py imports its sibling nowstate, and it runs from its own
    # directory in the composer image rather than as an installed package.
    sys.path.insert(0, str(SOURCE.parent))
    try:
        spec = importlib.util.spec_from_file_location("slideshow_under_test", SOURCE)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except ImportError as exc:
            pytest.skip(f"producer dependency missing: {exc}")
        yield module
    finally:
        sys.path.remove(str(SOURCE.parent))


def settings(slideshow, tmp_path, order="sequential", position=""):
    return slideshow.Settings(
        images_list=str(tmp_path / "images.list"),
        width=1280, height=720, fps=10.0, hold=20.0, fade=2.0, quality=88,
        order=order, stats_interval=30.0, zmq_endpoint="", transition=2.0,
        color_mode="auto", color_mode_file="", position_file=position,
    )


SLIDES = ["/media/a.jpg", "/media/b.jpg", "/media/c.jpg", "/media/d.jpg"]


def test_a_fresh_channel_starts_at_the_top(slideshow, tmp_path) -> None:
    loader = slideshow.SlideLoader(settings(slideshow, tmp_path))
    loader.adopt(SLIDES)
    assert loader.index == 0


def test_it_resumes_on_the_slide_that_was_showing(slideshow, tmp_path) -> None:
    """Not the next one: that slide never finished its hold."""
    position = tmp_path / "slide-position"
    position.write_text("/media/c.jpg", encoding="utf-8")

    loader = slideshow.SlideLoader(settings(slideshow, tmp_path, position=str(position)))
    loader.adopt(SLIDES)
    assert loader.index == SLIDES.index("/media/c.jpg")


def test_a_slide_that_is_gone_falls_back_to_the_top(slideshow, tmp_path) -> None:
    position = tmp_path / "slide-position"
    position.write_text("/media/deleted.jpg", encoding="utf-8")

    loader = slideshow.SlideLoader(settings(slideshow, tmp_path, position=str(position)))
    loader.adopt(SLIDES)
    assert loader.index == 0


def test_the_saved_position_is_used_once_not_forever(slideshow, tmp_path) -> None:
    """A later list change must follow the live slide, not the stale file."""
    position = tmp_path / "slide-position"
    position.write_text("/media/c.jpg", encoding="utf-8")

    loader = slideshow.SlideLoader(settings(slideshow, tmp_path, position=str(position)))
    loader.adopt(SLIDES)
    loader.current = "/media/a.jpg"
    loader.adopt(SLIDES)
    assert loader.index == SLIDES.index("/media/a.jpg") + 1


def test_the_position_is_recorded_as_each_slide_goes_up(slideshow, tmp_path) -> None:
    position = tmp_path / "slide-position"
    loader = slideshow.SlideLoader(settings(slideshow, tmp_path, position=str(position)))
    loader.ready = slideshow.Slide("/media/b.jpg", object(), b"", None)

    assert loader.take() is not None
    assert position.read_text(encoding="utf-8") == "/media/b.jpg"


def test_a_missing_position_file_is_not_an_error(slideshow, tmp_path) -> None:
    assert slideshow.read_position(str(tmp_path / "nope")) == ""
    assert slideshow.read_position("") == ""


def test_an_unwritable_position_never_stops_the_broadcast(slideshow, tmp_path) -> None:
    """Losing continuity is survivable; killing the producer EOFs the pipe."""
    slideshow.write_position(str(tmp_path / "no-such-dir" / "pos"), "/media/a.jpg")
    slideshow.write_position("", "/media/a.jpg")


def test_automatic_colors_fade_from_the_previous_slide(slideshow) -> None:
    sender = slideshow.ColorSender.__new__(slideshow.ColorSender)
    sender.transition = 2.0
    sender.enabled = True
    sender.baked_accent = "#4FC3F7"
    sender.brightness = 0.0
    sender.saturation = 1.0
    sender.viz_hue = 0.0
    sender.ramp_start = 0.0
    sender.start_brightness = 0.0
    sender.start_saturation = 1.0
    sender.start_viz_hue = 0.0
    sender.queue = slideshow.queue.Queue(maxsize=1)

    sender.apply({"accent": "#FF8800", "brightness": 0.75, "warmth": 0.4}, 10.0)
    first = sender.queue.get_nowait()
    sender.apply({"accent": "#00FF66", "brightness": 0.25, "warmth": -0.4}, 20.0)
    second = sender.queue.get_nowait()
    sender.apply({"accent": "#FF00AA", "brightness": 1.0, "warmth": 0.0}, 21.0)
    interrupted = sender.queue.get_nowait()

    assert any("eq@eq brightness (0.0000+(0.1000)" in message for message in first)
    assert any("hue@viz h (0.0000+(" in message for message in first)
    assert all("hue@hue" not in message for message in first + second)
    assert any("eq@eq brightness (0.1000+(-0.2000)" in message for message in second)
    assert any("eq@eq saturation (1.1000+(-0.2000)" in message for message in second)
    assert any("hue@viz h" in message and "(t-20.000)" in message for message in second)
    assert any("eq@eq brightness (0.0000+(0.2000)" in message for message in interrupted)
    assert any("eq@eq saturation (1.0000+(0.0000)" in message for message in interrupted)
    assert any("hue@viz h" in message and "(t-21.000)" in message for message in interrupted)


def test_adopting_a_backend_color_queues_no_duplicate_command(slideshow) -> None:
    sender = slideshow.ColorSender.__new__(slideshow.ColorSender)
    sender.baked_accent = "#4FC3F7"
    sender.brightness = 0.0
    sender.saturation = 1.0
    sender.viz_hue = 0.0
    sender.ramp_start = 0.0
    sender.start_brightness = 0.0
    sender.start_saturation = 1.0
    sender.start_viz_hue = 0.0
    sender.queue = slideshow.queue.Queue(maxsize=1)

    sender.adopt({"accent": "#FF8800", "brightness": 0.75, "warmth": 0.4})

    assert sender.queue.empty()
    assert sender.brightness == pytest.approx(0.1)
    assert sender.saturation == pytest.approx(1.1)
    assert sender.viz_hue != 0.0


def test_automatic_color_skips_viz_hue_when_the_branch_is_absent(slideshow) -> None:
    sender = slideshow.ColorSender.__new__(slideshow.ColorSender)
    sender.transition = 2.0
    sender.enabled = True
    sender.baked_accent = ""
    sender.brightness = 0.0
    sender.saturation = 1.0
    sender.viz_hue = 0.0
    sender.ramp_start = 0.0
    sender.start_brightness = 0.0
    sender.start_saturation = 1.0
    sender.start_viz_hue = 0.0
    sender.queue = slideshow.queue.Queue(maxsize=1)

    sender.apply({"accent": "#FF8800", "brightness": 0.75, "warmth": 0.4}, 10.0)

    messages = sender.queue.get_nowait()
    assert len(messages) == 2
    assert all(message.startswith("eq@eq ") for message in messages)


def test_it_resumes_within_a_shuffled_order_too(slideshow, tmp_path) -> None:
    position = tmp_path / "slide-position"
    position.write_text("/media/c.jpg", encoding="utf-8")

    loader = slideshow.SlideLoader(settings(slideshow, tmp_path, "shuffle", str(position)))
    loader.adopt(SLIDES)
    assert loader.order[loader.index] == "/media/c.jpg"
