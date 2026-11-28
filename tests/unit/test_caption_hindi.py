"""Hindi captions: font choice, text shaping, and a clear refusal when shaping is missing."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import ImageFont

from iftyedits.adapters import caption_render
from iftyedits.adapters.caption_render import (
    find_font,
    render_cue,
    shaping_available,
    shaping_environment,
)
from iftyedits.core.captions import CaptionCue
from iftyedits.core.errors import ShortRenderError
from iftyedits.core.shorts import CaptionStyle

HINDI = "किताब हिंदी में, क्या बात है।"
_FONT_FILES = [p for p, _, _ in caption_render._DEVANAGARI_FONTS] + [
    p for p, _ in caption_render._DEVANAGARI_FILES
]
needs_hindi = pytest.mark.skipif(
    not (shaping_available() and any(Path(p).is_file() for p in _FONT_FILES)),
    reason="needs Raqm with FriBiDi and a Devanagari font",
)


def _cue(text: str) -> CaptionCue:
    return CaptionCue(0.0, 2.0, text, (), "segment", 0)


def test_hindi_is_refused_when_shaping_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(caption_render, "shaping_available", lambda: False)
    with pytest.raises(ShortRenderError, match="brew install fribidi"):
        render_cue(_cue(HINDI), CaptionStyle(), (540, 960))


def test_latin_captions_do_not_need_shaping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(caption_render, "shaping_available", lambda: False)
    image, _ = render_cue(_cue("plain words"), CaptionStyle(), (540, 960))
    assert image.getbbox() is not None


def test_hindi_is_refused_without_a_font(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(caption_render, "shaping_available", lambda: True)
    monkeypatch.setattr(caption_render, "_DEVANAGARI_FONTS", ())
    monkeypatch.setattr(caption_render, "_DEVANAGARI_FILES", ())
    with pytest.raises(ShortRenderError, match="No installed font"):
        find_font(40, bold=True, text="हिंदी")


@needs_hindi
def test_hindi_uses_real_letters_in_the_right_order() -> None:
    font, note = find_font(80, bold=True, text="हिंदी")
    assert note == ""
    # Different letters must look different: missing glyphs would all be the same box.
    assert bytes(font.getmask("क")) != bytes(font.getmask("म"))
    assert isinstance(font, ImageFont.FreeTypeFont)
    unshaped = font.font_variant(layout_engine=ImageFont.Layout.BASIC)
    # The short i sign is typed after its consonant and drawn before it; a joined pair of
    # consonants takes less room than the two drawn one after the other.
    assert bytes(font.getmask("कि")) != bytes(unshaped.getmask("कि"))
    assert font.getlength("क्ष") < unshaped.getlength("क्ष")


@needs_hindi
@pytest.mark.parametrize("frame", [(1080, 1920), (540, 960)])
def test_hindi_captions_stay_inside_the_safe_area(frame: tuple[int, int]) -> None:
    image, layout = render_cue(_cue(HINDI), CaptionStyle(), frame)
    assert image.getbbox() is not None
    assert layout.left >= 0 and layout.right <= frame[0]
    assert layout.top >= 0.09 * frame[1] and layout.bottom <= 0.81 * frame[1]
    assert layout.note == ""


def test_shaping_environment_adds_the_homebrew_folder(tmp_path: Path) -> None:
    (tmp_path / "libfribidi.dylib").write_bytes(b"")
    env = shaping_environment({"A": "1"}, platform="darwin", library_dir=tmp_path)
    assert env is not None and env["A"] == "1"
    assert env["DYLD_FALLBACK_LIBRARY_PATH"] == f"/usr/local/lib:/usr/lib:{tmp_path}"
    # A restarted process sees the folder and is not restarted again.
    assert shaping_environment(env, platform="darwin", library_dir=tmp_path) is None


def test_shaping_environment_keeps_existing_folders(tmp_path: Path) -> None:
    (tmp_path / "libfribidi.dylib").write_bytes(b"")
    env = shaping_environment(
        {"DYLD_FALLBACK_LIBRARY_PATH": "/mine"}, platform="darwin", library_dir=tmp_path
    )
    assert env is not None
    assert env["DYLD_FALLBACK_LIBRARY_PATH"] == f"/mine:{tmp_path}"


def test_shaping_environment_changes_nothing_when_not_needed(tmp_path: Path) -> None:
    assert shaping_environment({}, platform="darwin", library_dir=tmp_path) is None
    (tmp_path / "libfribidi.dylib").write_bytes(b"")
    assert shaping_environment({}, platform="linux", library_dir=tmp_path) is None
