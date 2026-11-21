"""Deterministic test data for clip intelligence: scripted speech, tones and scene cuts.

Nothing here runs a model or FFmpeg. Word timings are spread evenly over each line.
"""

from __future__ import annotations

import math
import struct
import wave
from collections.abc import Sequence
from pathlib import Path

from iftyedits.core.clip_models import SceneCut
from iftyedits.core.models import TranscriptSegment, TranscriptWord
from iftyedits.storage.clips import NewAnalysis


def line_segment(
    start: float,
    text: str,
    *,
    wps: float = 2.8,
    probability: float | None = 0.92,
    with_words: bool = True,
) -> TranscriptSegment:
    """One segment whose words are spoken at ``wps`` words per second from ``start``."""
    words = text.split()
    step = 1.0 / wps
    timed = tuple(
        TranscriptWord(
            round(start + i * step, 3),
            round(start + (i + 1) * step - 0.02, 3),
            f" {w}",
            probability,
        )
        for i, w in enumerate(words)
    )
    end = round(start + len(words) * step, 3)
    return TranscriptSegment(start, end, f" {text}", timed if with_words else (), -0.15, 0.02)


def script(
    lines: Sequence[str],
    *,
    start: float = 0.5,
    gap: float = 0.35,
    pauses: dict[int, float] | None = None,
    wps: float = 2.8,
    with_words: bool = True,
    probability: float | None = 0.92,
) -> tuple[TranscriptSegment, ...]:
    """Segments for ``lines`` with ``gap`` seconds between them (``pauses`` overrides)."""
    segments = []
    cursor = start
    for i, text in enumerate(lines):
        seg = line_segment(cursor, text, wps=wps, probability=probability, with_words=with_words)
        segments.append(seg)
        cursor = seg.end_s + (pauses or {}).get(i, gap)
    return tuple(segments)


PODCAST = (
    "Welcome back to the show, today we have a special guest with us.",
    "So tell me, how did you grow your channel from zero to a million subscribers?",
    "Honestly it was not easy at all and it took me years of work.",
    "The key is consistency, you need to upload every single week no matter what.",
    "I remember one time I almost quit because nothing was working for me.",
    "I was so scared that I had wasted three years of my life on this dream.",
    "But then one video went viral overnight and everything changed for me.",
    "That was the moment I realised that hard work always pays off eventually.",
    "Haha, my mom still does not understand what I do for a living.",
    "She thinks I just play games all day, which is honestly kind of true.",
    "What is the biggest mistake new creators make on their first videos?",
    "They try to copy other people instead of finding their own voice.",
    "You should make the video that only you can make, that is the secret.",
    "And the second mistake is ignoring the first three seconds of the video.",
    "If the hook is boring, viewers scroll away and never come back.",
    "Let's switch gears and talk about gaming for a minute now.",
    "Here's the craziest thing that happened during my last tournament stream.",
    "We were down two rounds and the whole team was tilted and angry.",
    "Then out of nowhere our sniper hit three headshots in a row.",
    "No way, I could not believe it, the chat went absolutely insane!",
    "We won the match and the clip got five million views the next day.",
    "Wait, what?",
    "Five million?",
    "Yeah, five million!",
    "Are you serious?",
    "Completely serious, it was unbelievable.",
    "Turns out the editor posted it as a short before I even woke up.",
    "Believe it or not, that one short brought more subscribers than a whole year.",
    "So my advice is simple, clip your best moments and post them fast.",
    "Make sure the payoff lands before the clip ends, that is really important.",
    "Thanks so much for coming on, this was an amazing conversation today.",
)


def podcast(**kwargs: object) -> tuple[TranscriptSegment, ...]:
    """A scripted two-topic podcast (advice, then a gaming story) of about 3.5 minutes."""
    return script(PODCAST, pauses={14: 2.2, 25: 1.4}, **kwargs)  # type: ignore[arg-type]


def write_levels_wav(
    path: Path,
    pattern: Sequence[tuple[float, float]],
    *,
    rate: int = 16_000,
    channels: int = 1,
    frequency: float = 220.0,
) -> Path:
    """WAV of consecutive ``(seconds, amplitude 0..1)`` sine sections (amplitude 0 = silence)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    t = 0
    for seconds, amplitude in pattern:
        for _ in range(int(seconds * rate)):
            value = int(amplitude * 32767 * math.sin(2 * math.pi * frequency * t / rate))
            frames += struct.pack("<h", value) * channels
            t += 1
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return path


def cuts(*times: float, score: float = 0.6) -> tuple[SceneCut, ...]:
    """Scene cuts at ``times``."""
    return tuple(SceneCut(t, score) for t in times)


def new_analysis(
    video_sha: str,
    video_id: int,
    transcript_id: int,
    *,
    params_hash: str = "h1",
    segments: Sequence[TranscriptSegment] | None = None,
    count: int = 4,
) -> NewAnalysis:
    """A real analysis (signals, scores, ranking) of a scripted podcast, ready to store."""
    from iftyedits.core.candidates import CandidateConfig, generate_candidates
    from iftyedits.core.scoring import (
        SCORING_VERSION,
        RankingConfig,
        candidate_key,
        rank_candidates,
        score_candidates,
    )
    from iftyedits.core.signals import SIGNALS_VERSION, build_evidence

    segs = tuple(segments) if segments is not None else podcast()
    evidence = build_evidence(
        segs, duration_s=segs[-1].end_s + 2, language="en", loudness=None, scene_cuts=None
    )
    config = CandidateConfig()
    scored = score_candidates(evidence, generate_candidates(evidence, config), config)
    kept, _ = rank_candidates(scored, RankingConfig(max_count=count))
    return NewAnalysis(
        video_id=video_id,
        transcript_id=transcript_id,
        analysis_version=SIGNALS_VERSION,
        scoring_version=SCORING_VERSION,
        params_hash=params_hash,
        params={"p": params_hash},
        duration_s=evidence.duration_s,
        sources={"transcript": {"used": True}},
        stats={"best_score": kept[0].score if kept else None},
        signals=evidence.signals,
        candidates=[(candidate_key(video_sha, c.start_s, c.end_s), c) for c in kept],
        processing_s=0.5,
    )
