"""The upload-readiness gate: a pure function from recorded facts to a status and checklist.

A Short is "Ready for upload (you confirmed)" only when every required item is satisfied by
saved data AND the owner pressed the final confirmation AND nothing it depended on has changed
since. The status says the owner finished their own review. It is not a statement that the
clip is cleared, compliant or safe, and nothing here ever uploads anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from iftyedits.core import rights as rights_mod
from iftyedits.core import safety as safety_mod

DRAFT = "draft"
METADATA_INCOMPLETE = "metadata_incomplete"
REVIEW_REQUIRED = "review_required"
RIGHTS_UNVERIFIED = "rights_unverified"
BLOCKED = "blocked"
AWAITING = "awaiting_confirmation"
READY = "ready"

STATUS_LABELS = {
    DRAFT: "Draft",
    METADATA_INCOMPLETE: "Metadata incomplete",
    REVIEW_REQUIRED: "Review required",
    RIGHTS_UNVERIFIED: "Rights unverified",
    BLOCKED: "Blocked",
    AWAITING: "Checklist done, awaiting your confirmation",
    READY: "Ready for upload (you confirmed)",
}
STATUS_TONES = {
    DRAFT: "muted",
    METADATA_INCOMPLETE: "warn",
    REVIEW_REQUIRED: "warn",
    RIGHTS_UNVERIFIED: "warn",
    BLOCKED: "bad",
    AWAITING: "accent",
    READY: "good",
}
READY_MEANING = (
    "“Ready for upload” means you completed this checklist and confirmed it. It does not "
    "mean the clip is cleared for reuse, copyright-safe, monetizable or compliant with "
    "YouTube's policies. The studio never uploads or publishes anything."
)

# Checklist keys the user ticks (stored). Each is paired with a label for the UI.
USER_CHECKS: dict[str, str] = {
    "title_reviewed": "I reviewed the title",
    "description_reviewed": "I reviewed the description",
    "hashtags_reviewed": "I reviewed the hashtags",
    "tags_reviewed": "I reviewed the search tags",
    "clip_watched": "I watched the whole clip",
    "transcript_checked": "I checked the transcript and context",
    "language_reviewed": "I checked the language setting",
    "disclosures_considered": "I considered whether any disclosures apply",
    "preview_reviewed": "I watched the final exported video",
}
AUDIENCES = {
    "": "Not chosen yet",
    "not_for_kids": "No, not made for kids",
    "for_kids": "Yes, made for kids",
}
VISIBILITIES = {
    "": "Not chosen yet",
    "private": "Private",
    "unlisted": "Unlisted",
    "public": "Public",
}

# group, key, label
GROUPS = ("Metadata", "Content review", "Rights", "Upload settings", "Export")


@dataclass(frozen=True, slots=True)
class Facts:
    """Everything the gate looks at, gathered from saved data by the service."""

    # metadata
    has_metadata: bool = False
    metadata_current: bool = True
    title: str = ""
    description: str = ""
    title_errors: tuple[str, ...] = ()
    description_errors: tuple[str, ...] = ()
    # content review
    review_status: str | None = None  # safety_mod.STATUS_*; None = never reviewed
    review_current: bool = True
    flags: tuple[tuple[str, str, str | None], ...] = ()  # (key, severity, resolution)
    # rights
    rights_status: str = rights_mod.UNKNOWN
    rights_reviewed: bool = False
    source_identified: bool = False
    attribution_required: str = "unknown"
    attribution_text: str = ""
    evidence_recorded: bool = False
    rights_ack: bool = False
    # upload settings
    audience: str = ""
    visibility: str = ""
    # export
    export_exists: bool = False
    export_file_ok: bool = False
    export_current: bool = False
    # the user's saved ticks and confirmation
    checks: frozenset[str] = frozenset()
    confirmed: bool = False
    confirmation_valid: bool = False


@dataclass(frozen=True, slots=True)
class Item:
    """One line of the checklist."""

    group: str
    key: str
    label: str
    done: bool
    detail: str = ""
    required: bool = True
    blocking: bool = False  # cannot be satisfied by a tick; the data has to change
    caveat: bool = False  # satisfied, but by acknowledging something unresolved


@dataclass(frozen=True, slots=True)
class Report:
    """The gate's verdict."""

    status: str
    items: tuple[Item, ...]
    blockers: tuple[str, ...] = ()
    caveats: tuple[str, ...] = ()
    revoked: bool = field(default=False)

    @property
    def label(self) -> str:
        """Readable status."""
        return STATUS_LABELS[self.status]

    @property
    def tone(self) -> str:
        """Pill tone for the status."""
        return STATUS_TONES[self.status]

    @property
    def can_confirm(self) -> bool:
        """True when every required item is done and the owner has not confirmed yet."""
        return self.status == AWAITING

    @property
    def open_items(self) -> tuple[Item, ...]:
        """Required items still to do."""
        return tuple(i for i in self.items if i.required and not i.done)


