"""Content signals (Phase 2A): explainable evidence of interesting moments.

Everything here is deterministic and works on data that has already been measured:

- **Transcript** (``SignalSource.TRANSCRIPT``): sentences are rebuilt from word timings
  (or interpolated inside segments without word timings), then questions, question and
  answer exchanges, hook-style openings, topic changes, fast speech, pauses, uncertain
  transcription and, for English, wording cues (stories, emotional language, humor,
  surprise, advice, motivation) are detected. Wording cues describe *words*, not
  recognised emotions.
- **Audio** (``SignalSource.AUDIO``): silences and sudden volume rises in the loudness
  envelope of the prepared 16 kHz audio. Loudness is evidence of emphasis, not quality.
- **Video** (``SignalSource.VIDEO``): scene changes measured by FFmpeg on the proxy.

Faces and speakers are not analysed (no data for them yet), so no signal claims them.
"""

from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from iftyedits.core.clip_models import (
    SIGNAL_LABELS,
    SIGNAL_SOURCES,
    ContentSignal,
    Evidence,
    LoudnessEnvelope,
    SceneCut,
    Sentence,
    SignalKind,
)
from iftyedits.core.models import TranscriptWord

SIGNALS_VERSION = 2
"""Bump when detection rules change, so stored analyses are not reused."""

MAX_SENTENCE_S = 30.0
SPLIT_PAUSE_S = 1.2
PAUSE_SIGNAL_S = 1.0
LOW_CONFIDENCE = 0.45
DEFAULT_CONFIDENCE = 0.6
LEXICON_CONFIDENCE = 0.85
"""Wording cues are heuristics: their confidence is scaled down by this factor."""
SILENCE_DB = -45.0
SILENCE_MIN_S = 1.0
PEAK_WINDOW_S = 0.5
PEAK_RISE_DB = 10.0
PEAK_MIN_DB = -30.0
PEAK_BASELINE_S = 10.0
MAX_PEAKS = 200
TOPIC_WINDOW_S = 45.0
TOPIC_MIN_WORDS = 15
TOPIC_MIN_GAP_S = 30.0
QA_MAX_ANSWER_S = 40.0
QA_MAX_GAP_S = 2.5

_TERMINAL_RE = re.compile(r"[.!?…\u3002\uff01\uff1f]['\"\u201d\u2019)\]]*$")
_SPLIT_RE = re.compile(r"(?<=[.!?…\u3002\uff01\uff1f])\s+")
_WORD_RE = re.compile(r"[\w']+", re.UNICODE)
_LAUGH_MARK_RE = re.compile(r"[\[(]\s*(laugh\w*|chuckl\w*|giggl\w*)\s*[\])]", re.IGNORECASE)
_LIST_HOOK_RE = re.compile(
    r"^(\d+|two|three|four|five|six|seven|eight|nine|ten)\s+(things|ways|reasons|tips|rules|"
    r"mistakes|secrets|lessons)\b"
)


class SegmentLike(Protocol):
    """A transcript segment with optional word timings (stored or fresh)."""

    @property
    def start_s(self) -> float: ...
    @property
    def end_s(self) -> float: ...
    @property
    def text(self) -> str: ...
    @property
    def words(self) -> tuple[TranscriptWord, ...]: ...
    @property
    def avg_logprob(self) -> float | None: ...


# ---------------------------------------------------------------------- lexicons (English)

Lexicon = tuple[tuple[str, float], ...]

