"""faster-whisper adapter against a stand-in ``faster_whisper`` module (no model, no inference)."""

from __future__ import annotations

import sys
import threading
import time
import types
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from iftyedits.adapters import faster_whisper as adapter
from iftyedits.core.errors import (
    JobCancelledError,
    TranscriptionEngineUnavailableError,
    TranscriptionError,
    TranscriptionTimeoutError,
)
from iftyedits.core.models import TranscriptionOptions, TranscriptSegment
from tests.transcription_fakes import write_wav

OPTIONS = TranscriptionOptions(model="small", language=None)


@dataclass
class RawWord:
    start: float
    end: float
    word: str
    probability: float | None


@dataclass
class RawSegment:
    start: float
    end: float
    text: str
    words: list[RawWord] | None
    avg_logprob: float | None = -0.3
    no_speech_prob: float | None = 0.02


@dataclass
class RawInfo:
    language: str = "en"
    language_probability: float = 0.91
    duration: float = 3.0


@dataclass
class FakeWhisperModel:
    path: str
    kwargs: dict[str, Any]
    segments: list[RawSegment] = field(default_factory=list)
    fail_during: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)
    heard: str = "en"
    """The language reported when none is asked for."""

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Iterator[RawSegment], RawInfo]:
        self.calls.append({"audio": audio, **kwargs})

        def generate() -> Iterator[RawSegment]:
            yield from self.segments
            if self.fail_during:
                raise RuntimeError("decoder exploded")

        return generate(), RawInfo(language=kwargs.get("language") or self.heard)


