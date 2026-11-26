"""YouTube upload rules: what an approval covers, limits and error classification.

Pure functions and data only. Nothing here talks to Google or the disk.

Safety rules encoded here (see docs/YOUTUBE.md):

* Only ``private`` and ``unlisted`` can be uploaded. ``public`` is never offered, so the studio
  cannot publish; the owner publishes in YouTube Studio.
* An upload is tied to an approval of the exact file (SHA-256), title, description, tags,
  visibility, audience and destination channel. Changing any of them changes the fingerprint
  and voids the approval.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from urllib.parse import urlparse

SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)
"""``upload`` creates videos; ``readonly`` only lets us show which channel will receive them."""

ALLOWED_VISIBILITY = ("private", "unlisted")
DEFAULT_CATEGORY_ID = "22"  # "People & Blogs"; the owner can change it in YouTube Studio

TITLE_MAX = 100
DESCRIPTION_MAX_BYTES = 5000
TAGS_MAX_CHARS = 500

DRAFT = "draft"
APPROVED = "approved"
UPLOADING = "uploading"
UPLOADED = "uploaded"
FAILED = "failed"
CANCELLED = "cancelled"
STATUSES = (DRAFT, APPROVED, UPLOADING, UPLOADED, FAILED, CANCELLED)
STATUS_LABELS = {
    DRAFT: "Waiting for your approval",
    APPROVED: "Approved, queued to upload",
    UPLOADING: "Uploading",
    UPLOADED: "Uploaded (not published by the studio)",
    FAILED: "Failed",
    CANCELLED: "Cancelled",
}

# error kinds stored with a failure
KIND_AUTH = "auth"
KIND_QUOTA = "quota"
KIND_RATE = "rate"
KIND_NO_CHANNEL = "no_channel"
KIND_REJECTED = "rejected"
KIND_NETWORK = "network"
KIND_OTHER = "other"
KIND_CANCELLED = "cancelled"

APPROVAL_TEXT = "I approve this upload"


@dataclass(frozen=True, slots=True)
class UploadDetails:
    """Everything the owner approves. This is exactly what is sent to YouTube."""

    project_id: int
    render_id: int
    file_name: str
    file_size: int
    file_sha256: str
    title: str
    description: str
    tags: tuple[str, ...]
    visibility: str
    made_for_kids: bool
    channel_id: str
    channel_title: str

    def to_dict(self) -> dict[str, object]:
        """Plain dict for storage and display."""
        data = asdict(self)
        data["tags"] = list(self.tags)
        return data

    def fingerprint(self) -> str:
        """SHA-256 over every approved field."""
        payload = json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()


def validate(details: UploadDetails) -> list[str]:
    """Reasons this cannot be uploaded (empty when it can)."""
    problems: list[str] = []
    if not details.title.strip():
        problems.append("The title is empty.")
    elif len(details.title) > TITLE_MAX:
        problems.append(f"The title is longer than {TITLE_MAX} characters.")
    if "<" in details.title or ">" in details.title:
        problems.append("The title contains < or >, which YouTube does not accept.")
    if len(details.description.encode()) > DESCRIPTION_MAX_BYTES:
        problems.append(f"The description is longer than {DESCRIPTION_MAX_BYTES} bytes.")
    if "<" in details.description or ">" in details.description:
        problems.append("The description contains < or >, which YouTube does not accept.")
    if sum(len(t) for t in details.tags) + len(details.tags) > TAGS_MAX_CHARS:
        problems.append(f"The search tags are longer than {TAGS_MAX_CHARS} characters in total.")
    if details.visibility not in ALLOWED_VISIBILITY:
        problems.append(
            "Only Private or Unlisted uploads are supported. The studio never publishes; "
            "change visibility in YouTube Studio after you have checked the upload."
        )
    if details.file_size <= 0 or not details.file_sha256:
        problems.append("There is no finished export file to upload.")
    if not details.file_name.lower().endswith(".mp4"):
        problems.append("Only MP4 exports can be uploaded.")
    if not details.channel_id:
        problems.append("No destination channel is known. Connect your YouTube account first.")
    return problems


def video_resource(
    details: UploadDetails, category_id: str = DEFAULT_CATEGORY_ID
) -> dict[str, object]:
    """The ``videos.insert`` request body for an approved upload."""
    return {
        "snippet": {
            "title": details.title,
            "description": details.description,
            "tags": list(details.tags),
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": details.visibility,
            "selfDeclaredMadeForKids": details.made_for_kids,
        },
    }


def classify_api_error(status: int, body: bytes) -> tuple[str, str]:
    """Map a YouTube API error response to ``(kind, plain message)``."""
    reason, text = "", ""
    try:
        data = json.loads(body)
        err = data.get("error", {})
        text = " ".join(str(err.get("message", "")).split())[:200]
        errors = err.get("errors") or []
        reason = str(errors[0].get("reason", "")) if errors else ""
    except (ValueError, AttributeError, IndexError, TypeError):
        pass
    if reason in ("quotaExceeded", "dailyLimitExceeded", "uploadLimitExceeded"):
        return KIND_QUOTA, (
            "YouTube says the daily quota or upload limit is used up. Nothing is wrong with "
            "the video; try again after the quota resets (midnight Pacific Time)."
        )
    if reason in ("rateLimitExceeded", "userRateLimitExceeded") or status == 429:
        return KIND_RATE, "YouTube is rate limiting requests. Wait a few minutes and retry."
    if status == 401 or reason in ("authError", "invalidCredentials"):
        return KIND_AUTH, "Google rejected the saved sign-in. Reconnect your YouTube account."
    if reason in ("youtubeSignupRequired", "channelNotFound"):
        return KIND_NO_CHANNEL, "This Google account has no YouTube channel to upload to."
    if status == 403:
        return KIND_REJECTED, f"YouTube refused this request (403). {text}".strip()
    if status == 400:
        return KIND_REJECTED, f"YouTube rejected the video details (400). {text}".strip()
    if status >= 500:
        return KIND_NETWORK, f"YouTube had a server problem (HTTP {status}). Retry later."
    return KIND_OTHER, f"YouTube answered HTTP {status}. {text}".strip()


_GOOGLE_SUFFIXES = (".googleapis.com", ".google.com")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def trusted_endpoint(url: str) -> bool:
    """Whether credentials may be sent to this address.

    Only HTTPS addresses on Google domains, or plain HTTP on this computer (for local test
    stubs). Anything else could leak a token, so it is refused.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https":
        return host == "google.com" or host.endswith(_GOOGLE_SUFFIXES)
    return parsed.scheme == "http" and host in _LOOPBACK