STORY: Lexicon = (
    ("i remember", 1.0), ("one time", 1.0), ("one day", 0.8), ("back when", 0.8),
    ("back in the day", 1.0), ("when i was", 0.8), ("years ago", 0.8), ("last week", 0.5),
    ("last year", 0.5), ("so there i was", 1.0), ("true story", 1.0),
    ("long story short", 1.0), ("this one time", 1.0), ("what happened was", 1.0),
    ("the first time", 0.7), ("i was like", 0.5), ("he was like", 0.5),
    ("she was like", 0.5), ("story", 0.5), ("happened", 0.4),
)  # fmt: skip
EMOTION: Lexicon = (
    ("love", 0.5), ("hate", 0.6), ("scared", 0.8), ("terrified", 1.0), ("crazy", 0.6),
    ("insane", 0.8), ("amazing", 0.6), ("incredible", 0.7), ("unbelievable", 0.8),
    ("awesome", 0.5), ("terrible", 0.7), ("horrible", 0.7), ("heartbreaking", 1.0),
    ("cried", 1.0), ("crying", 1.0), ("angry", 0.8), ("furious", 1.0), ("excited", 0.6),
    ("obsessed", 0.7), ("disgusting", 0.6), ("nightmare", 0.8), ("wow", 0.6),
    ("oh my god", 1.0), ("oh my gosh", 1.0), ("omg", 0.8), ("holy", 0.5),
    ("so happy", 0.8), ("so sad", 0.8), ("proud", 0.6), ("embarrassing", 0.8),
    ("painful", 0.7), ("freaking", 0.4), ("badass", 0.6),
)  # fmt: skip
HUMOR: Lexicon = (
    ("haha", 1.0), ("hahaha", 1.0), ("lol", 0.8), ("hilarious", 1.0), ("funny", 0.6),
    ("joke", 0.6), ("joking", 0.7), ("kidding", 0.7), ("i'm dead", 0.9),
    ("ridiculous", 0.6), ("laughing", 0.8), ("that's so dumb", 0.7),
)  # fmt: skip
SURPRISE: Lexicon = (
    ("no way", 1.0), ("can't believe", 1.0), ("cannot believe", 1.0), ("plot twist", 1.0),
    ("turns out", 0.9), ("shocking", 0.9), ("shocked", 0.9), ("surprised", 0.7),
    ("surprising", 0.7), ("nobody knows", 0.9), ("no one knows", 0.9), ("secret", 0.6),
    ("the truth is", 0.9), ("revealed", 0.8), ("reveal", 0.7), ("guess what", 0.9),
    ("believe it or not", 1.0), ("you won't believe", 1.0), ("out of nowhere", 0.8),
    ("suddenly", 0.6), ("wait what", 1.0), ("what the", 0.6),
)  # fmt: skip
INSIGHT: Lexicon = (
    ("the key is", 1.0), ("the trick is", 1.0), ("the secret is", 1.0), ("the reason", 0.7),
    ("here's why", 1.0), ("here's how", 1.0), ("how to", 0.6), ("what i learned", 1.0),
    ("the lesson", 0.9), ("lesson", 0.5), ("tip", 0.5), ("step one", 0.8),
    ("first step", 0.8), ("you should", 0.6), ("you need to", 0.6), ("make sure", 0.6),
    ("the most important", 0.9), ("important", 0.4), ("the problem is", 0.8),
    ("the point is", 0.8), ("for example", 0.6), ("which means", 0.6), ("that's why", 0.6),
)  # fmt: skip
MOTIVATION: Lexicon = (
    ("never give up", 1.0), ("don't give up", 1.0), ("believe in yourself", 1.0),
    ("keep going", 0.9), ("hard work", 0.8), ("work hard", 0.8), ("discipline", 0.8),
    ("consistency", 0.8), ("consistent", 0.6), ("dream", 0.5), ("dreams", 0.5),
    ("goal", 0.4), ("goals", 0.5), ("success", 0.6), ("successful", 0.6),
    ("you can do it", 1.0), ("push yourself", 1.0), ("no excuses", 1.0), ("grind", 0.6),
    ("mindset", 0.8), ("comeback", 0.7),
)  # fmt: skip
HOOK_STARTS: tuple[str, ...] = (
    "here's", "here is", "the biggest", "the best", "the worst", "the secret", "the reason",
    "the truth", "nobody", "no one", "if you", "you need", "you have to", "you won't",
    "stop", "never", "what if", "imagine", "this is why", "this is how", "let me tell you",
    "the craziest", "the most", "i can't believe", "did you know", "why do", "why does",
    "how do", "how does", "what's the", "what is the", "this is the",
)  # fmt: skip
CONNECTOR_STARTS: frozenset[str] = frozenset(
    {"and", "but", "so", "because", "or", "then", "also", "which", "um", "uh", "like",
     "yeah", "anyway", "plus", "though", "cause", "'cause", "that's", "it's"}
)  # fmt: skip
WH_WORDS = frozenset({"what", "why", "how", "who", "where", "when", "which", "would", "do",
                      "does", "did", "is", "are", "can", "could", "should", "have"})  # fmt: skip
STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "so", "to", "of", "in", "on", "at", "for", "with",
    "from", "by", "as", "is", "are", "was", "were", "be", "been", "being", "it", "its", "it's",
    "this", "that", "these", "those", "i", "you", "he", "she", "we", "they", "me", "him",
    "her", "us", "them", "my", "your", "his", "our", "their", "what", "which", "who", "whom",
    "whose", "when", "where", "why", "how", "not", "no", "yes", "do", "does", "did", "done",
    "have", "has", "had", "will", "would", "can", "could", "should", "shall", "may", "might",
    "must", "just", "like", "really", "very", "about", "there", "here", "then", "than", "also",
    "too", "into", "out", "up", "down", "over", "again", "more", "most", "some", "any", "all",
    "one", "two", "get", "got", "go", "going", "gonna", "know", "think", "mean", "yeah",
    "okay", "right", "well", "um", "uh", "oh", "actually", "basically", "kind", "sort",
    "thing", "things", "lot",
})  # fmt: skip
LEXICON_LANGUAGES = frozenset({"en"})

