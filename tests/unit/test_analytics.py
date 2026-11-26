# ruff: noqa: F811
"""Phase 8: performance tracking. A local stub speaks Google's wire formats; no real calls."""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from streamlit.testing.v1 import AppTest

from iftyedits import cli
from iftyedits.config import Settings
from iftyedits.core import analytics as an
from iftyedits.core.errors import (
    AnalyticsError,
    YouTubeAuthError,
    YouTubeNotConfiguredError,
)
from iftyedits.services.analytics import AnalyticsService, attributes_of, video_id_from
from iftyedits.services.publishing import PublishingService
from iftyedits.services.youtube_upload import YouTubeUploadService
from iftyedits.storage.analytics import AnalyticsRepository
from iftyedits.storage.db import open_database
from iftyedits.storage.shorts import ShortProject
from iftyedits.ui.analytics import comparison_rows, video_rows
from tests.unit.test_phase69_validation import export, project, video  # noqa: F401
from tests.unit.test_youtube import Google, google, make_client  # noqa: F401

APP = Path(__file__).resolve().parents[2] / "app"
MINE = "aaaaaaaaaaa"
OTHER = "bbbbbbbbbbb"
GONE = "ccccccccccc"


def item(video_id: str, channel: str = "UC123", **stats: str) -> dict[str, object]:
    numbers = {"viewCount": "100", "likeCount": "10", "commentCount": "1"} | stats
    return {
        "id": video_id,
        "snippet": {
            "channelId": channel,
            "title": f"Video {video_id[:1]}",
            "publishedAt": "2026-11-01T00:00:00Z",
        },
        "status": {"privacyStatus": "private"},
        "statistics": numbers,
    }


def report(*video_ids: str) -> dict[str, object]:
    names = ["video", *an.REPORT_METRICS]
    values = [90, 12.5, 14.0, 62.5, 9, 1, 2, 3]
    return {
        "columnHeaders": [{"name": n} for n in names],
        "rows": [[i, *values] for i in video_ids],
    }


# ------------------------------------------------------------------ core


def test_parse_videos_reads_numbers_and_skips_junk() -> None:
    payload = {"items": [item(MINE), "junk", {"no": "id"}]}
    [found] = an.parse_videos(payload)
    assert (found.video_id, found.views, found.likes, found.comments) == (MINE, 100, 10, 1)
    assert found.channel_id == "UC123" and found.privacy == "private"


@pytest.mark.parametrize("payload", [{}, {"items": None}, {"items": []}])
def test_parse_videos_empty(payload: dict[str, object]) -> None:
    assert an.parse_videos(payload) == []


def test_hidden_or_invalid_statistics_stay_unknown() -> None:
    entry = item(MINE) | {"statistics": {"viewCount": "-3", "likeCount": "many"}}
    [found] = an.parse_videos({"items": [entry]})
    assert found.views is None and found.likes is None and found.comments is None


def test_parse_report_matches_columns_by_name() -> None:
    payload = {
        "columnHeaders": [{"name": "views"}, {"name": "video"}],
        "rows": [[5, MINE], [1]],
    }
    rows = an.parse_report(payload)
    assert rows[MINE].views == 5 and rows[MINE].watch_minutes is None and len(rows) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"columnHeaders": [{"name": "video"}]},  # no data yet: Google omits "rows"
        {"columnHeaders": [{"name": "views"}], "rows": [[1]]},
    ],
)
def test_parse_report_without_usable_rows(payload: dict[str, object]) -> None:
    assert an.parse_report(payload) == {}


@pytest.mark.parametrize(
    ("seconds", "group"),
    [(None, ""), (0, ""), (24.9, "under 25 s"), (25, "25 to 45 s"), (45.1, "over 45 s")],
)
def test_length_groups(seconds: float | None, group: str) -> None:
    assert an.length_bucket(seconds) == group


