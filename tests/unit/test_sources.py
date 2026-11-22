"""URL validation, the yt-dlp output parsing and the URL ingest service (no network)."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from iftyedits.adapters.ytdlp import explain_failure, parse_metadata
from iftyedits.config import Settings
from iftyedits.core.errors import InvalidSourceError, JobCancelledError, SourceDownloadError
from iftyedits.core.models import ImportOutcome
from iftyedits.core.sources import SourceMetadata, VideoUrl, parse_video_url
from iftyedits.services.ingest import IngestService
from iftyedits.services.url_ingest import UrlIngestService, safe_name
from iftyedits.storage.video_urls import VideoUrlRepository
from iftyedits.storage.videos import VideoRepository
from tests.conftest import FAKE_VIDEO

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "text",
    [
        f"https://www.youtube.com/watch?v={VID}",
        f"https://youtu.be/{VID}?t=30",
        f"https://m.youtube.com/watch?v={VID}&list=PL123&index=2",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/live/{VID}",
        f"youtube.com/watch?v={VID}",
        f"  https://music.youtube.com/watch?v={VID}  ",
    ],
)
def test_valid_links_become_one_canonical_url(text: str) -> None:
    url = parse_video_url(text)
    assert url == VideoUrl("youtube", VID)
    assert url.canonical == f"https://www.youtube.com/watch?v={VID}"
    assert url.source_key == f"youtube:{VID}"


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("", "Paste"),
        ("   ", "Paste"),
        ("http://www.youtube.com/watch?v=" + VID, "https"),
        ("https://vimeo.com/12345", "YouTube"),
        ("https://evil.example/watch?v=" + VID, "YouTube"),
        ("https://youtube.com.evil.example/watch?v=" + VID, "YouTube"),
        ("https://www.youtube.com/playlist?list=PL1234", "playlist"),
        ("https://www.youtube.com/@somechannel", "channel"),
        ("https://www.youtube.com/watch?v=short", "video id"),
        ("https://www.youtube.com/", "video id"),
        ("not a link at all", "single valid link"),
        ("https://youtu.be/" + VID + " https://youtu.be/" + VID, "single valid link"),
        ("https://www.youtube.com/watch?v=" + "x" * 3000, "single valid link"),
    ],
)
def test_bad_links_are_rejected_with_a_reason(text: str, fragment: str) -> None:
    with pytest.raises(InvalidSourceError, match=fragment):
        parse_video_url(text)


def test_metadata_parsing_and_playlist_refusal() -> None:
    url = parse_video_url(f"https://youtu.be/{VID}")
    meta = parse_metadata(
        '{"title": "T", "uploader": "U", "duration": 61, "upload_date": "20250101", '
        '"live_status": "not_live", "license": "Creative Commons"}',
        url,
    )
    assert (meta.title, meta.uploader, meta.duration_s, meta.is_live) == ("T", "U", 61.0, False)
    assert meta.license == "Creative Commons"
    assert parse_metadata('{"live_status": "is_live"}', url).is_live
    with pytest.raises(SourceDownloadError, match="playlist"):
        parse_metadata('{"_type": "playlist"}', url)
    with pytest.raises(SourceDownloadError, match="could not be read"):
        parse_metadata("not json", url)


@pytest.mark.parametrize(
    ("stderr", "fragment"),
    [
        ("ERROR: [youtube] x: Private video. Sign in", "private"),
        ("ERROR: Sign in to confirm your age", "age-restricted"),
        ("ERROR: This video is DRM protected", "DRM"),
        ("ERROR: Video unavailable", "unavailable"),
        ("ERROR: <urlopen error [Errno 8] nodename nor servname>", "Could not reach"),
        ("ERROR: something odd happened", "something odd"),
    ],
)
def test_failures_are_explained_in_plain_words(stderr: str, fragment: str) -> None:
    assert fragment.lower() in explain_failure(stderr).lower()


class FakeDownloader:
    def __init__(self, *, live: bool = False, duration: float = 60.0, fail: str | None = None):
        self.live, self.duration, self.fail = live, duration, fail
        self.downloads = 0
        self.after_start: Callable[[], None] | None = None

    def fetch_metadata(self, url: VideoUrl) -> SourceMetadata:
        return SourceMetadata(
            url.source_key, url.canonical, "My Great Video!", "Chan", self.duration,
            "20250101", None, self.live,
        )  # fmt: skip

    def download(
        self,
        url: VideoUrl,
        directory: Path,
        *,
        progress: Callable[[float, str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> Path:
        self.downloads += 1
        if self.fail:
            raise SourceDownloadError(self.fail)
        if progress:
            progress(0.5, "half")
        path = directory / f"{url.video_id}.mp4"
        path.write_bytes(FAKE_VIDEO + b"downloaded-bytes" * 50)
        if self.after_start:
            self.after_start()
        return path


@pytest.fixture
def service(
    settings: Settings, ingest: IngestService, db_conn: sqlite3.Connection
) -> UrlIngestService:
    return UrlIngestService(
        settings,
        ingest,
        VideoRepository(db_conn),
        VideoUrlRepository(db_conn),
        FakeDownloader(),
    )


def test_fetch_imports_into_the_vault_and_records_the_link(
    service: UrlIngestService, settings: Settings, db_conn: sqlite3.Connection
) -> None:
    steps: list[float] = []
    out = service.fetch(f"https://youtu.be/{VID}", progress=lambda f, _m: steps.append(f))
    assert out.outcome is ImportOutcome.IMPORTED and not out.reused
    assert out.video.original_filename == f"My-Great-Video-{VID}.mp4"
    assert (settings.originals_dir / out.video.vault_relpath).is_file()
    assert steps == sorted(steps) and steps[-1] == 1.0
    record = VideoUrlRepository(db_conn).get(f"youtube:{VID}")
    assert record is not None and record.status == "downloaded"
    assert record.video_id == out.video.id and record.title == "My Great Video!"
    assert VideoUrlRepository(db_conn).for_video(out.video.id) == record
    assert not list(settings.staging_dir.glob("url-*"))  # own staging folder removed


def test_same_link_twice_reuses_the_original_without_downloading(
    service: UrlIngestService,
) -> None:
    first = service.fetch(f"https://youtu.be/{VID}")
    second = service.fetch(f"https://www.youtube.com/watch?v={VID}&t=5")
    assert second.reused and second.video.id == first.video.id
    assert isinstance(service.downloader, FakeDownloader) and service.downloader.downloads == 1


def test_failure_is_remembered_and_leaves_nothing_behind(
    service: UrlIngestService, settings: Settings, db_conn: sqlite3.Connection
) -> None:
    service.downloader = FakeDownloader(fail="This video is private.")
    with pytest.raises(SourceDownloadError, match="private"):
        service.fetch(f"https://youtu.be/{VID}")
    record = VideoUrlRepository(db_conn).get(f"youtube:{VID}")
    assert record is not None and record.status == "failed" and record.video_id is None
    assert not list(settings.staging_dir.glob("url-*"))
    assert db_conn.execute("SELECT count(*) FROM videos").fetchone()[0] == 0
    service.downloader = FakeDownloader()  # a retry works and clears the failure
    assert not service.fetch(f"https://youtu.be/{VID}").reused
    record = VideoUrlRepository(db_conn).get(f"youtube:{VID}")
    assert record is not None and record.status == "downloaded" and record.error is None


@pytest.mark.parametrize(
    ("live", "duration", "fragment"), [(True, 60.0, "Live"), (False, 99 * 3600.0, "limit")]
)
def test_live_and_overlong_videos_are_refused_before_downloading(
    service: UrlIngestService, live: bool, duration: float, fragment: str
) -> None:
    fake = FakeDownloader(live=live, duration=duration)
    service.downloader = fake
    with pytest.raises(SourceDownloadError, match=fragment):
        service.fetch(f"https://youtu.be/{VID}")
    assert fake.downloads == 0


def test_cancel_during_download_stores_nothing(
    service: UrlIngestService, settings: Settings, db_conn: sqlite3.Connection
) -> None:
    cancel = threading.Event()
    assert isinstance(service.downloader, FakeDownloader)
    service.downloader.after_start = cancel.set
    with pytest.raises(JobCancelledError):
        service.fetch(f"https://youtu.be/{VID}", cancel=cancel)
    assert not list(settings.staging_dir.glob("url-*"))
    assert db_conn.execute("SELECT count(*) FROM videos").fetchone()[0] == 0


def test_invalid_link_never_reaches_the_downloader(service: UrlIngestService) -> None:
    with pytest.raises(InvalidSourceError):
        service.fetch("https://example.com/video")
    assert isinstance(service.downloader, FakeDownloader) and service.downloader.downloads == 0


def test_import_download_refuses_files_outside_staging(
    ingest: IngestService, make_media: Callable[..., Path]
) -> None:
    from iftyedits.core.errors import OriginalProtectionError

    with pytest.raises(OriginalProtectionError):
        ingest.import_download(make_media("a.mp4"))


def test_safe_names_are_ascii_and_bounded() -> None:
    assert safe_name("../../etc/passwd: ünï", VID) == f"etc-passwd-uni-{VID}.mp4"
    assert safe_name("日本語", VID) == f"video-{VID}.mp4"
    assert len(safe_name("a" * 500, VID)) <= 60 + 1 + 11 + 4