CALL_TO_ACTION: Lexicon = (
    ("subscribe", 1.0), ("like and subscribe", 1.0), ("hit the like", 1.0),
    ("smash the like", 1.0), ("hit like", 0.9), ("click like", 1.0), ("click the like", 1.0),
    ("give it a like", 1.0), ("leave a like", 1.0), ("drop a like", 1.0), ("thumbs up", 0.9),
    ("hit the bell", 1.0), ("bell icon", 1.0), ("notification bell", 1.0),
    ("turn on notifications", 1.0), ("enable notifications", 1.0), ("notifications", 0.6),
    ("leave a comment", 0.9), ("leave us a comment", 0.9), ("comment below", 0.9),
    ("comments below", 0.9),
    ("down in the comments", 0.9), ("let me know in the comments", 1.0),
    ("link in the description", 1.0), ("links in the description", 1.0),
    ("link below", 0.9), ("links below", 0.9), ("in the description", 0.6),
    ("if you liked this video", 1.0), ("if you like this video", 1.0),
    ("if you enjoyed this video", 1.0), ("if you enjoyed the video", 1.0),
    ("like this video", 0.8), ("share this video", 1.0), ("my channel", 0.8),
    ("our channel", 0.8), ("this channel", 0.7), ("the channel", 0.4), ("follow me", 0.7),
    ("patreon", 0.9), ("sponsored by", 0.9), ("new videos every", 0.8), ("check out", 0.4),
    ("don't forget to", 0.5),
)  # fmt: skip
INTRO_WORDING: Lexicon = (
    ("welcome back", 0.9), ("welcome to", 0.5), ("hey guys", 0.9), ("hey folks", 0.9),
    ("hey everyone", 0.9), ("hey everybody", 0.9), ("hi everyone", 0.9), ("hi guys", 0.9),
    ("hello everyone", 0.9), ("hello guys", 0.9), ("what's up guys", 1.0),
    ("what's up everyone", 1.0), ("in this video", 0.65), ("in today's video", 1.0),
    ("today's video", 0.8), ("my name is", 0.8), ("before we get started", 0.9),
    ("before we get into it", 0.9), ("without further ado", 1.0), ("let's jump right in", 0.7),
    ("let's dive in", 0.7), ("let's get into it", 0.5),
)  # fmt: skip
OUTRO_WORDING: Lexicon = (
    ("thanks for watching", 1.0), ("thank you for watching", 1.0),
    ("thank you very much for watching", 1.0), ("for watching this video", 0.9),
    ("thanks for tuning in", 1.0), ("thanks for listening", 1.0),
    ("see you in the next", 1.0), ("see you next time", 1.0), ("see you guys", 0.8),
    ("see you later", 0.8), ("until next time", 0.9), ("that's it for this video", 1.0),
    ("that's all for this video", 1.0), ("that's all for today", 1.0),
    ("that's it for today", 1.0), ("catch you in the next", 1.0), ("catch you later", 0.8),
    ("peace out", 0.9), ("goodbye", 0.6), ("bye bye", 0.8), ("stay tuned", 0.8),
    ("before you go", 0.8), ("take care", 0.5), ("see you", 0.4),
)  # fmt: skip
BOILERPLATE: tuple[tuple[str, str, Lexicon], ...] = (
    ("call_to_action", "call to action", CALL_TO_ACTION),
    ("intro", "channel intro", INTRO_WORDING),
    ("outro", "sign-off", OUTRO_WORDING),
)
BOILERPLATE_MIN = 0.6
"""A sentence is channel boilerplate when its best cue reaches this strength."""
BOILERPLATE_MAX_WORDS = 25
"""Longer sentences are content with a passing mention, never boilerplate."""
STRONG_CUE = 0.8
"""An emotional or humor cue this strong stands alone; weaker ones need support."""
REACTION_MAX_WORDS = 9
_REACTION_OPENERS = frozenset(
    {"i", "i'm", "we", "it", "it's", "that", "that's", "this", "so", "wow", "oh"}
)
_NEGATORS = frozenset(
    {"not", "no", "never", "isn't", "wasn't", "aren't", "weren't", "don't", "didn't",
     "doesn't", "wouldn't", "hardly", "barely", "nothing"}
)  # fmt: skip
_BLOCKED_AFTER: dict[str, frozenset[str]] = {
    "guess what": frozenset({"i", "you", "we", "they", "to", "can", "would", "could"}),
}
"""Words that, directly before a phrase, show it is used literally ("i guess what...")."""


def normalise(text: str) -> str:
    """Lower-case text with straight apostrophes, for matching."""
    return text.replace("\u2019", "'").replace("\u2018", "'").lower()


def tokens(text: str) -> list[str]:
    """Lower-case word tokens."""
    return _WORD_RE.findall(normalise(text))


def first_word(text: str) -> str:
    """First lower-case word of a text ('' if none)."""
    found = tokens(text)
    return found[0] if found else ""


def starts_with_connector(text: str) -> bool:
    """True if the text opens with a continuation word (and, but, so, because...)."""
    return first_word(text) in CONNECTOR_STARTS


def _occurrences(words: Sequence[str], phrase: str) -> list[int]:
    target = phrase.split()
    size = len(target)
    return [i for i in range(len(words) - size + 1) if list(words[i : i + size]) == target]


def match_lexicon(text: str, lexicon: Lexicon) -> list[tuple[str, float]]:
    """Phrases of ``lexicon`` found in ``text`` as whole words."""
    words = tokens(text)
    padded = " " + " ".join(words) + " "
    found: list[tuple[str, float]] = []
    for phrase, weight in lexicon:
        if f" {phrase} " not in padded:
            continue
        blocked = _BLOCKED_AFTER.get(phrase)
        if blocked and all(i > 0 and words[i - 1] in blocked for i in _occurrences(words, phrase)):
            continue
        found.append((phrase, weight))
    return found


