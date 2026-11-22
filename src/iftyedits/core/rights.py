"""Source rights and permission tracking: statuses, evidence rules and attribution text.

Nothing here decides whether a clip may be reused. It records what the owner knows and
reminds them of what is still unverified. A public video, a credit line, a short length or
heavy editing never counts as permission.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

ORIGINAL = "original"
PERMISSION = "permission"
LICENSE = "license"
CREATIVE_COMMONS = "creative_commons"
PUBLIC_DOMAIN = "public_domain"
REQUESTED = "requested"
UNKNOWN = "unknown"
RESTRICTED = "restricted"
REVIEW_REQUIRED = "review_required"

STATUSES: dict[str, str] = {
    UNKNOWN: "Permission unknown",
    REVIEW_REQUIRED: "Rights review required",
    ORIGINAL: "Original content (I made it)",
    PERMISSION: "Permission obtained",
    LICENSE: "Verified license",
    CREATIVE_COMMONS: "Creative Commons (verification required)",
    PUBLIC_DOMAIN: "Public domain (verification required)",
    REQUESTED: "Permission requested, no answer yet",
    RESTRICTED: "Restricted / not cleared",
}
# Statuses that claim the owner may use the material and so need written evidence.
EVIDENCE_REQUIRED = {PERMISSION, LICENSE, CREATIVE_COMMONS, PUBLIC_DOMAIN}
# Statuses the owner has to confirm are still unresolved before they can continue.
UNVERIFIED = {UNKNOWN, REVIEW_REQUIRED, REQUESTED, CREATIVE_COMMONS, PUBLIC_DOMAIN}
CLEARED = {ORIGINAL, PERMISSION, LICENSE}
TONES = {
    ORIGINAL: "good",
    PERMISSION: "good",
    LICENSE: "good",
    CREATIVE_COMMONS: "warn",
    PUBLIC_DOMAIN: "warn",
    REQUESTED: "warn",
    UNKNOWN: "warn",
    REVIEW_REQUIRED: "warn",
    RESTRICTED: "bad",
}

SOURCE_TYPES = {
    "youtube": "YouTube video",
    "own": "My own recording",
    "file": "File from someone else",
    "other": "Other",
}
TRISTATE = {"unknown": "Unknown", "yes": "Yes", "no": "No"}

NOT_LEGAL_ADVICE = (
    "This tracker records what you know. It is not legal advice and cannot confirm that you may "
    "use a video. Being public on YouTube, giving credit, keeping a clip short, or adding "
    "captions, zooms or commentary does not by itself grant permission or make a use fair. "
    "Fair use depends on the facts and is decided by a court, not by software."
)

HISTORY_FIELDS = ("status", "license_type", "license_url", "evidence", "permission_notes")


@dataclass(frozen=True, slots=True)
class Rights:
    """What is known about the right to reuse one source video."""

    status: str = UNKNOWN
    source_type: str = ""
    source_title: str = ""
    creator: str = ""
    source_url: str = ""
    source_id: str = ""
    owner: str = ""
    license_type: str = ""
    license_url: str = ""
    evidence: str = ""
    permission_notes: str = ""
    attribution_required: str = "unknown"
    attribution_text: str = ""
    commercial_use: str = "unknown"
    derivatives_allowed: str = "unknown"
    expires_on: str = ""
    reviewed_on: str = ""
    user_confirmed: bool = False
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @property
    def label(self) -> str:
        """Readable status name."""
        return STATUSES.get(self.status, STATUSES[UNKNOWN])

    @property
    def tone(self) -> str:
        """Pill tone for the status."""
        return TONES.get(self.status, "warn")

    @property
    def needs_evidence(self) -> bool:
        """True when the status claims permission that should be written down."""
        return self.status in EVIDENCE_REQUIRED

    @property
    def evidence_recorded(self) -> bool:
        """True when some evidence, license link or note has been entered."""
        return bool(
            self.evidence.strip() or self.license_url.strip() or self.permission_notes.strip()
        )

    @property
    def source_identified(self) -> bool:
        """True when the source is named (a link, or a title and creator)."""
        return bool(self.source_url.strip() or (self.source_title.strip() and self.creator.strip()))


def validate_rights(rights: Rights) -> list[str]:
    """Problems that stop a record from being saved."""
    problems: list[str] = []
    if rights.status not in STATUSES:
        problems.append("Choose a rights status.")
    for name, value in (
        ("attribution_required", rights.attribution_required),
        ("commercial_use", rights.commercial_use),
        ("derivatives_allowed", rights.derivatives_allowed),
    ):
        if value not in TRISTATE:
            problems.append(f"{name.replace('_', ' ').capitalize()} must be yes, no or unknown.")
    for name, value in (("Source link", rights.source_url), ("License link", rights.license_url)):
        if value and not value.lower().startswith(("http://", "https://")):
            problems.append(f"{name} must start with http:// or https://.")
    if rights.expires_on:
        parts = rights.expires_on.split("-")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            problems.append("Expiry date must look like 2027-01-31.")
    if rights.status in CLEARED and rights.status != ORIGINAL and not rights.evidence_recorded:
        problems.append("Record the evidence for this status (a link, a message, a license).")
    return problems


def generate_attribution(rights: Rights) -> str:
    """A credit line made only from facts the user recorded. Empty when nothing is known."""
    if rights.status == ORIGINAL:
        return ""
    title = rights.source_title.strip()
    creator = rights.creator.strip()
    if not (title or creator):
        return ""
    parts = []
    if title and creator:
        parts.append(f"“{title}” by {creator}")
    elif title:
        parts.append(f"“{title}”")
    else:
        parts.append(f"Source: {creator}")
    if rights.license_type.strip():
        parts.append(f"License: {rights.license_type.strip()}")
    return " · ".join(parts)


def hint_from_site(license_text: str | None) -> str:
    """A reminder shown beside the license the site reported. It is never applied as a status."""
    if not license_text:
        return ""
    return (
        f"The site reported the license “{license_text}”. That is only a label on the page: "
        "check the original video and the license terms yourself before choosing a status."
    )
