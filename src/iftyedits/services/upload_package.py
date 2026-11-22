"""The upload package for a Short: everything to paste into YouTube, in one structured record.

Building a package only reads saved data. Writing one creates a new file in the exports folder
(never replacing an earlier file). Nothing is uploaded.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from iftyedits.core import readiness as rd
from iftyedits.core import rights as rt
from iftyedits.core import safety as sf
from iftyedits.core.errors import PublishError
from iftyedits.core.metadata import Metadata
from iftyedits.services.publishing import PublishingService
from iftyedits.services.shorts import slugify
from iftyedits.storage.originals import assert_safe_output_path

PACKAGE_VERSION = 1


def description_with_hashtags(meta: Metadata, attribution: str = "", source_url: str = "") -> str:
    """The description as it would be pasted: text, credit and the chosen hashtags."""
    body = meta.description.strip()
    if attribution and attribution not in body:
        body = f"{body}\n\n{attribution}".strip()
    if source_url and source_url not in body:
        body = f"{body}\nOriginal video: {source_url}".strip()
    missing = [h for h in meta.hashtags if h.casefold() not in body.casefold()]
    if missing:
        body = f"{body}\n\n{' '.join(missing)}".strip()
    return body


def build_package(service: PublishingService, project_id: int) -> dict[str, Any]:
    """Collect the package from saved data."""
    project = service.project(project_id)
    stored = service.metadata(project_id)
    meta = stored.metadata if stored else Metadata()
    rights = service.rights(project.video_id)
    report = service.readiness(project_id)
    review = service.repo.latest_review(project_id)
    resolutions = service.repo.resolutions(project_id)
    export = service.latest_export(project_id)
    settings, _ = service.choices(project_id)
    attribution = rights.attribution_text or rt.generate_attribution(rights)
    flags: list[dict[str, Any]] = []
    for flag in review.flags if review else ():
        res = resolutions.get(flag.key)
        flags.append(
            {
                **flag.to_dict(),
                "resolution": res.action if res else "open",
                "resolution_note": res.note if res else "",
            }
        )
    return {
        "package_version": PACKAGE_VERSION,
        "disclaimer": rd.READY_MEANING,
        "short": {
            "project_id": project.id,
            "video_id": project.video_id,
            "clip_start_s": project.config.start_s,
            "clip_end_s": project.config.end_s,
            "export_file": export.output_path.name if export and export.output_path else None,
            "export_render_id": export.id if export else None,
        },
        "readiness": {
            "status": report.label,
            "blockers": list(report.blockers),
            "caveats": list(report.caveats),
            "open_items": [i.label for i in report.open_items],
        },
        "title": meta.selected_title,
        "title_options": [{"text": t.text, "style": t.style} for t in meta.titles],
        "description": description_with_hashtags(meta, attribution, rights.source_url),
        "hashtags": list(meta.hashtags),
        "search_tags": list(meta.tags),
        "search_phrases": list(meta.search_phrases),
        "language": meta.language or (service.context(project_id).language),
        "category_suggestion": meta.youtube_category or meta.category,
        "thumbnail": {
            "text": meta.thumbnail_text,
            "concept": meta.thumbnail_concept,
            "composition": meta.thumbnail_composition,
            "frame_s_from_clip_start": meta.thumbnail_frame_s,
        },
        "pinned_comment": meta.pinned_comment,
        "summary": meta.summary,
        "upload_settings": {
            "audience_chosen_by_you": rd.AUDIENCES.get(settings.get("audience", ""), ""),
            "visibility_chosen_by_you": rd.VISIBILITIES.get(settings.get("visibility", ""), ""),
            "comments_advice": meta.comments_advice,
            "visibility_advice": meta.visibility_advice,
            "disclosure_reminders": list(meta.disclosure_notes),
        },
        "source": {
            "title": rights.source_title,
            "creator": rights.creator,
            "url": rights.source_url,
            "attribution": attribution,
        },
        "rights_review": {
            "status": rights.label,
            "reviewed_on": rights.reviewed_on,
            "license": rights.license_type,
            "license_url": rights.license_url,
            "evidence_recorded": rights.evidence_recorded,
            "attribution_required": rights.attribution_required,
            "commercial_use": rights.commercial_use,
            "derivatives_allowed": rights.derivatives_allowed,
            "note": rt.NOT_LEGAL_ADVICE,
        },
        "content_review": {
            "status": review.status if review else "not run",
            "risk_level": sf.RISK_LABELS.get(review.risk_level, "") if review else "",
            "model": review.model if review else "",
            "summary": review.summary if review else "",
            "flags": flags,
            "note": sf.DISCLAIMER,
        },
        "generated_with": {
            "metadata_model": stored.model if stored else "",
            "fields_written_by_ai": sorted(
                k for k, v in (stored.provenance if stored else {}).items() if v == "ai"
            ),
        },
    }


def render_text(package: dict[str, Any]) -> str:
    """A readable plain-text version."""
    lines: list[str] = []

    def section(title: str, body: str) -> None:
        lines.extend([title.upper(), body.strip() or "(empty)", ""])

    section("Title", str(package["title"]))
    section("Description", str(package["description"]))
    section("Hashtags", " ".join(package["hashtags"]))
    section("Search tags", ", ".join(package["search_tags"]))
    thumb = package["thumbnail"]
    section(
        "Thumbnail",
        f"Text: {thumb['text']}\nConcept: {thumb['concept']}\nComposition: {thumb['composition']}\n"
        f"Frame: {thumb['frame_s_from_clip_start']} s from clip start",
    )
    section("Pinned comment", str(package["pinned_comment"]))
    up = package["upload_settings"]
    section(
        "Upload settings",
        f"Made for kids (your choice): {up['audience_chosen_by_you']}\n"
        f"Visibility (your choice): {up['visibility_chosen_by_you']}\n"
        f"Category suggestion: {package['category_suggestion']}\n"
        f"Language: {package['language']}\n"
        f"Disclosure reminders: {'; '.join(up['disclosure_reminders'])}",
    )
    src = package["source"]
    rev = package["rights_review"]
    section(
        "Source and rights",
        f"{src['title']} · {src['creator']} · {src['url']}\nStatus: {rev['status']}\n"
        f"Attribution: {src['attribution']}\n{rev['note']}",
    )
    cr = package["content_review"]
    flag_lines = [
        f"- [{f['severity']}] {f['category']} ({f['resolution']}): {f['explanation']}"
        for f in cr["flags"]
    ]
    section(
        "Content review",
        f"{cr['status']} · {cr['risk_level']}\n" + "\n".join(flag_lines) + f"\n{cr['note']}",
    )
    section("Readiness", f"{package['readiness']['status']}\n{package['disclaimer']}")
    return "\n".join(lines).rstrip() + "\n"


def write_package(
    service: PublishingService, project_id: int, fmt: str = "json", *, directory: Path | None = None
) -> Path:
    """Write the package to a new file in ``exports/metadata`` and return its path."""
    if fmt not in ("json", "txt"):
        raise PublishError("Choose json or txt.")
    package = build_package(service, project_id)
    project = service.project(project_id)
    title_part = slugify(str(package["title"]) or f"short-{project.id}", limit=40)
    out_dir = directory or service.settings.exports_dir / "metadata"
    out_dir = assert_safe_output_path(
        out_dir,
        originals_root=service.settings.originals_dir,
        allowed_roots=service.settings.output_roots,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    text = (
        json.dumps(package, indent=2, ensure_ascii=False) + "\n"
        if fmt == "json"
        else render_text(package)
    )
    stem = f"{title_part}_p{project.id}_metadata"
    for number in range(1, 1000):
        name = f"{stem}.{fmt}" if number == 1 else f"{stem}-{number}.{fmt}"
        target = out_dir / name
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        return assert_safe_output_path(
            target,
            originals_root=service.settings.originals_dir,
            allowed_roots=service.settings.output_roots,
        )
    raise PublishError("Could not find a free file name for the metadata file.")