def is_negated(text: str, phrase: str) -> bool:
    """True when every use of ``phrase`` follows a negation ("not funny", "isn't that scary")."""
    words = tokens(text)
    spots = _occurrences(words, phrase)
    return bool(spots) and all(any(w in _NEGATORS for w in words[max(0, i - 3) : i]) for i in spots)


@dataclass(frozen=True, slots=True)
class Boilerplate:
    """Channel boilerplate found in a sentence: a call to action, an intro or a sign-off."""

    kind: str
    label: str
    strength: float
    phrases: tuple[str, ...]


def boilerplate_match(sentence: Sentence) -> Boilerplate | None:
    """Channel boilerplate in one sentence, or None when it reads as content.

    Only English wording is recognised. A short line with a strong cue ("like and
    subscribe", "welcome back", "thanks for watching") is boilerplate; one weak cue or a long
    sentence that merely mentions the channel is not.
    """
    if sentence.word_count > BOILERPLATE_MAX_WORDS:
        return None
    best: Boilerplate | None = None
    for kind, label, lexicon in BOILERPLATE:
        matched = match_lexicon(sentence.text, lexicon)
        if not matched:
            continue
        strength = min(1.0, max(w for _, w in matched) + 0.15 * (len(matched) - 1))
        if strength >= BOILERPLATE_MIN and (best is None or strength > best.strength):
            best = Boilerplate(kind, label, round(strength, 3), tuple(p for p, _ in matched))
    return best


def boilerplate_share(sentences: Sequence[Sentence]) -> float:
    """Share of the words that are channel boilerplate (0 to 1), weighted by cue strength."""
    words = sum(s.word_count for s in sentences)
    if words <= 0:
        return 0.0
    weighted = 0.0
    for s in sentences:
        found = boilerplate_match(s)
        if found is not None:
            weighted += found.strength * s.word_count
    return weighted / words


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    if not math.isfinite(value):
        return low
    return max(low, min(high, value))


# ---------------------------------------------------------------------- sentences


@dataclass(slots=True)
class _Token:
    start_s: float
    end_s: float
    text: str
    probability: float | None
    timed: bool
    logprob_confidence: float | None


def _join(texts: Iterable[str]) -> str:
    joined = "".join(t if t[:1].isspace() else f" {t}" for t in texts)
    return re.sub(r"\s+", " ", joined).strip()


def _segment_tokens(segment: SegmentLike) -> list[_Token]:
    lp = segment.avg_logprob
    lp_conf = _clamp(math.exp(lp)) if lp is not None and math.isfinite(lp) else None
    if segment.words:
        return [
            _Token(w.start_s, w.end_s, w.text, w.probability, True, lp_conf)
            for w in segment.words
            if w.text.strip()
        ]
    pieces = [p for p in _SPLIT_RE.split(segment.text.strip()) if p.strip()]
    if not pieces:
        return []
    total = sum(len(p) for p in pieces)
    span = max(0.0, segment.end_s - segment.start_s)
    out: list[_Token] = []
    cursor = segment.start_s
    for piece in pieces:
        length = span * len(piece) / total if total else 0.0
        out.append(_Token(cursor, cursor + length, piece, None, False, lp_conf))
        cursor += length
    return out


def build_sentences(
    segments: Sequence[SegmentLike],
    *,
    max_sentence_s: float = MAX_SENTENCE_S,
    split_pause_s: float = SPLIT_PAUSE_S,
) -> tuple[Sentence, ...]:
    """Rebuild sentences across segment boundaries.

    Whisper segments often end mid-sentence, so words are regrouped: a sentence ends at
    sentence punctuation, at a pause of ``split_pause_s`` or more, or (at a short pause)
    once it is longer than ``max_sentence_s``.
    """
    stream: list[_Token] = []
    for segment in sorted(segments, key=lambda s: (s.start_s, s.end_s)):
        stream.extend(_segment_tokens(segment))
    stream.sort(key=lambda t: (t.start_s, t.end_s))

    groups: list[list[_Token]] = []
    current: list[_Token] = []
    for i, token in enumerate(stream):
        current.append(token)
        nxt = stream[i + 1] if i + 1 < len(stream) else None
        gap = (nxt.start_s - token.end_s) if nxt else math.inf
        length = token.end_s - current[0].start_s
        close = (
            bool(_TERMINAL_RE.search(token.text.strip()))
            or gap >= split_pause_s
            or (length >= max_sentence_s and (gap >= 0.25 or token.text.rstrip().endswith(",")))
            or length >= max_sentence_s * 1.5
        )
        if close:
            groups.append(current)
            current = []
    if current:
        groups.append(current)

    sentences: list[Sentence] = []
    for group in groups:
        text = _join(t.text for t in group)
        if not text:
            continue
        probabilities = [t.probability for t in group if t.probability is not None]
        if probabilities:
            confidence: float | None = _clamp(statistics.fmean(probabilities))
        else:
            lp = [t.logprob_confidence for t in group if t.logprob_confidence is not None]
            confidence = _clamp(statistics.fmean(lp)) if lp else None
        word_count = sum(len(tokens(t.text)) or 1 for t in group)
        sentences.append(
            Sentence(
                index=len(sentences),
                start_s=round(group[0].start_s, 3),
                end_s=round(max(group[-1].end_s, group[0].start_s), 3),
                text=text,
                word_count=word_count,
                terminal=bool(_TERMINAL_RE.search(text)),
                confidence=None if confidence is None else round(confidence, 4),
                timed_by_words=all(t.timed for t in group),
            )
        )
    with_gaps: list[Sentence] = []
    for i, s in enumerate(sentences):
        before = s.start_s - sentences[i - 1].end_s if i > 0 else s.start_s
        after = sentences[i + 1].start_s - s.end_s if i + 1 < len(sentences) else math.inf
        with_gaps.append(
            Sentence(
                index=s.index,
                start_s=s.start_s,
                end_s=s.end_s,
                text=s.text,
                word_count=s.word_count,
                terminal=s.terminal,
                confidence=s.confidence,
                timed_by_words=s.timed_by_words,
                gap_before_s=round(max(0.0, before), 3),
                gap_after_s=round(max(0.0, after), 3) if math.isfinite(after) else 999.0,
            )
        )
    return tuple(with_gaps)


