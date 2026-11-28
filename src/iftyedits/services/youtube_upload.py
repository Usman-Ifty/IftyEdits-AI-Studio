"""Optional YouTube upload with explicit, exact approval.

Flow: ``prepare`` (needs the Phase 6 checklist confirmed) saves a draft of the exact details,
``approve`` (needs the owner's typed approval) queues a background job, and ``run_upload`` (the
worker) re-checks everything against the approved fingerprint before any byte leaves the machine.
Uploads are private or unlisted only; the studio never publishes or schedules.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from iftyedits.adapters.youtube import (
    TokenStore,
    YouTubeClient,
    default_token_path,
    load_client_config,
)
from iftyedits.config import Settings, project_home
from iftyedits.core import readiness as rd
from iftyedits.core import youtube as yt
from iftyedits.core.errors import (
    JobCancelledError,
    PublishError,
    UploadNotApprovedError,
    YouTubeError,
    YouTubeNotConfiguredError,
    YouTubeQuotaError,
)
from iftyedits.pipeline.jobs import UPLOAD_YOUTUBE, JobQueue
from iftyedits.services.publishing import PublishingService
from iftyedits.services.upload_package import build_package
from iftyedits.storage.youtube import UploadRecord, YouTubeRepository

ClientFactory = Callable[[], YouTubeClient]


@dataclass(frozen=True, slots=True)
class Connection:
    """What the Settings and Publish screens show about the YouTube connection."""

    configured: bool
    connected: bool
    problem: str = ""
    token_location: str = ""


@dataclass(frozen=True, slots=True)
class DraftResult:
    """Outcome of preparing an upload."""

    record: UploadRecord | None
    details: yt.UploadDetails | None
    problems: tuple[str, ...] = field(default=())


def sha256_file(path: Path) -> str:
    """Streamed SHA-256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class YouTubeUploadService:
    """Use cases for connecting, approving and uploading."""

    def __init__(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        publishing: PublishingService,
        client_factory: ClientFactory | None = None,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self.conn = conn
        self.publishing = publishing
        self.repo = YouTubeRepository(conn)
        self.queue = JobQueue(conn)
        self._factory = client_factory or self._default_client
        self._now = now

    # ------------------------------------------------------------------ connection

    def _store(self) -> TokenStore:
        return TokenStore(
            self.settings.youtube_token_path or default_token_path(),
            forbidden_root=project_home(),
        )

    def _default_client(self) -> YouTubeClient:
        client = load_client_config(self.settings.youtube_client_secrets)
        return YouTubeClient(
            client,
            self._store(),
            chunk_bytes=self.settings.youtube_chunk_mb * 1024 * 1024,
            api_base=self.settings.youtube_api_base,
            upload_base=self.settings.youtube_upload_base,
            analytics_base=self.settings.youtube_analytics_base,
        )

    def connection(self) -> Connection:
        """Configured / connected, with the reason when not. Makes no network call."""
        try:
            load_client_config(self.settings.youtube_client_secrets)
            store = self._store()
        except YouTubeNotConfiguredError as exc:
            return Connection(False, False, str(exc))
        return Connection(True, store.load() is not None, "", str(store.path))

    def client(self) -> YouTubeClient:
        """A client for the saved connection (also used for reading analytics)."""
        return self._factory()

    def connect(self, on_url: Callable[[str], None] | None = None) -> None:
        """Browser sign-in. Blocks until finished."""
        self._factory().authorize(on_url=on_url)

    def disconnect(self) -> None:
        """Forget the saved sign-in."""
        self._factory().disconnect()

    # ------------------------------------------------------------------ details

    def _details(
        self, project_id: int, channel_id: str, channel_title: str
    ) -> yt.UploadDetails | None:
        render = self.publishing.latest_export(project_id)
        if render is None or render.output_path is None or not render.output_path.is_file():
            return None
        package = build_package(self.publishing, project_id)
        settings, _ = self.publishing.choices(project_id)
        path = render.output_path
        return yt.UploadDetails(
            project_id=project_id,
            render_id=render.id,
            file_name=path.name,
            file_size=path.stat().st_size,
            file_sha256=sha256_file(path),
            title=str(package["title"]),
            description=str(package["description"]),
            tags=tuple(str(t) for t in package["search_tags"]),
            visibility=settings.get("visibility", ""),
            made_for_kids=settings.get("audience", "") == "for_kids",
            channel_id=channel_id,
            channel_title=channel_title,
        )

    def _require_ready(self, project_id: int) -> None:
        report = self.publishing.readiness(project_id)
        if report.status != rd.READY:
            raise PublishError(
                f"This Short is not ready for upload (“{report.label}”). Finish and confirm the "
                "checklist in the Upload package first."
            )

    # ------------------------------------------------------------------ prepare / approve

    def prepare(self, project_id: int) -> DraftResult:
        """Save a draft of exactly what would be uploaded (reads the channel from YouTube)."""
        self._require_ready(project_id)
        client = self._factory()
        if not client.connected():
            raise YouTubeNotConfiguredError(
                "YouTube is not connected. Connect your account in Settings.", yt.KIND_AUTH
            )
        channel_id, channel_title = client.channel()
        details = self._details(project_id, channel_id, channel_title)
        if details is None:
            raise PublishError("There is no finished export file to upload.")
        problems = yt.validate(details)
        if problems:
            return DraftResult(None, details, tuple(problems))
        return DraftResult(self.repo.create_draft(details), details)

    def uploads_today(self) -> int:
        """Uploads started since 00:00 UTC."""
        start = self._now().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.repo.started_since(start.isoformat())

    def approve(self, upload_id: int, typed: str, *, allow_duplicate: bool = False) -> int:
        """The owner's explicit approval of a draft. Queues the upload and returns the job id."""
        record = self.repo.get(upload_id)
        if record is None or record.status != yt.DRAFT:
            raise UploadNotApprovedError("That upload is not waiting for approval.")
        if typed.strip() != yt.APPROVAL_TEXT:
            raise UploadNotApprovedError(f"Type exactly “{yt.APPROVAL_TEXT}” to approve.")
        self._require_ready(record.project_id)
        current = self._details(
            record.project_id,
            str(record.details["channel_id"]),
            str(record.details["channel_title"]),
        )
        if current is None or current.fingerprint() != record.fingerprint:
            raise UploadNotApprovedError(
                "The video, title, description or settings changed after this draft was made, "
                "so it was not approved. Discard this draft, then press “Review upload "
                "details” again."
            )
        if not allow_duplicate:
            for other in self.repo.list_for_project(record.project_id, 50):
                if (
                    other.status == yt.UPLOADED
                    and other.details.get("file_sha256") == record.details["file_sha256"]
                ):
                    raise UploadNotApprovedError(
                        "This exact video was already uploaded. Tick “upload again” if you "
                        "really want a second copy."
                    )
        limit = self.settings.youtube_daily_upload_limit
        if limit and self.uploads_today() >= limit:
            raise YouTubeQuotaError(
                f"The studio's own daily limit of {limit} uploads is reached (it protects your "
                "YouTube quota). Try tomorrow or raise IFTYEDITS_YOUTUBE_DAILY_UPLOAD_LIMIT.",
                yt.KIND_QUOTA,
            )
        if not self.repo.approve(upload_id, record.fingerprint):
            raise UploadNotApprovedError("That upload is no longer waiting for approval.")
        job_id = self.queue.enqueue(UPLOAD_YOUTUBE, {"upload_id": upload_id})
        self.repo.set_job(upload_id, job_id)
        return job_id

    def retry(self, upload_id: int) -> int:
        """Retry an approved upload that failed. Everything is re-verified when it runs."""
        record = self.repo.get(upload_id)
        if record is None or not self.repo.reapprove_after_failure(upload_id):
            raise UploadNotApprovedError("Only a failed, approved upload can be retried.")
        job_id = self.queue.enqueue(UPLOAD_YOUTUBE, {"upload_id": upload_id})
        self.repo.set_job(upload_id, job_id)
        return job_id

    def cancel(self, upload_id: int) -> bool:
        """Cancel before it starts, or ask a running upload to stop between chunks."""
        record = self.repo.get(upload_id)
        if record is None:
            return False
        done = self.repo.cancel(upload_id)
        if done and record.job_id:
            self.queue.request_cancel(record.job_id)
        if not done and record.status == yt.UPLOADING and record.job_id:
            self.queue.request_cancel(record.job_id)  # the worker stops between chunks
            return True
        return done

    def records(self, project_id: int) -> list[UploadRecord]:
        """Upload history for a Short, newest first."""
        return self.repo.list_for_project(project_id)

    # ------------------------------------------------------------------ worker side

    def run_upload(
        self,
        upload_id: int,
        *,
        on_progress: Callable[[float, str], None] | None = None,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> UploadRecord:
        """Upload an approved record after re-verifying every approved detail."""
        record = self.repo.get(upload_id)
        if record is None or record.status != yt.APPROVED or not record.approved_at:
            raise UploadNotApprovedError("This upload has not been approved.")
        if not self.repo.start(upload_id):
            raise UploadNotApprovedError("This upload is already running or finished.")
        try:
            self._verify(record)
            client = self._factory()
            resource = yt.video_resource(self._as_details(record))
            path = self._export_path(record)

            def progress(sent: int, total: int) -> None:
                self.repo.progress(upload_id, sent)
                if on_progress:
                    on_progress(sent / total if total else 1.0, "Uploading to YouTube")

            video_id = client.upload(path, resource, on_progress=progress, cancelled=cancelled)
        except JobCancelledError:
            self.repo.fail(
                upload_id, yt.KIND_CANCELLED, "You cancelled this upload. Nothing was published."
            )
            raise
        except UploadNotApprovedError as exc:
            self.repo.fail(upload_id, yt.KIND_OTHER, str(exc))
            raise
        except YouTubeError as exc:
            self.repo.fail(upload_id, exc.kind, str(exc))
            raise
        self.repo.succeed(upload_id, video_id)
        done = self.repo.get(upload_id)
        if done is None:
            raise UploadNotApprovedError("The upload record disappeared.")
        return done

    @staticmethod
    def _as_details(record: UploadRecord) -> yt.UploadDetails:
        d = record.details
        return yt.UploadDetails(
            project_id=int(d["project_id"]),
            render_id=int(d["render_id"]),
            file_name=str(d["file_name"]),
            file_size=int(d["file_size"]),
            file_sha256=str(d["file_sha256"]),
            title=str(d["title"]),
            description=str(d["description"]),
            tags=tuple(str(t) for t in d["tags"]),
            visibility=str(d["visibility"]),
            made_for_kids=bool(d["made_for_kids"]),
            channel_id=str(d["channel_id"]),
            channel_title=str(d["channel_title"]),
        )

    def _export_path(self, record: UploadRecord) -> Path:
        render = self.publishing.latest_export(record.project_id)
        if render is None or render.output_path is None:
            raise UploadNotApprovedError("The approved export no longer exists.")
        return render.output_path

    def _verify(self, record: UploadRecord) -> None:
        """Everything approved must still be true, including the destination channel."""
        self._require_ready_for_worker(record.project_id)
        approved = self._as_details(record)
        current = self._details(approved.project_id, approved.channel_id, approved.channel_title)
        if current is None or current.fingerprint() != record.fingerprint:
            raise UploadNotApprovedError(
                "The video or its details changed after approval, so nothing was uploaded. "
                "Prepare and approve the upload again."
            )
        channel_id, _ = self._factory().channel()
        if channel_id != approved.channel_id:
            raise UploadNotApprovedError(
                "The connected YouTube channel is not the one you approved, so nothing was "
                "uploaded."
            )

    def _require_ready_for_worker(self, project_id: int) -> None:
        try:
            self._require_ready(project_id)
        except PublishError as exc:
            raise UploadNotApprovedError(str(exc)) from exc


def build_youtube_service(settings: Settings, conn: sqlite3.Connection) -> YouTubeUploadService:
    """A service wired to the publishing service and the configured credentials."""
    from iftyedits.services.publishing import build_publishing_service

    return YouTubeUploadService(settings, conn, build_publishing_service(settings, conn))
