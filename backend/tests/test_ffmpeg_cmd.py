"""Command-builder tests: the ladder, the mandatory input flags, and the
substitute-rather-than-fail encoder rule."""

from __future__ import annotations

import pytest

from ambient.ffmpeg_cmd import (
    ComposerSpec,
    EncoderSelection,
    EncoderUnavailable,
    PreviewOutput,
    ProbeResult,
    build_composer_command,
    geometry,
    rates,
    select_encoder,
    video_flags,
)
from ambient.models import Encoder

GRAPH = "[0:a]anull[aout];[1:v]fps=30,realtime[vout]"


def spec(**kwargs) -> ComposerSpec:
    base = dict(
        channel="lofi",
        resolution="720p",
        fps=30,
        encoder=Encoder.LIBX264,
        icecast_url="http://icecast:8081/lofi",
        filter_complex=GRAPH,
        rtmp_url="rtmp://a.rtmp.youtube.com/live2",
        stream_key="abcd-efgh-ijkl-mnop-qrst",
    )
    base.update(kwargs)
    return ComposerSpec(**base)


def test_ladder_matches_the_skill() -> None:
    assert rates("720p").video_kbps == 3000
    assert rates("720p").bufsize_kbps == 6000
    assert rates("1080p").audio_kbps == 192
    assert rates("2160p").video_kbps == 16000
    for name in ("480p", "720p", "1080p", "1440p", "2160p"):
        assert rates(name).bufsize_kbps == rates(name).video_kbps * 2


def test_sixty_fps_raises_video_bitrate() -> None:
    assert rates("720p", 60).video_kbps == 4500
    assert rates("720p", 60).bufsize_kbps == 9000


def test_geometry() -> None:
    assert geometry("720p") == (1280, 720)
    with pytest.raises(ValueError):
        geometry("999p")


def test_cbr_and_gop_settings() -> None:
    flags = video_flags(Encoder.LIBX264, 30, rates("720p"))
    joined = " ".join(flags)
    assert "-b:v 3000k" in joined and "-minrate 3000k" in joined and "-maxrate 3000k" in joined
    assert "-bufsize 6000k" in joined
    assert "-g 60" in joined and "-keyint_min 60" in joined
    assert "nal-hrd=cbr:force-cfr=1" in joined
    assert "-fps_mode cfr" in joined
    assert "-pix_fmt yuv420p" in joined


def test_hardware_profiles_keep_cbr_intent() -> None:
    nvenc = " ".join(video_flags(Encoder.NVENC, 30, rates("720p")))
    assert "-c:v h264_nvenc" in nvenc and "-rc cbr" in nvenc and "-cbr 1" in nvenc
    qsv = " ".join(video_flags(Encoder.QSV, 30, rates("720p")))
    assert "-c:v h264_qsv" in qsv and "-rc_mode CBR" in qsv


def test_mandatory_input_flags_are_present() -> None:
    argv = build_composer_command(spec()).argv
    joined = " ".join(argv)
    assert "-probesize 32k" in joined
    assert "-analyzeduration 500000" in joined
    assert "-reconnect_on_network_error 1" in joined
    assert "-f image2pipe -framerate 10 -i pipe:0" in joined
    assert argv[argv.index("-i") + 1] == "http://icecast:8081/lofi"


def test_stream_key_is_redacted_in_the_printable_form() -> None:
    command = build_composer_command(spec())
    assert "abcd-efgh-ijkl-mnop-qrst" in " ".join(command.argv)
    assert "abcd-efgh-ijkl-mnop-qrst" not in str(command)
    assert "abcd-efgh-ijkl-mnop-qrst" not in repr(command)
    assert "<redacted>" in str(command)


def test_preview_adds_a_second_output() -> None:
    command = build_composer_command(
        spec(preview=PreviewOutput(video_label="preview", rtmp_url="rtmp://mediamtx:1935/lofi/preview"))
    )
    joined = " ".join(command.argv)
    assert joined.count("-f flv") == 2
    assert "rtmp://mediamtx:1935/lofi/preview" in joined
    assert "-b:v 1000k" in joined  # the 360p rung


def test_empty_filtergraph_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_composer_command(spec(filter_complex="  "))


def test_encoder_falls_back_when_the_probe_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_probe(encoder, ffmpeg="ffmpeg", timeout=60.0, use_cache=True):
        name = getattr(encoder, "value", encoder)
        if name == "h264_nvenc":
            return ProbeResult(name, False, "OpenEncodeSessionEx failed: unsupported device (2)")
        return ProbeResult(name, True)

    monkeypatch.setattr("ambient.ffmpeg_cmd.probe_encoder", fake_probe)
    selection = select_encoder(Encoder.NVENC, [Encoder.NVENC, Encoder.QSV, Encoder.LIBX264], Encoder.LIBX264)
    assert selection == EncoderSelection("libx264", "h264_nvenc", True, selection.reason)
    assert "unsupported device" in selection.reason


def test_no_encoder_available_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ambient.ffmpeg_cmd.probe_encoder",
        lambda encoder, ffmpeg="ffmpeg", timeout=60.0, use_cache=True: ProbeResult(
            getattr(encoder, "value", encoder), False, "no device"
        ),
    )
    with pytest.raises(EncoderUnavailable):
        select_encoder(Encoder.LIBX264, [Encoder.LIBX264], Encoder.LIBX264)


def test_working_encoder_is_not_substituted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ambient.ffmpeg_cmd.probe_encoder",
        lambda encoder, ffmpeg="ffmpeg", timeout=60.0, use_cache=True: ProbeResult(
            getattr(encoder, "value", encoder), True
        ),
    )
    selection = select_encoder(Encoder.LIBX264, [Encoder.LIBX264], Encoder.LIBX264)
    assert selection.substituted is False
    assert selection.encoder == "libx264"