# ---------------------------------------------------------------------- transcript signals


def _signal(
    kind: SignalKind,
    start_s: float,
    end_s: float,
    strength: float,
    confidence: float,
    label: str | None = None,
    **evidence: object,
) -> ContentSignal:
    return ContentSignal(
        kind=kind,
        source=SIGNAL_SOURCES[kind],
        start_s=round(max(0.0, start_s), 3),
        end_s=round(max(start_s, end_s), 3),
        strength=round(_clamp(strength), 4),
        confidence=round(_clamp(confidence), 4),
        label=label or SIGNAL_LABELS[kind],
        evidence=dict(evidence),
    )


def _confidence(sentence: Sentence) -> float:
    return sentence.confidence if sentence.confidence is not None else DEFAULT_CONFIDENCE


def _snippet(text: str, limit: int = 160) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def is_question(sentence: Sentence) -> bool:
    """True when the sentence ends with a question mark."""
    return sentence.text.rstrip("\"'\u201d\u2019) ").endswith(("?", "\uff1f"))


def hook_strength(sentence: Sentence, lexicon_supported: bool) -> tuple[float, str] | None:
    """How well a sentence works as an opening line, with the reason; None if it does not."""
    if sentence.word_count > 22 or starts_with_connector(sentence.text):
        return None
    if lexicon_supported and boilerplate_match(sentence) is not None:
        return None  # "if you liked this video..." opens with a hook phrase but sells the channel
    if lexicon_supported:
        lowered = " ".join(tokens(sentence.text))
        for phrase in HOOK_STARTS:
            if lowered.startswith(phrase):
                return 0.75 + (0.15 if sentence.word_count <= 14 else 0.0), f'opens with "{phrase}"'
        if _LIST_HOOK_RE.search(lowered):
            return 0.8, "announces a list"
    if is_question(sentence) and first_word(sentence.text) in WH_WORDS:
        return 0.7, "opens with a direct question"
    if lexicon_supported and sentence.word_count <= 14:
        cues = match_lexicon(sentence.text, SURPRISE) + match_lexicon(sentence.text, EMOTION)
        if any(weight >= 0.8 for _, weight in cues):
            return 0.65, "short line with strong wording"
    return None


_WORDING: tuple[tuple[SignalKind, Lexicon], ...] = (
    (SignalKind.STORY, STORY),
    (SignalKind.EMOTIONAL_WORDING, EMOTION),
    (SignalKind.HUMOR_CUE, HUMOR),
    (SignalKind.SURPRISE, SURPRISE),
    (SignalKind.INSIGHT, INSIGHT),
    (SignalKind.MOTIVATION, MOTIVATION),
)


def _affect_cues(s: Sentence, kind: SignalKind, lexicon: Lexicon) -> list[tuple[str, float]]:
    """Emotional or humor cues in a sentence, only when they read as an actual reaction.

    Ordinary narration uses these words descriptively ("a funny game mechanic", "crazy zany
    NPCs", a song title). A cue counts when it is strong (laughter marked, "oh my god"), when
    two cues support each other, when the line is exclaimed, or when it is a short first-person
    or "it's/that's" reaction ("That's crazy."). Negated cues ("not funny") never count.
    """
    matched = [(p, w) for p, w in match_lexicon(s.text, lexicon) if not is_negated(s.text, p)]
    if kind is SignalKind.HUMOR_CUE and _LAUGH_MARK_RE.search(s.text):
        matched.append(("laughter marked in transcript", 1.0))
    if not matched:
        return []
    lively = "!" in s.text
    reaction = s.word_count <= REACTION_MAX_WORDS and first_word(s.text) in _REACTION_OPENERS
    strong = any(w >= STRONG_CUE for _, w in matched)
    if not (strong or len(matched) >= 2 or lively or reaction):
        return []
    if kind is SignalKind.EMOTIONAL_WORDING and lively:
        matched.append(("exclamation mark", 0.4))
    return matched