def samples(group: str, count: int, views: int) -> list[an.Sample]:
    return [
        an.Sample(f"{group}{i}", views + i, views // 10, 50.0, {"category": group})
        for i in range(count)
    ]


def test_small_groups_are_shown_but_not_compared() -> None:
    result = an.compare(samples("funny", an.MIN_GROUP, 100) + samples("sad", 4, 900), "category")
    assert not result.comparable and "Not enough data" in result.note
    assert [(g.group, g.videos) for g in result.groups] == [("funny", 5), ("sad", 4)]


def test_comparison_reports_sample_size_and_no_causation() -> None:
    data = samples("funny", 5, 100) + samples("action", 6, 300)
    result = an.compare(data, "category")
    assert result.comparable
    assert "'action'" in result.note and "6 videos" in result.note
    assert "not proof" in result.note
    assert result.groups[0].likes_per_100_views == pytest.approx(9.93, abs=0.02)


def test_unknown_attribute_values_are_left_out() -> None:
    assert an.compare([an.Sample("x", 1, 1, None, {})], "category").groups == ()


@pytest.mark.parametrize(
    "reference",
    [
        MINE,
        f"https://youtube.com/shorts/{MINE}",
        f"https://youtu.be/{MINE}?si=x",
        f"  https://www.youtube.com/watch?v={MINE}&t=3  ",
    ],
)
def test_video_id_from_link_or_id(reference: str) -> None:
    assert video_id_from(reference) == MINE


@pytest.mark.parametrize("reference", ["", "hello", "https://vimeo.com/1", "https://[bad"])
def test_video_id_from_rejects_other_text(reference: str) -> None:
    with pytest.raises(AnalyticsError):
        video_id_from(reference)


# ------------------------------------------------------------------ adapter


def test_requests_are_read_only_and_authorised(google: Google, tmp_path: Path) -> None:
    google.videos = [item(MINE)]
    google.report = report(MINE)
    client = make_client(google, tmp_path)
    assert client.video_list([MINE])["items"]
    client.analytics_report([MINE, OTHER], "2026-11-01", "2026-11-28", ("views",))
    gets = [(p, h) for m, p, h, _ in google.requests if m == "GET"]
    assert all(h["authorization"].startswith("Bearer ") for _, h in gets)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(gets[-1][0]).query)
    assert query["ids"] == ["channel==MINE"]
    assert query["filters"] == [f"video=={MINE},{OTHER}"]
    assert query["dimensions"] == ["video"] and query["maxResults"] == ["2"]
    assert google.count("PUT", "/") == 0 and google.count("POST", "/upload") == 0


def test_untrusted_analytics_host_is_refused(google: Google, tmp_path: Path) -> None:
    good = make_client(google, tmp_path)
    with pytest.raises(YouTubeNotConfiguredError):
        type(good)(good.client, good.store, analytics_base="https://evil.example.com/v2")


def test_extra_permission_is_requested_only_when_asked(google: Google, tmp_path: Path) -> None:
    client = make_client(google, tmp_path, signed_in=False)
    asked: list[str] = []

    def browser(url: str) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        asked.append(query["scope"][0])

    client._open = browser
    for extra in ((), (an.ANALYTICS_SCOPE,)):
        with pytest.raises(YouTubeAuthError):
            client.authorize(wait_s=0.2, extra_scopes=extra)
    assert an.ANALYTICS_SCOPE not in asked[0] and an.ANALYTICS_SCOPE in asked[1]


# ------------------------------------------------------------------ service


@pytest.fixture
def configured(settings: Settings, tmp_path: Path, google: Google) -> Settings:
    secrets = tmp_path / "client.json"
    secrets.write_text(json.dumps({"installed": {"client_id": "x", "client_secret": "y"}}))
    return settings.model_copy(
        update={
            "youtube_client_secrets": secrets,
            "youtube_token_path": tmp_path / "cfg" / "token.json",
        }
    )


def make_analytics(
    settings: Settings,
    conn: sqlite3.Connection,
    g: Google,
    tmp_path: Path,
    *,
    detailed: bool = True,
) -> AnalyticsService:
    client = make_client(g, tmp_path)
    scope = an.ANALYTICS_SCOPE if detailed else ""
    saved = {"access_token": "t", "refresh_token": "r", "expires_at": 9e12, "scope": scope}
    client.store.save(saved)
    youtube = YouTubeUploadService(
        settings, conn, PublishingService(settings, conn, None), lambda: client
    )
    return AnalyticsService(youtube, AnalyticsRepository(conn), today=lambda: date(2026, 11, 28))


