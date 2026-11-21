"""Loudness envelope of a prepared 16-bit PCM WAV, read in chunks with numpy.

The file is streamed (one chunk of a few seconds at a time), so long recordings never
sit in memory. Each frame's value is its RMS level in dBFS (0 dBFS = full scale).
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

from iftyedits.core.clip_models import LoudnessEnvelope
from iftyedits.core.errors import ClipAnalysisError, JobCancelledError
from iftyedits.core.protocols import CancelToken

FRAME_S = 0.1
FLOOR_DB = -120.0
_CHUNK_FRAMES = 300
"""Analysis frames read per chunk (30 s of audio at the default frame size)."""


def loudness_envelope(
    path: Path, *, frame_s: float = FRAME_S, cancel: CancelToken | None = None
) -> LoudnessEnvelope:
    """RMS level per ``frame_s`` of a 16-bit WAV (channels are averaged)."""
    try:
        handle = wave.open(str(path), "rb")  # noqa: SIM115 - closed by the with below
    except (wave.Error, EOFError, OSError) as exc:
        raise ClipAnalysisError(f"The prepared audio file is unreadable ({exc})") from exc
    with handle:
        if handle.getsampwidth() != 2:
            raise ClipAnalysisError("The prepared audio is not 16-bit PCM; regenerate the media")
        rate, channels = handle.getframerate(), handle.getnchannels()
        per_frame = max(1, round(rate * frame_s))
        levels: list[float] = []
        carry = np.zeros(0, dtype=np.float64)
        while True:
            if cancel is not None and cancel.is_set():
                raise JobCancelledError("Cancelled while measuring loudness")
            raw = handle.readframes(per_frame * _CHUNK_FRAMES)
            if not raw:
                break
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
            if channels > 1:
                usable = len(samples) - len(samples) % channels
                samples = samples[:usable].reshape(-1, channels).mean(axis=1)
            data = np.concatenate([carry, samples])
            whole = len(data) // per_frame
            if whole:
                blocks = data[: whole * per_frame].reshape(whole, per_frame)
                levels.extend(_db(np.sqrt(np.mean(blocks * blocks, axis=1))))
            carry = data[whole * per_frame :]
        if len(carry) >= per_frame // 2:
            levels.extend(_db(np.sqrt(np.mean(carry * carry, keepdims=True))))
    return LoudnessEnvelope(frame_s=frame_s, db=tuple(levels))


def _db(rms: np.ndarray) -> list[float]:
    with np.errstate(divide="ignore"):
        values = 20.0 * np.log10(rms)
    clipped = np.where(np.isfinite(values), np.maximum(values, FLOOR_DB), FLOOR_DB)
    return [round(float(v), 2) for v in clipped]
