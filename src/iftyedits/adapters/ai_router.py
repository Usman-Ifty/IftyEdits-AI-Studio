"""Chooses which AI provider answers, enforces limits and records usage.

One small layer in front of the existing :class:`~iftyedits.core.protocols.TextModel`
adapters. Business services still see a single ``TextModel``.

Rules (see docs/AI_PROVIDERS.md):

* ``claude``: only Claude. A failure is reported; nothing else is tried.
* ``ollama``: only the local model. No request leaves the computer for an AI API.
* ``hybrid``: Claude first; if it cannot answer, the local model is tried and the switch is
  made visible (``name`` changes to ``ollama:<model>`` and ``notice`` says why). Choosing
  ``hybrid`` is the explicit opt-in; it never happens in any other mode.
* A hard monthly limit stops Claude calls before they are made.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from iftyedits.adapters.claude import AnthropicModel
from iftyedits.adapters.llm import OpenAICompatibleModel, endpoint_host, is_local
from iftyedits.config import Settings
from iftyedits.core import usage as us
from iftyedits.core.errors import LlmBudgetError, LlmError, LlmFormatError, LlmUnavailableError
from iftyedits.core.protocols import TextModel
from iftyedits.storage.ai import AiRepository

log = logging.getLogger(__name__)

_SETTING_KEYS = ("mode", "monthly_usd", "monthly_tokens", "hard", "max_output_tokens")


@dataclass(frozen=True, slots=True)
class AiConfig:
    """The effective, non-secret AI configuration: environment defaults plus saved choices."""

    mode: str
    limits: us.Limits
    prices: us.Prices
    max_output_tokens: int


def resolve_config(settings: Settings, repo: AiRepository | None) -> AiConfig:
    """Environment defaults, overridden by what the owner saved in Settings."""

    def saved(key: str) -> str | None:
        return repo.get_setting(key) if repo else None

    mode = saved("mode")
    if mode not in us.MODES:
        mode = settings.ai_provider
    usd, tokens, hard, out = (saved(k) for k in _SETTING_KEYS[1:])
    return AiConfig(
        mode=mode,
        limits=us.Limits(
            monthly_usd=_float(usd, settings.ai_monthly_budget_usd),
            monthly_tokens=int(_float(tokens, settings.ai_monthly_token_limit)),
            hard=(hard == "1") if hard in ("0", "1") else settings.ai_budget_hard,
        ),
        prices=us.Prices(
            settings.claude_price_input_per_mtok, settings.claude_price_output_per_mtok
        ),
        max_output_tokens=max(256, int(_float(out, settings.ai_max_output_tokens))),
    )


def save_choices(
    repo: AiRepository,
    *,
    mode: str,
    monthly_usd: float,
    monthly_tokens: int,
    hard: bool,
    max_output_tokens: int,
) -> None:
    """Store the owner's non-secret choices."""
    if mode not in us.MODES:
        raise ValueError(f"Unknown AI mode {mode!r}")
    repo.set_setting("mode", mode)
    repo.set_setting("monthly_usd", f"{max(0.0, monthly_usd):g}")
    repo.set_setting("monthly_tokens", str(max(0, int(monthly_tokens))))
    repo.set_setting("hard", "1" if hard else "0")
    repo.set_setting("max_output_tokens", str(max(256, int(max_output_tokens))))


def _float(raw: str | None, default: float) -> float:
    try:
        return float(raw) if raw is not None else float(default)
    except ValueError:
        return float(default)


def current_month(now: datetime | None = None) -> str:
    """``YYYY-MM`` in UTC."""
    return (now or datetime.now(UTC)).strftime("%Y-%m")


def claude_ready(settings: Settings) -> str | None:
    """None when Claude can be used, else what is missing."""
    if not settings.anthropic_api_key.get_secret_value().strip():
        return "ANTHROPIC_API_KEY is not set."
    if not settings.claude_model.strip():
        return "IFTYEDITS_CLAUDE_MODEL is not set."
    return None


def ollama_ready(settings: Settings) -> str | None:
    """None when a local/OpenAI-compatible model is named, else what is missing."""
    if not settings.llm_model.strip():
        return "IFTYEDITS_LLM_MODEL is not set."
    return None


