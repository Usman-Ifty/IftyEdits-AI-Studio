"""Upload preparation service: metadata, review, rights, readiness, package (fake model)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from iftyedits.config import Settings
from iftyedits.core import readiness as rd
from iftyedits.core import rights as rt
from iftyedits.core import safety as sf
from iftyedits.core.errors import LlmRateLimitError, LlmTimeoutError, PublishError
from iftyedits.core.models import TranscriptSegment, Video
from iftyedits.core.shorts import ShortConfig
from iftyedits.services.ingest import IngestService
from iftyedits.services.publishing import PublishingService
from iftyedits.services.upload_package import build_package, write_package
from iftyedits.storage.publishing import PublishRepository
from iftyedits.storage.shorts import KIND_EXPORT, ShortProject, ShortRepository
from iftyedits.storage.transcripts import NewTranscript, TranscriptRepository
from tests.conftest import MakeMedia

CONFIG = ShortConfig(start_s=0.0, end_s=20.0)
SPEECH = ["we met at the park", "he said you are a total idiot", "and then we left"]


class FakeModel:
    name = "fake-model"

    def __init__(self, *replies: object) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 2048) -> str:
        self.calls.append((system, user))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return str(reply)


META_REPLY = json.dumps(
    {
        "titles": [
            {"text": "The park meeting", "style": "informative"},
            {"text": "Wait for it", "style": "hook"},
        ],
        "recommended_index": 0,
        "description": "A short scene at the park.",
        "hashtags": ["#park", "#shorts"],
        "tags": ["park", "meeting"],
        "summary": "Two people meet at a park.",
        "confidence": "medium",
        "thumbnail_frame_s": 3.0,
        "thumbnail_text": "AT THE PARK",
    }
)
SAFETY_REPLY = json.dumps(
    {
        "summary": "One insult.",
        "flags": [
            {
                "category": "harassment",
                "severity": "high",
                "start_s": 5.0,
                "end_s": 8.0,
                "excerpt": "you are a total idiot",
                "explanation": "Insult.",
            }
        ],
    }
)
CLEAN_REPLY = json.dumps({"summary": "Nothing found.", "flags": []})


@pytest.fixture
def video(ingest: IngestService, make_media: MakeMedia) -> Video:
    return ingest.import_video(make_media("Talk.mp4")).video


@pytest.fixture
def project(db_conn: sqlite3.Connection, video: Video) -> ShortProject:
    segs = [TranscriptSegment(i * 5.0, i * 5.0 + 4.0, t) for i, t in enumerate(SPEECH)]
    TranscriptRepository(db_conn).save(
        NewTranscript(
            video_id=video.id,
            audio_sha256="a" * 64,
            engine="fake",
            engine_version="1",
            model="m",
            model_revision=None,
            device="cpu",
            compute_type="int8",
            language_requested=None,
            language="en",
            language_probability=0.99,
            duration_s=20.0,
            params_hash="p" * 8,
            params={},
            segments=segs,
        )
    )
    return ShortRepository(db_conn).create_project(video.id, "k", "Park scene", 0.0, 20.0, CONFIG)


def svc(settings: Settings, conn: sqlite3.Connection, model: FakeModel | None) -> PublishingService:
    return PublishingService(settings, conn, model, clock=lambda: "2026-11-22T10:00:00+00:00")


def make_export(
    settings: Settings, conn: sqlite3.Connection, video: Video, project: ShortProject
) -> int:
    repo = ShortRepository(conn)
    digest = project.config.config_hash(video_sha256=video.sha256, kind=KIND_EXPORT, has_audio=True)
    render = repo.create_render(project.id, KIND_EXPORT, digest, project.config)
    repo.mark_running(render.id)
    out = settings.exports_dir / f"short-{render.id}.mp4"
    out.write_bytes(b"x" * 100)
    repo.mark_succeeded(
        render.id,
        output_path=out,
        width=1080,
        height=1920,
        duration_s=20.0,
        size_bytes=100,
        has_audio=True,
        encoder="libx264",
    )
    return render.id


# ---------------------------------------------------------------- metadata


def test_generation_saves_validated_metadata_and_grounds_prompt(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    model = FakeModel(META_REPLY)
    service = svc(settings, db_conn, model)
    result = service.generate_metadata(project.id)
    assert result.ok
    stored = service.metadata(project.id)
    assert stored is not None
    assert stored.metadata.selected_title == "The park meeting"
    assert stored.provenance["titles"] == "ai" and stored.model == "fake-model"
    assert "we met at the park" in model.calls[0][1]
    assert service.metadata_is_current(project.id)


def test_no_model_says_so_and_saves_nothing(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    service = svc(settings, db_conn, None)
    result = service.generate_metadata(project.id)
    assert not result.ok and "No AI model" in result.message
    assert service.metadata(project.id) is None


@pytest.mark.parametrize(
    "failure",
    [LlmTimeoutError("slow"), LlmRateLimitError("limit"), "not json at all", "{}"],
)
def test_provider_failure_or_junk_changes_nothing(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject, failure: object
) -> None:
    service = svc(settings, db_conn, FakeModel(META_REPLY))
    service.generate_metadata(project.id)
    before = service.metadata(project.id)
    broken = svc(settings, db_conn, FakeModel(failure))
    result = broken.generate_metadata(project.id)
    assert not result.ok
    assert broken.metadata(project.id) == before


def test_field_regeneration_keeps_user_title_and_other_fields(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    service = svc(settings, db_conn, FakeModel(META_REPLY))
    service.generate_metadata(project.id)
    service.edit_metadata(project.id, selected_title="My own title")
    again = json.dumps({"hashtags": ["#new"], "tags": ["newtag"]})
    service.model = FakeModel(again)
    assert service.generate_metadata(project.id, ["hashtags", "tags"]).ok
    meta = service.metadata(project.id)
    assert meta is not None
    assert meta.metadata.selected_title == "My own title"
    assert meta.metadata.hashtags == ("#new",)
    assert meta.metadata.description == "A short scene at the park."
    # a full regeneration also keeps the chosen title
    service.model = FakeModel(META_REPLY)
    service.generate_metadata(project.id)
    again_meta = service.metadata(project.id)
    assert again_meta is not None and again_meta.metadata.selected_title == "My own title"


def test_edit_validates_and_marks_manual(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    service = svc(settings, db_conn, None)
    meta = service.edit_metadata(
        project.id,
        selected_title="  <b>Hi</b>\x00 there ",
        hashtags=["tag", "#tag", "#Other"],
        thumbnail_frame_s=2.5,
    )
    assert "\x00" not in meta.selected_title and meta.hashtags == ("#tag", "#Other")
    stored = service.metadata(project.id)
    assert stored is not None and stored.provenance["selected_title"] == "manual"
    with pytest.raises(PublishError):
        service.edit_metadata(project.id, thumbnail_frame_s=99)
    with pytest.raises(PublishError):
        service.edit_metadata(project.id, bogus="x")


def test_editing_text_withdraws_its_reviewed_tick(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    service = svc(settings, db_conn, None)
    service.edit_metadata(project.id, selected_title="One")
    service.set_check(project.id, "title_reviewed", True)
    service.set_check(project.id, "tags_reviewed", True)
    service.edit_metadata(project.id, selected_title="Two")
    assert service.facts(project.id).checks == {"tags_reviewed"}


def test_metadata_goes_stale_when_the_clip_changes(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    service = svc(settings, db_conn, FakeModel(META_REPLY))
    service.generate_metadata(project.id)
    ShortRepository(db_conn).save_config(project.id, ShortConfig(start_s=0.0, end_s=9.0))
    assert not service.metadata_is_current(project.id)
    assert not service.facts(project.id).metadata_current
    service.accept_metadata_for_current_clip(project.id)
    assert service.metadata_is_current(project.id)


def test_clip_without_transcript_is_flagged_in_notes(
    settings: Settings, db_conn: sqlite3.Connection, video: Video
) -> None:
    bare = ShortRepository(db_conn).create_project(video.id, "bare", "Bare", 0.0, 10.0, CONFIG)
    service = svc(settings, db_conn, FakeModel(META_REPLY))
    result = service.generate_metadata(bare.id)
    assert result.ok and any("no transcript" in n for n in result.notes)


# ---------------------------------------------------------------- content review


def test_review_without_model_is_unable_and_blocks_readiness(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    service = svc(settings, db_conn, None)
    review = service.run_review(project.id)
    assert review.status == sf.STATUS_UNAVAILABLE and review.risk_level == sf.UNABLE
    assert service.facts(project.id).review_status != sf.STATUS_OK
    report = service.readiness(project.id)
    assert any("review" in i.label.lower() for i in report.open_items)


def test_review_failure_is_recorded_not_hidden(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    service = svc(settings, db_conn, FakeModel(LlmTimeoutError("timed out")))
    review = service.run_review(project.id)
    assert review.status == sf.STATUS_FAILED and "timed out" in review.note


def test_flag_dismissal_needs_a_note_and_survives_re_review(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    service = svc(settings, db_conn, FakeModel(SAFETY_REPLY))
    review = service.run_review(project.id)
    assert review.risk_level == sf.HIGH
    key = review.flags[0].key
    with pytest.raises(PublishError):
        service.resolve_flag(project.id, key, sf.RESOLUTION_DISMISSED, "")
    service.resolve_flag(project.id, key, sf.RESOLUTION_DISMISSED, "Friendly teasing, context ok")
    assert service.facts(project.id).flags[0][2] == sf.RESOLUTION_DISMISSED
    second = service.run_review(project.id)
    assert second.id != review.id and len(service.repo.reviews(project.id)) == 2
    assert service.facts(project.id).flags[0][2] == sf.RESOLUTION_DISMISSED
    service.resolve_flag(project.id, key, "reopened", "")
    assert service.facts(project.id).flags[0][2] is None  # open again
    assert len(service.repo.resolution_history(project.id)) == 2
    with pytest.raises(PublishError):
        service.resolve_flag(project.id, "nope", sf.RESOLUTION_ACKNOWLEDGED, "x")


def test_review_goes_stale_after_edit(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    service = svc(settings, db_conn, FakeModel(CLEAN_REPLY))
    service.run_review(project.id)
    assert service.review_is_current(project.id)
    ShortRepository(db_conn).save_config(project.id, ShortConfig(start_s=1.0, end_s=19.0))
    assert not service.review_is_current(project.id)


def test_manual_review_needs_a_note_and_counts(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    service = svc(settings, db_conn, None)
    with pytest.raises(PublishError):
        service.record_manual_review(project.id, " ")
    review = service.record_manual_review(project.id, "Watched twice, fine")
    assert review.status == sf.STATUS_MANUAL
    assert service.facts(project.id).review_status == sf.STATUS_MANUAL


# ---------------------------------------------------------------- rights


def test_rights_default_unknown_inherit_per_source_and_keep_history(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject, video: Video
) -> None:
    service = svc(settings, db_conn, None)
    assert service.rights(video.id).status == rt.UNKNOWN
    with pytest.raises(PublishError):
        service.save_rights(video.id, rt.Rights(status=rt.LICENSE), confirmed=True)
    saved = service.save_rights(
        video.id,
        rt.Rights(status=rt.PERMISSION, evidence="Email 2026-11-01 from owner"),
        confirmed=True,
    )
    assert saved.reviewed_on == "2026-11-22" and saved.user_confirmed
    other = ShortRepository(db_conn).create_project(video.id, "k2", "Other", 0.0, 5.0, CONFIG)
    assert service.rights(other.video_id).status == rt.PERMISSION
    service.save_rights(video.id, rt.Rights(status=rt.RESTRICTED), confirmed=True)
    history = service.repo.rights_history(video.id)
    assert len(history) >= 2
    assert any("status" in c.changes or c.new_status for c in history)


# ---------------------------------------------------------------- readiness and workflow


def finish_checklist(service: PublishingService, project_id: int, video_id: int) -> None:
    service.edit_metadata(
        project_id, selected_title="The park meeting", description="A park scene."
    )
    service.set_setting(project_id, "audience", "not_for_kids")
    service.set_setting(project_id, "visibility", "private")
    for key in rd.USER_CHECKS:
        service.set_check(project_id, key, True)
    service.save_rights(
        video_id,
        rt.Rights(status=rt.ORIGINAL, source_title="Talk", creator="me"),
        confirmed=True,
    )
    service.record_manual_review(project_id, "Watched it, nothing concerning")


def test_full_workflow_confirm_and_revocation(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject, video: Video
) -> None:
    service = svc(settings, db_conn, None)
    assert service.readiness(project.id).status in (rd.DRAFT, rd.METADATA_INCOMPLETE)
    finish_checklist(service, project.id, video.id)
    # no export yet
    assert not service.readiness(project.id).can_confirm
    with pytest.raises(PublishError):
        service.confirm_ready(project.id)
    export_id = make_export(settings, db_conn, video, project)
    assert service.readiness(project.id).status == rd.AWAITING
    report = service.confirm_ready(project.id)
    assert report.status == rd.READY
    # persisted: a new service on the same DB agrees
    again = svc(settings, db_conn, None)
    assert again.readiness(project.id).status == rd.READY
    stored = PublishRepository(db_conn).get_readiness(project.id)
    assert stored is not None and stored.confirmed_render_id == export_id
    # editing metadata revokes
    again.edit_metadata(project.id, description="Changed after confirmation")
    revoked = again.readiness(project.id)
    assert revoked.status != rd.READY
    stored = PublishRepository(db_conn).get_readiness(project.id)
    assert stored is not None and stored.confirmed_at is None
    assert any(e[0] == "confirmation_withdrawn" for e in again.repo.events(project.id))


def test_rights_change_and_missing_export_revoke(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject, video: Video
) -> None:
    service = svc(settings, db_conn, None)
    finish_checklist(service, project.id, video.id)
    make_export(settings, db_conn, video, project)
    assert service.confirm_ready(project.id).status == rd.READY
    service.save_rights(
        video.id,
        rt.Rights(status=rt.ORIGINAL, source_title="Talk", creator="someone else"),
        confirmed=True,
    )
    assert service.readiness(project.id).status == rd.AWAITING
    service.confirm_ready(project.id)
    next(settings.exports_dir.glob("short-*.mp4")).unlink()
    report = service.readiness(project.id)
    assert report.status == rd.REVIEW_REQUIRED and not report.can_confirm


def test_clip_edit_makes_export_and_confirmation_stale(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject, video: Video
) -> None:
    service = svc(settings, db_conn, None)
    finish_checklist(service, project.id, video.id)
    make_export(settings, db_conn, video, project)
    service.confirm_ready(project.id)
    ShortRepository(db_conn).save_config(project.id, ShortConfig(start_s=0.0, end_s=15.0))
    report = service.readiness(project.id)
    assert report.status != rd.READY
    assert not service.facts(project.id).export_current


def test_export_outside_exports_dir_is_not_accepted(
    settings: Settings,
    db_conn: sqlite3.Connection,
    project: ShortProject,
    video: Video,
    tmp_path: Path,
) -> None:
    service = svc(settings, db_conn, None)
    finish_checklist(service, project.id, video.id)
    repo = ShortRepository(db_conn)
    digest = project.config.config_hash(video_sha256=video.sha256, kind=KIND_EXPORT, has_audio=True)
    render = repo.create_render(project.id, KIND_EXPORT, digest, project.config)
    stray = tmp_path / "elsewhere.mp4"
    stray.write_bytes(b"x" * 10)
    repo.mark_succeeded(
        render.id,
        output_path=stray,
        width=1080,
        height=1920,
        duration_s=1.0,
        size_bytes=10,
        has_audio=True,
        encoder="libx264",
    )
    facts = service.facts(project.id)
    assert facts.export_exists and not facts.export_file_ok


def test_restricted_source_blocks_even_when_everything_else_is_done(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject, video: Video
) -> None:
    service = svc(settings, db_conn, None)
    finish_checklist(service, project.id, video.id)
    make_export(settings, db_conn, video, project)
    service.save_rights(video.id, rt.Rights(status=rt.RESTRICTED), confirmed=True)
    report = service.readiness(project.id)
    assert report.status == rd.BLOCKED
    with pytest.raises(PublishError):
        service.confirm_ready(project.id)


def test_unresolved_high_flag_blocks_until_dismissed_with_note(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject, video: Video
) -> None:
    service = svc(settings, db_conn, FakeModel(SAFETY_REPLY))
    finish_checklist(service, project.id, video.id)
    make_export(settings, db_conn, video, project)
    review = service.run_review(project.id)
    assert service.readiness(project.id).status == rd.BLOCKED
    service.resolve_flag(project.id, review.flags[0].key, sf.RESOLUTION_DISMISSED, "Joking tone")
    assert service.readiness(project.id).status == rd.AWAITING


def test_invalid_settings_and_checks_rejected(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject
) -> None:
    service = svc(settings, db_conn, None)
    with pytest.raises(PublishError):
        service.set_check(project.id, "made_up", True)
    with pytest.raises(PublishError):
        service.set_setting(project.id, "audience", "maybe")
    with pytest.raises(PublishError):
        service.set_setting(project.id, "color", "red")
    with pytest.raises(PublishError):
        service.metadata_is_current(9999)


# ---------------------------------------------------------------- package


def test_package_export_never_overwrites_and_has_no_secrets(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject, video: Video
) -> None:
    service = svc(settings, db_conn, None)
    finish_checklist(service, project.id, video.id)
    service.edit_metadata(project.id, hashtags=["park"])
    first = write_package(service, project.id, "json")
    second = write_package(service, project.id, "json")
    txt = write_package(service, project.id, "txt")
    assert len({first, second, txt}) == 3 and first.parent == settings.exports_dir / "metadata"
    data = json.loads(first.read_text())
    assert data["title"] == "The park meeting" and "#park" in data["description"]
    assert data["readiness"]["status"] != rd.READY
    assert "does not" in data["disclaimer"]
    assert "TITLE" in txt.read_text()
    with pytest.raises(PublishError):
        write_package(service, project.id, "exe")


def test_package_description_adds_only_known_source_and_credit(
    settings: Settings, db_conn: sqlite3.Connection, project: ShortProject, video: Video
) -> None:
    service = svc(settings, db_conn, None)
    service.edit_metadata(project.id, description="Hello")
    pkg = build_package(service, project.id)
    assert "http" not in pkg["description"]
    service.save_rights(
        video.id,
        rt.Rights(
            status=rt.LICENSE,
            license_url="https://example.test/l",
            source_url="https://example.test/v",
            creator="Ana",
            source_title="Talk",
        ),
        confirmed=True,
    )
    pkg = build_package(service, project.id)
    assert "https://example.test/v" in pkg["description"]