def wording_signals(sentences: Sequence[Sentence]) -> list[ContentSignal]:
    """English wording cues per sentence (stories, emotional words, humor, surprise...).

    Channel boilerplate ("if you love this video, subscribe") is skipped: its words are
    promotion, not a reaction. Emotional and humor cues must also pass ``_affect_cues``.
    """
    found: list[ContentSignal] = []
    for s in sentences:
        if boilerplate_match(s) is not None:
            continue
        for kind, lexicon in _WORDING:
            if kind in (SignalKind.EMOTIONAL_WORDING, SignalKind.HUMOR_CUE):
                matched = _affect_cues(s, kind, lexicon)
            else:
                matched = match_lexicon(s.text, lexicon)
            total = sum(weight for _, weight in matched)
            if total < 0.5:
                continue
            found.append(
                _signal(
                    kind,
                    s.start_s,
                    s.end_s,
                    0.35 + 0.25 * min(total, 2.6),
                    _confidence(s) * LEXICON_CONFIDENCE,
                    matched=[phrase for phrase, _ in matched],
                    cue_weight=round(total, 2),
                    sentence=s.index,
                    text=_snippet(s.text),
                )
            )
    return found


def question_signals(sentences: Sequence[Sentence]) -> list[ContentSignal]:
    """Questions, and question-then-answer exchanges."""
    found: list[ContentSignal] = []
    for i, s in enumerate(sentences):
        if not is_question(s) or boilerplate_match(s) is not None:
            continue
        wh = first_word(s.text) in WH_WORDS
        found.append(
            _signal(
                SignalKind.QUESTION,
                s.start_s,
                s.end_s,
                0.55 + (0.2 if wh else 0.0) + (0.1 if s.word_count <= 15 else 0.0),
                _confidence(s),
                sentence=s.index,
                text=_snippet(s.text),
            )
        )
        answer: list[Sentence] = []
        for nxt in sentences[i + 1 :]:
            if is_question(nxt) or nxt.gap_before_s > QA_MAX_GAP_S:
                break
            if answer and nxt.end_s - answer[0].start_s > QA_MAX_ANSWER_S:
                break
            answer.append(nxt)
        if not answer:
            continue
        answer_s = answer[-1].end_s - answer[0].start_s
        if len(answer) < 2 and answer_s < 4.0:
            continue
        confidences = [_confidence(x) for x in (s, *answer)]
        found.append(
            _signal(
                SignalKind.QUESTION_ANSWER,
                s.start_s,
                answer[-1].end_s,
                0.5 + 0.1 * min(4, len(answer)),
                statistics.fmean(confidences),
                question=_snippet(s.text),
                answer=_snippet(answer[0].text),
                sentences=[s.index, answer[-1].index],
            )
        )
    return found


def hook_signals(sentences: Sequence[Sentence], lexicon_supported: bool) -> list[ContentSignal]:
    """Sentences that would make a strong first line of a Short."""
    found: list[ContentSignal] = []
    for s in sentences:
        hook = hook_strength(s, lexicon_supported)
        if hook is None:
            continue
        strength, reason = hook
        found.append(
            _signal(
                SignalKind.HOOK,
                s.start_s,
                s.end_s,
                strength,
                _confidence(s) * LEXICON_CONFIDENCE,
                reason=reason,
                sentence=s.index,
                text=_snippet(s.text),
            )
        )
    return found


def rapid_exchange_signals(sentences: Sequence[Sentence]) -> list[ContentSignal]:
    """Runs of short lines with a question or exclamation (speakers are not identified)."""
    found: list[ContentSignal] = []
    i = 0
    while i < len(sentences):
        run = [sentences[i]]
        j = i + 1
        while (
            j < len(sentences)
            and sentences[j].duration_s <= 2.5
            and sentences[j].word_count <= 8
            and sentences[j].gap_before_s <= 1.5
            and sentences[j].end_s - run[0].start_s <= 15.0
        ):
            run.append(sentences[j])
            j += 1
        short = run[0].duration_s <= 2.5 and run[0].word_count <= 8
        lively = any(is_question(s) or "!" in s.text for s in run)
        if short and len(run) >= 4 and lively and boilerplate_share(run) < 0.5:
            found.append(
                _signal(
                    SignalKind.RAPID_EXCHANGE,
                    run[0].start_s,
                    run[-1].end_s,
                    0.5 + 0.08 * min(5, len(run) - 4),
                    statistics.fmean(_confidence(s) for s in run) * LEXICON_CONFIDENCE,
                    "Rapid back-and-forth (short lines; speakers are not identified)",
                    lines=len(run),
                    sentences=[run[0].index, run[-1].index],
                )
            )
            i = j
        else:
            i += 1
    return found


def _content_terms(text: str, extra_stopwords: frozenset[str]) -> list[str]:
    terms = []
    for token in tokens(text):
        if len(token) < 3 or token in STOPWORDS or token in extra_stopwords or token.isdigit():
            continue
        terms.append(token[:-1] if token.endswith("s") and len(token) > 4 else token)
    return terms


