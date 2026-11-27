# ruff: noqa: F811
"""Friendly names, the upload "what is left" guide, the gallery and in-page transcription."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from iftyedits.config import Settings
from iftyedits.core import readiness as rd
from iftyedits.core.models import Job, Video
from iftyedits.pipeline.jobs import TRANSCRIBE_VIDEO, JobQueue
from iftyedits.services.ingest import IngestService
from iftyedits.services.shorts import build_short_service
from iftyedits.storage.shorts import ShortRender
from iftyedits.ui.formatting import job_target, video_title
from iftyedits.ui.publish import remaining_steps, tab_for
from tests.clip_db_fakes import save_transcript
from tests.clip_fakes import podcast
from tests.conftest import MakeMedia
from tests.unit.test_ui_clips_page import _button, _run, app_env, video  # noqa: F401


@pytest.mark.parametrize(
    ("filename", "title"),
    [
        ("I-Busted-22-MYTHS-In-GTA-6-CaXA_NIo1fY.mp4", "I Busted 22 MYTHS In GTA 6"),
        ("GTA 6_ Gameplay DETAILS WE FOUND.mp4", "GTA 6 Gameplay DETAILS WE FOUND"),
        ("team-celebration.mov", "team celebration"),
        ("___.mp4", "___.mp4"),
    ],
)
def test_video_title(filename: str, title: str) -> None:
    assert video_title(filename) == title


def _job(video_id: int | None) -> Job:
    values: dict[str, object] = dict.fromkeys(Job.__dataclass_fields__)
    values.update(payload={} if video_id is None else {"video_id": video_id})
    return Job(**values)  # type: ignore[arg-type]


def test_job_target_prefers_the_video_name() -> None:
    assert job_target(_job(3), {3: "My_Show.mp4"}) == "My Show"
    assert job_target(_job(4), {3: "My_Show.mp4"}) == "video #4"
    assert job_target(_job(3)) == "video #3"


def test_a_link_job_shows_the_video_it_produced() -> None:
    url = "https://www.youtube.com/watch?v=abc"
    job = replace(_job(3), payload={"url": url}, result={"video_id": 3})
    assert job_target(job, {3: "My_Show.mp4"}) == "My Show"
    assert job_target(job) == "youtube.com/watch?v=abc"


def _item(group: str, key: str, *, done: bool = False, required: bool = True) -> rd.Item:
    return rd.Item(group, key, key, done, required=required)


def test_remaining_steps_group_by_tab_in_working_order() -> None:
    items = [
        _item("Export", "preview_reviewed"),
        _item("Content review", "risk_reviewed"),
        _item("Rights", "source_identified"),
        _item("Upload settings", "audience"),
        _item("Metadata", "title_valid"),
        _item("Export", "export_current"),
        _item("Metadata", "metadata", done=True),
        _item("Rights", "evidence", required=False),
    ]
    assert [tab for tab, _ in remaining_steps(items)] == [
        "Rights",
        "Metadata",
        "Safety review",
        "Edit Short",
        "Checklist",
    ]
    assert tab_for(_item("Rights", "rights_ack")) == "Checklist"
    assert remaining_steps([_item("Metadata", "metadata", done=True)]) == []


def test_thumbnail_is_refused_outside_exports(
    settings: Settings, db_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"x")
    values: dict[str, object] = dict.fromkeys(ShortRender.__dataclass_fields__)
    values.update(id=1, status="succeeded", output_path=outside, duration_s=10.0)
    render = ShortRender(**values)  # type: ignore[arg-type]
    assert build_short_service(settings, db_conn).export_thumbnail(render) is None
    assert not (settings.derived_dir / "shorts" / "thumbs").exists()


def test_clips_page_can_start_transcription(
    app_env: None,
    db_conn: sqlite3.Connection,
    video: Video,
) -> None:
    app = _run("clips.py")
    _button(app, "Transcribe this video").click()
    app.run()
    assert not app.exception
    job = JobQueue(db_conn).latest(TRANSCRIBE_VIDEO, video.id)
    assert job is not None
    assert not [b for b in app.button if b.label == "Transcribe this video"]


def test_clips_page_opens_on_a_video_with_a_transcript(
    app_env: None,
    db_conn: sqlite3.Connection,
    video: Video,
    ingest: IngestService,
    make_media: MakeMedia,
) -> None:
    save_transcript(db_conn, video, podcast())
    ingest.import_video(make_media("Newer.mp4"))
    app = _run("clips.py")
    assert str(app.selectbox(key="clips-video").value).endswith("Show.mp4")
