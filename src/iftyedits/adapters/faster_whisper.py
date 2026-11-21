"""faster-whisper (CTranslate2) speech recognition. Implements ``SpeechTranscriber``.

The ``faster_whisper`` package is imported only when a transcription runs, so the rest
of the application works (and reports a clear error) without it. Inference runs on the
CPU with int8 weights by default. CTranslate2 4.9.0's experimental Apple Metal (``mps``)
device was measured slower than CPU int8 on this hardware, so it is not used (docs/DECISIONS.md
D15 and D16).

Audio is decoded here, not by faster-whisper: the prepared 16 kHz mono 16-bit WAV is read
with the standard ``wave`` module in chunks into one float32 array (the format
faster-whisper's own decoder produces). faster-whisper 1.2.1's PyAV-based decoder calls
``av.open(metadata_errors=...)``, which PyAV 19 rejects, and decoding ourselves also avoids
a second, different resampler. Memory: 4 bytes per sample, about 230 MB per hour of audio.

The loaded model is kept for the life of the worker process, so consecutive jobs with
the same model and settings do not pay the load time again. Only one model is held at
a time.
"""

from __future__ import annotations

import logging
import time
import wave
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from iftyedits.core.errors import (
    JobCancelledError,
    TranscriptionEngineUnavailableError,
    TranscriptionError,
    TranscriptionTimeoutError,
)
from iftyedits.core.models import (
    EngineTranscription,
    TranscriptionOptions,
    TranscriptSegment,
    TranscriptWord,
)
from iftyedits.core.protocols import CancelToken, SegmentCallback

logger = logging.getLogger(__name__)

ENGINE_NAME = "faster-whisper"
DEVICE = "cpu"
COMPUTE_TYPES = ("int8", "int8_float32", "float32")
SAMPLE_RATE = 16_000
_CHUNK_FRAMES = 1_048_576


def load_wav(path: Path) -> Any:
    """16 kHz mono 16-bit WAV as a float32 numpy array in [-1, 1), read in chunks."""
    import numpy as np  # installed with faster-whisper; loaded on demand

    with wave.open(str(path), "rb") as handle:
        rate, channels, width = (
            handle.getframerate(),
            handle.getnchannels(),
            handle.getsampwidth(),
        )
        if (rate, channels, width) != (SAMPLE_RATE, 1, 2):
            raise TranscriptionError(
                f"Audio must be 16 kHz mono 16-bit WAV, got {rate} Hz, {channels} channel(s), "
                f"{width * 8}-bit"
            )
        total = handle.getnframes()
        audio = np.empty(total, dtype=np.float32)
        done = 0
        while done < total:
            chunk = handle.readframes(min(_CHUNK_FRAMES, total - done))
            if not chunk:
                break
            samples = np.frombuffer(chunk, dtype="<i2")
            audio[done : done + samples.size] = samples
            done += samples.size
    audio = audio[:done]
    audio /= 32768.0
    return audio


def _engine_module() -> Any:
    try:
        import faster_whisper
    except ImportError as exc:
        raise TranscriptionEngineUnavailableError(
            "The transcription engine (faster-whisper) is not installed. "
            "Run `uv sync` in the project folder, then restart the worker."
        ) from exc
    return faster_whisper


def engine_version() -> str | None:
    """Installed faster-whisper version, or None if it is not installed."""
    try:
        return str(_engine_module().__version__)
    except TranscriptionEngineUnavailableError:
        return None


def supported_languages() -> tuple[str, ...]:
    """Language codes the installed engine accepts."""
    from faster_whisper.tokenizer import _LANGUAGE_CODES

    return tuple(_LANGUAGE_CODES)


def _word(raw: Any) -> TranscriptWord:
    return TranscriptWord(
        start_s=float(raw.start),
        end_s=float(raw.end),
        text=str(raw.word),
        probability=float(raw.probability) if raw.probability is not None else None,
    )


