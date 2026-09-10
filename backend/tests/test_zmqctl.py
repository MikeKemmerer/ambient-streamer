"""Validator tests. A malformed message aborts FFmpeg with SIGABRT, so these
cases are the boundary between a bad string and a dead 24/7 stream."""

from __future__ import annotations

import pytest

from ambient.zmqctl import (
    ZmqValidationError,
    build_message,
    stream_select_message,
    validate_address,
    validate_message,
)


@pytest.mark.parametrize(
    "message",
    [
        "eq@eq",  # measured: free(): invalid pointer -> exit 134
        "",  # measured: double free -> exit 134
        "   ",  # measured: invalid pointer -> exit 134
        "eq@eq ",  # measured: invalid pointer -> exit 134
        "nosuchfilter",  # measured: munmap_chunk(): invalid pointer -> exit 134
        "eq@eq brightness",
        "eq@eq brightness 0.2 0.3",
        "eq@eq  brightness 0.2",
        "\n",
        "eq@eq brightness\t0.2 extra",
    ],
)
def test_malformed_messages_are_rejected(message: str) -> None:
    with pytest.raises(ZmqValidationError):
        validate_message(message)


def test_valid_message_parses_into_three_tokens() -> None:
    assert validate_message("eq@eq brightness 0.2") == ("eq@eq", "brightness", "0.2")


@pytest.mark.parametrize("target", ["eq", "all", "Parsed_eq_2", "eq@", "@eq", "eq eq", "eq@eq@eq"])
def test_unlabelled_targets_are_rejected(target: str) -> None:
    with pytest.raises(ZmqValidationError):
        build_message(target, "brightness", "0.2")


def test_labelled_target_is_accepted() -> None:
    assert build_message("eq@eq", "brightness", "0.2") == "eq@eq brightness 0.2"


@pytest.mark.parametrize(
    "filter_target,param,value",
    [
        ("eq@eq", "brightness", "99"),  # measured: FFmpeg answers 0 Success and ignores it
        ("eq@eq", "brightness", "-1.5"),
        ("eq@eq", "saturation", "-1"),
        ("eq@eq", "gamma", "0"),
        ("hue@hue", "h", "720"),
        ("streamselect@sel", "map", "-1"),
    ],
)
def test_out_of_range_values_are_rejected(filter_target: str, param: str, value: str) -> None:
    with pytest.raises(ZmqValidationError):
        build_message(filter_target, param, value)


def test_in_range_values_pass() -> None:
    assert build_message("eq@eq", "brightness", "0.2").endswith("0.2")
    assert build_message("hue@hue", "h", "190").endswith("190")


def test_self_animating_expression_is_allowed() -> None:
    assert build_message("eq@eq", "brightness", "0.35*sin(2*PI*t/2)")
    assert build_message("hue@hue", "h", "mod(t*120,360)")


@pytest.mark.parametrize(
    "value",
    [
        "0.2;rm",
        "$(whoami)",
        "`id`",
        "system(1)",
        "'0.2'",
        "0.2|0.3",
    ],
)
def test_hostile_values_are_rejected(value: str) -> None:
    with pytest.raises(ZmqValidationError):
        build_message("eq@eq", "brightness", value)


def test_drawbox_refuses_expressions() -> None:
    with pytest.raises(ZmqValidationError):
        build_message("drawbox@box", "t", "sin(t)")


def test_drawbox_color_is_accepted() -> None:
    assert build_message("drawbox@box", "color", "red") == "drawbox@box color red"
    assert build_message("drawbox@box", "color", "#4FC3F7").endswith("#4FC3F7")


def test_stream_select_bounds_check() -> None:
    assert stream_select_message("streamselect@sel", 1, 3) == "streamselect@sel map 1"
    for bad in (-1, 3, 99):
        with pytest.raises(ZmqValidationError):
            stream_select_message("streamselect@sel", bad, 3)


def test_stream_select_rejects_non_integer_map() -> None:
    with pytest.raises(ZmqValidationError):
        build_message("streamselect@sel", "map", "1.5")
    with pytest.raises(ZmqValidationError):
        build_message("streamselect@sel", "map", "abc")


@pytest.mark.parametrize("address", ["tcp://*:5555", "tcp://0.0.0.0:5555", "", "   "])
def test_wildcard_addresses_are_rejected(address: str) -> None:
    with pytest.raises(ZmqValidationError):
        validate_address(address)


def test_loopback_address_is_accepted() -> None:
    assert validate_address("tcp://127.0.0.1:5555") == "tcp://127.0.0.1:5555"


def test_non_string_input_is_rejected() -> None:
    for bad in (None, 3, [], object()):
        with pytest.raises(ZmqValidationError):
            build_message("eq@eq", "brightness", bad)  # type: ignore[arg-type]
