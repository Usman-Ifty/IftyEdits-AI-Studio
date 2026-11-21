"""Clip intelligence for one imported video: signals, candidates, scores, ranking.

Steps for one video:

1. Check the video exists, its original is still in the vault, and it has a current
   transcript (never transcribed here; the transcript and the prepared audio and proxy
   are only read).
2. Measure loudness from the prepared audio and scene changes from the proxy. A missing,
   damaged or failing source is skipped and recorded honestly; the analysis still runs on
   what exists.
3. Build content signals, propose clips, score them (deterministic, algorithmic) and rank.
4. If an analysis with identical inputs and settings is stored, reuse it unless ``force``.
5. Store signals and suggestions in one transaction, after checking the job is still
   owned by this worker. A failed or cancelled run stores nothing.

User decisions live apart from analyses and are applied to the ranking as feedback.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from iftyedits.adapters.audio_levels import loudness_envelope
from iftyedits.config import Settings
from iftyedits.core.candidates import CANDIDATES_VERSION, CandidateConfig, generate_candidates
from iftyedits.core.clip_models import ClipAnalysis, Evidence, LoudnessEnvelope, SceneCut
from iftyedits.core.errors import (
    ClipAnalysisError,
    ExternalToolError,
    InvalidSourceError,
    JobCancelledError,
    OriginalProtectionError,
)
from iftyedits.core.models import Artifact, ArtifactKind, Video
from iftyedits.core.protocols import CancelToken, SceneDetector
from iftyedits.core.scoring import (
    SCORING_VERSION,
    Feedback,
    RankingConfig,
    ScoringWeights,
    candidate_key,
    evidence_summary,
    rank_candidates,
    score_candidates,
)
from iftyedits.core.signals import SIGNALS_VERSION, build_evidence
from iftyedits.storage.clips import ClipRepository, NewAnalysis
from iftyedits.storage.processing import ProcessingRepository
from iftyedits.storage.transcripts import TranscriptRepository
from iftyedits.storage.videos import VideoRepository

logger = logging.getLogger(__name__)

Progress = Callable[[float, str], None]
ArtifactResolver = Callable[[Artifact], Path]
SceneDetectorFactory = Callable[[], SceneDetector | None]

LOUDNESS_SHARE = (0.05, 0.15)
SCENE_SHARE = (0.15, 0.70)
"""Parts of the progress bar used by the loudness and scene steps."""


@dataclass(frozen=True, slots=True)
class ClipOptions:
    """Everything that shapes an analysis (all of it is part of the stored parameters)."""

    candidates: CandidateConfig = field(default_factory=CandidateConfig)
    ranking: RankingConfig = field(default_factory=RankingConfig)
    weights: ScoringWeights = field(default_factory=ScoringWeights)
    scene_threshold: float = 0.3
    use_audio: bool = True
    use_scenes: bool = True


def options_from_settings(settings: Settings) -> ClipOptions:
    """Analysis options from ``IFTYEDITS_CLIP_*`` settings."""
    return ClipOptions(
        candidates=CandidateConfig(
            min_s=settings.clip_min_s,
            target_s=settings.clip_target_s,
            max_s=settings.clip_max_s,
        ),
        ranking=RankingConfig(
            max_count=settings.clip_max_candidates, max_overlap=settings.clip_max_overlap
        ),
        scene_threshold=settings.clip_scene_threshold,
        use_audio=settings.clip_use_audio,
        use_scenes=settings.clip_use_scenes,
    )


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=list)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def feedback_fingerprint(feedback: Feedback) -> str:
    """Stable hash of the user decisions that influence a ranking."""
    return _fingerprint(
        [feedback.favorite_spans, feedback.rejected_spans, sorted(feedback.category_bias.items())]
    )


@dataclass(frozen=True, slots=True)
class ClipReport:
    """What one analysis run produced."""

    video_id: int
    analysis: ClipAnalysis
    reused: bool

    def as_result(self) -> dict[str, Any]:
        """JSON-serialisable job result."""
        a = self.analysis
        return {
            "video_id": self.video_id,
            "analysis_id": a.id,
            "signals": a.signal_count,
            "candidates": a.candidate_count,
            "reused": self.reused,
            "processing_s": a.processing_s,
            "scoring_version": a.scoring_version,
        }

    def summary(self) -> str:
        """One-line description for the job message."""
        a = self.analysis
        prefix = "Reused existing analysis: " if self.reused else ""
        if a.candidate_count == 0:
            note = a.stats.get("note") or "no suitable moments found"
            return f"{prefix}No clip suggestions ({note})"
        best = a.stats.get("best_score")
        best_text = f", best score {best:.0f}" if isinstance(best, int | float) else ""
        count = f"{a.candidate_count} clip suggestions from {a.signal_count} signals"
        return f"{prefix}{count}{best_text}"


def _degraded(analysis: ClipAnalysis) -> bool:
    """True if an evidence source failed during the run (so a retry may do better)."""
    return any(isinstance(v, dict) and v.get("failed") for v in analysis.sources.values())


def _check_cancel(cancel: CancelToken | None) -> None:
    if cancel is not None and cancel.is_set():
        raise JobCancelledError("Clip analysis cancelled; nothing was stored")


class ClipAnalysisService:
    """Analyses videos and stores ranked clip suggestions."""

    def __init__(
        self,
        settings: Settings,
        videos: VideoRepository,
        processing: ProcessingRepository,
        transcripts: TranscriptRepository,
        clips: ClipRepository,
        resolve_artifact: ArtifactResolver,
        scene_detector: SceneDetectorFactory,
    ) -> None:
        self.settings = settings
        self.videos = videos
        self.processing = processing
        self.transcripts = transcripts
        self.clips = clips
        self._resolve = resolve_artifact
        self._scene_detector = scene_detector

    def check(self, video_id: int) -> Video:
        """Raise a clear error if this video cannot be analysed right now."""
        video = self.videos.get(video_id)
        if video is None:
            raise InvalidSourceError(f"No video with id {video_id}")
        transcript = self.transcripts.current(video_id)
        if transcript is None:
            raise ClipAnalysisError(
                "This video has no transcript yet. Transcribe it first (Transcripts page or "
                f"`uv run iftyedits transcribe {video_id}`), then analyse it."
            )
        original = self.settings.originals_dir / video.vault_relpath
        if not original.is_file():
            raise ClipAnalysisError(
                f"The original for video {video_id} is missing from the vault "
                f"({video.vault_relpath}). Run `uv run iftyedits reconcile` and check the vault."
            )
        return video

    def analyze(
        self,
        video_id: int,
        options: ClipOptions | None = None,
        *,
        force: bool = False,
        job_id: int | None = None,
        worker_id: str | None = None,
        progress: Progress | None = None,
        cancel: CancelToken | None = None,
    ) -> ClipReport:
        """Analyse a video, store and return the result."""
        options = options or options_from_settings(self.settings)
        started = time.monotonic()

        def report(fraction: float, message: str) -> None:
            if progress is not None:
                progress(min(1.0, max(0.0, fraction)), message)

        video = self.check(video_id)
        transcript = self.transcripts.current(video_id)
        if transcript is None:  # check() guarantees it; keeps mypy and races honest
            raise ClipAnalysisError("The transcript disappeared during analysis")
        _check_cancel(cancel)
        report(0.02, "Reading transcript")
        segments = self.transcripts.segments(transcript.id, with_words=True)
        duration = max(
            video.duration_s or 0.0,
            transcript.duration_s or 0.0,
            segments[-1].end_s if segments else 0.0,
        )
        sources: dict[str, Any] = {
            "transcript": {
                "used": True,
                "transcript_id": transcript.id,
                "language": transcript.language,
                "segments": transcript.segment_count,
                "words": transcript.word_count,
            },
            "speakers": {"used": False, "note": "Speaker separation is not analysed"},
            "faces": {"used": False, "note": "Faces and visual events are not analysed"},
        }

        audio = self._artifact_file(video.id, ArtifactKind.AUDIO_WAV) if options.use_audio else None
        proxy = (
            self._artifact_file(video.id, ArtifactKind.PROXY_MP4) if options.use_scenes else None
        )
        audio_sha = audio[0].sha256 if isinstance(audio, tuple) else None
        proxy_sha = proxy[0].sha256 if isinstance(proxy, tuple) else None
        _check_cancel(cancel)

        feedback = self.clips.feedback(video_id)
        params: dict[str, Any] = {
            "signals_version": SIGNALS_VERSION,
            "candidates_version": CANDIDATES_VERSION,
            "scoring_version": SCORING_VERSION,
            "transcript_id": transcript.id,
            "transcript_params": transcript.params_hash,
            "audio_sha256": audio_sha,
            "proxy_sha256": proxy_sha,
            "scene_threshold": options.scene_threshold,
            "min_s": options.candidates.min_s,
            "target_s": options.candidates.target_s,
            "max_s": options.candidates.max_s,
            "max_candidates": options.ranking.max_count,
            "max_overlap": options.ranking.max_overlap,
            "weights": options.weights.as_dict(),
            "feedback": feedback_fingerprint(feedback),
        }
        phash = _fingerprint(params)
        if not force:
            existing = self.clips.find(video_id, phash)
            if existing is not None and not _degraded(existing):
                report(0.99, "Reusing the stored analysis")
                current = self.clips.make_current(existing.id, job_id=job_id, worker_id=worker_id)
                report(1.0, "Done")
                return ClipReport(video_id, current, reused=True)

        loudness = self._loudness(video, options, audio, sources, report, cancel)
        _check_cancel(cancel)
        scene_cuts = self._scenes(options, proxy, duration, sources, report, cancel)
        _check_cancel(cancel)
        report(SCENE_SHARE[1] + 0.02, "Finding signals")
        evidence = build_evidence(
            segments,
            duration_s=duration,
            language=transcript.language,
            loudness=loudness,
            scene_cuts=scene_cuts,
        )
        _check_cancel(cancel)
        report(0.80, "Proposing clips")
        drafts = generate_candidates(evidence, options.candidates)
        _check_cancel(cancel)
        report(0.88, f"Scoring {len(drafts)} proposals")
        scored = score_candidates(evidence, drafts, options.candidates, options.weights, feedback)
        ranked, dropped = rank_candidates(scored, options.ranking)
        _check_cancel(cancel)

        stats = self._stats(evidence, len(drafts), dropped, ranked)
        if not evidence.sentences:
            stats["note"] = "the transcript has no speech to analyse"
        elif not ranked:
            stats["note"] = "the speech was too short to make a clip"
        report(0.97, "Saving suggestions")
        analysis = self.clips.save(
            NewAnalysis(
                video_id=video_id,
                transcript_id=transcript.id,
                analysis_version=SIGNALS_VERSION,
                scoring_version=SCORING_VERSION,
                params_hash=phash,
                params=params,
                duration_s=duration,
                sources=sources,
                stats=stats,
                signals=evidence.signals,
                candidates=[(candidate_key(video.sha256, c.start_s, c.end_s), c) for c in ranked],
                processing_s=round(time.monotonic() - started, 3),
            ),
            job_id=job_id,
            worker_id=worker_id,
        )
        report(1.0, "Done")
        return ClipReport(video_id, analysis, reused=False)

    # ------------------------------------------------------------------ evidence sources

    def _artifact_file(self, video_id: int, kind: ArtifactKind) -> tuple[Artifact, Path] | str:
        """The intact artifact file, or the reason it cannot be used."""
        artifact = self.processing.artifact(video_id, kind)
        if artifact is None:
            return "not prepared yet (run Prepare media)"
        try:
            path = self._resolve(artifact)
        except OriginalProtectionError:
            return "its registered path is outside the output folders"
        if path.is_symlink() or not path.is_file():
            return "the prepared file is missing"
        if path.stat().st_size != artifact.size_bytes:
            return "the prepared file does not match its record"
        return artifact, path

    @staticmethod
    def _loudness(
        video: Video,
        options: ClipOptions,
        found: tuple[Artifact, Path] | str | None,
        sources: dict[str, Any],
        report: Progress,
        cancel: CancelToken | None,
    ) -> LoudnessEnvelope | None:
        if not options.use_audio or found is None:
            sources["audio"] = {"used": False, "note": "Disabled in settings"}
            return None
        if isinstance(found, str):
            sources["audio"] = {"used": False, "note": f"Loudness skipped: {found}"}
            return None
        _, path = found
        report(LOUDNESS_SHARE[0], "Measuring loudness")
        try:
            envelope = loudness_envelope(path, cancel=cancel)
        except ClipAnalysisError as exc:
            logger.warning("Loudness skipped for video %d: %s", video.id, exc)
            sources["audio"] = {"used": False, "failed": True, "note": f"Loudness skipped: {exc}"}
            return None
        report(LOUDNESS_SHARE[1], "Loudness measured")
        sources["audio"] = {"used": True, "frames": len(envelope.db), "frame_s": envelope.frame_s}
        return envelope

    def _scenes(
        self,
        options: ClipOptions,
        found: tuple[Artifact, Path] | str | None,
        duration: float,
        sources: dict[str, Any],
        report: Progress,
        cancel: CancelToken | None,
    ) -> list[SceneCut] | None:
        if not options.use_scenes or found is None:
            sources["video"] = {"used": False, "note": "Disabled in settings"}
            return None
        if isinstance(found, str):
            sources["video"] = {"used": False, "note": f"Scene detection skipped: {found}"}
            return None
        _, path = found
        try:
            detector = self._scene_detector()
        except ExternalToolError as exc:
            sources["video"] = {
                "used": False,
                "failed": True,
                "note": f"Scene detection skipped: {exc}",
            }
            return None
        if detector is None:
            sources["video"] = {"used": False, "note": "Scene detection skipped: no detector"}
            return None
        low, high = SCENE_SHARE

        def on_seconds(seconds: float) -> None:
            fraction = min(1.0, seconds / duration) if duration > 0 else 1.0
            report(low + (high - low) * fraction, "Detecting scene changes")

        report(low, "Detecting scene changes")
        try:
            cuts = detector.detect(
                path,
                threshold=options.scene_threshold,
                duration_s=duration or None,
                progress=on_seconds,
                cancel=cancel,
            )
        except (ExternalToolError, ClipAnalysisError) as exc:
            logger.warning("Scene detection failed for a video: %s", exc)
            sources["video"] = {
                "used": False,
                "failed": True,
                "note": f"Scene detection failed: {exc}",
            }
            return None
        sources["video"] = {
            "used": True,
            "scene_cuts": len(cuts),
            "threshold": options.scene_threshold,
        }
        return cuts

    @staticmethod
    def _stats(
        evidence: Evidence, proposals: int, dropped: int, ranked: list[Any]
    ) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        for s in evidence.signals:
            by_kind[s.kind.value] = by_kind.get(s.kind.value, 0) + 1
        return {
            "sentences": len(evidence.sentences),
            "signals_by_kind": dict(sorted(by_kind.items())),
            "proposals": proposals,
            "duplicates_or_overlaps_dropped": dropped,
            "lexicon_supported": evidence.lexicon_supported,
            **evidence_summary(ranked),
        }
