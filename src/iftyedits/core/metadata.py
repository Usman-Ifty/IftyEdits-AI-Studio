"""Upload metadata for one Short: the data model, validation and parsing of model answers.

Everything here is pure. A model answer is untrusted text: it is parsed, trimmed, deduplicated
and bounded before it is stored, and never executed or rendered as HTML.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields, replace
from typing import Any

from iftyedits.core.errors import LlmFormatError

TITLE_STYLES: dict[str, str] = {
    "curiosity": "Curiosity",
    "funny": "Funny / entertainment",
    "emotional": "Emotional",
    "informative": "Informative",
    "hook": "Strong hook",
    "search": "Search-focused",
    "conversational": "Natural conversational",
}
TITLE_MAX = 100
TITLE_LONG = 70
DESCRIPTION_MAX = 5000
TAG_MAX = 60
TAGS_TOTAL_MAX = 500
HASHTAG_MAX = 40
HASHTAGS_RECOMMENDED = 5
HASHTAGS_LIMIT = 15
MAX_TITLES = 7
MIN_TITLES = 3

# Regeneratable groups -> the metadata fields each one writes.
GROUPS: dict[str, tuple[str, ...]] = {
    "titles": ("titles", "recommended_index"),
    "description": ("description", "description_long"),
    "hashtags": ("hashtags",),
    "tags": ("tags", "search_phrases"),
    "thumbnail": (
        "thumbnail_text",
        "thumbnail_concept",
        "thumbnail_composition",
        "thumbnail_frame_s",
    ),
    "summary": ("summary", "topic", "category", "audience", "language", "confidence"),
    "engagement": ("pinned_comment", "hooks", "cta"),
    "upload": ("youtube_category", "comments_advice", "visibility_advice", "disclosure_notes"),
}
GROUP_LABELS: dict[str, str] = {
    "titles": "Titles",
    "description": "Description",
    "hashtags": "Hashtags",
    "tags": "Search tags",
    "thumbnail": "Thumbnail",
    "summary": "Summary and topic",
    "engagement": "Pinned comment and hooks",
    "upload": "Upload recommendations",
}
CONFIDENCE_LEVELS = ("low", "medium", "high")

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE = re.compile(r"\s+")
_URL = re.compile(r"https?://[^\s<>\"')]+", re.IGNORECASE)
_HASHTAG_BODY = re.compile(r"[^\w]", re.UNICODE)
_SPAM_PHRASES = (
    "subscribe now",
    "click here",
    "free money",
    "like and share",
    "link in bio",
    "giveaway",
    "100% guaranteed",
    "you won't believe",
    "you wont believe",
)


@dataclass(frozen=True, slots=True)
class TitleOption:
    """One suggested title and the approach it takes."""

    text: str
    style: str = ""


@dataclass(frozen=True, slots=True)
class Metadata:
    """The upload package text for one Short. Empty values mean "not written yet"."""

    titles: tuple[TitleOption, ...] = ()
    recommended_index: int | None = None
    selected_title: str = ""
    description: str = ""
    description_long: str = ""
    hashtags: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    search_phrases: tuple[str, ...] = ()
    summary: str = ""
    topic: str = ""
    category: str = ""
    audience: str = ""
    language: str = ""
    confidence: str = ""
    pinned_comment: str = ""
    hooks: tuple[str, ...] = ()
    cta: str = ""
    thumbnail_text: str = ""
    thumbnail_concept: str = ""
    thumbnail_composition: str = ""
    thumbnail_frame_s: float | None = None
    youtube_category: str = ""
    comments_advice: str = ""
    visibility_advice: str = ""
    disclosure_notes: tuple[str, ...] = ()

    @property
    def recommended_title(self) -> str:
        """The model's recommended title, or the first option."""
        if not self.titles:
            return ""
        index = self.recommended_index if self.recommended_index is not None else 0
        return self.titles[index if 0 <= index < len(self.titles) else 0].text

    @property
    def final_title(self) -> str:
        """The title the user chose (empty until they choose or edit one)."""
        return self.selected_title

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form for storage."""
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "titles":
                value = [{"text": t.text, "style": t.style} for t in value]
            elif isinstance(value, tuple):
                value = list(value)
            out[f.name] = value
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Metadata:
        """Rebuild from stored JSON, ignoring unknown keys and bad values."""
        kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name not in raw:
                continue
            value = raw[f.name]
            if f.name == "titles" and isinstance(value, list):
                kwargs[f.name] = tuple(
                    TitleOption(str(t.get("text", "")), str(t.get("style", "")))
                    for t in value
                    if isinstance(t, Mapping) and t.get("text")
                )
            elif f.name in _TUPLE_FIELDS and isinstance(value, list):
                kwargs[f.name] = tuple(str(v) for v in value)
            elif (f.name in _STR_FIELDS and isinstance(value, str)) or (
                f.name in ("recommended_index",) and isinstance(value, int)
            ):
                kwargs[f.name] = value
            elif f.name == "thumbnail_frame_s" and isinstance(value, int | float):
                kwargs[f.name] = float(value)
        return cls(**kwargs)

    def with_values(self, **values: Any) -> Metadata:
        """A copy with some fields replaced."""
        return replace(self, **values)

    @property
    def filled(self) -> bool:
        """True once there is anything worth keeping."""
        return bool(self.titles or self.selected_title or self.description)


_TUPLE_FIELDS = {
    "hashtags",
    "tags",
    "search_phrases",
    "hooks",
    "disclosure_notes",
}
_STR_FIELDS = {
    f.name
    for f in fields(Metadata)
    if f.name not in _TUPLE_FIELDS | {"titles", "recommended_index", "thumbnail_frame_s"}
}


def clean_text(value: object, limit: int, *, multiline: bool = False) -> str:
    """Trim, normalise and bound untrusted text. Control characters are removed."""
    if not isinstance(value, str):
        return ""
    text = unicodedata.normalize("NFC", _CONTROL.sub("", value)).replace("\r\n", "\n")
    if multiline:
        text = "\n".join(_SPACE.sub(" ", line).strip() for line in text.split("\n"))
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
    else:
        text = _SPACE.sub(" ", text).strip()
    return text[:limit].rstrip()


def normalize_hashtag(value: str) -> str | None:
    """``#Word`` form, or None when the text cannot be a valid YouTube hashtag."""
    body = _HASHTAG_BODY.sub("", value.strip().lstrip("#").replace(" ", ""))
    if not body or body.isdigit() or len(body) > HASHTAG_MAX:
        return None
    return f"#{body}"