class RoutedModel:
    """A ``TextModel`` that picks the provider, enforces limits and records every call."""

    def __init__(
        self,
        config: AiConfig,
        claude: TextModel | None,
        claude_problem: str | None,
        ollama: TextModel | None,
        repo: AiRepository | None,
        *,
        now: datetime | None = None,
    ) -> None:
        self.config = config
        self._claude = claude
        self._claude_problem = claude_problem
        self._ollama = ollama
        self._repo = repo
        self._now = now
        self.notice = ""
        self.last_provider = ""
        self._label = self._primary_label()

    # -- identity ----------------------------------------------------------------------

    def _primary_label(self) -> str:
        if self.config.mode == us.MODE_OLLAMA and self._ollama:
            return f"{us.PROVIDER_OLLAMA}:{self._ollama.name}"
        if self._claude:
            return f"{us.PROVIDER_CLAUDE}:{self._claude.name}"
        if self._ollama:
            return f"{us.PROVIDER_OLLAMA}:{self._ollama.name}"
        return ""

    @property
    def name(self) -> str:
        """``provider:model`` of the model that answered last (or will answer first)."""
        return self._label

    # -- calls -------------------------------------------------------------------------

    def complete(self, system: str, user: str, *, max_tokens: int = 2048) -> str:
        """Answer with the selected provider; fall back only in hybrid mode."""
        self.notice = ""
        capped = min(max_tokens, self.config.max_output_tokens)
        mode = self.config.mode
        if mode == us.MODE_OLLAMA:
            return self._call(us.PROVIDER_OLLAMA, self._ollama, system, user, capped)
        try:
            return self._call_claude(system, user, capped)
        except LlmFormatError:
            raise  # Claude answered; a badly formed answer is not an outage
        except LlmError as exc:
            if mode != us.MODE_HYBRID:
                raise
            if self._ollama is None:
                raise LlmUnavailableError(
                    f"{exc} The local fallback is not configured (IFTYEDITS_LLM_MODEL)."
                ) from exc
            self.notice = f"Claude could not answer ({exc}) so the local model wrote this."
            log.warning("claude failed (%s); falling back to ollama", type(exc).__name__)
            return self._call(us.PROVIDER_OLLAMA, self._ollama, system, user, capped)

    def _call_claude(self, system: str, user: str, max_tokens: int) -> str:
        if self._claude is None:
            raise LlmUnavailableError(self._claude_problem or "Claude is not configured.")
        state = self.budget_state()
        if state.blocks:
            raise LlmBudgetError(
                f"The monthly Claude limit blocks further requests. {state.message}"
            )
        return self._call(us.PROVIDER_CLAUDE, self._claude, system, user, max_tokens)

    def _call(
        self, provider: str, model: TextModel | None, system: str, user: str, max_tokens: int
    ) -> str:
        if model is None:
            raise LlmUnavailableError(
                "No local model is configured (IFTYEDITS_LLM_MODEL)."
                if provider == us.PROVIDER_OLLAMA
                else "Claude is not configured."
            )
        label = f"{provider}:{model.name}"
        ok, note = False, ""
        try:
            reply = model.complete(system, user, max_tokens=max_tokens)
            ok = True
        except LlmError as exc:
            note = type(exc).__name__
            raise
        finally:
            self._record(provider, model, ok, note)
        self.last_provider = provider
        self._label = label
        return reply

    def _record(self, provider: str, model: TextModel, ok: bool, note: str) -> None:
        if self._repo is None:
            return
        reported = getattr(model, "last_usage", None)
        try:
            self._repo.add_usage(
                provider=provider,
                model=model.name,
                feature=us.current_feature(),
                usage=reported if isinstance(reported, us.Usage) else None,
                ok=ok,
                note=note,
            )
        except sqlite3.Error as exc:  # accounting must never break a generation
            log.warning("could not record AI usage: %s", type(exc).__name__)

    # -- budget ------------------------------------------------------------------------

    def budget_state(self) -> us.BudgetState:
        """This month's Claude usage against the limits."""
        return month_budget(self._repo, self.config, self._now)


def month_budget(
    repo: AiRepository | None, config: AiConfig, now: datetime | None = None
) -> us.BudgetState:
    """Budget state for the current month (no limit when there is no repository)."""
    if repo is None:
        return us.BudgetState("ok", "No monthly limit is set.", False)
    totals = repo.totals(us.PROVIDER_CLAUDE, current_month(now))
    return us.evaluate_budget(totals, config.prices, config.limits)


