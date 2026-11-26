"""Scene detection and loudness on real media generated with FFmpeg."""

from __future__ import annotations

from pathlib import Path

import pytest

from iftyedits.adapters.audio_levels import loudness_envelope
from iftyedits.adapters.ffmpeg import FFmpeg
from iftyedits.adapters.scenes import FFmpegSceneDetector
from iftyedits.config import Settings
from iftyedits.core.clip_models import SignalKind
from iftyedits.core.signals import audio_signals
from iftyedits.storage.originals import assert_safe_output_path
from tests.integration.conftest import run_ffmpeg

pytestmark = pytest.mark.ffmpeg


def test_scene_cut_between_two_colours_is_found(
    ffmpeg: str, settings: Settings, tmp_path: Path
) -> None:
    video = tmp_path / "cuts.mp4"
    run_ffmpeg(
        ffmpeg,
        "-f", "lavfi", "-i", "color=c=red:s=320x180:r=30:d=2",
        "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30:d=2",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(video),
    )  # fmt: skip

    def guard(path: Path) -> Path:
        return assert_safe_output_path(
            path, originals_root=settings.originals_dir, allowed_roots=settings.output_roots
        )

    detector = FFmpegSceneDetector(FFmpeg(ffmpeg), settings.derived_dir / "analysis", guard)
    found = detector.detect(video, threshold=0.3, duration_s=4.0)
    assert len(found) == 1
    assert found[0].time_s == pytest.approx(2.0, abs=0.15)
    assert found[0].score > 0.3


def test_loud_burst_in_real_audio_is_a_volume_rise(ffmpeg: str, tmp_path: Path) -> None:
    wav = tmp_path / "burst.wav"
    run_ffmpeg(
        ffmpeg,
        "-f", "lavfi", "-i",
        "aevalsrc='if(between(t,12,13),0.9,0.03)*sin(2*PI*300*t)':s=16000:d=16",
        "-ac", "1", "-c:a", "pcm_s16le", str(wav),
    )  # fmt: skip
    peaks = [s for s in audio_signals(loudness_envelope(wav)) if s.kind is SignalKind.LOUDNESS_PEAK]
    assert len(peaks) == 1
    assert peaks[0].start_s == pytest.approx(12.0, abs=0.5)