def _cosine(a: Counter[str], b: Counter[str]) -> float:
    shared = set(a) & set(b)
    dot = sum(a[t] * b[t] for t in shared)
    norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return dot / norm if norm else 0.0


def topic_shift_signals(sentences: Sequence[Sentence]) -> list[ContentSignal]:
    """Points where the vocabulary before and after changes most (lexical cohesion).

    For every sentence boundary the content words of the preceding and following
    ``TOPIC_WINDOW_S`` seconds are compared; local minima of their cosine similarity
    well below the typical similarity are topic changes. Works for any language that
    separates words with spaces; words found in most sentences act as extra stopwords.
    """
    if len(sentences) < 6:
        return []
    document_frequency = Counter(t for s in sentences for t in set(tokens(s.text)))
    extra = frozenset(
        t for t, n in document_frequency.items() if n >= 0.6 * len(sentences)
    )  # words in most sentences carry no topic (stopwords of any language)
    terms = [_content_terms(s.text, extra) for s in sentences]
    sims: list[tuple[int, float]] = []
    for k in range(len(sentences) - 1):
        boundary = sentences[k].end_s
        left: Counter[str] = Counter()
        right: Counter[str] = Counter()
        for i in range(k, -1, -1):
            if boundary - sentences[i].start_s > TOPIC_WINDOW_S:
                break
            left.update(terms[i])
        for i in range(k + 1, len(sentences)):
            if sentences[i].end_s - boundary > TOPIC_WINDOW_S:
                break
            right.update(terms[i])
        if sum(left.values()) >= TOPIC_MIN_WORDS and sum(right.values()) >= TOPIC_MIN_WORDS:
            sims.append((k, _cosine(left, right)))
    if len(sims) < 5:
        return []
    typical = statistics.median(v for _, v in sims)
    threshold = max(0.05, typical * 0.5)
    minima = []
    for pos, (k, value) in enumerate(sims):
        neighbours = [v for _, v in sims[max(0, pos - 2) : pos + 3]]
        if value < threshold and value <= min(neighbours):
            minima.append((value, k))
    chosen: list[tuple[float, int]] = []
    for value, k in sorted(minima):
        t = sentences[k].end_s
        if all(abs(t - sentences[c].end_s) >= TOPIC_MIN_GAP_S for _, c in chosen):
            chosen.append((value, k))
    found = []
    for value, k in sorted(chosen, key=lambda item: item[1]):
        t = sentences[k].end_s
        strength = 1.0 - value / typical if typical > 0 else 1.0
        found.append(
            _signal(
                SignalKind.TOPIC_SHIFT,
                t,
                sentences[k + 1].start_s,
                strength,
                0.5,
                similarity=round(value, 3),
                typical_similarity=round(typical, 3),
                after_sentence=k,
            )
        )
    return found


def pace_signals(sentences: Sequence[Sentence]) -> list[ContentSignal]:
    """Sentences spoken much faster than this video's typical pace."""
    rates = [
        (s, s.word_count / s.duration_s) for s in sentences if s.duration_s >= 2.0 and s.word_count
    ]
    if len(rates) < 5:
        return []
    typical = statistics.median(r for _, r in rates)
    found = []
    for s, rate in rates:
        ratio = rate / typical if typical else 0.0
        if ratio >= 1.35 and rate >= 3.2:
            found.append(
                _signal(
                    SignalKind.FAST_PACE,
                    s.start_s,
                    s.end_s,
                    (ratio - 1.0) / 0.8,
                    _confidence(s) * (1.0 if s.timed_by_words else 0.6),
                    words_per_s=round(rate, 2),
                    typical_words_per_s=round(typical, 2),
                    sentence=s.index,
                )
            )
    return found


def pause_and_quality_signals(sentences: Sequence[Sentence]) -> list[ContentSignal]:
    """Pauses between sentences and stretches of uncertain transcription."""
    found = []
    for s in sentences:
        if PAUSE_SIGNAL_S <= s.gap_after_s < 999.0:
            found.append(
                _signal(
                    SignalKind.PAUSE,
                    s.end_s,
                    s.end_s + s.gap_after_s,
                    s.gap_after_s / 4.0,
                    0.9 if s.timed_by_words else 0.5,
                    gap_s=round(s.gap_after_s, 2),
                    after_sentence=s.index,
                )
            )
        if s.confidence is not None and s.confidence < LOW_CONFIDENCE:
            found.append(
                _signal(
                    SignalKind.LOW_CONFIDENCE,
                    s.start_s,
                    s.end_s,
                    1.0 - s.confidence / LOW_CONFIDENCE,
                    0.9,
                    mean_probability=round(s.confidence, 3),
                    sentence=s.index,
                    text=_snippet(s.text),
                )
            )
    return found


def transcript_signals(
    sentences: Sequence[Sentence], *, lexicon_supported: bool
) -> list[ContentSignal]:
    """All transcript-derived signals."""
    found = question_signals(sentences)
    found += hook_signals(sentences, lexicon_supported)
    found += rapid_exchange_signals(sentences)
    found += topic_shift_signals(sentences)
    found += pace_signals(sentences)
    found += pause_and_quality_signals(sentences)
    if lexicon_supported:
        found += wording_signals(sentences)
    return found