def normalize_tag(value: str) -> str | None:
    """A search tag: plain words, no angle brackets or commas, bounded length."""
    text = _SPACE.sub(" ", value.replace("<", "").replace(">", "").replace(",", " ")).strip()
    text = text.lstrip("#").strip()
    if len(text) < 2 or len(text) > TAG_MAX or "\x00" in text:
        return None
    return text


def dedupe(items: Iterable[str]) -> tuple[str, ...]:
    """Remove case-insensitive duplicates, keeping first spelling and order."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return tuple(out)


def clean_hashtags(items: Iterable[object], limit: int = HASHTAGS_LIMIT) -> tuple[str, ...]:
    """Validate, deduplicate and cap hashtags."""
    tags = [normalize_hashtag(i) for i in items if isinstance(i, str)]
    return dedupe(t for t in tags if t)[:limit]


def clean_tags(items: Iterable[object]) -> tuple[str, ...]:
    """Validate and deduplicate search tags; the total length stays within YouTube's limit."""
    cleaned = dedupe(t for t in (normalize_tag(i) for i in items if isinstance(i, str)) if t)
    kept: list[str] = []
    total = 0
    for tag in cleaned:
        cost = len(tag) + (3 if " " in tag else 1)
        if total + cost > TAGS_TOTAL_MAX:
            break
        kept.append(tag)
        total += cost
    return tuple(kept)


def tags_length(tags: Iterable[str]) -> int:
    """Characters the tags use against YouTube's 500-character limit."""
    return sum(len(t) + (3 if " " in t else 1) for t in tags)


