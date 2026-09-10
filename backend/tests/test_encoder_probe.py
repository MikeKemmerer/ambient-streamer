"""What the encoder probe reports, and why it was lying.

`--gpus all` grants compute,utility but NOT video. NVENC then sees the GPU, loads
libcuda, and fails on libnvidia-encode.so.1 - so the probe reported h264_nvenc
unavailable on a host that had been streaming with it for hours. Worse, the
failure detail showed ffmpeg's closing line ("Nothing was written into output
file"), which is true of every failed encode and names no cause.
"""

from __future__ import annotations

from ambient.ffmpeg_cmd import probe_device_args, probe_failure_detail
from ambient.models import Encoder

NVENC_STDERR = """\
[h264_nvenc @ 0x5da0f1695840] Cannot load libnvidia-encode.so.1
[h264_nvenc @ 0x5da0f1695840] The minimum required Nvidia driver for nvenc is (unknown) or newer
[vost#0:0/h264_nvenc @ 0x5da0f1695440] Error while opening encoder - maybe incorrect parameters
Error while filtering: Operation not permitted
[out#0/null @ 0x5da0f1694140] Nothing was written into output file, because at least one of \
its streams received no packets.
"""


def test_the_nvenc_probe_asks_for_the_video_capability() -> None:
    args = probe_device_args(Encoder.NVENC)
    joined = " ".join(args)
    assert "--gpus" in args
    assert "video" in joined, (
        "without NVIDIA_DRIVER_CAPABILITIES=...video the probe cannot load "
        "libnvidia-encode and reports a working encoder as broken"
    )


def test_the_probe_matches_how_a_channel_actually_runs() -> None:
    """Whatever compose gives the composer, the probe has to give the probe."""
    from pathlib import Path

    template = Path("docker/compose.channel.yml.j2").read_text(encoding="utf-8")
    capabilities = [
        line.split(":", 1)[1].strip()
        for line in template.splitlines()
        if "NVIDIA_DRIVER_CAPABILITIES" in line and ":" in line
    ]
    assert capabilities, "the template must declare the capabilities"
    assert any(c in " ".join(probe_device_args(Encoder.NVENC)) for c in capabilities)


def test_qsv_asks_for_the_render_node() -> None:
    assert probe_device_args(Encoder.QSV) == ["--device", "/dev/dri"]


def test_software_encoding_needs_no_device() -> None:
    assert probe_device_args(Encoder.LIBX264) == []


def test_the_failure_detail_names_the_cause_not_the_symptom() -> None:
    detail = probe_failure_detail(NVENC_STDERR, 1)
    assert "Nothing was written" not in detail, "true of every failed encode; names nothing"
    assert "maybe incorrect parameters" not in detail, "ffmpeg's generic fallback, and wrong here"
    assert "nvenc" in detail.lower() or "nvidia" in detail.lower()


def test_a_detail_is_still_produced_when_every_line_is_noise() -> None:
    noise = "Error while filtering: Operation not permitted\nConversion failed!\n"
    assert probe_failure_detail(noise, 1)


def test_an_empty_stderr_falls_back_to_the_return_code() -> None:
    assert probe_failure_detail("", 137) == "rc=137"
