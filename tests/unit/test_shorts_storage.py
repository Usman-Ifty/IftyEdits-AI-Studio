"""Short projects and renders: migration, persistence, dedupe, reconcile, jobs, naming."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from iftyedits.adapters.caption_render import render_cue, wrap_words
from iftyedits.core.captions import CaptionCue, CueWord
from iftyedits.core.models import JobStatus, Video
from iftyedits.core.shorts import CAPTION_PRESETS, CaptionStyle, ShortConfig
from iftyedits.pipeline.jobs import RENDER_SHORT, JobQueue
from iftyedits.services.ingest import IngestService
from iftyedits.services.shorts import claim_unique_path, export_stem, slugify
from iftyedits.storage.shorts import KIND_EXPORT, ShortRepository
from tests.conftest import MakeMedia


@pytest.fixture
def video(ingest: IngestService, make_media: MakeMedia) -> Video:
    return ingest.import_video(make_media("Show.mp4")).video


@pytest.fixture
def repo(db_conn: sqlite3.Connection) -> ShortRepository:
    return ShortRepository(db_conn)


CONFIG = ShortConfig(start_s=5.0, end_s=30.0)


def test_project_is_created_once_and_keeps_the_original_range(
    repo: ShortRepository, video: Video
) -> None:
    first = repo.create_project(video.id, "k1", "Title", 5.0, 30.0, CONFIG)
    again = repo.create_project(video.id, "k1", "Other", 1.0, 2.0, CONFIG)
    assert again.id == first.id and again.original_start_s == 5.0
    other = repo.create_project(video.id, "k2", "Title 2", 40.0, 60.0, CONFIG)
    assert other.id != first.id


def test_saved_config_is_isolated_per_project(repo: ShortRepository, video: Video) -> None:
    a = repo.create_project(video.id, "a", "A", 5.0, 30.0, CONFIG)
    b = repo.create_project(video.id, "b", "B", 5.0, 30.0, CONFIG)
    repo.save_config(a.id, ShortConfig(start_s=6.0, end_s=20.0, crop_position=0.1))
    loaded_a, loaded_b = repo.get_project(a.id), repo.get_project(b.id)
    assert loaded_a is not None and loaded_b is not None
    assert loaded_a.config.crop_position == 0.1 and loaded_a.config.start_s == 6.0
    assert loaded_b.config == CONFIG
    assert loaded_a.original_start_s == 5.0 and loaded_a.original_end_s == 30.0


def test_active_render_is_unique_per_config_and_frees_up_after_finish(
    repo: ShortRepository, video: Video
) -> None:
    project = repo.create_project(video.id, "k", "T", 5.0, 30.0, CONFIG)
    first = repo.create_render(project.id, KIND_EXPORT, "h" * 64, CONFIG)
    with pytest.raises(sqlite3.IntegrityError):
        repo.create_render(project.id, KIND_EXPORT, "h" * 64, CONFIG)
    assert repo.find_active(project.id, KIND_EXPORT, "h" * 64) == first
    repo.mark_failed(first.id, "boom")
    second = repo.create_render(project.id, KIND_EXPORT, "h" * 64, CONFIG)
    assert second.id != first.id
    failed = repo.get_render(first.id)
    assert failed is not None and failed.status == "failed" and failed.error == "boom"


def test_success_requires_the_file_to_exist_to_count_as_reusable(
    repo: ShortRepository, video: Video, tmp_path: Path
) -> None:
    project = repo.create_project(video.id, "k", "T", 5.0, 30.0, CONFIG)
    render = repo.create_render(project.id, KIND_EXPORT, "h" * 64, CONFIG)
    repo.mark_running(render.id)
    out = tmp_path / "a.mp4"
    out.write_bytes(b"x")
    repo.mark_succeeded(
        render.id,
        output_path=out,
        width=1080,
        height=1920,
        duration_s=25.0,
        size_bytes=1,
        has_audio=True,
        encoder="libx264",
    )
    assert repo.find_succeeded(project.id, KIND_EXPORT, "h" * 64) is not None
    out.unlink()
    assert repo.find_succeeded(project.id, KIND_EXPORT, "h" * 64) is None


def test_reconcile_fails_abandoned_renders_after_the_grace_period(
    repo: ShortRepository, video: Video
) -> None:
    project = repo.create_project(video.id, "k", "T", 5.0, 30.0, CONFIG)
    render = repo.create_render(project.id, KIND_EXPORT, "h" * 64, CONFIG)
    repo.mark_running(render.id)
    assert repo.reconcile(grace_s=3600) == 0
    assert repo.reconcile(grace_s=-1) == 1
    row = repo.get_render(render.id)
    assert row is not None and row.status == "failed" and row.error


def test_render_job_is_deduplicated_per_render(db_conn: sqlite3.Connection, video: Video) -> None:
    queue = JobQueue(db_conn)
    job_id, created = queue.enqueue_short_render(7, video.id)
    assert created and queue.enqueue_short_render(7, video.id) == (job_id, False)
    other, created = queue.enqueue_short_render(8, video.id)
    assert created and other != job_id
    job = queue.get(job_id)
    assert job is not None and job.kind == RENDER_SHORT and job.status is JobStatus.QUEUED
    assert queue.request_cancel(job_id)


def test_deleting_a_video_cascade_is_not_needed_but_rows_reference_it(
    db_conn: sqlite3.Connection, repo: ShortRepository, video: Video
) -> None:
    repo.create_project(video.id, "k", "T", 5.0, 30.0, CONFIG)
    assert db_conn.execute("PRAGMA foreign_key_check").fetchall() == []


# ----------------------------------------------------------------------- naming


def test_slugify_is_ascii_safe() -> None:
    assert slugify("../../Évil Näme!!.mp4") == "evil-name-mp4"
    assert slugify("日本語") == "short"
    assert "/" not in slugify("a/b\\c") and len(slugify("x" * 200)) <= 40


def test_export_stem_names_source_range_and_settings(video: Video) -> None:
    stem = export_stem(video, ShortConfig(start_s=65.0, end_s=100.0), "abcdef1234567890")
    assert stem == f"show_v{video.id}_1m05s-1m40s_abcdef12"


def test_claim_unique_path_never_replaces(tmp_path: Path) -> None:
    (tmp_path / "a.mp4").write_bytes(b"old")
    partial = tmp_path / "p.partial"
    partial.write_bytes(b"new")
    target = claim_unique_path(partial, tmp_path, "a")
    assert target.name == "a-2.mp4" and target.read_bytes() == b"new"
    assert (tmp_path / "a.mp4").read_bytes() == b"old" and not partial.exists()


# ----------------------------------------------------------------------- caption drawing


def _cue(text: str, *, words: bool = True) -> CaptionCue:
    tokens = text.split()
    timed = (
        tuple(CueWord(t, i * 0.3, i * 0.3 + 0.25) for i, t in enumerate(tokens)) if words else ()
    )
    end = max(len(tokens) * 0.3, 0.5)
    return CaptionCue(0.0, end, text, timed, "word" if words else "segment", 0)


@pytest.mark.parametrize("frame", [(1080, 1920), (540, 960), (720, 1280)])
def test_caption_stays_inside_the_safe_area_and_frame(frame: tuple[int, int]) -> None:
    text = "this is a rather long caption that has to wrap onto lines"
    image, layout = render_cue(_cue(text), CaptionStyle(), frame)
    assert image.size == frame
    assert image.getbbox() is not None
    assert layout.left >= 0 and layout.right <= frame[0]
    assert layout.top >= 0.09 * frame[1] and layout.bottom <= 0.81 * frame[1]
    assert layout.lines <= CaptionStyle().max_lines


def test_text_with_markup_and_unicode_is_drawn_literally() -> None:
    image, _ = render_cue(_cue("{\\an8}%{x} 'quote' \"q\" ñ 日本語"), CaptionStyle(), (540, 960))
    assert image.getbbox() is not None


def test_highlight_only_changes_pixels_when_asked() -> None:
    style = CaptionStyle(highlight=True)
    cue = _cue("one two three")
    plain, _ = render_cue(cue, style, (540, 960))
    lit, _ = render_cue(cue, style, (540, 960), active_word=1)
    assert plain.tobytes() != lit.tobytes()


def test_wrap_never_drops_words() -> None:
    from iftyedits.adapters.caption_render import _Metrics

    words = ("alpha beta gamma delta epsilon " * 5).split()
    lines = wrap_words(words, _Metrics(40, True), 300)
    assert [w for line in lines for w in line] == words and len(lines) > 1


@pytest.mark.parametrize("preset", sorted(CAPTION_PRESETS))
@pytest.mark.parametrize(
    "text",
    [
        "Short.",
        "Supercalifragilisticexpialidocious-pneumonoultramicroscopicsilicovolcanoconiosis",
        "Wait, what?! Five million... really — no way: ñandú café 日本語 العربية 🙂",
    ],
)
def test_every_preset_keeps_any_text_inside_the_mobile_safe_area(preset: str, text: str) -> None:
    style = CaptionStyle.from_preset(preset)
    image, layout = render_cue(_cue(text), style, (1080, 1920))
    assert image.getbbox() is not None
    assert layout.left >= 0 and layout.right <= 1080
    assert layout.top >= 0.09 * 1920 and layout.bottom <= 0.81 * 1920
