"""The production orchestrator with fake stages and fake rendering (no media, no network)."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from iftyedits.adapters.short_render import VerifiedOutput
from iftyedits.core.clip_models import ClipCandidate, ClipDecision, ClipSpan
from iftyedits.core.errors import JobCancelledError, ShortRenderError
from iftyedits.core.framing import CropSuggestion
from iftyedits.core.shorts import ShortConfig
from iftyedits.services.production import (
    ProductionOptions,
    ProductionService,
    Stages,
    select_candidates,
)


def cand(rank: int, score: float, decision: ClipDecision | None = None) -> ClipCandidate:
    span = ClipSpan(0.0, 1.0, "x")
    return ClipCandidate(
        id=rank, analysis_id=1, video_id=7, key=f"key{rank:04d}", rank=rank,
        start_s=rank * 100.0, end_s=rank * 100.0 + 30.0, category="story", methods=(),
        title=f"Clip {rank}", excerpt="", hook=span, context=None, payoff=span, score=score,
        confidence=0.5, components=(), strengths=(), weaknesses=(), explanation="",
        signal_positions=(), evidence={}, decision=decision,
    )  # fmt: skip


def test_selection_skips_rejected_and_low_scores_and_prefers_favorites() -> None:
    found = [
        cand(1, 90), cand(2, 80, ClipDecision.REJECTED), cand(3, 70),
        cand(4, 30), cand(5, 60, ClipDecision.FAVORITE), cand(6, 55),
    ]  # fmt: skip
    picked = select_candidates(found, ProductionOptions(max_clips=3, min_score=40))
    assert [c.rank for c in picked] == [5, 1, 3]
    assert select_candidates(found, ProductionOptions(max_clips=50, min_score=99)) == []


def test_options_are_validated_and_roundtrip() -> None:
    options = ProductionOptions(max_clips=3, caption_preset="boxed")
    assert ProductionOptions.from_payload({**options.as_payload(), "junk": 1}) == options
    for bad in (
        ProductionOptions(max_clips=0),
        ProductionOptions(min_score=101),
        ProductionOptions(caption_preset="nope"),
    ):
        with pytest.raises(ValueError):
            bad.validate()


class FakeShorts:
    def __init__(self, fail_keys: set[str] | None = None, cancel_after: int | None = None) -> None:
        self.fail_keys = fail_keys or set()
        self.cancel_after = cancel_after
        self.cancel = threading.Event()
        self.rendered: list[str] = []
        self.saved: list[object] = []

    def open_project(self, video_id: int, key: str) -> Any:
        config = ShortConfig(start_s=1.0, end_s=20.0)
        return SimpleNamespace(id=int(key[3:]), config=config)

    def save_config(self, project_id: int, config: object) -> None:
        self.saved.append(config)

    def request_render(self, project_id: int, kind: str, *, queue_job: bool = True) -> Any:
        assert not queue_job and kind == "export"
        render = SimpleNamespace(id=project_id, active=False)
        return SimpleNamespace(render=render, created=True)

    def run_render(self, render_id: int, *, progress: Any, cancel: Any) -> Any:
        key = f"key{render_id:04d}"
        if key in self.fail_keys:
            raise ShortRenderError("encoder exploded")
        self.rendered.append(key)
        progress(0.5, "encoding")
        if self.cancel_after is not None and len(self.rendered) >= self.cancel_after:
            self.cancel.set()
        out = Path(f"/exports/{key}.mp4")
        v = VerifiedOutput(1080, 1920, 19.0, 1000, True, "h264")
        return SimpleNamespace(output_path=out, verified=v)


class FramingShorts(FakeShorts):
    """Offers a crop suggestion; can be set to fail."""

    def __init__(self, *, fail: bool = False, start: float = 0.5) -> None:
        super().__init__()
        self.fail = fail
        self.start = start

    def open_project(self, video_id: int, key: str) -> Any:
        config = ShortConfig(start_s=1.0, end_s=20.0, crop_position=self.start)
        return SimpleNamespace(id=int(key[3:]), config=config)

    def suggest_crop(self, project_id: int) -> Any:
        if self.fail:
            raise OSError("no frames")
        return CropSuggestion(0.8, "detail", 0.7, "busy right side")


class FakeClips:
    def __init__(self, found: list[ClipCandidate]) -> None:
        self.found = found

    def current(self, video_id: int) -> Any:
        return SimpleNamespace(id=1)

    def candidates(self, analysis_id: int) -> list[ClipCandidate]:
        return self.found


def make(
    found: list[ClipCandidate], shorts: FakeShorts, calls: list[str]
) -> tuple[ProductionService, FakeShorts]:
    def fetch(url: str, progress: Any, cancel: Any) -> int:
        calls.append(f"fetch:{url}")
        progress(1.0, "downloaded")
        return 7

    def stage(name: str) -> Any:
        def run(video_id: int, options: Any, progress: Any, cancel: Any) -> None:
            calls.append(f"{name}:{video_id}")
            progress(0.5, name)
            progress(1.0, name)

        return run

    stages = Stages(fetch, stage("prepare"), stage("transcribe"), stage("analyze"))
    return ProductionService(stages, cast(Any, shorts), cast(Any, FakeClips(found))), shorts


def run(
    service: ProductionService,
    options: ProductionOptions,
    *,
    url: str | None = "https://youtu.be/x",
    video_id: int | None = None,
    cancel: threading.Event | None = None,
) -> tuple[Any, list[float]]:
    seen: list[float] = []
    report = service.run(
        url=url, video_id=video_id, options=options,
        progress=lambda f, _m: seen.append(f), cancel=cancel,
    )  # fmt: skip
    return report, seen


def test_full_run_exports_each_clip_separately_with_monotonic_progress() -> None:
    calls: list[str] = []
    service, shorts = make([cand(i, 80 - i) for i in range(1, 6)], FakeShorts(), calls)
    report, seen = run(service, ProductionOptions(max_clips=3))
    assert calls == [
        "fetch:https://youtu.be/x", "prepare:7", "transcribe:7", "analyze:7"
    ]  # fmt: skip
    assert shorts.rendered == ["key0001", "key0002", "key0003"]
    assert report.succeeded == 3 and report.failed == 0 and report.candidates_found == 5
    assert len({c.output for c in report.clips}) == 3  # one file per clip
    assert seen == sorted(seen) and 0 < seen[-1] < 1
    result = report.as_result()
    assert result["succeeded"] == 3 and result["clips"][0]["width"] == 1080
    assert report.summary() == "3 of 3 Shorts exported"
    assert len(shorts.saved) == 3  # the chosen caption preset was applied


def test_a_failed_clip_does_not_stop_or_spoil_the_others() -> None:
    service, shorts = make(
        [cand(1, 90), cand(2, 80), cand(3, 70)], FakeShorts(fail_keys={"key0002"}), []
    )
    report, _ = run(service, ProductionOptions(max_clips=3), url=None, video_id=7)
    assert [c.status for c in report.clips] == ["succeeded", "failed", "succeeded"]
    assert report.clips[1].error == "encoder exploded" and report.clips[1].output is None
    assert shorts.rendered == ["key0001", "key0003"]
    assert report.summary() == "2 of 3 Shorts exported, 1 failed"


def test_cancel_stops_between_clips_and_keeps_what_finished() -> None:
    shorts = FakeShorts(cancel_after=1)
    service, _ = make([cand(1, 90), cand(2, 80), cand(3, 70)], shorts, [])
    with pytest.raises(JobCancelledError):
        run(service, ProductionOptions(max_clips=3), url=None, video_id=7, cancel=shorts.cancel)
    assert shorts.rendered == ["key0001"]


def test_no_suggestion_above_the_minimum_gives_an_actionable_note() -> None:
    service, shorts = make([cand(1, 20)], FakeShorts(), [])
    report, _ = run(service, ProductionOptions(min_score=50), url=None, video_id=7)
    assert report.clips == () and "minimum score" in report.note
    assert shorts.rendered == []
    empty, _ = run(make([], FakeShorts(), [])[0], ProductionOptions(), url=None, video_id=7)
    assert "no suggestions" in empty.note


def test_needs_exactly_one_input() -> None:
    service, _ = make([], FakeShorts(), [])
    with pytest.raises(ValueError):
        run(service, ProductionOptions(), url=None, video_id=None)
    with pytest.raises(ValueError):
        run(service, ProductionOptions(), url="https://youtu.be/x", video_id=1)


def _positions(shorts: FakeShorts) -> list[float]:
    return [cast(ShortConfig, c).crop_position for c in shorts.saved]


def test_untouched_framing_is_chosen_automatically() -> None:
    service, shorts = make([cand(1, 90)], FramingShorts(), [])
    report, _ = run(service, ProductionOptions(), url=None, video_id=7)
    assert report.succeeded == 1 and 0.8 in _positions(shorts)


def test_a_manual_crop_is_never_overridden() -> None:
    service, shorts = make([cand(1, 90)], FramingShorts(start=0.2), [])
    run(service, ProductionOptions(), url=None, video_id=7)
    assert 0.8 not in _positions(shorts)


def test_framing_failure_falls_back_and_still_exports() -> None:
    service, shorts = make([cand(1, 90)], FramingShorts(fail=True), [])
    report, _ = run(service, ProductionOptions(), url=None, video_id=7)
    assert report.succeeded == 1 and 0.8 not in _positions(shorts)
