"""Upload preparation for one Short: metadata, content review, rights and the readiness gate.

Nothing here uploads, publishes or schedules anything. AI output is validated before it is
stored and is always labelled as AI output. Every state the gate relies on comes from saved
rows, and a confirmation is tied to a fingerprint so that changing the clip, the export, the
metadata or the rights withdraws it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from iftyedits.config import Settings
from iftyedits.core import metadata as md
from iftyedits.core import prompts
from iftyedits.core import readiness as rd
from iftyedits.core import rights as rt
from iftyedits.core import safety as sf
from iftyedits.core import usage as us
from iftyedits.core.errors import LlmError, LlmUnavailableError, PublishError
from iftyedits.core.metadata import Metadata
from iftyedits.core.protocols import TextModel
from iftyedits.core.rights import Rights
from iftyedits.storage.db import utc_now
from iftyedits.storage.originals import is_within
from iftyedits.storage.publishing import PublishRepository, StoredMetadata, StoredReview
from iftyedits.storage.shorts import KIND_EXPORT, ShortProject, ShortRender, ShortRepository
from iftyedits.storage.transcripts import TranscriptRepository
from iftyedits.storage.video_urls import VideoUrlRepository
from iftyedits.storage.videos import VideoRepository

log = logging.getLogger(__name__)

SETTING_KEYS = ("audience", "visibility")


@dataclass(frozen=True, slots=True)
class ClipContext:
    """What the clip actually says, and where it came from."""

    project_id: int
    video_id: int
    start_s: float
    end_s: float
    transcript: str  # timed lines in clip time, bounded for prompts
    plain_text: str  # all spoken words, for evidence checks
    language: str
    context_hash: str
    source_title: str
    source_channel: str
    source_url: str
    topic_hint: str

    @property
    def duration_s(self) -> float:
        """Clip length."""
        return self.end_s - self.start_s

    @property
    def has_speech(self) -> bool:
        """True when the transcript has words in this range."""
        return bool(self.plain_text.strip())


@dataclass(frozen=True, slots=True)
class GenerationResult:
    """Outcome of a metadata generation."""

    ok: bool
    message: str
    notes: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()


class PublishingService:
    """All upload-preparation use cases."""

    def __init__(
        self,
        settings: Settings,
        conn: sqlite3.Connection,
        model: TextModel | None,
        *,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.settings = settings
        self.conn = conn
        self.model = model
        self.clock = clock
        self.repo = PublishRepository(conn)
        self.shorts = ShortRepository(conn)
        self.videos = VideoRepository(conn)
        self.transcripts = TranscriptRepository(conn)
        self.urls = VideoUrlRepository(conn)

    # ------------------------------------------------------------------ context

    def project(self, project_id: int) -> ShortProject:
        """The Short project, or a clear error."""
        project = self.shorts.get_project(project_id)
        if project is None:
            raise PublishError("That Short no longer exists.")
        return project

    def context(self, project_id: int) -> ClipContext:
        """Gather the clip's transcript and source details from saved data."""
        project = self.project(project_id)
        config = project.config
        video = self.videos.get(project.video_id)
        if video is None:
            raise PublishError("The source video of this Short is missing.")
        transcript = self.transcripts.current(project.video_id)
        lines: list[str] = []
        plain: list[str] = []
        language = ""
        if transcript is not None:
            language = transcript.language or ""
            for seg in self.transcripts.segments(transcript.id, with_words=False):
                if seg.end_s <= config.start_s or seg.start_s >= config.end_s:
                    continue
                text = config.text_overrides.get(seg.position, seg.text).strip()
                if not text:
                    continue
                rel = max(0.0, seg.start_s - config.start_s)
                lines.append(f"[{rel:6.1f}s] {text}")
                plain.append(text)
        body = "\n".join(lines)
        limit = self.settings.llm_max_transcript_chars
        if len(body) > limit:
            body = body[:limit].rsplit("\n", 1)[0] + "\n[transcript cut for length]"
        fingerprint = json.dumps(
            [round(config.start_s, 2), round(config.end_s, 2), plain], ensure_ascii=False
        )
        url = self.urls.for_video(project.video_id)
        rights = self.repo.get_rights(project.video_id)
        title = (rights.source_title if rights else "") or (url.title if url else "")
        channel = (rights.creator if rights else "") or (url.uploader if url else "")
        link = (rights.source_url if rights else "") or (url.url if url else "")
        return ClipContext(
            project_id=project.id,
            video_id=project.video_id,
            start_s=config.start_s,
            end_s=config.end_s,
            transcript=body,
            plain_text=" ".join(plain),
            language=language,
            context_hash=hashlib.sha256(fingerprint.encode()).hexdigest()[:24],
            source_title=title or video.original_filename,
            source_channel=channel,
            source_url=link,
            topic_hint=project.title,
        )

    # ------------------------------------------------------------------ metadata

    def metadata(self, project_id: int) -> StoredMetadata | None:
        """Saved metadata, or None."""
        return self.repo.get_metadata(project_id)

    def metadata_is_current(self, project_id: int) -> bool:
        """False when the clip changed after the metadata was written."""
        self.project(project_id)
        stored = self.repo.get_metadata(project_id)
        return stored is None or stored.context_hash == self.context(project_id).context_hash

    def generate_metadata(
        self, project_id: int, groups: Iterable[str] | None = None
    ) -> GenerationResult:
        """Ask the model for some (or all) metadata groups and save what validates.

        User-written fields in other groups are kept. A failed call changes nothing.
        """
        wanted = [g for g in (groups or md.GROUPS) if g in md.GROUPS]
        if not wanted:
            raise PublishError("Choose what to generate.")
        if self.model is None:
            return GenerationResult(
                False,
                "No AI model is configured, so nothing was generated. Add a Claude key or a "
                "local model in Settings (AI writing and review), or write the metadata yourself.",
            )
        ctx = self.context(project_id)
        existing = self.repo.get_metadata(project_id)
        current = existing.metadata if existing else Metadata()
        keep = (
            {"title": current.selected_title}
            if current.selected_title and "titles" not in wanted
            else None
        )
        prompt = prompts.metadata_prompt(
            wanted,
            transcript=ctx.transcript,
            duration_s=ctx.duration_s,
            source_title=ctx.source_title,
            source_channel=ctx.source_channel,
            topic_hint=ctx.topic_hint,
            current=dict(keep) if keep else None,
        )
        try:
            with us.feature_scope("metadata:" + "+".join(wanted)):
                reply = self.model.complete(prompts.METADATA_SYSTEM, prompt, max_tokens=2048)
            values, notes = md.parse_metadata(
                reply,
                wanted,
                clip_duration_s=ctx.duration_s,
                allowed_urls=[ctx.source_url],
            )
        except LlmError as exc:
            log.warning("metadata generation failed: %s", type(exc).__name__)
            return GenerationResult(False, str(exc))
        if not ctx.has_speech:
            notes = [*notes, "The clip has no transcript, so this is a guess from the title only."]
        notice = str(getattr(self.model, "notice", "") or "")
        if notice:
            notes = [*notes, notice]
        updated = current.with_values(**values)
        provenance = dict(existing.provenance) if existing else {}
        for name in values:
            provenance[name] = "ai"
        if "titles" in values and not updated.selected_title:
            updated = updated.with_values(selected_title=updated.recommended_title)
            provenance["selected_title"] = "ai"
        elif "titles" in values:
            # a regenerated list never overwrites a title the user already chose
            pass
        self.repo.save_metadata(
            project_id,
            updated,
            provenance,
            model=self.model.name,
            context_hash=ctx.context_hash,
        )
        done = tuple(g for g in wanted if any(n in values for n in md.GROUPS[g]))
        return GenerationResult(True, "Metadata saved.", tuple(notes), done)

    def edit_metadata(self, project_id: int, **values: object) -> Metadata:
        """Save the owner's edits. Values are validated and marked as manual."""
        existing = self.repo.get_metadata(project_id)
        current = existing.metadata if existing else Metadata()
        clean: dict[str, object] = {}
        for name, raw in values.items():
            if name in ("hashtags", "hooks", "disclosure_notes", "tags", "search_phrases"):
                items = raw if isinstance(raw, list | tuple) else str(raw).splitlines()
                if name == "hashtags":
                    clean[name] = md.clean_hashtags(items)
                elif name == "tags":
                    clean[name] = md.clean_tags(items)
                else:
                    clean[name] = md.dedupe(t for t in (md.clean_text(i, 200) for i in items) if t)
            elif name == "selected_title":
                clean[name] = md.clean_text(raw, md.TITLE_MAX + 50)
            elif name in ("description", "description_long", "pinned_comment"):
                clean[name] = md.clean_text(raw, md.DESCRIPTION_MAX, multiline=True)
            elif name == "thumbnail_frame_s":
                ctx = self.context(project_id)
                if raw is None:
                    clean[name] = None
                elif isinstance(raw, int | float) and 0 <= float(raw) <= ctx.duration_s:
                    clean[name] = round(float(raw), 2)
                else:
                    raise PublishError("The thumbnail time must be inside the clip.")
            elif name in {f for fields_ in md.GROUPS.values() for f in fields_} - {
                "titles",
                "recommended_index",
            }:
                clean[name] = md.clean_text(raw, 400)
            else:
                raise PublishError(f"Unknown metadata field: {name}")
        updated = replace(current, **clean)  # type: ignore[arg-type]
        provenance = dict(existing.provenance) if existing else {}
        for name in clean:
            provenance[name] = "manual"
        ctx = self.context(project_id)
        self.repo.save_metadata(
            project_id,
            updated,
            provenance,
            model=existing.model if existing else "",
            context_hash=existing.context_hash if existing else ctx.context_hash,
        )
        self._drop_title_review_if_changed(project_id, clean)
        return updated

    def _drop_title_review_if_changed(self, project_id: int, changed: dict[str, object]) -> None:
        # Editing text after the owner ticked "reviewed" withdraws that tick.
        mapping = {
            "selected_title": "title_reviewed",
            "description": "description_reviewed",
            "description_long": "description_reviewed",
            "hashtags": "hashtags_reviewed",
            "tags": "tags_reviewed",
        }
        stored = self.repo.get_readiness(project_id)
        if stored is None:
            return
        drop = {mapping[k] for k in changed if k in mapping} & stored.checks
        if drop:
            self._save_readiness(project_id, checks=stored.checks - drop)

    def accept_metadata_for_current_clip(self, project_id: int) -> None:
        """The owner confirms the saved metadata still describes the edited clip."""
        stored = self.repo.get_metadata(project_id)
        if stored is None:
            raise PublishError("There is no metadata yet.")
        self.repo.save_metadata(
            project_id,
            stored.metadata,
            stored.provenance,
            model=stored.model,
            context_hash=self.context(project_id).context_hash,
        )

    def validate_metadata(self, meta: Metadata) -> tuple[list[str], list[str]]:
        """``(errors, warnings)`` for the chosen title and description."""
        errors, warnings = md.title_issues(meta.selected_title)
        d_err, d_warn = md.description_issues(meta.description, meta.hashtags)
        return errors + d_err, warnings + d_warn

    # ------------------------------------------------------------------ content review

    def run_review(self, project_id: int) -> StoredReview:
        """Run the advisory content review and save it (also when it cannot run)."""
        ctx = self.context(project_id)
        if self.model is None:
            return self._store_review(
                ctx,
                status=sf.STATUS_UNAVAILABLE,
                level=sf.UNABLE,
                model="",
                note="No AI model is configured. Review the clip yourself and record it.",
            )
        if not ctx.has_speech:
            return self._store_review(
                ctx,
                status=sf.STATUS_UNAVAILABLE,
                level=sf.UNABLE,
                model=self.model.name,
                note="The clip has no transcript, so only a manual review is possible.",
            )
        try:
            with us.feature_scope("safety_review"):
                reply = self.model.complete(
                    prompts.SAFETY_SYSTEM,
                    prompts.safety_prompt(transcript=ctx.transcript, duration_s=ctx.duration_s),
                    max_tokens=2048,
                )
            flags, summary, notes = sf.parse_safety(
                reply, ctx.plain_text, clip_duration_s=ctx.duration_s
            )
        except LlmError as exc:
            log.warning("content review failed: %s", type(exc).__name__)
            return self._store_review(
                ctx,
                status=sf.STATUS_FAILED,
                level=sf.UNABLE,
                model=self.model.name,
                note=str(exc),
            )
        return self._store_review(
            ctx,
            status=sf.STATUS_OK,
            level=sf.risk_level(flags),
            model=self.model.name,
            flags=flags,
            summary=summary,
            notes=(
                *notes,
                *(
                    [str(getattr(self.model, "notice", ""))]
                    if getattr(self.model, "notice", "")
                    else []
                ),
            ),
        )

    def _store_review(
        self,
        ctx: ClipContext,
        *,
        status: str,
        level: str,
        model: str,
        flags: tuple[sf.SafetyFlag, ...] = (),
        summary: str = "",
        notes: tuple[str, ...] = (),
        note: str = "",
    ) -> StoredReview:
        review_id = self.repo.add_review(
            ctx.project_id,
            status=status,
            risk_level=level,
            model=model,
            context_hash=ctx.context_hash,
            flags=flags,
            summary=summary,
            notes=notes,
            note=note,
        )
        latest = self.repo.latest_review(ctx.project_id)
        if latest is None or latest.id != review_id:
            raise PublishError("The review could not be saved.")
        return latest

    def record_manual_review(self, project_id: int, note: str) -> StoredReview:
        """The owner records that they reviewed the clip themselves."""
        text = md.clean_text(note, 500, multiline=True)
        if len(text) < 3:
            raise PublishError("Write a short note about what you checked.")
        return self._store_review(
            self.context(project_id),
            status=sf.STATUS_MANUAL,
            level=sf.UNABLE,
            model="",
            note=text,
        )

    def resolve_flag(self, project_id: int, flag_key: str, action: str, note: str) -> None:
        """Dismiss a flag (note required) or acknowledge it as still unresolved."""
        latest = self.repo.latest_review(project_id)
        if latest is None or flag_key not in {f.key for f in latest.flags}:
            raise PublishError("That flag is not part of the latest review.")
        text = md.clean_text(note, 500, multiline=True)
        if action == sf.RESOLUTION_DISMISSED and len(text) < 3:
            raise PublishError("Explain why this is a false positive.")
        if action not in (sf.RESOLUTION_DISMISSED, sf.RESOLUTION_ACKNOWLEDGED, "reopened"):
            raise PublishError("Unknown action.")
        self.repo.resolve_flag(project_id, flag_key, action, text)

    def review_is_current(self, project_id: int, review: StoredReview | None = None) -> bool:
        """True when the review was done on the clip as it is now."""
        found = review or self.repo.latest_review(project_id)
        return found is not None and found.context_hash == self.context(project_id).context_hash

    # ------------------------------------------------------------------ rights

    def rights(self, video_id: int) -> Rights:
        """Saved rights, or a not-yet-reviewed record filled with what the source reports."""
        saved = self.repo.get_rights(video_id)
        if saved is not None:
            return saved
        url = self.urls.for_video(video_id)
        video = self.videos.get(video_id)
        base = Rights(status=rt.UNKNOWN)
        if url is not None:
            return replace(
                base,
                source_type="youtube" if "youtu" in url.url else "other",
                source_title=url.title,
                creator=url.uploader,
                source_url=url.url,
                source_id=url.source_key.split(":", 1)[-1],
            )
        if video is not None:
            return replace(
                base, source_title=Path(video.original_filename).stem, source_type="file"
            )
        return base

    def site_license_hint(self, video_id: int) -> str:
        """The license label the site reported, as a reminder only."""
        url = self.urls.for_video(video_id)
        return rt.hint_from_site(url.license if url else None)

    def save_rights(self, video_id: int, rights: Rights, *, confirmed: bool) -> Rights:
        """Validate and save rights. ``confirmed`` records the owner's own review date."""
        if self.videos.get(video_id) is None:
            raise PublishError("The source video is missing.")
        cleaned = replace(
            rights,
            source_title=md.clean_text(rights.source_title, 300),
            creator=md.clean_text(rights.creator, 200),
            owner=md.clean_text(rights.owner, 200),
            source_url=rights.source_url.strip()[:500],
            license_type=md.clean_text(rights.license_type, 200),
            license_url=rights.license_url.strip()[:500],
            evidence=md.clean_text(rights.evidence, 3000, multiline=True),
            permission_notes=md.clean_text(rights.permission_notes, 3000, multiline=True),
            attribution_text=md.clean_text(rights.attribution_text, 500, multiline=True),
            expires_on=rights.expires_on.strip(),
            user_confirmed=confirmed,
            reviewed_on=self.clock()[:10] if confirmed else "",
        )
        problems = rt.validate_rights(cleaned)
        if problems:
            raise PublishError(" ".join(problems))
        self.repo.save_rights(video_id, cleaned)
        saved = self.repo.get_rights(video_id)
        return saved if saved is not None else cleaned

    # ------------------------------------------------------------------ readiness

    def _readiness_row(self, project_id: int) -> tuple[frozenset[str], dict[str, str], bool]:
        stored = self.repo.get_readiness(project_id)
        if stored is None:
            return frozenset(), {}, False
        return stored.checks, stored.settings, stored.rights_ack

    def choices(self, project_id: int) -> tuple[dict[str, str], bool]:
        """The owner's saved upload settings and whether they acknowledged unverified rights."""
        _, settings, ack = self._readiness_row(project_id)
        return dict(settings), ack

    def _save_readiness(
        self,
        project_id: int,
        *,
        checks: frozenset[str] | None = None,
        settings: dict[str, str] | None = None,
        rights_ack: bool | None = None,
        confirmation: tuple[str | None, int | None, str | None] | None = None,
    ) -> None:
        stored = self.repo.get_readiness(project_id)
        old_checks, old_settings, old_ack = self._readiness_row(project_id)
        at, render_id, fingerprint = (
            confirmation
            if confirmation is not None
            else (
                (stored.confirmed_at, stored.confirmed_render_id, stored.confirmed_fingerprint)
                if stored
                else (None, None, None)
            )
        )
        self.repo.save_readiness(
            project_id,
            checks=old_checks if checks is None else checks,
            settings=old_settings if settings is None else settings,
            rights_ack=old_ack if rights_ack is None else rights_ack,
            confirmed_at=at,
            confirmed_render_id=render_id,
            confirmed_fingerprint=fingerprint,
        )

    def set_check(self, project_id: int, key: str, value: bool) -> None:
        """Tick or untick a checklist item the owner answers."""
        if key not in rd.USER_CHECKS:
            raise PublishError("Unknown checklist item.")
        self.project(project_id)
        checks, _, _ = self._readiness_row(project_id)
        self._save_readiness(project_id, checks=checks | {key} if value else checks - {key})

    def set_setting(self, project_id: int, key: str, value: str) -> None:
        """Record an upload setting the owner chose (audience, visibility)."""
        allowed = {"audience": rd.AUDIENCES, "visibility": rd.VISIBILITIES}
        if key not in allowed or value not in allowed[key]:
            raise PublishError("That choice is not available.")
        self.project(project_id)
        _, settings, _ = self._readiness_row(project_id)
        self._save_readiness(project_id, settings={**settings, key: value})

    def set_rights_ack(self, project_id: int, value: bool) -> None:
        """The owner acknowledges the source rights are unverified."""
        self.project(project_id)
        self._save_readiness(project_id, rights_ack=value)

    def latest_export(self, project_id: int) -> ShortRender | None:
        """The newest finished export of a Short."""
        for render in self.shorts.list_renders(project_id, KIND_EXPORT):
            if render.status == "succeeded" and render.output_path is not None:
                return render
        return None

    def _export_checks(
        self, project: ShortProject, render: ShortRender | None
    ) -> tuple[bool, bool, bool]:
        if render is None:
            return False, False, False
        path = render.output_path
        ok = bool(
            path
            and path.is_file()
            and path.stat().st_size > 0
            and is_within(path, self.settings.exports_dir)
            and (render.size_bytes is None or path.stat().st_size == render.size_bytes)
        )
        video = self.videos.get(project.video_id)
        current = False
        if video is not None:
            digest = project.config.config_hash(
                video_sha256=video.sha256,
                kind=KIND_EXPORT,
                has_audio=bool(render.has_audio) if render.has_audio is not None else True,
            )
            current = digest == render.config_hash
        return True, ok, current

    def fingerprint(self, project_id: int, render: ShortRender | None) -> str:
        """Hash of everything a confirmation depends on."""
        ctx = self.context(project_id)
        stored = self.repo.get_metadata(project_id)
        meta = stored.metadata.to_dict() if stored else {}
        project = self.project(project_id)
        rights = self.rights(project.video_id).to_dict()
        rights.pop("updated_at", None)
        _, settings, ack = self._readiness_row(project_id)
        payload = json.dumps(
            [
                render.id if render else None,
                render.config_hash if render else None,
                ctx.context_hash,
                meta,
                rights,
                settings,
                ack,
            ],
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def facts(self, project_id: int) -> rd.Facts:
        """Collect everything the gate needs from saved data."""
        project = self.project(project_id)
        ctx = self.context(project_id)
        stored_meta = self.repo.get_metadata(project_id)
        meta = stored_meta.metadata if stored_meta else Metadata()
        t_err, _ = md.title_issues(meta.selected_title)
        d_err, _ = md.description_issues(meta.description, meta.hashtags)
        review = self.repo.latest_review(project_id)
        resolutions = self.repo.resolutions(project_id)
        flags = tuple(
            (f.key, f.severity, resolutions[f.key].action if f.key in resolutions else None)
            for f in (review.flags if review else ())
        )
        rights = self.rights(project.video_id)
        checks, settings, ack = self._readiness_row(project_id)
        render = self.latest_export(project_id)
        exists, file_ok, current = self._export_checks(project, render)
        stored = self.repo.get_readiness(project_id)
        confirmed = bool(stored and stored.confirmed_at)
        valid = bool(
            confirmed
            and stored
            and render is not None
            and stored.confirmed_render_id == render.id
            and stored.confirmed_fingerprint == self.fingerprint(project_id, render)
        )
        return rd.Facts(
            has_metadata=bool(stored_meta and stored_meta.metadata.filled),
            metadata_current=bool(
                stored_meta is None or stored_meta.context_hash == ctx.context_hash
            ),
            title=meta.selected_title,
            description=meta.description,
            title_errors=tuple(t_err),
            description_errors=tuple(d_err),
            review_status=review.status if review else None,
            review_current=bool(review and review.context_hash == ctx.context_hash),
            flags=flags,
            rights_status=rights.status,
            rights_reviewed=rights.user_confirmed and bool(rights.reviewed_on),
            source_identified=rights.source_identified,
            attribution_required=rights.attribution_required,
            attribution_text=rights.attribution_text,
            evidence_recorded=rights.evidence_recorded,
            rights_ack=ack,
            audience=settings.get("audience", ""),
            visibility=settings.get("visibility", ""),
            export_exists=exists,
            export_file_ok=file_ok,
            export_current=current,
            checks=checks,
            confirmed=confirmed,
            confirmation_valid=valid,
        )

    def readiness(self, project_id: int) -> rd.Report:
        """The gate's current verdict. A confirmation that no longer holds is withdrawn."""
        report = rd.evaluate(self.facts(project_id))
        if report.revoked:
            stored = self.repo.get_readiness(project_id)
            if stored and stored.confirmed_at:
                self._save_readiness(project_id, confirmation=(None, None, None))
                self.repo.add_event(
                    project_id,
                    "confirmation_withdrawn",
                    "Something changed after you confirmed, so the confirmation no longer applies.",
                )
        return report

    def confirm_ready(self, project_id: int) -> rd.Report:
        """The owner's explicit final confirmation. Refused unless every required item is done."""
        report = self.readiness(project_id)
        if not report.can_confirm:
            raise PublishError(
                "The checklist is not finished: " + "; ".join(i.label for i in report.open_items)
                if report.open_items
                else f"Status is “{report.label}”."
            )
        render = self.latest_export(project_id)
        if render is None:
            raise PublishError("There is no finished export to confirm.")
        self._save_readiness(
            project_id,
            confirmation=(self.clock(), render.id, self.fingerprint(project_id, render)),
        )
        self.repo.add_event(project_id, "confirmed", f"export #{render.id}")
        return self.readiness(project_id)

    def withdraw_confirmation(self, project_id: int) -> None:
        """The owner takes the confirmation back."""
        self.project(project_id)
        self._save_readiness(project_id, confirmation=(None, None, None))
        self.repo.add_event(project_id, "withdrawn", "by the owner")


def build_publishing_service(settings: Settings, conn: sqlite3.Connection) -> PublishingService:
    """A service wired to the configured model (if any)."""
    from iftyedits.adapters.ai_router import build_text_model

    try:
        model: TextModel | None = build_text_model(settings, conn)
    except LlmUnavailableError:
        model = None
    return PublishingService(settings, conn, model)
