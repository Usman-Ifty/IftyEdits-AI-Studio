"""AI usage accounting: token counts, an optional cost estimate and budget rules.

Nothing here talks to a provider. Token counts come only from what a provider reported; a
missing count stays missing and is never replaced by a guess. A cost is computed only when the
owner has configured the per-token prices; otherwise it is reported as unavailable.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

PROVIDER_CLAUDE = "claude"
PROVIDER_OLLAMA = "ollama"
MODE_CLAUDE = "claude"
MODE_OLLAMA = "ollama"
MODE_HYBRID = "hybrid"
MODES = (MODE_CLAUDE, MODE_OLLAMA, MODE_HYBRID)

WARN_FRACTION = 0.8

_feature: ContextVar[str] = ContextVar("ai_feature", default="")


@contextmanager
def feature_scope(name: str) -> Iterator[None]:
    """Label every model call made inside the block with the feature that started it."""
    token = _feature.set(name)
    try:
        yield
    finally:
        _feature.reset(token)


def current_feature() -> str:
    """The feature label of the call being made, or an empty string."""
    return _feature.get()


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts a provider reported for one request (None when it did not say)."""

    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """Sums over a set of recorded requests."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    calls_without_usage: int = 0

    @property
    def total_tokens(self) -> int:
        """Input plus output tokens that were reported."""
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class Prices:
    """Owner-configured prices in US dollars per million tokens (None = unknown)."""

    input_per_mtok: float | None = None
    output_per_mtok: float | None = None

    @property
    def known(self) -> bool:
        """True when both prices are set."""
        return self.input_per_mtok is not None and self.output_per_mtok is not None


def estimate_cost(totals: UsageTotals, prices: Prices) -> float | None:
    """Estimated dollars for the reported tokens, or None when prices are not configured."""
    if prices.input_per_mtok is None or prices.output_per_mtok is None:
        return None
    return (
        totals.input_tokens * prices.input_per_mtok + totals.output_tokens * prices.output_per_mtok
    ) / 1_000_000


@dataclass(frozen=True, slots=True)
class Limits:
    """Optional monthly Claude limits. Zero means no limit."""

    monthly_usd: float = 0.0
    monthly_tokens: int = 0
    hard: bool = False


@dataclass(frozen=True, slots=True)
class BudgetState:
    """Where this month's Claude usage stands against the limits."""

    level: str  # "ok" | "warning" | "reached" | "unenforceable"
    message: str
    blocks: bool  # True when a Claude call must not be made


def evaluate_budget(totals: UsageTotals, prices: Prices, limits: Limits) -> BudgetState:
    """Compare usage with limits. Only a hard limit blocks calls; a soft limit only warns."""
    if limits.monthly_usd <= 0 and limits.monthly_tokens <= 0:
        return BudgetState("ok", "No monthly limit is set.", False)
    reasons: list[str] = []
    level = "ok"

    def raise_to(new: str) -> None:
        nonlocal level
        order = {"ok": 0, "warning": 1, "unenforceable": 2, "reached": 3}
        if order[new] > order[level]:
            level = new

    if limits.monthly_tokens > 0:
        used = totals.total_tokens
        if used >= limits.monthly_tokens:
            raise_to("reached")
            reasons.append(f"{used:,} of {limits.monthly_tokens:,} tokens used this month")
        elif used >= WARN_FRACTION * limits.monthly_tokens:
            raise_to("warning")
            reasons.append(f"{used:,} of {limits.monthly_tokens:,} tokens used this month")
    if limits.monthly_usd > 0:
        cost = estimate_cost(totals, prices)
        if cost is None:
            raise_to("unenforceable")
            reasons.append(
                "a dollar limit is set but model prices are not, so spending cannot be "
                "estimated (set IFTYEDITS_CLAUDE_PRICE_INPUT_PER_MTOK and "
                "IFTYEDITS_CLAUDE_PRICE_OUTPUT_PER_MTOK, or use a token limit)"
            )
        elif cost >= limits.monthly_usd:
            raise_to("reached")
            reasons.append(f"estimated ${cost:.2f} of ${limits.monthly_usd:.2f} this month")
        elif cost >= WARN_FRACTION * limits.monthly_usd:
            raise_to("warning")
            reasons.append(f"estimated ${cost:.2f} of ${limits.monthly_usd:.2f} this month")
    if level == "ok":
        return BudgetState("ok", "Within the monthly limit.", False)
    text = "; ".join(reasons).capitalize() + "."
    # A hard limit fails closed: if the spend cannot be measured, Claude calls stop.
    blocks = limits.hard and level in ("reached", "unenforceable")
    return BudgetState(level, text, blocks)
