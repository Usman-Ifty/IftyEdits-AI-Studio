"""One-click production: link or video in, several separate vertical Shorts out.

The stages reuse the services that already exist (fetch, prepare, transcribe, analyse,
render), run one after another inside a single job. Every stage skips work that is already
stored, so retrying a failed or cancelled production only does what is still missing.

Clips are rendered one at a time (an FFmpeg encode already uses the machine well) and each
clip is isolated: a failed clip is recorded and the rest continue, and finished exports are
never touched. A cancelled job stops between steps and keeps what is finished.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from iftyedits.core.clip_models import ClipCandidate, ClipDecision
from iftyedits.core.errors import ClipAnalysisError, IftyEditsError, JobCancelledError
from iftyedits.core.protocols import CancelToken
from iftyedits.core.shorts import CAPTION_PRESETS, CaptionStyle, ShortConfig
from iftyedits.services.shorts import ShortService
from iftyedits.storage.clips import ClipRepository
from iftyedits.storage.shorts import KIND_EXPORT

logger = logging.getLogger(__name__)

Progress = Callable[[float, str], None]

STAGE_SHARES: tuple[tuple[str, float], ...] = (
    ("fetch", 0.15),
    ("prepare", 0.15),
    ("transcribe", 0.30),
    ("analyze", 0.05),
    ("render", 0.35),
)
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_SELECTED = "selected"


@dataclass(frozen=True, slots=True)
class ProductionOptions:
    """Choices for one production. Defaults suit a first run."""

    max_clips: int = 5
    min_score: float = 40.0
    caption_preset: str = "highlight"
    model: str | None = None
    language: str | None = None
    force: bool = False
    min_duration_s: float = 10.0
    max_duration_s: float = 90.0
    min_gap_s: float = 5.0
    """Selected Shorts must be at least this far apart (no near-identical moments)."""
    review_only: bool = False
    """Choose the clips and stop before rendering, so they can be reviewed first."""

    def validate(self) -> ProductionOptions:
        """Raise ``ValueError`` for settings that cannot work."""
        if not 1 <= self.max_clips <= 50:
            raise ValueError("Choose between 1 and 50 clips")
        if not 0 <= self.min_score <= 100:
            raise ValueError("The minimum score must be between 0 and 100")
        if not 0 < self.min_duration_s <= self.max_duration_s <= 180:
            raise ValueError("Clip length limits must satisfy 0 < min <= max <= 180 seconds")
        if self.min_gap_s < 0:
            raise ValueError("The gap between Shorts cannot be negative")
        if self.caption_preset not in CAPTION_PRESETS:
            raise ValueError(f"Unknown caption preset {self.caption_preset!r}")
        return self

    def as_payload(self) -> dict[str, Any]:
        """JSON stored with the job."""
        return {
            "max_clips": self.max_clips,
            "min_score": self.min_score,
            "caption_preset": self.caption_preset,
            "model": self.model,
            "language": self.language,
            "force": self.force,
            "min_duration_s": self.min_duration_s,
            "max_duration_s": self.max_duration_s,
            "min_gap_s": self.min_gap_s,
            "review_only": self.review_only,
        }

    @classmethod
    def from_payload(cls, data: dict[str, Any]) -> ProductionOptions:
        """Rebuild options from a job payload, ignoring unknown keys."""
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known).validate()


@dataclass(frozen=True, slots=True)
class Stages:
    """The work that comes before rendering. The worker supplies real implementations."""

    fetch: Callable[[str, Progress, CancelToken | None], int]
    prepare: Callable[[int, ProductionOptions, Progress, CancelToken | None], None]
    transcribe: Callable[[int, ProductionOptions, Progress, CancelToken | None], None]
    analyze: Callable[[int, ProductionOptions, Progress, CancelToken | None], None]


@dataclass(frozen=True, slots=True)
class ClipOutcome:
    """What happened to one chosen suggestion."""

    rank: int
    key: str
    title: str
    score: float
    start_s: float
    end_s: float
    status: str
    output: str | None = None
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None
    has_audio: bool | None = None
    reused: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Stored in the job result."""
        return {
            "rank": self.rank,
            "key": self.key,
            "title": self.title,
            "score": round(self.score, 1),
            "start_s": round(self.start_s, 2),
            "end_s": round(self.end_s, 2),
            "status": self.status,
            "output": self.output,
            "width": self.width,
            "height": self.height,
            "duration_s": self.duration_s,
            "has_audio": self.has_audio,
            "reused": self.reused,
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class ProductionReport:
    """The final summary of a production."""

    video_id: int
    clips: tuple[ClipOutcome, ...]
    candidates_found: int
    note: str = ""
    stage_notes: dict[str, str] = field(default_factory=dict)

    @property
    def succeeded(self) -> int:
        """Clips with a verified export."""
        return sum(1 for c in self.clips if c.status == STATUS_SUCCEEDED)

    @property
    def failed(self) -> int:
        """Clips that could not be rendered."""
        return sum(1 for c in self.clips if c.status == STATUS_FAILED)

    def as_result(self) -> dict[str, Any]:
        """Stored on the job so the Jobs and Exports pages can show the summary."""
        return {
            "video_id": self.video_id,
            "candidates_found": self.candidates_found,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "clips": [c.as_dict() for c in self.clips],
            "note": self.note,
            "stages": self.stage_notes,
        }

    def failure_message(self) -> str | None:
        """Why the whole production failed, or None when at least one Short was made.

        A run that tried to render clips and exported none must not look like a success.
        """
        rendered = [c for c in self.clips if c.status in (STATUS_SUCCEEDED, STATUS_FAILED)]
        if not rendered or self.succeeded:
            return None
        reasons = "; ".join(
            dict.fromkeys(f"{c.error or 'unknown error'}" for c in rendered if c.error)
        )
        return f"No Shorts could be exported ({self.failed} failed). {reasons}".strip()

    def summary(self) -> str:
        """One line for the jobs list."""
        if not self.clips:
            return self.note or "No clip suggestions met the settings"
        if self.clips and all(c.status == STATUS_SELECTED for c in self.clips):
            return f"{len(self.clips)} Short(s) selected for review"
        text = f"{self.succeeded} of {len(self.clips)} Shorts exported"
        return f"{text}, {self.failed} failed" if self.failed else text


def select_candidates(
    candidates: list[ClipCandidate], options: ProductionOptions
) -> list[ClipCandidate]:
    """The suggestions to render: best first, never rejected ones, favourites before the rest.

    Policy: skip rejected suggestions, scores below ``min_score`` and lengths outside the
    duration limits; take favourites first, then by rank; skip a suggestion that overlaps an
    already chosen one or starts within ``min_gap_s`` of it, so two Shorts never show the same
    moment. Fewer than ``max_clips`` is normal: weak clips are never added to fill the count.
    """
    usable = [
        c
        for c in candidates
        if c.decision is not ClipDecision.REJECTED
        and c.score >= options.min_score
        and options.min_duration_s
        <= c.effective_end_s - c.effective_start_s
        <= options.max_duration_s
    ]
    usable.sort(key=lambda c: (c.decision is not ClipDecision.FAVORITE, c.rank))
    chosen: list[ClipCandidate] = []
    for candidate in usable:
        if len(chosen) >= options.max_clips:
            break
        lo, hi = candidate.effective_start_s, candidate.effective_end_s
        if any(
            lo < c.effective_end_s + options.min_gap_s
            and c.effective_start_s < hi + options.min_gap_s
            for c in chosen
        ):
            continue
        chosen.append(candidate)
    return chosen


class ProductionService:
    """Runs the whole pipeline for a link or an already stored video."""

    def __init__(self, stages: Stages, shorts: ShortService, clips: ClipRepository) -> None:
        self.stages = stages
        self.shorts = shorts
        self.clips = clips

    def run(
        self,
        *,
        url: str | None,
        video_id: int | None,
        options: ProductionOptions,
        progress: Progress,
        cancel: CancelToken | None = None,
    ) -> ProductionReport:
        """Produce Shorts. Raises only for failures that stop the whole job."""
        options.validate()
        if (url is None) == (video_id is None):
            raise ValueError("Give either a link or a stored video")
        offset = 0.0
        shares = dict(STAGE_SHARES)

        def stage(name: str) -> Progress:
            base = offset

            def report(fraction: float, message: str) -> None:
                progress(min(0.999, base + shares[name] * max(0.0, min(1.0, fraction))), message)

            return report

        def advance(name: str) -> None:
            nonlocal offset
            offset += shares[name]
            progress(min(0.999, offset), "")

        def check() -> None:
            if cancel is not None and cancel.is_set():
                raise JobCancelledError("Production cancelled; finished Shorts were kept")

        if url is not None:
            video_id = self.stages.fetch(url, stage("fetch"), cancel)
        advance("fetch")
        if video_id is None:  # pragma: no cover - guarded above
            raise IftyEditsError("No video to process")
        check()
        self.stages.prepare(video_id, options, stage("prepare"), cancel)
        advance("prepare")
        check()
        self.stages.transcribe(video_id, options, stage("transcribe"), cancel)
        advance("transcribe")
        check()
        self.stages.analyze(video_id, options, stage("analyze"), cancel)
        advance("analyze")
        check()

        analysis = self.clips.current(video_id)
        if analysis is None:
            raise ClipAnalysisError("Analysis finished without stored suggestions")
        found = self.clips.candidates(analysis.id)
        chosen = select_candidates(found, options)
        if not chosen:
            note = (
                "The analysis found no suggestions" if not found
                else f"No suggestion reached the minimum score of {options.min_score:g}. "
                "Lower the minimum score or review the suggestions on the Clips page."
            )  # fmt: skip
            return ProductionReport(video_id, (), len(found), note)
        if options.review_only:
            picked = tuple(
                ClipOutcome(
                    c.rank, c.key, c.title, c.score, c.effective_start_s, c.effective_end_s,
                    STATUS_SELECTED,
                )
                for c in chosen
            )  # fmt: skip
            return ProductionReport(
                video_id, picked, len(found),
                "Review only: nothing was rendered. Open the Clips page, then run again "
                "without review.",
            )  # fmt: skip
        render = stage("render")
        outcomes: list[ClipOutcome] = []
        for index, candidate in enumerate(chosen):
            check()
            label = f"Clip {index + 1} of {len(chosen)}: {candidate.title}"

            def clip_progress(
                fraction: float, message: str, i: int = index, text: str = label
            ) -> None:
                render((i + fraction) / len(chosen), f"{text} ({message})" if message else text)

            outcomes.append(self._render_one(candidate, options, clip_progress, cancel))
            render((index + 1) / len(chosen), label)
        return ProductionReport(video_id, tuple(outcomes), len(found))

    def _frame(self, project_id: int, config: ShortConfig) -> None:
        """Choose automatic framing for a Short whose framing was never touched.

        A crop the user set (a position other than the default centre, or a stored moving
        path) is left alone. Order: a smooth moving window, then one steady window from the
        detail heuristic, then the centre. A failure never stops the export.
        """
        if config.crop_position != 0.5 or config.crop_path:
            return
        try:
            path = self.shorts.suggest_path(project_id)
            if path.method == "follow":
                self.shorts.save_config(
                    project_id,
                    replace(config, crop_position=path.keyframes[0][1], crop_path=path.keyframes),
                )
                return
            if path.method == "steady":
                self.shorts.save_config(
                    project_id, replace(config, crop_position=path.keyframes[0][1])
                )
                return
        except Exception as exc:
            logger.info("Moving framing skipped: %s", exc)
        try:
            suggestion = self.shorts.suggest_crop(project_id)
            if suggestion.method == "detail":
                self.shorts.save_config(
                    project_id, replace(config, crop_position=suggestion.position)
                )
        except Exception as exc:
            logger.info("Auto framing skipped: %s", exc)

    def _render_one(
        self,
        candidate: ClipCandidate,
        options: ProductionOptions,
        progress: Progress,
        cancel: CancelToken | None,
    ) -> ClipOutcome:
        base = ClipOutcome(
            candidate.rank,
            candidate.key,
            candidate.title,
            candidate.score,
            candidate.effective_start_s,
            candidate.effective_end_s,
            STATUS_FAILED,
        )
        try:
            project = self.shorts.open_project(candidate.video_id, candidate.key)
            config = project.config
            if config.captions.preset != options.caption_preset and (
                config.captions == CaptionStyle()
            ):
                config = replace(config, captions=CaptionStyle.from_preset(options.caption_preset))
                self.shorts.save_config(project.id, config)
            self._frame(project.id, config)  # saving the crop must keep the chosen captions
            request = self.shorts.request_render(project.id, KIND_EXPORT, queue_job=False)
            render = request.render
            if not request.created and render.active:
                return replace(
                    base,
                    status=STATUS_SKIPPED,
                    error="This Short is already being rendered from the editor; not started again",
                )
            report = self.shorts.run_render(render.id, progress=progress, cancel=cancel)
        except JobCancelledError:
            raise
        except IftyEditsError as exc:
            logger.warning("Clip %s failed: %s", candidate.key[:8], exc)
            return replace(base, error=str(exc))
        except Exception as exc:
            logger.exception("Clip %s crashed", candidate.key[:8])
            return replace(base, error=f"Unexpected error: {type(exc).__name__}: {exc}")
        v = report.verified
        return replace(
            base,
            status=STATUS_SUCCEEDED,
            output=str(report.output_path),
            width=v.width,
            height=v.height,
            duration_s=round(v.duration_s, 2),
            has_audio=v.has_audio,
            reused=not request.created,
        )
