"""A language-model client for any OpenAI-compatible chat endpoint (Ollama, LM Studio, ...).

The default endpoint is a local Ollama server, so nothing leaves the machine unless the owner
points ``IFTYEDITS_LLM_BASE_URL`` somewhere else. Standard library only. The API key is read
from settings, sent only in the Authorization header, and never logged or put in an error.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

from iftyedits.config import Settings
from iftyedits.core.errors import (
    LlmError,
    LlmFormatError,
    LlmRateLimitError,
    LlmTimeoutError,
    LlmUnavailableError,
)
from iftyedits.core.protocols import TextModel
from iftyedits.core.usage import Usage

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def endpoint_host(base_url: str) -> str:
    """Host name of the endpoint (what the UI shows; never the path or credentials)."""
    return urlparse(base_url).hostname or ""


def is_local(base_url: str) -> bool:
    """True when prompts stay on this computer."""
    return endpoint_host(base_url) in _LOCAL_HOSTS


class OpenAICompatibleModel:
    """Chat completions over HTTP."""

    def __init__(self, base_url: str, model: str, api_key: str, timeout_s: float) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise LlmUnavailableError("The AI endpoint must be an http:// or https:// address.")
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self._timeout = timeout_s
        self.last_usage: Usage | None = None

    @property
    def name(self) -> str:
        """Model identifier."""
        return self._model

    def complete(self, system: str, user: str, *, max_tokens: int = 2048) -> str:
        """Send one prompt and return the reply text."""
        self.last_usage = None
        payload = json.dumps(
            {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.3,
                "max_tokens": max_tokens,
                "stream": False,
            }
        ).encode()
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(self._url, data=payload, headers=headers, method="POST")  # noqa: S310 - scheme checked in __init__
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:  # noqa: S310
                body = response.read(8_000_000)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise LlmRateLimitError(
                    "The AI service is rate limiting requests. Wait and retry."
                ) from exc
            if exc.code in (401, 403):
                raise LlmUnavailableError(
                    "The AI service rejected the credentials (check IFTYEDITS_LLM_API_KEY)."
                ) from exc
            if exc.code == 404:
                raise LlmUnavailableError(
                    f"The AI service has no model named “{self._model}” or no chat endpoint."
                ) from exc
            raise LlmError(f"The AI service answered with an error (HTTP {exc.code}).") from exc
        except TimeoutError as exc:
            raise LlmTimeoutError("The AI service took too long to answer.") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, socket.timeout | TimeoutError):
                raise LlmTimeoutError("The AI service took too long to answer.") from exc
            raise LlmUnavailableError(
                "Could not reach the AI service. Is it running? "
                f"(host: {endpoint_host(self._url) or 'unknown'})"
            ) from exc
        except OSError as exc:
            raise LlmUnavailableError("Could not reach the AI service.") from exc
        try:
            data = json.loads(body)
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LlmFormatError("The AI service reply was not in the expected format.") from exc
        used = data.get("usage") if isinstance(data, dict) else None
        if isinstance(used, dict):
            p_tok, c_tok = used.get("prompt_tokens"), used.get("completion_tokens")
            self.last_usage = Usage(
                p_tok if isinstance(p_tok, int) and not isinstance(p_tok, bool) else None,
                c_tok if isinstance(c_tok, int) and not isinstance(c_tok, bool) else None,
            )
        if not isinstance(content, str) or not content.strip():
            raise LlmFormatError("The AI service returned an empty answer.")
        return content


def build_model(settings: Settings) -> TextModel | None:
    """The configured model, or None when no model name is set."""
    model = settings.llm_model.strip()
    if not model:
        return None
    return OpenAICompatibleModel(
        settings.llm_base_url,
        model,
        settings.llm_api_key.get_secret_value(),
        settings.llm_timeout_s,
    )
