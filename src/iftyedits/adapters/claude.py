"""Anthropic Claude client (Messages API) using only the standard library.

Why no SDK: the studio makes one blocking request at a time and needs only the Messages
endpoint, so a dependency tree (httpx and friends) would add risk for no gain; see D33.

The API key is sent only in the ``x-api-key`` header. It is never logged and never placed in an
error message. Errors are mapped to the studio's own :mod:`~iftyedits.core.errors` types.
Transient failures (429, 5xx, 529 overloaded, dropped connections) are retried a small, bounded
number of times with backoff; authentication and request errors are not retried.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlparse

from iftyedits.core.errors import (
    LlmError,
    LlmFormatError,
    LlmRateLimitError,
    LlmTimeoutError,
    LlmUnavailableError,
)
from iftyedits.core.usage import Usage

ANTHROPIC_VERSION = "2023-06-01"
_RETRY_STATUS = {429, 500, 502, 503, 504, 529}
_MAX_BACKOFF_S = 20.0


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _error_text(body: bytes) -> str:
    """The API's own short error message, if it sent one (never contains our key)."""
    try:
        data = json.loads(body)
        message = data["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return ""
    return " ".join(message.split())[:200] if isinstance(message, str) else ""


class AnthropicModel:
    """Claude via ``POST /v1/messages``."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        timeout_s: float,
        *,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise LlmUnavailableError("The Claude endpoint must be an http:// or https:// address.")
        if not api_key.strip():
            raise LlmUnavailableError("ANTHROPIC_API_KEY is not set.")
        if not model.strip():
            raise LlmUnavailableError("No Claude model is configured (IFTYEDITS_CLAUDE_MODEL).")
        self._url = base_url.rstrip("/") + "/v1/messages"
        self._model = model.strip()
        self._api_key = api_key.strip()
        self._timeout = timeout_s
        self._retries = max_retries
        self._sleep = sleep
        self.last_usage: Usage | None = None
        self.last_stop_reason: str = ""

    @property
    def name(self) -> str:
        """Model identifier."""
        return self._model

    def complete(self, system: str, user: str, *, max_tokens: int = 2048) -> str:
        """Send one prompt and return the reply text."""
        self.last_usage = None
        self.last_stop_reason = ""
        payload = json.dumps(
            {
                "model": self._model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
        ).encode()
        headers = {
            "content-type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        attempt = 0
        while True:
            try:
                body = self._post(payload, headers)
                break
            except _Retryable as exc:
                if attempt >= self._retries:
                    raise exc.error from None
                self._sleep(
                    min(exc.delay if exc.delay is not None else 2.0 * 2**attempt, _MAX_BACKOFF_S)
                )
                attempt += 1
        return self._parse(body)

    def _post(self, payload: bytes, headers: dict[str, str]) -> bytes:
        request = urllib.request.Request(self._url, data=payload, headers=headers, method="POST")  # noqa: S310 - scheme checked in __init__
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                return bytes(response.read(8_000_000))
        except urllib.error.HTTPError as exc:
            detail = _error_text(exc.read(20_000))
            raise self._http_error(exc.code, detail, exc.headers.get("retry-after")) from exc
        except TimeoutError as exc:
            raise _Retryable(LlmTimeoutError("Claude took too long to answer."), None) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout | TimeoutError):
                raise _Retryable(LlmTimeoutError("Claude took too long to answer."), None) from exc
            raise _Retryable(
                LlmUnavailableError("Could not reach the Claude API. Check the network."), None
            ) from exc
        except OSError as exc:
            raise _Retryable(
                LlmUnavailableError("Could not reach the Claude API. Check the network."), None
            ) from exc

    def _http_error(self, status: int, detail: str, retry_after: str | None) -> Exception:
        suffix = f" ({detail})" if detail else ""
        delay: float | None = None
        if retry_after:
            try:
                delay = max(0.0, float(retry_after))
            except ValueError:
                delay = None
        if status == 401:
            return LlmUnavailableError(
                "Claude rejected the API key (401). Check ANTHROPIC_API_KEY belongs to an "
                "Anthropic API account; a Claude subscription is not an API key."
            )
        if status == 403:
            return LlmUnavailableError(
                f"Claude refused this request (403): the key may lack access.{suffix}"
            )
        if status == 404:
            return LlmUnavailableError(
                f"Claude has no model named “{self._model}” (404). Check IFTYEDITS_CLAUDE_MODEL."
            )
        if status == 429:
            return _Retryable(
                LlmRateLimitError("Claude is rate limiting requests (429). Wait and retry."),
                delay,
            )
        if status == 400:
            return LlmError(f"Claude rejected the request (400).{suffix}")
        if status in _RETRY_STATUS:
            label = "overloaded" if status == 529 else f"unavailable (HTTP {status})"
            return _Retryable(
                LlmUnavailableError(f"The Claude API is {label}. Try again later."), delay
            )
        return LlmError(f"The Claude API answered with an error (HTTP {status}).{suffix}")

    def _parse(self, body: bytes) -> str:
        try:
            data = json.loads(body)
            blocks = data["content"]
            text = "".join(
                b["text"] for b in blocks if isinstance(b, dict) and b.get("type") == "text"
            )
            usage = data.get("usage") or {}
            self.last_usage = Usage(
                _int_or_none(usage.get("input_tokens")), _int_or_none(usage.get("output_tokens"))
            )
            self.last_stop_reason = str(data.get("stop_reason") or "")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise LlmFormatError("The Claude reply was not in the expected format.") from exc
        if not text.strip():
            raise LlmFormatError("Claude returned an empty answer.")
        if self.last_stop_reason == "max_tokens":
            raise LlmFormatError(
                "Claude's answer was cut off by the output token limit. Raise "
                "IFTYEDITS_AI_MAX_OUTPUT_TOKENS or shorten the clip."
            )
        return text


class _Retryable(Exception):
    """Internal: a failure worth retrying, carrying the error to raise if retries run out."""

    def __init__(self, error: LlmError, delay: float | None) -> None:
        super().__init__(str(error))
        self.error = error
        self.delay = delay
