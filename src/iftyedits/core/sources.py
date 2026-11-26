"""Validation of video page URLs (YouTube only) and the metadata fetched for them.

Only ordinary, public watch URLs are accepted. Playlists, channels, other sites and
non-HTTPS links are rejected with a message that says what to paste instead. Nothing here
touches the network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from iftyedits.core.errors import InvalidSourceError

_YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"}
)
_SHORT_HOSTS = frozenset({"youtu.be", "www.youtu.be"})
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_PATH_PREFIXES = ("shorts", "live", "embed", "v")
MAX_URL_LENGTH = 2048
EXTRACTOR = "youtube"

PERMISSION_NOTICE = (
    "Only process videos you own or have permission to reuse. YouTube's terms and the "
    "video owner's rights still apply; downloading here is not a permission grant. "
    "Private, age-restricted, members-only, paid and DRM-protected videos are not supported."
)


@dataclass(frozen=True, slots=True)
class VideoUrl:
    """A validated video link in canonical form."""

    extractor: str
    video_id: str

    @property
    def canonical(self) -> str:
        """Stable watch URL with every other parameter dropped."""
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def source_key(self) -> str:
        """Identity used to detect the same video pasted twice."""
        return f"{self.extractor}:{self.video_id}"


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    """What the site reports about a video before it is downloaded."""

    source_key: str
    url: str
    title: str
    uploader: str
    duration_s: float | None
    upload_date: str | None
    license: str | None
    is_live: bool
    description: str = ""


def parse_video_url(text: str) -> VideoUrl:
    """Validate pasted text and return its canonical video link.

    Raises :class:`InvalidSourceError` with an actionable message otherwise.
    """
    raw = text.strip()
    if not raw:
        raise InvalidSourceError("Paste a YouTube video link.")
    if len(raw) > MAX_URL_LENGTH or any(ch.isspace() for ch in raw):
        raise InvalidSourceError("That is not a single valid link. Paste one video URL.")
    parts = urlsplit(raw if "://" in raw else f"https://{raw}")
    if parts.scheme != "https":
        raise InvalidSourceError("Only https:// links are accepted.")
    host = (parts.hostname or "").lower()
    video_id = ""
    if host in _SHORT_HOSTS:
        video_id = parts.path.strip("/").split("/")[0]
    elif host in _YOUTUBE_HOSTS:
        segments = [s for s in parts.path.split("/") if s]
        query = parse_qs(parts.query)
        if segments and segments[0] == "watch":
            video_id = (query.get("v") or [""])[0]
        elif len(segments) >= 2 and segments[0] in _PATH_PREFIXES:
            video_id = segments[1]
        elif segments and segments[0] in {"playlist", "channel", "c", "user", "results"}:
            raise InvalidSourceError(
                "That link is a playlist, channel or search page. Paste the link of one video."
            )
        elif segments and segments[0].startswith("@"):
            raise InvalidSourceError("That link is a channel page. Paste the link of one video.")
    else:
        raise InvalidSourceError(
            "Only YouTube video links are supported. For other videos, import the file instead."
        )
    if not _VIDEO_ID.match(video_id):
        raise InvalidSourceError("Could not find a YouTube video id in that link.")
    return VideoUrl(EXTRACTOR, video_id)
