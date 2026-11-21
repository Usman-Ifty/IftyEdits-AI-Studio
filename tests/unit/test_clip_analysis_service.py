"""ClipAnalysisService with a scripted transcript, synthetic audio and a fake scene detector."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from iftyedits.config import Settings
from iftyedits.core.clip_models import ClipDecision, SceneCut
from iftyedits.core.errors import (
    ClipAnalysisError,
    ExternalToolError,
    InvalidSourceError,
    JobCancelledError,
    JobOwnershipError,
)
from iftyedits.core.models import ArtifactKind, Video
from iftyedits.core.protocols import CancelToken
from iftyedits.core.scoring import SCORING_VERSION, RankingConfig
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.services.clip_analysis import (
    ClipAnalysisService,
    ClipOptions,
    options_from_settings,
)
from iftyedits.services.ingest import IngestService
from iftyedits.storage.clips import ClipRepository
from iftyedits.storage.originals import sha256_file
from iftyedits.storage.processing import ProcessingRepository
from iftyedits.storage.transcripts import TranscriptRepository
from iftyedits.storage.videos import VideoRepository
from tests.clip_db_fakes import add_artifact, prepare_media, save_transcript
from tests.clip_fakes import cuts, podcast
from tests.conftest import MakeMedia


class FakeScenes:
    def __init__(self, found: Sequence[SceneCut] = ()) -> None:
        self.found = list(found)
        self.calls = 0
        self.fail: Exception | None = None

    def detect(
        self,
        video: Path,
        *,
        threshold: float,
        duration_s: float | None = None,
        progress: Callable[[float], None] | None = None,
        cancel: CancelToken | None = None,
    ) -> list[SceneCut]:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        if progress is not None:
            progress(duration_s or 1.0)
        return list(self.found)


@pytest.fixture
def video(ingest: IngestService, make_media: MakeMedia) -> Video:
    return ingest.import_video(make_media("Podcast.mp4")).video


@pytest.fixture
def scenes() -> FakeScenes:
    return FakeScenes(cuts(30.0, 95.0))


@pytest.fixture
def service(
    settings: Settings, db_conn: sqlite3.Connection, scenes: FakeScenes
) -> ClipAnalysisService:
    processing = ProcessingRepository(db_conn)
    return ClipAnalysisService(
        settings,
        VideoRepository(db_conn),
        processing,
        TranscriptRepository(db_conn),
        ClipRepository(db_conn),
        lambda artifact: settings.derived_dir.resolve() / artifact.relpath,
        lambda: scenes,
    )


@pytest.fixture
def ready(settings: Settings, db_conn: sqlite3.Connection, video: Video) -> Video:
    save_transcript(db_conn, video, podcast())
    prepare_media(settings, db_conn, video)
    return video


def test_analyses_and_stores_ranked_explained_suggestions(
    service: ClipAnalysisService, ready: Video, scenes: FakeScenes, settings: Settings
) -> None:
    vault_file = settings.originals_dir / ready.vault_relpath
    before = sha256_file(vault_file)
    progress: list[tuple[float, str]] = []
    report = service.analyze(ready.id, progress=lambda f, m: progress.append((f, m)))
    a = report.analysis
    assert not report.reused and a.is_current and a.candidate_count >= 2
    assert a.scoring_version == SCORING_VERSION and a.signal_count > 0
    assert a.sources["audio"]["used"] and a.sources["video"]["used"]
    assert a.sources["video"]["scene_cuts"] == 2 and scenes.calls == 1
    assert a.sources["faces"]["used"] is False and a.sources["speakers"]["used"] is False
    assert a.stats["best_score"] > 0
    assert "clip suggestions" in report.summary()
    assert report.as_result()["candidates"] == a.candidate_count
    fractions = [f for f, _ in progress]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0
    assert sha256_file(vault_file) == before


def test_rerun_reuses_and_force_replaces(
    service: ClipAnalysisService, ready: Video, db_conn: sqlite3.Connection, scenes: FakeScenes
) -> None:
    repo = ClipRepository(db_conn)
    first = service.analyze(ready.id)
    first_keys = [c.key for c in repo.candidates(first.analysis.id)]
    second = service.analyze(ready.id)
    assert second.reused and second.analysis.id == first.analysis.id
    assert second.summary().startswith("Reused")
    assert scenes.calls == 1  # reuse skips the expensive passes
    forced = service.analyze(ready.id, force=True)
    assert not forced.reused and scenes.calls == 2
    assert db_conn.execute("SELECT count(*) FROM clip_analyses").fetchone()[0] == 1
    assert [c.key for c in repo.candidates(forced.analysis.id)] == first_keys


def test_changed_settings_make_a_new_analysis(
    service: ClipAnalysisService, ready: Video, db_conn: sqlite3.Connection
) -> None:
    first = service.analyze(ready.id)
    options = ClipOptions(ranking=RankingConfig(max_count=2))
    other = service.analyze(ready.id, options)
    assert not other.reused and other.analysis.id != first.analysis.id
    assert other.analysis.candidate_count == 2
    assert ClipRepository(db_conn).current(ready.id) == other.analysis


def test_decisions_change_the_next_ranking_and_survive(
    service: ClipAnalysisService, ready: Video, db_conn: sqlite3.Connection
) -> None:
    repo = ClipRepository(db_conn)
    first = service.analyze(ready.id)
    top = repo.candidates(first.analysis.id)[0]
    repo.decide(top.id, ClipDecision.REJECTED)
    second = service.analyze(ready.id)
    assert not second.reused  # feedback is part of the stored parameters
    again = {c.key: c for c in repo.candidates(second.analysis.id)}
    if top.key in again:
        assert again[top.key].decision is ClipDecision.REJECTED
        assert again[top.key].score < top.score
    assert service.analyze(ready.id).reused


def test_needs_a_transcript(
    service: ClipAnalysisService, video: Video, db_conn: sqlite3.Connection
) -> None:
    with pytest.raises(ClipAnalysisError, match="Transcribe it first"):
        service.analyze(video.id)
    with pytest.raises(InvalidSourceError):
        service.analyze(999)
    assert db_conn.execute("SELECT count(*) FROM clip_analyses").fetchone()[0] == 0


def test_missing_original_is_refused(
    service: ClipAnalysisService, ready: Video, settings: Settings
) -> None:
    vault_file = settings.originals_dir / ready.vault_relpath
    vault_file.chmod(0o644)
    moved = vault_file.with_suffix(".moved")
    vault_file.rename(moved)
    with pytest.raises(ClipAnalysisError, match="missing from the vault"):
        service.analyze(ready.id)
    moved.rename(vault_file)
    vault_file.chmod(0o444)


def test_empty_transcript_gives_an_honest_empty_result(
    service: ClipAnalysisService, video: Video, db_conn: sqlite3.Connection, settings: Settings
) -> None:
    save_transcript(db_conn, video, (), language=None)
    prepare_media(settings, db_conn, video)
    report = service.analyze(video.id)
    assert report.analysis.candidate_count == 0
    assert "no speech" in report.analysis.stats["note"]
    assert "No clip suggestions" in report.summary()


def test_missing_media_degrades_with_notes(
    service: ClipAnalysisService, video: Video, db_conn: sqlite3.Connection, scenes: FakeScenes
) -> None:
    save_transcript(db_conn, video, podcast())
    report = service.analyze(video.id)
    sources = report.analysis.sources
    assert not sources["audio"]["used"] and "not prepared" in sources["audio"]["note"]
    assert not sources["video"]["used"] and scenes.calls == 0
    assert report.analysis.candidate_count >= 2  # transcript alone is enough


def test_damaged_or_changed_media_is_skipped(
    service: ClipAnalysisService, ready: Video, settings: Settings, db_conn: sqlite3.Connection
) -> None:
    audio = settings.derived_dir.resolve() / f"v{ready.id}" / "audio.wav"
    audio.write_bytes(b"x")  # size no longer matches the record
    proxy = settings.derived_dir.resolve() / f"v{ready.id}" / "proxy.mp4"
    proxy.unlink()
    report = service.analyze(ready.id)
    assert "does not match" in report.analysis.sources["audio"]["note"]
    assert "missing" in report.analysis.sources["video"]["note"]


def test_unreadable_audio_and_scene_failures_are_skipped(
    service: ClipAnalysisService, ready: Video, settings: Settings, scenes: FakeScenes
) -> None:
    audio = settings.derived_dir.resolve() / f"v{ready.id}" / "audio.wav"
    audio.write_bytes(b"not a wav file")
    add_artifact(settings, service.processing.conn, ready, ArtifactKind.AUDIO_WAV, audio)
    scenes.fail = ExternalToolError("ffmpeg crashed")
    report = service.analyze(ready.id)
    assert "unreadable" in report.analysis.sources["audio"]["note"]
    assert "ffmpeg crashed" in report.analysis.sources["video"]["note"]
    assert report.analysis.candidate_count >= 2


def test_sources_can_be_disabled(
    service: ClipAnalysisService, ready: Video, scenes: FakeScenes
) -> None:
    report = service.analyze(ready.id, ClipOptions(use_audio=False, use_scenes=False))
    assert report.analysis.sources["audio"]["note"] == "Disabled in settings"
    assert report.analysis.sources["video"]["note"] == "Disabled in settings"
    assert scenes.calls == 0


def test_non_english_transcript_still_works_with_honest_scoring(
    service: ClipAnalysisService, video: Video, db_conn: sqlite3.Connection
) -> None:
    save_transcript(db_conn, video, podcast(), language="fr")
    report = service.analyze(video.id)
    assert report.analysis.candidate_count >= 1
    assert report.analysis.stats["lexicon_supported"] is False


def test_cancellation_stores_nothing(
    service: ClipAnalysisService, ready: Video, db_conn: sqlite3.Connection
) -> None:
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(JobCancelledError):
        service.analyze(ready.id, cancel=cancel)
    for table in ("clip_analyses", "clip_signals", "clip_candidates"):
        assert db_conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0  # noqa: S608


def test_cancellation_midway_stores_nothing(
    service: ClipAnalysisService, ready: Video, db_conn: sqlite3.Connection
) -> None:
    cancel = threading.Event()

    def on_progress(fraction: float, message: str) -> None:
        if message == "Proposing clips":
            cancel.set()

    with pytest.raises(JobCancelledError):
        service.analyze(ready.id, progress=on_progress, cancel=cancel)
    assert db_conn.execute("SELECT count(*) FROM clip_analyses").fetchone()[0] == 0


def test_lost_ownership_stores_nothing(
    service: ClipAnalysisService, ready: Video, db_conn: sqlite3.Connection
) -> None:
    queue = JobQueue(db_conn)
    job_id, _ = queue.enqueue_transcription(ready.id, model="small", language=None)
    queue.claim("w1")
    with pytest.raises(JobOwnershipError):
        service.analyze(ready.id, job_id=job_id, worker_id="other")
    assert db_conn.execute("SELECT count(*) FROM clip_analyses").fetchone()[0] == 0
    saved = service.analyze(ready.id, job_id=job_id, worker_id="w1")
    assert saved.analysis.job_id == job_id


def test_new_transcript_invalidates_reuse(
    service: ClipAnalysisService, ready: Video, db_conn: sqlite3.Connection
) -> None:
    first = service.analyze(ready.id)
    save_transcript(db_conn, ready, podcast(), params_hash="t2")
    second = service.analyze(ready.id)
    assert not second.reused and second.analysis.transcript_id != first.analysis.transcript_id


def test_options_from_settings(settings: Settings) -> None:
    options = options_from_settings(
        settings.model_copy(
            update={"clip_min_s": 10.0, "clip_max_candidates": 5, "clip_use_scenes": False}
        )
    )
    assert options.candidates.min_s == 10.0 and options.ranking.max_count == 5
    assert not options.use_scenes and options.use_audio


def test_failed_sources_are_not_reused_so_a_retry_can_do_better(
    service: ClipAnalysisService, ready: Video, scenes: FakeScenes
) -> None:
    scenes.fail = ExternalToolError("ffmpeg crashed")
    first = service.analyze(ready.id)
    assert not first.analysis.sources["video"]["used"]
    scenes.fail = None
    scenes.found = list(cuts(40.0))
    second = service.analyze(ready.id)
    assert not second.reused and second.analysis.sources["video"]["scene_cuts"] == 1
    assert service.analyze(ready.id).reused
