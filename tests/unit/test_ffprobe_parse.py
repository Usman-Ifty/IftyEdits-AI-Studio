"""ffprobe JSON parsing, using representative fixtures (no ffprobe binary needed)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from iftyedits.adapters.ffprobe import FFprobe, parse_probe_json, parse_rate, parse_time_base
from iftyedits.core.errors import ExternalToolError, ProbeError

H264_AAC: dict[str, Any] = {
    "streams": [
        {
            "index": 0,
            "codec_name": "h264",
            "profile": "High",
            "codec_type": "video",
            "width": 1920,
            "height": 1080,
            "pix_fmt": "yuv420p",
            "r_frame_rate": "30000/1001",
            "avg_frame_rate": "30000/1001",
            "time_base": "1/30000",
            "duration": "120.120000",
            "bit_rate": "8000000",
            "disposition": {"attached_pic": 0},
            "tags": {"language": "und"},
        },
        {
            "index": 1,
            "codec_name": "aac",
            "codec_type": "audio",
            "sample_rate": "48000",
            "channels": 2,
            "channel_layout": "stereo",
            "bit_rate": "192000",
            "tags": {"language": "eng"},
        },
    ],
    "format": {
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "duration": "120.12",
        "bit_rate": "8200000",
    },
}

IPHONE_HEVC_HDR_ROTATED: dict[str, Any] = {
    "streams": [
        {
            "index": 0,
            "codec_name": "hevc",
            "profile": "Main 10",
            "codec_type": "video",
            "width": 3840,
            "height": 2160,
            "pix_fmt": "yuv420p10le",
            "color_transfer": "arib-std-b67",
            "color_primaries": "bt2020",
            "r_frame_rate": "60/1",
            "avg_frame_rate": "5994/100",
            "side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}],
        },
        {"index": 1, "codec_type": "audio", "codec_name": "aac", "sample_rate": "44100"},
        {"index": 2, "codec_type": "data", "codec_name": "none"},
    ],
    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "12.5"},
}

VFR_NO_AUDIO: dict[str, Any] = {
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1080,
            "height": 1920,
            "r_frame_rate": "60/1",
            "avg_frame_rate": "2400/89",
            "tags": {"rotate": "180"},
        }
    ],
    "format": {"format_name": "mov,mp4", "duration": "5.0"},
}

AUDIO_WITH_COVER_ART: dict[str, Any] = {
    "streams": [
        {"index": 0, "codec_type": "audio", "codec_name": "mp3"},
        {
            "index": 1,
            "codec_type": "video",
            "codec_name": "mjpeg",
            "width": 500,
            "height": 500,
            "r_frame_rate": "90000/1",
            "avg_frame_rate": "0/0",
            "disposition": {"attached_pic": 1},
        },
    ],
    "format": {"format_name": "mp3", "duration": "200.0"},
}


def test_h264_aac() -> None:
    info = parse_probe_json(json.dumps(H264_AAC))
    assert info.format_name == "mov,mp4,m4a,3gp,3g2,mj2"
    assert info.duration_s == pytest.approx(120.12)
    assert info.bit_rate == 8_200_000
    video = info.primary_video
    assert video is not None
    assert (video.codec_name, video.width, video.height) == ("h264", 1920, 1080)
    assert video.avg_frame_rate == pytest.approx(29.97, abs=0.01)
    assert not video.is_vfr
    assert not video.is_hdr
    assert video.rotation == 0
    assert video.time_base == "1/30000"
    audio = info.audio_streams[0]
    assert (audio.sample_rate, audio.channels, audio.language) == (48000, 2, "eng")
    assert json.loads(info.raw_json) == H264_AAC


def test_iphone_hevc_hdr_rotated() -> None:
    info = parse_probe_json(json.dumps(IPHONE_HEVC_HDR_ROTATED))
    video = info.primary_video
    assert video is not None
    assert video.is_hdr
    assert video.rotation == 90
    assert video.display_size == (2160, 3840)
    assert not video.is_vfr  # 59.94 vs 60 is within tolerance
    assert len(info.streams) == 3


def test_vfr_and_no_audio() -> None:
    info = parse_probe_json(json.dumps(VFR_NO_AUDIO))
    video = info.primary_video
    assert video is not None
    assert video.is_vfr
    assert video.rotation == 180
    assert info.audio_streams == ()


def test_cover_art_is_not_a_video_stream() -> None:
    info = parse_probe_json(json.dumps(AUDIO_WITH_COVER_ART))
    assert info.video_streams == ()
    assert info.primary_video is None
    assert info.streams[1].avg_frame_rate is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("30000/1001", 29.97002997), ("25/1", 25.0), ("0/0", None), ("", None), (None, None)],
)
def test_parse_rate(value: object, expected: float | None) -> None:
    result = parse_rate(value)
    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1/15360", "1/15360"), ("1001/30000", "1001/30000"), ("0/1", None), ("1/0", None),
     ("abc", None), ("", None), (None, None)],
)  # fmt: skip
def test_parse_time_base(value: object, expected: str | None) -> None:
    assert parse_time_base(value) == expected


@pytest.mark.parametrize("text", ["not json", "[]", '{"streams": {}, "format": []}'])
def test_malformed_output_raises(text: str) -> None:
    with pytest.raises(ProbeError):
        parse_probe_json(text)


def test_missing_binary_raises() -> None:
    with pytest.raises(ExternalToolError):
        FFprobe(binary="definitely-not-ffprobe-xyz")


def _fake_ffprobe(monkeypatch: pytest.MonkeyPatch, **result: Any) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr("iftyedits.adapters.ffprobe.shutil.which", lambda _: "/usr/bin/ffprobe")

    def fake_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if "raise_timeout" in result:
            raise subprocess.TimeoutExpired(args, 1)
        return subprocess.CompletedProcess(
            args, result.get("returncode", 0), result.get("stdout", ""), result.get("stderr", "")
        )

    monkeypatch.setattr("iftyedits.adapters.ffprobe.subprocess.run", fake_run)
    return calls


def test_probe_uses_argument_list_and_absolute_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    media = tmp_path / "-tricky name.mp4"
    media.write_bytes(b"x")
    calls = _fake_ffprobe(monkeypatch, stdout=json.dumps(H264_AAC))
    FFprobe().probe(media)
    args = calls[0]
    assert args[-2:] == ["-i", str(media.resolve())]
    assert args[-1].startswith("/")


def test_probe_failure_raises_probe_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    media = tmp_path / "bad.mp4"
    media.write_bytes(b"x")
    _fake_ffprobe(monkeypatch, returncode=1, stderr="Invalid data found when processing input")
    with pytest.raises(ProbeError, match="Invalid data"):
        FFprobe().probe(media)


def test_probe_timeout_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    media = tmp_path / "slow.mp4"
    media.write_bytes(b"x")
    _fake_ffprobe(monkeypatch, raise_timeout=True)
    with pytest.raises(ExternalToolError):
        FFprobe().probe(media)
