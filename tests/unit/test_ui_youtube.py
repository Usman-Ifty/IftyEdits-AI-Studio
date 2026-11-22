# ruff: noqa: F811
"""Phase 7 screens: the YouTube tab and Settings card, unconfigured and connected."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from iftyedits.adapters.youtube import TokenStore
from tests.unit.test_ui_publish import _page, app_env, seeded  # noqa: F401


def _text(app: AppTest) -> str:
    parts = [
        str(e.value) for e in [*app.markdown, *app.caption, *app.info, *app.warning, *app.error]
    ]
    return " ".join(parts)


@pytest.fixture
def connected(app_env: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    client = tmp_path / "client.json"
    client.write_text(json.dumps({"installed": {"client_id": "id", "client_secret": "sec"}}))
    token = tmp_path / "elsewhere" / "token.json"
    monkeypatch.setenv("IFTYEDITS_YOUTUBE_CLIENT_SECRETS", str(client))
    monkeypatch.setenv("IFTYEDITS_YOUTUBE_TOKEN_PATH", str(token))
    return token


def test_publish_page_without_setup_explains_manual_export(seeded: int) -> None:
    at = _page("publish.py").run()
    assert not at.exception
    assert "Not set up" in _text(at)
    assert "Package" in [t.label for t in at.tabs]
    assert "YouTube" in [t.label for t in at.tabs]
    assert not any(b.label == "Approve and upload" for b in at.button)


def test_settings_youtube_tab_without_setup(app_env: None) -> None:
    at = _page("settings.py").run()
    assert not at.exception
    assert "Not set up" in _text(at)
    assert "never published" in _text(at).lower() or "publish" in _text(at).lower()


def test_configured_but_not_connected_offers_connect(seeded: int, connected: Path) -> None:
    at = _page("publish.py").run()
    assert not at.exception
    assert any(b.label == "Connect YouTube account" for b in at.button)
    assert not any(b.label == "Review upload details" for b in at.button)


def test_connected_but_not_ready_blocks_review_and_explains(seeded: int, connected: Path) -> None:
    TokenStore(connected).save(
        {"refresh_token": "r", "access_token": "a", "expires_at": 9e12, "scope": ""}
    )
    at = _page("publish.py").run()
    assert not at.exception
    review = next(b for b in at.button if b.label == "Review upload details")
    assert review.disabled
    assert "Connected" in _text(at)
    assert "confirm" in _text(at).lower()
    assert not any(b.label == "Approve and upload" for b in at.button)