def evaluate(f: Facts) -> Report:
    """Run the gate."""
    items: list[Item] = []
    blockers: list[str] = []
    caveats: list[str] = []

    def add(group: str, key: str, label: str, done: bool, detail: str = "", **kw: bool) -> None:
        items.append(Item(group, key, label, done, detail, **kw))

    # ---- metadata
    add("Metadata", "metadata", "Metadata written", f.has_metadata, "Generate or write it.")
    stale_meta = f.has_metadata and not f.metadata_current
    add(
        "Metadata",
        "metadata_current",
        "Metadata matches the current clip",
        not stale_meta,
        "The clip changed after the metadata was written. Regenerate it or confirm it still fits."
        if stale_meta
        else "",
    )
    add(
        "Metadata",
        "title_valid",
        "A valid title is chosen",
        bool(f.title.strip()) and not f.title_errors,
        "; ".join(f.title_errors) or "Choose or write a title.",
    )
    add(
        "Metadata",
        "description_valid",
        "A valid description is written",
        bool(f.description.strip()) and not f.description_errors,
        "; ".join(f.description_errors) or "Write or generate a description.",
    )
    for key in ("title_reviewed", "description_reviewed", "hashtags_reviewed", "tags_reviewed"):
        add("Metadata", key, USER_CHECKS[key], key in f.checks)

    # ---- content review
    add("Content review", "clip_watched", USER_CHECKS["clip_watched"], "clip_watched" in f.checks)
    add(
        "Content review",
        "transcript_checked",
        USER_CHECKS["transcript_checked"],
        "transcript_checked" in f.checks,
    )
    reviewed = f.review_status in (safety_mod.STATUS_OK, safety_mod.STATUS_MANUAL)
    if f.review_status in (safety_mod.STATUS_UNAVAILABLE, safety_mod.STATUS_FAILED):
        detail = "The automated review could not run. Run it again, or record a manual review."
    elif reviewed and not f.review_current:
        detail = "The clip changed since the last review. Review it again."
    else:
        detail = "Run the content review, or record a manual review."
    add(
        "Content review",
        "risk_reviewed",
        "Content risk review done for the current clip",
        reviewed and f.review_current,
        detail,
    )
    unresolved_high = [k for k, sev, res in f.flags if res is None and sev == "high"]
    unresolved_any = [k for k, _sev, res in f.flags if res is None]
    acked = [k for k, _sev, res in f.flags if res == safety_mod.RESOLUTION_ACKNOWLEDGED]
    add(
        "Content review",
        "flags_resolved",
        "Every flagged concern is dismissed or acknowledged",
        not unresolved_any,
        f"{len(unresolved_any)} flag(s) still open." if unresolved_any else "",
    )
    if unresolved_high:
        blockers.append(f"{len(unresolved_high)} high-severity content flag(s) are unresolved.")
    if acked:
        caveats.append(f"{len(acked)} content concern(s) were acknowledged as still unresolved.")

    # ---- rights
    add(
        "Rights",
        "source_identified",
        "The source is identified (link, or title and creator)",
        f.source_identified,
        "Fill in the source link, or its title and creator.",
    )
    add(
        "Rights",
        "rights_reviewed",
        "Rights status reviewed and confirmed by you",
        f.rights_reviewed,
        "Open Rights, choose a status and confirm it.",
    )
    restricted = f.rights_status == rights_mod.RESTRICTED
    add(
        "Rights",
        "not_restricted",
        "The source is not marked restricted",
        not restricted,
        "Marked restricted or not cleared. Change the status only if that is no longer true.",
        blocking=True,
    )
    if restricted:
        blockers.append("The source is marked restricted or not cleared.")
    needs_evidence = f.rights_status in rights_mod.EVIDENCE_REQUIRED
    add(
        "Rights",
        "evidence",
        "Evidence recorded for the claimed permission",
        (not needs_evidence) or f.evidence_recorded,
        "Record a link, message or license text.",
        required=needs_evidence,
    )
    attribution_needed = f.attribution_required == "yes"
    add(
        "Rights",
        "attribution",
        "Required attribution is prepared",
        (not attribution_needed) or bool(f.attribution_text.strip()),
        "Attribution is required: write the credit text.",
        required=attribution_needed,
    )
    unverified = f.rights_status in rights_mod.UNVERIFIED
    add(
        "Rights",
        "rights_ack",
        "I understand the rights are not verified and accept responsibility",
        (not unverified) or f.rights_ack,
        "Rights are unknown or unverified. This tick does not make the clip cleared.",
        required=unverified,
        caveat=unverified and f.rights_ack,
    )
    if unverified and f.rights_ack:
        caveats.append("Rights remain unverified; you acknowledged that.")

    # ---- upload settings
    add(
        "Upload settings",
        "audience",
        "Audience: you chose made for kids or not",
        f.audience in ("not_for_kids", "for_kids"),
        "YouTube requires you to decide. The studio will not choose for you.",
    )
    add(
        "Upload settings",
        "language_reviewed",
        USER_CHECKS["language_reviewed"],
        "language_reviewed" in f.checks,
    )
    add(
        "Upload settings",
        "visibility",
        "Visibility: you chose one",
        f.visibility in ("private", "unlisted", "public"),
        "Choose private, unlisted or public.",
    )
    add(
        "Upload settings",
        "disclosures_considered",
        USER_CHECKS["disclosures_considered"],
        "disclosures_considered" in f.checks,
    )

    # ---- export
    add("Export", "export_exists", "A finished export exists", f.export_exists, "Export the Short.")
    add(
        "Export",
        "export_file_ok",
        "The export file is present and readable",
        f.export_file_ok,
        "The file is missing or unreadable. Export again.",
    )
    add(
        "Export",
        "export_current",
        "The export matches the current clip settings",
        f.export_current,
        "The clip changed after the last export. Export again.",
    )
    add(
        "Export",
        "preview_reviewed",
        USER_CHECKS["preview_reviewed"],
        "preview_reviewed" in f.checks,
    )

    # ---- verdict
    all_done = all(i.done for i in items if i.required)
    has_progress = f.has_metadata or bool(f.checks) or f.review_status is not None
    revoked = f.confirmed and not f.confirmation_valid
    if blockers:
        status = BLOCKED
    elif not has_progress and not f.export_exists:
        status = DRAFT
    elif not all(
        i.done
        for i in items
        if i.group == "Metadata"
        and i.key in ("metadata", "metadata_current", "title_valid", "description_valid")
    ):
        status = METADATA_INCOMPLETE
    elif not f.rights_reviewed or (unverified and not f.rights_ack):
        status = RIGHTS_UNVERIFIED
    elif not all_done:
        status = REVIEW_REQUIRED
    elif f.confirmed and f.confirmation_valid:
        status = READY
    else:
        status = AWAITING
    return Report(status, tuple(items), tuple(blockers), tuple(caveats), revoked)
