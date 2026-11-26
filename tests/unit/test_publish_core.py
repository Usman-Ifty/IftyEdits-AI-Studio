"""Metadata, content-review, rights and readiness rules (pure code, synthetic data)."""

from __future__ import annotations

import json

import pytest

from iftyedits.core import metadata as md
from iftyedits.core import readiness as rd
from iftyedits.core import rights as rt
from iftyedits.core import safety as sf
from iftyedits.core.errors import LlmFormatError


def _reply(**extra: object) -> str:
    data = {
        "titles": [
            {"text": "Why this habit changes everything", "style": "curiosity"},
            {"text": "A funny mix-up at the table", "style": "funny"},
            {"text": "How to build a habit in 5 minutes", "style": "informative"},
        ],
        "recommended_index": 2,
        "description": "A short talk about habits. See https://evil.example/x now.",
        "hashtags": ["#Habits", "habits", "Self Improvement", "123", "#"],
        "tags": ["habits", "Habits", "self improvement", "x"],
        "search_phrases": ["build a habit"],
        "thumbnail_frame_s": 4.0,
        "thumbnail_text": "NEW HABIT",
        "summary": "A speaker explains habits.",
        "confidence": "high",
        **extra,
    }
    return "Here you go:\n```json\n" + json.dumps(data) + "\n```"


def test_parse_metadata_validates_and_cleans() -> None:
    values, notes = md.parse_metadata(
        _reply(),
        ["titles", "description", "hashtags", "tags", "thumbnail", "summary"],
        clip_duration_s=20.0,
        allowed_urls=["https://youtu.be/real"],
    )
    assert values["titles"][0].text.startswith("Why this")
    assert values["recommended_index"] == 2
    assert "evil.example" not in values["description"]
    assert any("invented" in n for n in notes)
    assert values["hashtags"] == ("#Habits", "#SelfImprovement")  # dedupe, drop numbers/empty
    assert values["tags"] == ("habits", "self improvement")  # dedupe, drop too short
    assert values["thumbnail_frame_s"] == 4.0
    assert values["confidence"] == "high"


def test_thumbnail_time_outside_clip_is_dropped() -> None:
    values, notes = md.parse_metadata(
        _reply(thumbnail_frame_s=99), ["thumbnail", "summary"], clip_duration_s=20.0
    )
    assert "thumbnail_frame_s" not in values
    assert any("outside the clip" in n for n in notes)


@pytest.mark.parametrize("bad", ["", "no json here", "{broken", "[1, 2]", '{"titles": "x"}'])
def test_parse_metadata_rejects_malformed(bad: str) -> None:
    with pytest.raises(LlmFormatError):
        md.parse_metadata(bad, ["titles", "description"], clip_duration_s=10.0)


def test_title_rules() -> None:
    assert md.title_issues("")[0]
    assert md.title_issues("x" * 101)[0]
    errors, warnings = md.title_issues("THIS IS THE BEST VIDEO EVER!!!")
    assert not errors and len(warnings) >= 2
    assert md.title_issues("A calm, honest title")[1] == []
    assert any("long" in w for w in md.title_issues("a" * 80)[1])


def test_tags_are_bounded_and_deduplicated() -> None:
    many = [f"topic number {i} phrase" for i in range(80)]
    kept = md.clean_tags(many + many)
    assert len(set(map(str.casefold, kept))) == len(kept)
    assert md.tags_length(kept) <= md.TAGS_TOTAL_MAX
    assert md.normalize_tag("<script>") == "script"
    assert md.normalize_hashtag("#hello world!") == "#helloworld"
    assert md.normalize_hashtag("2024") is None


def test_description_composition_never_invents() -> None:
    text = md.compose_description(summary="Clip", hashtags=["#a"])
    assert "Original video" not in text and "#a" in text
    assert "Original video: https://x.test" in md.compose_description(
        summary="Clip", source_url="https://x.test"
    )


def test_metadata_roundtrip_ignores_junk() -> None:
    meta = md.Metadata(titles=(md.TitleOption("A", "funny"),), selected_title="A", hashtags=("#a",))
    again = md.Metadata.from_dict({**meta.to_dict(), "unknown": 1, "tags": "bad"})
    assert again.selected_title == "A" and again.tags == ()


TRANSCRIPT = "we met at the park and then he said you are a total idiot and nobody likes you"


def _safety(**flag: object) -> str:
    base = {
        "category": "harassment",
        "severity": "high",
        "start_s": 3.0,
        "end_s": 6.0,
        "excerpt": "you are a total idiot",
        "explanation": "Direct insult at a person.",
    }
    return json.dumps({"summary": "One insult.", "flags": [{**base, **flag}]})


def test_flag_with_matching_words_is_evidence() -> None:
    flags, summary, _ = sf.parse_safety(_safety(), TRANSCRIPT, clip_duration_s=20.0)
    assert flags[0].certainty == sf.EVIDENCE and flags[0].severity == "high"
    assert sf.risk_level(flags) == sf.HIGH and summary == "One insult."


def test_flag_without_evidence_is_inferred_and_capped() -> None:
    flags, _, notes = sf.parse_safety(
        _safety(excerpt="words that are not there"), TRANSCRIPT, clip_duration_s=20.0
    )
    assert flags[0].certainty == sf.INFERRED and flags[0].severity == "medium"
    assert sf.risk_level(flags) == sf.REVIEW and notes


def test_no_flags_is_low_detected_risk_not_cleared() -> None:
    flags, _, _ = sf.parse_safety('{"flags": []}', TRANSCRIPT, clip_duration_s=20.0)
    assert sf.risk_level(flags) == sf.LOW
    assert "not a guarantee" in sf.DISCLAIMER