@pytest.fixture
def fake_engine(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    loaded: list[FakeWhisperModel] = []
    state = types.SimpleNamespace(
        loaded=loaded, load_error=None, segments=[], fail_during=False, heard="en"
    )

    def whisper_model(path: str, **kwargs: Any) -> FakeWhisperModel:
        if state.load_error is not None:
            raise state.load_error
        model = FakeWhisperModel(
            path, kwargs, list(state.segments), state.fail_during, heard=state.heard
        )
        loaded.append(model)
        return model

    module = types.ModuleType("faster_whisper")
    module.WhisperModel = whisper_model  # type: ignore[attr-defined]
    module.__version__ = "1.2.1-fake"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return state


def _segments() -> list[RawSegment]:
    words = [RawWord(0.0, 0.5, " Hi", 0.9), RawWord(0.5, 1.2, " there.", None)]
    return [
        RawSegment(0.0, 1.2, " Hi there.", words),
        RawSegment(1.5, 2.9, " No words.", None, None, None),
    ]


def test_transcribe_converts_output_and_reports_segments(
    fake_engine: types.SimpleNamespace, tmp_path: Path
) -> None:
    fake_engine.segments = _segments()
    seen: list[tuple[TranscriptSegment, float | None]] = []
    engine = adapter.FasterWhisperTranscriber()
    out = engine.transcribe(
        write_wav(tmp_path / "a.wav", 1.0),
        tmp_path / "small",
        OPTIONS,
        on_segment=lambda s, d: seen.append((s, d)),
    )
    assert (out.engine, out.engine_version, out.device) == ("faster-whisper", "1.2.1-fake", "cpu")
    assert (out.language, out.language_probability, out.duration_s) == ("en", 0.91, 3.0)
    first, second = out.segments
    assert first.text == " Hi there." and first.words[1].probability is None
    assert second.words == () and second.avg_logprob is None
    assert [d for _, d in seen] == [3.0, 3.0]
    model = fake_engine.loaded[0]
    assert model.kwargs == {
        "device": "cpu",
        "compute_type": "int8",
        "cpu_threads": 0,
        "local_files_only": True,
    }
    call = model.calls[0]
    samples = call["audio"]
    assert str(samples.dtype) == "float32" and samples.shape == (16_000,)
    call = model.calls[0]
    assert call["language"] is None and call["word_timestamps"] and call["vad_filter"]
    assert call["task"] == "transcribe" and call["beam_size"] == 5


@pytest.mark.parametrize(
    ("heard", "asked", "calls"),
    [("ur", None, [None, "hi"]), ("en", "ur", ["hi"]), ("hi", None, [None])],
)
def test_urdu_speech_is_written_as_hindi(
    fake_engine: types.SimpleNamespace,
    tmp_path: Path,
    heard: str,
    asked: str | None,
    calls: list[str | None],
) -> None:
    fake_engine.heard = heard
    fake_engine.segments = _segments()
    out = adapter.FasterWhisperTranscriber().transcribe(
        write_wav(tmp_path / "a.wav", 1.0), tmp_path / "small", replace(OPTIONS, language=asked)
    )
    assert out.language == "hi" and len(out.segments) == 2
    assert [c["language"] for c in fake_engine.loaded[0].calls] == calls


def test_model_is_cached_and_replaced_when_settings_change(
    fake_engine: types.SimpleNamespace, tmp_path: Path
) -> None:
    engine = adapter.FasterWhisperTranscriber()
    engine.transcribe(write_wav(tmp_path / "a.wav", 1.0), tmp_path / "small", OPTIONS)
    engine.transcribe(write_wav(tmp_path / "b.wav", 0.5), tmp_path / "small", OPTIONS)
    assert len(fake_engine.loaded) == 1
    engine.transcribe(write_wav(tmp_path / "a.wav", 1.0), tmp_path / "tiny", OPTIONS)
    assert len(fake_engine.loaded) == 2


def test_load_failure_is_explained(fake_engine: types.SimpleNamespace, tmp_path: Path) -> None:
    fake_engine.load_error = RuntimeError("Unable to open file 'model.bin'")
    with pytest.raises(TranscriptionError, match="damaged; download the model again"):
        adapter.FasterWhisperTranscriber().transcribe(
            write_wav(tmp_path / "a.wav", 1.0), tmp_path / "small", OPTIONS
        )


def test_bad_compute_type_is_refused(fake_engine: types.SimpleNamespace, tmp_path: Path) -> None:
    options = TranscriptionOptions(model="small", compute_type="float16")
    with pytest.raises(TranscriptionError, match="Unsupported compute type"):
        adapter.FasterWhisperTranscriber().transcribe(
            write_wav(tmp_path / "a.wav", 1.0), tmp_path / "m", options
        )


def test_inference_failure_is_wrapped(fake_engine: types.SimpleNamespace, tmp_path: Path) -> None:
    fake_engine.segments = _segments()
    fake_engine.fail_during = True
    with pytest.raises(TranscriptionError, match="RuntimeError: decoder exploded"):
        adapter.FasterWhisperTranscriber().transcribe(
            write_wav(tmp_path / "a.wav", 1.0), tmp_path / "m", OPTIONS
        )


def test_cancellation_between_segments(fake_engine: types.SimpleNamespace, tmp_path: Path) -> None:
    fake_engine.segments = _segments()
    cancel = threading.Event()
    reported: list[float] = []

    def on_segment(segment: TranscriptSegment, duration: float | None) -> None:
        reported.append(segment.start_s)
        cancel.set()

    with pytest.raises(JobCancelledError):
        adapter.FasterWhisperTranscriber().transcribe(
            write_wav(tmp_path / "a.wav", 1.0),
            tmp_path / "m",
            OPTIONS,
            on_segment=on_segment,
            cancel=cancel,
        )
    assert reported == [0.0]  # stopped before the second segment


def test_deadline(fake_engine: types.SimpleNamespace, tmp_path: Path) -> None:
    fake_engine.segments = _segments()
    with pytest.raises(TranscriptionTimeoutError, match="IFTYEDITS_TRANSCRIPTION_TIMEOUT_S"):
        adapter.FasterWhisperTranscriber().transcribe(
            write_wav(tmp_path / "a.wav", 1.0),
            tmp_path / "m",
            OPTIONS,
            deadline=time.monotonic() - 1,
        )


def test_missing_package_is_explained(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    assert adapter.engine_version() is None
    with pytest.raises(TranscriptionEngineUnavailableError, match="uv sync"):
        adapter.FasterWhisperTranscriber().transcribe(
            write_wav(tmp_path / "a.wav", 1.0), tmp_path / "m", OPTIONS
        )


def test_real_engine_api_matches_adapter() -> None:
    """When faster-whisper is installed, the names the adapter relies on exist."""
    module = pytest.importorskip("faster_whisper")
    import inspect

    init = inspect.signature(module.WhisperModel.__init__).parameters
    assert {"device", "compute_type", "cpu_threads", "local_files_only"} <= set(init)
    transcribe = inspect.signature(module.WhisperModel.transcribe).parameters
    assert {"language", "task", "beam_size", "word_timestamps", "vad_filter"} <= set(transcribe)
    from faster_whisper.transcribe import Segment, Word

    assert {"start", "end", "text", "words", "avg_logprob", "no_speech_prob"} <= set(
        Segment.__dataclass_fields__
        if hasattr(Segment, "__dataclass_fields__")
        else Segment._fields
    )
    assert {"start", "end", "word", "probability"} <= set(
        Word.__dataclass_fields__ if hasattr(Word, "__dataclass_fields__") else Word._fields
    )
    assert "en" in adapter.supported_languages()


def test_load_wav_values_and_format_checks(tmp_path: Path) -> None:
    import wave

    import numpy as np

    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16_000)
        handle.writeframes(np.array([0, 16384, -32768, 32767], dtype="<i2").tobytes())
    audio = adapter.load_wav(path)
    assert audio.dtype == np.float32
    assert audio.tolist() == [0.0, 0.5, -1.0, 32767 / 32768]
    with pytest.raises(TranscriptionError, match="16 kHz mono"):
        adapter.load_wav(write_wav(tmp_path / "stereo.wav", 0.1, channels=2))


def test_unreadable_audio_is_explained(fake_engine: types.SimpleNamespace, tmp_path: Path) -> None:
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio")
    with pytest.raises(TranscriptionError, match="Could not read the prepared audio"):
        adapter.FasterWhisperTranscriber().transcribe(bad, tmp_path / "m", OPTIONS)