# ---------------------------------------------------------------------- audio signals


def _windows(envelope: LoudnessEnvelope, window_s: float) -> list[float]:
    """Mean-power loudness (dBFS) per ``window_s``."""
    per = max(1, round(window_s / envelope.frame_s))
    out = []
    for i in range(0, len(envelope.db), per):
        chunk = envelope.db[i : i + per]
        power = statistics.fmean(10 ** (v / 10) for v in chunk)
        out.append(10 * math.log10(power) if power > 0 else -120.0)
    return out


def silence_threshold(envelope: LoudnessEnvelope) -> float:
    """Silence level for this recording: -45 dBFS, lower for very quiet recordings."""
    if not envelope.db:
        return SILENCE_DB
    return min(SILENCE_DB, statistics.median(envelope.db) - 20.0)


def audio_signals(envelope: LoudnessEnvelope) -> list[ContentSignal]:
    """Silences and sudden volume rises in the prepared audio."""
    found: list[ContentSignal] = []
    if not envelope.db:
        return found
    threshold = silence_threshold(envelope)
    run_start: int | None = None
    for i, level in enumerate((*envelope.db, 0.0)):
        quiet = level < threshold and i < len(envelope.db)
        if quiet and run_start is None:
            run_start = i
        elif not quiet and run_start is not None:
            start, end = run_start * envelope.frame_s, i * envelope.frame_s
            if end - start >= SILENCE_MIN_S:
                found.append(
                    _signal(
                        SignalKind.SILENCE,
                        start,
                        end,
                        (end - start) / 5.0,
                        0.9,
                        threshold_db=round(threshold, 1),
                        duration_s=round(end - start, 2),
                    )
                )
            run_start = None

    windows = _windows(envelope, PEAK_WINDOW_S)
    lookback = max(6, round(PEAK_BASELINE_S / PEAK_WINDOW_S))
    peaks: list[tuple[float, int, float, float]] = []
    for i in range(6, len(windows)):
        baseline = statistics.median(windows[max(0, i - lookback) : i])
        rise = windows[i] - baseline
        if rise >= PEAK_RISE_DB and windows[i] >= PEAK_MIN_DB:
            peaks.append((rise, i, windows[i], baseline))
    merged: list[tuple[float, int, float, float]] = []
    for peak in sorted(peaks, key=lambda p: p[1]):
        if merged and peak[1] - merged[-1][1] <= round(2.0 / PEAK_WINDOW_S):
            if peak[0] > merged[-1][0]:
                merged[-1] = peak
            continue
        merged.append(peak)
    strongest = sorted(merged, key=lambda p: (-p[0], p[1]))[:MAX_PEAKS]
    for rise, i, level, baseline in sorted(strongest, key=lambda p: p[1]):
        start = i * PEAK_WINDOW_S
        found.append(
            _signal(
                SignalKind.LOUDNESS_PEAK,
                start,
                start + PEAK_WINDOW_S,
                0.4 + (rise - PEAK_RISE_DB) / 12.0,
                0.8,
                f"Sudden volume rise (+{rise:.0f} dB)",
                rise_db=round(rise, 1),
                level_db=round(level, 1),
                baseline_db=round(baseline, 1),
            )
        )
    return found


def scene_signals(cuts: Sequence[SceneCut]) -> list[ContentSignal]:
    """Scene changes from the proxy video."""
    return [
        _signal(SignalKind.SCENE_CUT, c.time_s, c.time_s, c.score, 0.8, score=round(c.score, 3))
        for c in cuts
    ]


# ---------------------------------------------------------------------- timeline


def lexicon_supported(language: str | None, sentences: Sequence[Sentence]) -> bool:
    """True when English wording cues apply to this transcript."""
    if language is not None:
        return language in LEXICON_LANGUAGES
    text = " ".join(s.text for s in sentences)
    return bool(text) and sum(c.isascii() for c in text) / len(text) > 0.95


def build_evidence(
    segments: Sequence[SegmentLike],
    *,
    duration_s: float,
    language: str | None,
    loudness: LoudnessEnvelope | None,
    scene_cuts: Sequence[SceneCut] | None,
) -> Evidence:
    """Combine transcript, audio and video signals on one timeline (sorted, deterministic)."""
    sentences = build_sentences(segments)
    supported = lexicon_supported(language, sentences)
    signals = transcript_signals(sentences, lexicon_supported=supported)
    if loudness is not None:
        signals += audio_signals(loudness)
    if scene_cuts is not None:
        signals += scene_signals(scene_cuts)
    ordered = tuple(
        sorted(
            (s for s in signals if s.start_s <= max(duration_s, 0.0) + 0.5),
            key=lambda s: (s.start_s, s.end_s, s.kind.value, s.label),
        )
    )
    return Evidence(
        duration_s=duration_s,
        sentences=sentences,
        signals=ordered,
        loudness=loudness,
        scene_cuts=None if scene_cuts is None else tuple(scene_cuts),
        language=language,
        lexicon_supported=supported,
    )