def title_issues(title: str) -> tuple[list[str], list[str]]:
    """``(errors, warnings)`` for a title. Errors block use; warnings advise."""
    errors: list[str] = []
    warnings: list[str] = []
    text = title.strip()
    if not text:
        errors.append("The title is empty.")
        return errors, warnings
    if len(text) > TITLE_MAX:
        errors.append(f"{len(text)} characters: YouTube allows at most {TITLE_MAX}.")
    elif len(text) > TITLE_LONG:
        warnings.append(f"{len(text)} characters: long titles are cut off in feeds.")
    if re.search(r"[!?]{2,}", text) or text.count("!") > 2:
        warnings.append("Heavy punctuation can look like spam.")
    words = [w for w in re.findall(r"[^\W\d_]+", text) if len(w) > 2]
    if len(words) >= 3 and sum(w.isupper() for w in words) / len(words) > 0.5:
        warnings.append("Mostly capital letters can look like shouting or spam.")
    lowered = text.casefold()
    for phrase in _SPAM_PHRASES:
        if phrase in lowered:
            warnings.append(f"“{phrase}” reads as spam or clickbait.")
    lower_words = [w.casefold() for w in re.findall(r"\w+", text)]
    if len(lower_words) >= 4 and len(set(lower_words)) <= len(lower_words) // 2:
        warnings.append("Many repeated words.")
    if len(re.findall(r"#\w+", text)) > 3:
        warnings.append("Put hashtags in the description, not the title.")
    return errors, warnings


def description_issues(
    description: str, hashtags: Iterable[str] = ()
) -> tuple[list[str], list[str]]:
    """``(errors, warnings)`` for a description."""
    errors: list[str] = []
    warnings: list[str] = []
    text = description.strip()
    if not text:
        errors.append("The description is empty.")
        return errors, warnings
    if len(text) > DESCRIPTION_MAX:
        errors.append(f"{len(text)} characters: YouTube allows at most {DESCRIPTION_MAX}.")
    count = len(set(re.findall(r"#\w+", text))) or len(tuple(hashtags))
    if count > HASHTAGS_LIMIT:
        warnings.append("YouTube ignores all hashtags when a video has more than 15.")
    elif count > HASHTAGS_RECOMMENDED:
        warnings.append("Three to five hashtags is usually enough.")
    return errors, warnings


def urls_in(text: str) -> list[str]:
    """Web addresses found in text."""
    return [u.rstrip(".,;:!?") for u in _URL.findall(text)]


def strip_unknown_urls(text: str, allowed: Iterable[str]) -> tuple[str, int]:
    """Remove web addresses the model made up; keep only ``allowed`` ones.

    Returns the cleaned text and how many addresses were removed.
    """
    allowed_set = {a.rstrip("/").casefold() for a in allowed if a}
    removed = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal removed
        url = match.group(0).rstrip(".,;:!?")
        if url.rstrip("/").casefold() in allowed_set:
            return match.group(0)
        removed += 1
        return ""

    return re.sub(r" {2,}", " ", _URL.sub(_replace, text)).strip(), removed


def compose_description(
    *,
    summary: str,
    context: str = "",
    attribution: str = "",
    source_url: str = "",
    hashtags: Iterable[str] = (),
) -> str:
    """Assemble the description from the pieces the user chose to include (never invents any)."""
    blocks: list[str] = []
    if summary.strip():
        blocks.append(summary.strip())
    if context.strip():
        blocks.append(context.strip())
    credit: list[str] = []
    if attribution.strip():
        credit.append(attribution.strip())
    if source_url.strip():
        credit.append(f"Original video: {source_url.strip()}")
    if credit:
        blocks.append("\n".join(credit))
    tags = " ".join(hashtags)
    if tags:
        blocks.append(tags)
    return "\n\n".join(blocks)