def build_text_model(
    settings: Settings, conn: sqlite3.Connection | None = None
) -> RoutedModel | None:
    """The routed model for the current choices, or None when no provider is usable."""
    repo = AiRepository(conn) if conn is not None else None
    config = resolve_config(settings, repo)
    claude: TextModel | None = None
    problem = claude_ready(settings)
    if config.mode in (us.MODE_CLAUDE, us.MODE_HYBRID) and problem is None:
        claude = AnthropicModel(
            settings.anthropic_base_url,
            settings.claude_model,
            settings.anthropic_api_key.get_secret_value(),
            settings.claude_timeout_s,
            max_retries=settings.ai_max_retries,
        )
    ollama: TextModel | None = None
    if config.mode in (us.MODE_OLLAMA, us.MODE_HYBRID) and ollama_ready(settings) is None:
        ollama = OpenAICompatibleModel(
            settings.llm_base_url,
            settings.llm_model.strip(),
            settings.llm_api_key.get_secret_value(),
            settings.llm_timeout_s,
        )
    if claude is None and ollama is None:
        return None
    return RoutedModel(config, claude, problem, ollama, repo)


def probe_ollama(settings: Settings, timeout_s: float = 3.0) -> str:
    """Check the local endpoint without sending a prompt.

    Reports whether the endpoint answers and whether the configured model is installed
    (``GET <base>/models``). It does not prove the model can generate; see :func:`check_local`.
    """
    url = settings.llm_base_url.rstrip("/") + "/models"
    if not url.startswith(("http://", "https://")):
        return "The endpoint must be an http:// or https:// address."
    host = endpoint_host(settings.llm_base_url) or "that address"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as response:  # noqa: S310
            raw = response.read(1_000_000)
    except urllib.error.HTTPError as exc:
        return f"The endpoint at {host} answered HTTP {exc.code}."
    except (urllib.error.URLError, OSError):
        return f"Nothing is listening at {host}. Is Ollama installed and running?"
    wanted = settings.llm_model.strip()
    try:
        data = json.loads(raw)
        names = [str(m.get("id", "")) for m in data.get("data", []) if isinstance(m, dict)]
    except (ValueError, AttributeError):
        return f"Reachable at {host}, but the model list could not be read."
    if not wanted:
        have = ", ".join(names[:8]) or "none installed"
        return f"Reachable at {host}. Set IFTYEDITS_LLM_MODEL to one of: {have}."
    if wanted not in names:
        have = ", ".join(names[:8]) or "none installed"
        return f"Reachable at {host}, but model '{wanted}' is not installed (installed: {have})."
    return f"Reachable at {host}; model '{wanted}' is installed."


def check_claude(settings: Settings) -> str:
    """One tiny live request with a synthetic prompt. Sends no project data; uses real tokens."""
    problem = claude_ready(settings)
    if problem:
        return f"Not tested: {problem}"
    model = AnthropicModel(
        settings.anthropic_base_url,
        settings.claude_model,
        settings.anthropic_api_key.get_secret_value(),
        min(settings.claude_timeout_s, 30.0),
        max_retries=0,
    )
    try:
        model.complete("Reply with the single word OK.", "ping", max_tokens=16)
    except LlmError as exc:
        return f"Claude test failed: {exc}"
    used = model.last_usage
    tail = f" ({used.input_tokens} in / {used.output_tokens} out tokens)" if used else ""
    return f"Claude answered with model {settings.claude_model}{tail}."


def check_local(settings: Settings) -> str:
    """One tiny generation on the local model with a synthetic prompt."""
    problem = ollama_ready(settings)
    if problem:
        return f"Not tested: {problem}"
    model = OpenAICompatibleModel(
        settings.llm_base_url,
        settings.llm_model.strip(),
        settings.llm_api_key.get_secret_value(),
        min(settings.llm_timeout_s, 60.0),
    )
    try:
        model.complete("Reply with the single word OK.", "ping", max_tokens=16)
    except LlmError as exc:
        return f"Local model test failed: {exc}"
    return f"Local model {settings.llm_model.strip()} answered."


def with_mode(config: AiConfig, mode: str) -> AiConfig:
    """A copy of the config with another provider mode (used by tests and previews)."""
    return replace(config, mode=mode)


__all__ = [
    "AiConfig",
    "RoutedModel",
    "build_text_model",
    "check_claude",
    "check_local",
    "claude_ready",
    "current_month",
    "is_local",
    "month_budget",
    "ollama_ready",
    "probe_ollama",
    "resolve_config",
    "save_choices",
    "with_mode",
]