def convert_segment(raw: Any) -> TranscriptSegment:
    """Engine segment (``faster_whisper.transcribe.Segment``) to the domain model."""
    words: Iterable[Any] = raw.words or ()
    return TranscriptSegment(
        start_s=float(raw.start),
        end_s=float(raw.end),
        text=str(raw.text),
        words=tuple(_word(w) for w in words),
        avg_logprob=float(raw.avg_logprob) if raw.avg_logprob is not None else None,
        no_speech_prob=float(raw.no_speech_prob) if raw.no_speech_prob is not None else None,
    )


class FasterWhisperTranscriber:
    """Runs faster-whisper on the CPU with a cached model."""

    engine = ENGINE_NAME

    def __init__(self) -> None:
        self._model: Any = None
        self._key: tuple[str, str, int] | None = None

    def _load(self, model_dir: Path, options: TranscriptionOptions) -> Any:
        key = (str(model_dir), options.compute_type, options.cpu_threads)
        if self._model is not None and self._key == key:
            return self._model
        if options.compute_type not in COMPUTE_TYPES:
            raise TranscriptionError(
                f"Unsupported compute type {options.compute_type!r}; "
                f"use one of {', '.join(COMPUTE_TYPES)}"
            )
        module = _engine_module()
        self._model = None  # release the previous model before loading another
        self._key = None
        started = time.monotonic()
        try:
            model = module.WhisperModel(
                str(model_dir),
                device=DEVICE,
                compute_type=options.compute_type,
                cpu_threads=options.cpu_threads,
                local_files_only=True,
            )
        except Exception as exc:
            raise TranscriptionError(
                f"Could not load speech model {model_dir.name}: {type(exc).__name__}: {exc}. "
                "The model files may be damaged; download the model again."
            ) from exc
        logger.info(
            "Loaded speech model %s (%s, %s threads) in %.1fs",
            model_dir.name,
            options.compute_type,
            options.cpu_threads or "default",
            time.monotonic() - started,
        )
        self._model, self._key = model, key
        return model

    def transcribe(
        self,
        audio: Path,
        model_dir: Path,
        options: TranscriptionOptions,
        *,
        on_segment: SegmentCallback | None = None,
        cancel: CancelToken | None = None,
        deadline: float | None = None,
    ) -> EngineTranscription:
        """Transcribe a WAV file; segments are reported as they are recognised."""
        model = self._load(model_dir, options)
        module = _engine_module()

        def check() -> None:
            if cancel is not None and cancel.is_set():
                raise JobCancelledError("Transcription cancelled")
            if deadline is not None and time.monotonic() > deadline:
                raise TranscriptionTimeoutError(
                    "Transcription took longer than the configured limit "
                    "(IFTYEDITS_TRANSCRIPTION_TIMEOUT_S); try a smaller model"
                )

        check()
        segments: list[TranscriptSegment] = []
        try:
            samples = load_wav(audio)
        except (wave.Error, EOFError, OSError) as exc:
            raise TranscriptionError(f"Could not read the prepared audio: {exc}") from exc
        try:
            raw_segments, info = model.transcribe(
                samples,
                language=options.language,
                task="transcribe",
                beam_size=options.beam_size,
                word_timestamps=options.word_timestamps,
                vad_filter=options.vad_filter,
            )
            duration = float(info.duration) if info.duration is not None else None
            for raw in raw_segments:
                check()
                segment = convert_segment(raw)
                segments.append(segment)
                if on_segment is not None:
                    on_segment(segment, duration)
            check()
        except (JobCancelledError, TranscriptionError):
            raise
        except Exception as exc:
            raise TranscriptionError(
                f"Speech recognition failed: {type(exc).__name__}: {exc}"
            ) from exc
        probability = info.language_probability
        return EngineTranscription(
            engine=ENGINE_NAME,
            engine_version=str(module.__version__),
            device=DEVICE,
            compute_type=options.compute_type,
            language=info.language,
            language_probability=float(probability) if probability is not None else None,
            duration_s=duration,
            segments=tuple(segments),
        )
