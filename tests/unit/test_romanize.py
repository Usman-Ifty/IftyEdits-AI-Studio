"""Hindi (Devanagari) text written in Roman letters for captions and titles."""

from __future__ import annotations

import pytest

from iftyedits.core.romanize import contains_devanagari, latin_only, romanize


@pytest.mark.parametrize(
    ("hindi", "roman"),
    [
        ("क्या बात है", "kya baat hai"),
        ("मैं नहीं करता", "main nahi karta"),
        ("लेकिन समझ", "lekin samajh"),
        ("काम नाम दूर तीन", "kaam naam door teen"),
        ("खाना सकता दुकान", "khana sakta dukan"),
        ("दोस्त मतलब", "dost matlab"),
        ("बड़ा ज़रूर फ़िल्म संभव", "bada zarur film sambhav"),
        ("पुलिस की गाड़ी", "police ki gaadi"),
        ("शायद। १२३", "shayad. 123"),
        ("क‍र", "kar"),
    ],
)
def test_hindi_is_written_the_way_people_type_it(hindi: str, roman: str) -> None:
    assert romanize(hindi) == roman


def test_other_text_is_left_alone() -> None:
    for text in ("", "Wait, what?! 100% ñandú", "日本語 العربية"):
        assert romanize(text) == text


def test_mixed_text_keeps_english_and_punctuation() -> None:
    assert romanize("GTA 6 में, wow!") == "GTA 6 mein, wow!"


def test_no_devanagari_survives_any_character() -> None:
    every = " ".join(chr(code) + "क" + chr(code) for code in range(0x0900, 0x0980))
    assert not contains_devanagari(romanize(every))


def test_broken_and_foreign_letters_are_removed_from_hindi_speech() -> None:
    assert latin_only("ku\ufffd puri 的 την bhai!") == "ku puri bhai!"
    assert latin_only("café, 100% sahi") == "café, 100% sahi"