def extract_json(text: str) -> dict[str, Any]:
    """Pull one JSON object out of a model reply (it may be fenced or have prose around it)."""
    body = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", body, re.DOTALL | re.IGNORECASE)
    if fence:
        body = fence.group(1).strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise LlmFormatError("The model reply did not contain a JSON object.")
    try:
        data = json.loads(body[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LlmFormatError(f"The model reply was not valid JSON ({exc.msg}).") from exc
    if not isinstance(data, dict):
        raise LlmFormatError("The model reply was not a JSON object.")
    return data


def _str_list(value: object, limit: int, each: int) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [t for t in (clean_text(v, each) for v in value[:limit]) if t]


def parse_metadata(
    text: str,
    groups: Iterable[str],
    *,
    clip_duration_s: float,
    allowed_urls: Iterable[str] = (),
) -> tuple[dict[str, Any], list[str]]:
    """Validate a model answer for some groups.

    Returns the field values that passed validation and human-readable notes about what was
    dropped. Raises :class:`LlmFormatError` when nothing usable came back.
    """
    data = extract_json(text)
    values: dict[str, Any] = {}
    notes: list[str] = []
    wanted = list(groups)

    if "titles" in wanted:
        raw_titles = data.get("titles")
        options: list[TitleOption] = []
        if isinstance(raw_titles, list):
            for item in raw_titles[: MAX_TITLES * 2]:
                if isinstance(item, str):
                    item = {"text": item}
                if not isinstance(item, Mapping):
                    continue
                title = clean_text(item.get("text"), TITLE_MAX + 20)
                style = clean_text(item.get("style"), 20).casefold()
                errors, _ = title_issues(title)
                if errors or any(o.text.casefold() == title.casefold() for o in options):
                    continue
                options.append(TitleOption(title, style if style in TITLE_STYLES else ""))
        options = options[:MAX_TITLES]
        if len(options) >= 1:
            values["titles"] = tuple(options)
            index = data.get("recommended_index", data.get("recommended"))
            values["recommended_index"] = (
                index if isinstance(index, int) and 0 <= index < len(options) else 0
            )
            if len(options) < MIN_TITLES:
                notes.append(f"Only {len(options)} usable title(s) came back.")
        else:
            notes.append("No usable titles in the model answer.")

    if "description" in wanted:
        short = clean_text(data.get("description"), DESCRIPTION_MAX, multiline=True)
        long = clean_text(data.get("description_long"), DESCRIPTION_MAX, multiline=True)
        for name, body in (("description", short), ("description_long", long)):
            cleaned, removed = strip_unknown_urls(body, allowed_urls)
            if removed:
                notes.append(f"Removed {removed} web address(es) the model invented from {name}.")
            if cleaned:
                values[name] = cleaned
        if "description" not in values:
            notes.append("No usable description in the model answer.")

    if "hashtags" in wanted:
        raw = data.get("hashtags")
        tags = clean_hashtags(raw if isinstance(raw, list) else [], HASHTAGS_RECOMMENDED + 3)
        if tags:
            values["hashtags"] = tags
        else:
            notes.append("No usable hashtags in the model answer.")

    if "tags" in wanted:
        raw_tags = data.get("tags")
        tags2 = clean_tags(raw_tags if isinstance(raw_tags, list) else [])
        if tags2:
            values["tags"] = tags2
        else:
            notes.append("No usable search tags in the model answer.")
        phrases = dedupe(_str_list(data.get("search_phrases"), 10, 80))
        if phrases:
            values["search_phrases"] = phrases

    if "thumbnail" in wanted:
        for name in ("thumbnail_text", "thumbnail_concept", "thumbnail_composition"):
            limit = 40 if name == "thumbnail_text" else 400
            text_value = clean_text(data.get(name), limit)
            if text_value:
                values[name] = text_value
        frame = data.get("thumbnail_frame_s")
        if isinstance(frame, int | float) and not isinstance(frame, bool):
            if 0 <= frame <= clip_duration_s:
                values["thumbnail_frame_s"] = round(float(frame), 2)
            else:
                notes.append("The suggested thumbnail time was outside the clip and was ignored.")

    if "summary" in wanted:
        for name, limit in (
            ("summary", 400),
            ("topic", 80),
            ("category", 60),
            ("audience", 200),
            ("language", 40),
        ):
            text_value = clean_text(data.get(name), limit)
            if text_value:
                values[name] = text_value
        confidence = clean_text(data.get("confidence"), 10).casefold()
        if confidence in CONFIDENCE_LEVELS:
            values["confidence"] = confidence

    if "engagement" in wanted:
        comment = clean_text(data.get("pinned_comment"), 500, multiline=True)
        if comment:
            values["pinned_comment"] = strip_unknown_urls(comment, allowed_urls)[0]
        hooks = dedupe(_str_list(data.get("hooks"), 5, 140))
        if hooks:
            values["hooks"] = hooks
        cta = clean_text(data.get("cta"), 140)
        if cta:
            values["cta"] = strip_unknown_urls(cta, allowed_urls)[0]

    if "upload" in wanted:
        for name, limit in (
            ("youtube_category", 60),
            ("comments_advice", 200),
            ("visibility_advice", 200),
        ):
            text_value = clean_text(data.get(name), limit)
            if text_value:
                values[name] = text_value
        notes_list = tuple(_str_list(data.get("disclosure_notes"), 5, 200))
        if notes_list:
            values["disclosure_notes"] = notes_list

    if not values:
        raise LlmFormatError("The model answer held nothing usable. " + " ".join(notes))
    return values, notes


def field_owner(name: str) -> str | None:
    """The regeneration group that writes a metadata field."""
    for group, names in GROUPS.items():
        if name in names:
            return group
    return None