def test_timestamps_are_clamped_and_unknown_category_becomes_other() -> None:
    flags, _, _ = sf.parse_safety(
        _safety(category="made up", start_s=-5, end_s=500), TRANSCRIPT, clip_duration_s=20.0
    )
    assert (flags[0].category, flags[0].start_s, flags[0].end_s) == ("other", 0.0, 20.0)


def test_flag_key_is_stable_for_same_concern() -> None:
    a, _, _ = sf.parse_safety(_safety(), TRANSCRIPT, clip_duration_s=20.0)
    b, _, _ = sf.parse_safety(
        _safety(explanation="Worded differently"), TRANSCRIPT, clip_duration_s=20.0
    )
    assert a[0].key == b[0].key


def test_review_answer_without_flag_list_is_rejected() -> None:
    with pytest.raises(LlmFormatError):
        sf.parse_safety('{"summary": "ok"}', TRANSCRIPT, clip_duration_s=20.0)


def test_rights_validation_and_attribution() -> None:
    assert rt.validate_rights(rt.Rights(status=rt.LICENSE))  # needs evidence
    assert not rt.validate_rights(rt.Rights(status=rt.LICENSE, license_url="https://x.test/l"))
    assert rt.validate_rights(rt.Rights(source_url="ftp://x"))
    assert rt.validate_rights(rt.Rights(expires_on="soon"))
    assert not rt.validate_rights(rt.Rights(status=rt.ORIGINAL))
    line = rt.generate_attribution(
        rt.Rights(source_title="Talk", creator="Ana", license_type="CC BY 4.0")
    )
    assert line == "“Talk” by Ana · License: CC BY 4.0"
    assert rt.generate_attribution(rt.Rights()) == ""
    assert rt.generate_attribution(rt.Rights(status=rt.ORIGINAL, creator="me")) == ""
    assert "only a label" in rt.hint_from_site("Creative Commons Attribution license")


def _ready_facts(**over: object) -> rd.Facts:
    base = rd.Facts(
        has_metadata=True,
        title="A title",
        description="A description",
        review_status=sf.STATUS_OK,
        rights_status=rt.ORIGINAL,
        rights_reviewed=True,
        source_identified=True,
        audience="not_for_kids",
        visibility="private",
        export_exists=True,
        export_file_ok=True,
        export_current=True,
        checks=frozenset(rd.USER_CHECKS),
    )
    from dataclasses import replace

    return replace(base, **over)  # type: ignore[arg-type]


def test_complete_checklist_still_needs_explicit_confirmation() -> None:
    report = rd.evaluate(_ready_facts())
    assert report.status == rd.AWAITING and report.can_confirm
    done = rd.evaluate(_ready_facts(confirmed=True, confirmation_valid=True))
    assert done.status == rd.READY


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"has_metadata": False}, rd.METADATA_INCOMPLETE),
        ({"title": ""}, rd.METADATA_INCOMPLETE),
        ({"metadata_current": False}, rd.METADATA_INCOMPLETE),
        ({"checks": frozenset()}, rd.REVIEW_REQUIRED),
        ({"review_status": None}, rd.REVIEW_REQUIRED),
        ({"review_status": sf.STATUS_UNAVAILABLE}, rd.REVIEW_REQUIRED),
        ({"review_current": False}, rd.REVIEW_REQUIRED),
        ({"rights_reviewed": False}, rd.RIGHTS_UNVERIFIED),
        ({"rights_status": rt.UNKNOWN}, rd.RIGHTS_UNVERIFIED),
        ({"rights_status": rt.RESTRICTED}, rd.BLOCKED),
        ({"export_exists": False}, rd.REVIEW_REQUIRED),
        ({"export_file_ok": False}, rd.REVIEW_REQUIRED),
        ({"export_current": False}, rd.REVIEW_REQUIRED),
        ({"audience": ""}, rd.REVIEW_REQUIRED),
        ({"visibility": ""}, rd.REVIEW_REQUIRED),
        ({"flags": (("k", "high", None),)}, rd.BLOCKED),
        ({"flags": (("k", "low", None),)}, rd.REVIEW_REQUIRED),
        (
            {"rights_status": rt.LICENSE, "evidence_recorded": False},
            rd.REVIEW_REQUIRED,
        ),
        (
            {"attribution_required": "yes", "attribution_text": ""},
            rd.REVIEW_REQUIRED,
        ),
    ],
)
def test_each_gap_blocks_readiness(change: dict[str, object], expected: str) -> None:
    report = rd.evaluate(_ready_facts(**change))
    assert report.status == expected
    assert not report.can_confirm


def test_acknowledged_concerns_stay_visible() -> None:
    report = rd.evaluate(
        _ready_facts(
            flags=(("k", "high", sf.RESOLUTION_ACKNOWLEDGED),),
            rights_status=rt.UNKNOWN,
            rights_ack=True,
        )
    )
    assert report.status == rd.AWAITING
    assert len(report.caveats) == 2
    assert "unresolved" in report.caveats[0] or "unresolved" in report.caveats[1]


def test_ready_meaning_denies_clearance() -> None:
    assert "does not" in rd.READY_MEANING and "never uploads" in rd.READY_MEANING


def test_withdrawn_confirmation_reports_revoked() -> None:
    report = rd.evaluate(_ready_facts(confirmed=True, confirmation_valid=False))
    assert report.status == rd.AWAITING and report.revoked