def test_not_connected_is_explained(settings: Settings, db_conn: sqlite3.Connection) -> None:
    youtube = YouTubeUploadService(settings, db_conn, PublishingService(settings, db_conn, None))
    service = AnalyticsService(youtube, AnalyticsRepository(db_conn))
    status = service.status()
    assert not status.connected and status.problem
    service.add_video(MINE)
    with pytest.raises(AnalyticsError, match="Settings"):
        service.refresh()


def test_nothing_followed_makes_no_request(
    configured: Settings, db_conn: sqlite3.Connection, google: Google, tmp_path: Path
) -> None:
    result = make_analytics(configured, db_conn, google, tmp_path).refresh()
    assert result.updated == 0 and "No videos" in result.note
    assert google.requests == []


@pytest.mark.parametrize("days", [0, 731])
def test_window_is_bounded(
    configured: Settings, db_conn: sqlite3.Connection, google: Google, tmp_path: Path, days: int
) -> None:
    with pytest.raises(AnalyticsError):
        make_analytics(configured, db_conn, google, tmp_path).refresh(days)


def test_refresh_keeps_only_videos_of_the_connected_channel(
    configured: Settings, db_conn: sqlite3.Connection, google: Google, tmp_path: Path
) -> None:
    google.videos = [item(MINE), item(OTHER, channel="UCsomeoneelse")]
    google.report = report(MINE)
    service = make_analytics(configured, db_conn, google, tmp_path)
    for video_id in (MINE, OTHER, GONE):
        service.add_video(video_id)
    result = service.refresh(7)
    assert result.updated == 1 and result.detailed == 1
    assert result.window == ("2026-11-22", "2026-11-28")
    reasons = dict(result.skipped)
    assert "not on your connected channel" in reasons[OTHER] and "no video" in reasons[GONE]
    rows = {v.video_id: v for v in service.videos()}
    assert rows[MINE].views == 100 and rows[MINE].avg_view_percent == 62.5
    assert rows[MINE].subscribers_gained == 3 and rows[MINE].window_start == "2026-11-22"
    assert rows[OTHER].fetched_at is None and service.repo.snapshot_count(OTHER) == 0
    asked = [p for _, p, _, _ in google.requests if p.startswith("/v2/reports")]
    assert asked and all(OTHER not in p for p in asked)


def test_without_the_optional_permission_no_report_is_requested(
    configured: Settings, db_conn: sqlite3.Connection, google: Google, tmp_path: Path
) -> None:
    google.videos = [item(MINE)]
    service = make_analytics(configured, db_conn, google, tmp_path, detailed=False)
    service.add_video(MINE)
    assert not service.status().detailed
    result = service.refresh()
    assert result.updated == 1 and result.detailed == 0 and "Allow detailed" in result.note
    assert google.count("GET", "/v2/reports") == 0
    [row] = service.videos()
    assert row.views == 100 and row.watch_minutes is None and row.window_start is None


def test_report_failure_keeps_basic_numbers(
    configured: Settings, db_conn: sqlite3.Connection, google: Google, tmp_path: Path
) -> None:
    google.videos = [item(MINE)]
    google.report_status = 403
    service = make_analytics(configured, db_conn, google, tmp_path)
    service.add_video(MINE)
    result = service.refresh()
    assert result.updated == 1 and "not available" in result.note
    assert service.videos()[0].views == 100


def test_empty_report_says_numbers_arrive_late(
    configured: Settings, db_conn: sqlite3.Connection, google: Google, tmp_path: Path
) -> None:
    google.videos = [item(MINE)]
    service = make_analytics(configured, db_conn, google, tmp_path)
    service.add_video(MINE)
    assert "days late" in service.refresh().note


def test_history_is_kept_and_latest_shown(
    configured: Settings, db_conn: sqlite3.Connection, google: Google, tmp_path: Path
) -> None:
    service = make_analytics(configured, db_conn, google, tmp_path, detailed=False)
    service.add_video(MINE)
    for views in ("10", "25"):
        google.videos = [item(MINE, viewCount=views)]
        service.refresh()
    assert service.repo.snapshot_count(MINE) == 2
    assert [v.views for v in service.videos()] == [25]


