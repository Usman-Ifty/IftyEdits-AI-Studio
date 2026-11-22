"""Remembered preferences and the escaping in the studio theme helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from iftyedits.services.preferences import Preferences
from iftyedits.ui import theme


def test_preference_roundtrip_and_defaults(tmp_path: Path) -> None:
    prefs = Preferences(tmp_path / "data")
    assert prefs.get("welcome_dismissed") is False
    assert prefs.set("welcome_dismissed", True)
    assert Preferences(tmp_path / "data").get("welcome_dismissed") is True
    assert not list((tmp_path / "data").glob(".prefs-*"))


def test_damaged_file_means_defaults_and_unknown_keys_are_rejected(tmp_path: Path) -> None:
    prefs = Preferences(tmp_path)
    prefs.path.write_text("{not json", encoding="utf-8")
    assert prefs.get("welcome_dismissed") is False
    prefs.path.write_text('{"welcome_dismissed": "yes"}', encoding="utf-8")
    assert prefs.get("welcome_dismissed") is False
    with pytest.raises(KeyError):
        prefs.set("nope", True)


def test_unwritable_location_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert Preferences(blocker / "sub").set("welcome_dismissed", True) is False


def test_html_helpers_escape_user_text() -> None:
    bad = '<script>alert("x")</script>'
    for html in (theme.pill(bad), theme.card(bad, bad), theme.hero(bad, bad), theme.steps([bad])):
        assert "<script>" not in html
    assert theme.clip_status("succeeded") == ("Ready", "good")
    assert theme.clip_status("weird")[1] == "muted"
    assert "<style>" in theme.CSS
