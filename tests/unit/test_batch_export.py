# ruff: noqa: F811
"""Exporting several favourite suggestions at once."""

from __future__ import annotations

import sqlite3

from iftyedits.config import Settings
from iftyedits.core.clip_models import ClipDecision
from iftyedits.core.models import Video
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.services.shorts import build_short_service
from iftyedits.storage.clips import ClipRepository
from tests.unit.test_ui_clips_page import _analysed, _button, _run, app_env, video  # noqa: F401


def _long_enough(conn: sqlite3.Connection, video: Video) -> None:
    with conn:
        conn.execute("UPDATE videos SET duration_s = 3600 WHERE id = ?", (video.id,))


def test_each_candidate_is_queued_once(
    settings: Settings,
    db_conn: sqlite3.Connection,
    video: Video,
) -> None:
    _analysed(db_conn, video)
    _long_enough(db_conn, video)
    current = ClipRepository(db_conn).current(video.id)
    assert current is not None
    keys = [c.key for c in ClipRepository(db_conn).candidates(current.id)][:2]
    service = build_short_service(settings, db_conn)
    first = service.export_candidates(video.id, keys)
    assert (first.queued, first.reused, first.failed) == (2, 0, ())
    again = service.export_candidates(video.id, keys)
    assert (again.queued, again.reused) == (0, 2)
    assert len(JobQueue(db_conn).list_active()) == 2


def test_one_bad_clip_does_not_stop_the_others(
    settings: Settings,
    db_conn: sqlite3.Connection,
    video: Video,
) -> None:
    _analysed(db_conn, video)
    _long_enough(db_conn, video)
    current = ClipRepository(db_conn).current(video.id)
    assert current is not None
    good = ClipRepository(db_conn).candidates(current.id)[0].key
    result = build_short_service(settings, db_conn).export_candidates(video.id, ["gone", good])
    assert result.queued == 1 and [key for key, _ in result.failed] == ["gone"]


def test_button_appears_only_with_favourites(
    app_env: None,
    db_conn: sqlite3.Connection,
    video: Video,
) -> None:
    _analysed(db_conn, video)
    _long_enough(db_conn, video)
    app = _run("clips.py")
    assert not [b for b in app.button if b.label.startswith("Export favourites")]
    repo = ClipRepository(db_conn)
    current = repo.current(video.id)
    assert current is not None
    repo.decide(repo.candidates(current.id)[0].id, ClipDecision.FAVORITE)
    app = _run("clips.py")
    _button(app, "Export favourites (1)").click()
    app.run()
    assert not app.exception
    assert any("Queued 1 export" in s.value for s in app.success)
    assert len(JobQueue(db_conn).list_active()) == 1