def test_follow_and_stop(db_conn: sqlite3.Connection, settings: Settings) -> None:
    youtube = YouTubeUploadService(settings, db_conn, PublishingService(settings, db_conn, None))
    service = AnalyticsService(youtube, AnalyticsRepository(db_conn))
    assert service.add_video(f"https://youtu.be/{MINE}") == MINE
    with pytest.raises(AnalyticsError, match="already"):
        service.add_video(MINE)
    assert service.remove_video(MINE) and not service.remove_video(MINE)
    assert service.videos() == []


def test_uploads_are_followed_and_linked_to_their_short(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject, video: Any
) -> None:
    render_id = export(settings, db_conn, video, project)
    with db_conn:
        db_conn.execute(
            "INSERT INTO youtube_uploads (project_id, render_id, status, details, fingerprint, "
            "youtube_video_id, created_at, updated_at) VALUES (?, ?, 'uploaded', '{}', 'f', ?, "
            "'2026-11-01', '2026-11-01')",
            (project.id, render_id, MINE),
        )
    youtube = YouTubeUploadService(settings, db_conn, PublishingService(settings, db_conn, None))
    service = AnalyticsService(youtube, AnalyticsRepository(db_conn))
    assert service.sync_uploads() == 1 and service.sync_uploads() == 0
    [row] = service.videos()
    assert (row.project_id, row.source, row.duration_s) == (project.id, "upload", 20.0)
    attributes = attributes_of(row)
    assert attributes["length"] == "under 25 s" and attributes["framing"] == "steady crop"
    assert attributes["captions"]


def test_a_later_link_fills_in_the_short(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    repo = AnalyticsRepository(db_conn)
    assert repo.track(MINE, None, "manual")
    assert not repo.track(MINE, project.id, "upload")
    assert repo.latest()[0].project_id == project.id


def test_unknown_numbers_show_a_dash(db_conn: sqlite3.Connection) -> None:
    repo = AnalyticsRepository(db_conn)
    repo.track(MINE, None, "manual")
    [row] = video_rows(repo.latest())
    assert row["Views"] == "-" and row["Refreshed (UTC)"] == "never" and row["Video"] == MINE
    assert comparison_rows(an.compare(samples("a", 1, 10), "category"))[0]["Videos"] == "1"


# ------------------------------------------------------------------ page and CLI


@pytest.fixture
def page_env(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("IFTYEDITS_HOME", str(home))
    monkeypatch.setenv("IFTYEDITS_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("IFTYEDITS_MODELS_DIR", str(settings.models_dir))
    monkeypatch.delenv("IFTYEDITS_YOUTUBE_CLIENT_SECRETS", raising=False)
    yield


def test_page_empty_state_then_add_and_stop(settings: Settings, page_env: None) -> None:
    page = AppTest.from_file(str(APP / "views" / "analytics.py"), default_timeout=60).run()
    assert not page.exception
    assert any("Connect your YouTube account" in i.value for i in page.info)
    refresh = next(b for b in page.button if b.label == "Refresh numbers")
    assert refresh.disabled
    page.text_input[0].input("nonsense")
    next(b for b in page.button if b.label == "Follow this video").click().run()
    assert any("11-character" in e.value for e in page.error)
    page.text_input[0].input(f"https://youtube.com/shorts/{MINE}")
    next(b for b in page.button if b.label == "Follow this video").click().run()
    assert not page.exception and len(page.dataframe) >= 1
    next(b for b in page.button if b.label == "Stop following").click().run()
    assert not page.exception
    with open_database(settings.db_path) as conn:
        assert AnalyticsRepository(conn).ids() == []


def test_cli_add_and_list(
    settings: Settings, page_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["analytics"]) == 0
    assert "No videos" in capsys.readouterr().out
    assert cli.main(["analytics", "add"]) != 0
    assert cli.main(["analytics", "add", MINE]) == 0
    assert cli.main(["analytics"]) == 0
    assert MINE in capsys.readouterr().out
    assert cli.main(["analytics", "refresh"]) != 0  # not connected
