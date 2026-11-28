"""Write Hindi (Devanagari) text in Roman letters, the way people type it in chat.

Speech recognition writes Hindi, and Urdu recognised as Hindi, in Devanagari. This turns it
into "Roman Urdu / Hinglish" for captions and titles: ``क्या बात है`` becomes ``kya baat hai``.
It changes the letters only. It does not translate and it cannot correct a misheard word.

The rules are simple and always give the same answer:

- the unwritten "a" after a consonant is dropped where Hindi speakers drop it
  (``करता`` is ``karta``);
- a long vowel is doubled in a short closed word (``काम`` is ``kaam``);
- common words and English words written in Devanagari use the table below.

Text in other scripts is returned unchanged.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "ळ": "l", "व": "v",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h",
}  # fmt: skip
_NUKTA = "़"
_WITH_NUKTA = {"क": "q", "ख": "kh", "ग": "g", "ज": "z", "ड": "d", "ढ": "dh", "फ": "f", "य": "y"}
_VOWELS = {
    "अ": "a", "आ": "aa", "इ": "i", "ई": "i", "उ": "u", "ऊ": "oo", "ऋ": "ri",
    "ऍ": "e", "ऎ": "e", "ए": "e", "ऐ": "ai", "ऑ": "o", "ऒ": "o", "ओ": "o", "औ": "au",
}  # fmt: skip
_MATRAS = {
    "ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u",
    "ृ": "ri", "ॅ": "e", "ॆ": "e", "े": "e", "ै": "ai",
    "ॉ": "o", "ॊ": "o", "ो": "o", "ौ": "au",
}  # fmt: skip
_LONG = {"ा": "aa", "ी": "ee", "ू": "oo"}
"""Spelling of a long vowel sign in a one-syllable word that ends in a consonant."""
_VIRAMA = "\u094d"
_NASALS = ("\u0902", "\u0901")  # anusvara, chandrabindu
_VISARGA = "\u0903"
_LABIALS = frozenset("पफबभम")
_SIGNS = str.maketrans(
    {"।": ".", "॥": ".", "ॐ": "om", "॰": "."}
    | {chr(0x0966 + digit): str(digit) for digit in range(10)}
)
"""Punctuation and digits; replaced first so they never count as part of a word."""

_COMMON = """
है=hai हैं=hain में=mein मैं=main हूं=hoon हूँ=hoon क्या=kya क्यों=kyun क्यो=kyun
नहीं=nahi वो=wo वह=woh ये=ye यह=yeh कुछ=kuch अच्छा=acha अच्छे=ache अच्छी=achi
पूरा=poora पुरा=pura बहुत=bahut और=aur तो=to भी=bhi ही=hi को=ko का=ka की=ki के=ke
से=se ने=ne पे=pe पर=par ना=na न=na जो=jo हो=ho कोई=koi भाई=bhai यार=yaar आप=aap
आपको=aapko आपने=aapne वाला=wala वाली=wali वाले=wale ऊपर=upar उपर=upar यहां=yahan
यहाँ=yahan वहां=wahan वहाँ=wahan गाडी=gaadi गाड़ी=gaadi आई=aayi आए=aaye आया=aaya
हुआ=hua हुए=hue गया=gaya गए=gaye दो=do एक=ek तीन=teen लिए=liye लिया=liya दिया=diya
मुझे=mujhe मुझ=mujh मेरा=mera मेरी=meri मेरे=mere तुम=tum कैसे=kaise कैसा=kaisa
गेम=game पुलिस=police पूलिस=police कार=car ट्रक=truck गन=gun फुल=full रियल=real
मशीन=machine क्रेजी=crazy क्रेज़ी=crazy चेक=check वेट=wait सीन=scene स्पीड=speed
केबल=cable बोटल=bottle क्रीम=cream बरगर=burger बर्गर=burger चिपस=chips चिप्स=chips
मिशन=mission लाइक=like वीडियो=video विडियो=video चैनल=channel सब्सक्राइब=subscribe
फोन=phone मोड=mode मिथ=myth गाइज=guys गाइज़=guys एटीएम=ATM जीटीए=GTA जीटी=GT
नंबर=number टाइम=time ओके=okay प्लीज=please प्लीज़=please सॉरी=sorry ट्राई=try
लेवल=level मैप=map पार्ट=part रेडी=ready ट्रेन=train बाइक=bike पावर=power
ट्रंक=trunk पुलीस=police फीचर=feature कमेंट=comment स्टार्ट=start आउट=out
बैंक=bank गाइस=guys एंड=and ट्राइ=try ट्राय=try लुक=look जेसन=Jason लूशिया=Lucia
सिगरेट=cigarette सिग्रेट=cigarette ठीक=theek
लास्ट=last फर्स्ट=first नेक्स्ट=next पीपल=people शॉप=shop ब्रो=bro यस=yes दिस=this
"""


def _normal(text: str) -> str:
    return unicodedata.normalize("NFD", text).replace("\u200c", "").replace("\u200d", "")


_WORDS = {
    _normal(source): roman for source, roman in (pair.split("=", 1) for pair in _COMMON.split())
}
_RUN = re.compile(r"[ऀ-ॿ‌‍]+")


@dataclass(slots=True)
class _Syllable:
    onset: str = ""
    vowel: str = ""
    inherent: bool = False
    """True while the vowel is the unwritten "a", which may be dropped."""
    matra: str = ""
    cluster: bool = False
    """True when the onset joins two or more consonants."""
    tail: str = ""


def contains_devanagari(text: str) -> bool:
    """True when ``text`` has at least one Devanagari character."""
    return any("ऀ" <= c <= "ॿ" for c in text)


def _syllables(word: str) -> list[_Syllable]:
    out: list[_Syllable] = []
    pending: list[str] = []  # consonants waiting for their vowel
    for index, char in enumerate(word):
        if char in _CONSONANTS:
            nukta = word[index + 1 : index + 2] == _NUKTA
            pending.append(_WITH_NUKTA.get(char, _CONSONANTS[char]) if nukta else _CONSONANTS[char])
            after = word[index + (2 if nukta else 1) : index + (3 if nukta else 2)]
            if after == _VIRAMA:
                continue
            if after not in _MATRAS:
                out.append(_Syllable("".join(pending), "a", True, cluster=len(pending) > 1))
                pending = []
        elif char in _MATRAS:
            out.append(
                _Syllable("".join(pending), _MATRAS[char], matra=char, cluster=len(pending) > 1)
            )
            pending = []
        elif char in _VOWELS:
            out.append(_Syllable("", _VOWELS[char]))
        elif char in _NASALS or char == _VISARGA:
            if not out:
                continue
            if char == _VISARGA:
                out[-1].tail += "h"
            else:
                labial = word[index + 1 : index + 2] in _LABIALS
                out[-1].tail += "m" if labial else "n"
                out[-1].inherent = False
    if pending:  # a word that ends on a virama
        out.append(_Syllable("".join(pending), cluster=len(pending) > 1))
    return out


def _drop_unspoken_vowels(parts: list[_Syllable]) -> None:
    if len(parts) < 2:
        return
    last = parts[-1]
    if last.inherent and not last.tail:
        last.vowel = ""
    # Right to left, so a dropped vowel protects the one before it.
    for index in range(len(parts) - 2, 0, -1):
        here, before, after = parts[index], parts[index - 1], parts[index + 1]
        if (
            here.inherent
            and not here.cluster
            and before.vowel
            and after.vowel
            and after.onset
            and not after.cluster
        ):
            here.vowel = ""


def _word(word: str) -> str:
    known = _WORDS.get(word)
    if known is not None:
        return known
    parts = _syllables(word)
    _drop_unspoken_vowels(parts)
    voiced = [p for p in parts if p.vowel]
    if len(voiced) == 1 and parts[-1] is not voiced[0] and voiced[0].matra in _LONG:
        voiced[0].vowel = _LONG[voiced[0].matra]
    return "".join(p.onset + p.vowel + p.tail for p in parts)


def _foreign(char: str) -> bool:
    """A broken character, or a letter or mark of a script other than Latin."""
    if char == "\ufffd":
        return True
    if unicodedata.category(char)[0] not in "LM":
        return False
    return not unicodedata.name(char, "").startswith(("LATIN", "COMBINING"))


def latin_only(text: str) -> str:
    """``text`` without broken characters or letters of other scripts.

    For Hindi transcripts after :func:`romanize`: the speech model sometimes invents a
    Chinese or Greek character, or leaves half of a letter behind.
    """
    if not any(map(_foreign, text)):
        return text
    return re.sub(r"  +", " ", "".join(c for c in text if not _foreign(c)))


def romanize(text: str) -> str:
    """``text`` with every Devanagari word written in Roman letters."""
    if not contains_devanagari(text):
        return text
    return _RUN.sub(lambda match: _word(_normal(match.group())), text.translate(_SIGNS))
