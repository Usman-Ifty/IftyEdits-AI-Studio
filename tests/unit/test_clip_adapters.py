"""Loudness envelope (numpy, chunked) and FFmpeg scene detection with a fake transcoder."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path

import pytest

from iftyedits.adapters.audio_levels import FLOOR_DB, loudness_envelope
from iftyedits.adapters.scenes import FFmpegSceneDetector, parse_scene_metadata, scene_filter
from iftyedits.config import Settings
from iftyedits.core.clip_models import SceneCut
from iftyedits.core.errors import ClipAnalysisError, JobCancelledError, OriginalProtectionError
from iftyedits.core.models import TranscoderCapabilities
from iftyedits.core.protocols import CancelToken, SecondsCallback
from iftyedits.storage.originals import assert_safe_output_path
from tests.clip_fakes import write_levels_wav
from tests.transcription_fakes import write_wav

# ---------------------------------------------------------------------- loudness


def test_envelope_measures_tone_and_silence(tmp_path: Path) -> None:
    wav = write_levels_wav(tmp_path / "a.wav", [(1.0, 0.5), (1.0, 0.0), (1.05, 0.05)])
    env = loudness_envelope(wav)
    assert env.frame_s == 0.1
    assert len(env.db) == 31  # 30 full frames plus the 0.05 s tail (half a frame)
    # A sine of amplitude 0.5 has an RMS of 0.5/sqrt(2): -9 dBFS.
    assert env.db[3] == pytest.approx(-9.03, abs=0.2)
    assert env.db[15] == FLOOR_DB
    assert env.db[25] == pytest.approx(-29.03, abs=0.3)
    assert env.duration_s == pytest.approx(3.1)
    assert env.window(1.0, 1.5) == env.db[10:16]
    assert env.window(5.0, 4.0) == ()


def test_envelope_spans_chunk_boundaries_and_averages_channels(tmp_path: Path) -> None:
    long_wav = write_levels_wav(tmp_path / "long.wav", [(31.0, 0.5), (1.0, 0.0)])
    env = loudness_envelope(long_wav)
    assert len(env.db) == 320
    assert all(v == pytest.approx(-9.03, abs=0.2) for v in env.db[:310])
    stereo = write_levels_wav(tmp_path / "stereo.wav", [(1.0, 0.5)], channels=2)
    assert loudness_envelope(stereo).db[5] == pytest.approx(-9.03, abs=0.2)


def test_envelope_errors_and_cancellation(tmp_path: Path) -> None:
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not a wav")
    with pytest.raises(ClipAnalysisError, match="unreadable"):
        loudness_envelope(broken)
    with pytest.raises(ClipAnalysisError, match="16-bit"):
        loudness_envelope(write_wav(tmp_path / "8bit.wav", 1.0, width=1))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JobCancelledError):
        loudness_envelope(write_wav(tmp_path / "ok.wav", 1.0), cancel=cancel)
    empty = loudness_envelope(write_wav(tmp_path / "empty.wav", 0.0))
    assert empty.db == ()


# ---------------------------------------------------------------------- scenes

METADATA = """frame:0    pts:51200  pts_time:2.0
lavfi.scene_score=0.812000
frame:1    pts:52000  pts_time:2.2
lavfi.scene_score=0.950000
frame:2    pts:128000 pts_time:5.0
lavfi.scene_score=0.400000
garbage line
"""


def test_parse_scene_metadata_merges_close_cuts() -> None:
    assert parse_scene_metadata(METADATA) == [SceneCut(2.0, 0.95), SceneCut(5.0, 0.4)]
    assert parse_scene_metadata("") == []


def test_scene_filter_quotes_the_path_and_rejects_unsafe_characters(tmp_path: Path) -> None:
    graph = scene_filter(0.3, tmp_path / "with space" / "s.txt")
    assert "select='gt(scene,0.300)'" in graph
    assert f"file='{tmp_path / 'with space' / 's.txt'}'" in graph
    with pytest.raises(ClipAnalysisError):
        scene_filter(0.3, tmp_path / "it's" / "s.txt")


class FakeTranscoder:
    """Writes canned metadata where the filter graph says, instead of running FFmpeg."""

    def __init__(self, text: str | None = METADATA) -> None:
        self.text = text
        self.args: list[str] = []

    def capabilities(self) -> TranscoderCapabilities:
        raise NotImplementedError

    def run(
        self,
        args: Sequence[str],
        *,
        progress: SecondsCallback | None = None,
        cancel: CancelToken | None = None,
        timeout_s: float | None = None,
        redact: Sequence[Path] = (),
    ) -> None:
        self.args = list(args)
        if cancel is not None and cancel.is_set():
            raise JobCancelledError("cancelled")
        if progress is not None:
            progress(5.0)
        graph = args[args.index("-vf") + 1]
        target = Path(graph.split("file='", 1)[1].rstrip("'"))
        if self.text is not None:
            target.write_text(self.text)


def _detector(settings: Settings, transcoder: FakeTranscoder) -> FFmpegSceneDetector:
    def guard(path: Path) -> Path:
        return assert_safe_output_path(
            path, originals_root=settings.originals_dir, allowed_roots=settings.output_roots
        )

    return FFmpegSceneDetector(transcoder, settings.derived_dir / "analysis", guard)


def test_detector_reads_metadata_and_cleans_up(settings: Settings) -> None:
    transcoder = FakeTranscoder()
    seen: list[float] = []
    found = _detector(settings, transcoder).detect(
        Path("/proxy.mp4"), threshold=0.3, duration_s=10.0, progress=seen.append
    )
    assert found == [SceneCut(2.0, 0.95), SceneCut(5.0, 0.4)]
    assert seen == [0.5]
    assert transcoder.args[:2] == ["-i", "/proxy.mp4"]
    assert list((settings.derived_dir / "analysis").iterdir()) == []  # temp folder removed


def test_detector_without_cuts_and_with_cancellation(settings: Settings) -> None:
    assert _detector(settings, FakeTranscoder(None)).detect(Path("/p.mp4"), threshold=0.3) == []
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JobCancelledError):
        _detector(settings, FakeTranscoder()).detect(Path("/p.mp4"), threshold=0.3, cancel=cancel)


def test_detector_refuses_to_write_inside_the_vault(settings: Settings) -> None:
    def guard(path: Path) -> Path:
        return assert_safe_output_path(
            path, originals_root=settings.originals_dir, allowed_roots=settings.output_roots
        )

    detector = FFmpegSceneDetector(FakeTranscoder(), settings.originals_dir / "tmp", guard)
    with pytest.raises(OriginalProtectionError):
        detector.detect(Path("/p.mp4"), threshold=0.3)
    assert not (settings.originals_dir / "tmp").exists()
