"""Phase 6.10: Claude adapter, provider routing, budgets and usage. No paid calls.

Claude is exercised against a local stub HTTP server speaking the Messages wire format. Nothing
here proves behaviour against the real Anthropic API.
"""

from __future__ import annotations

import http.server
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import pytest

from iftyedits.adapters.ai_router import (
    RoutedModel,
    build_text_model,
    claude_ready,
    month_budget,
    resolve_config,
    save_choices,
)
from iftyedits.adapters.claude import AnthropicModel
from iftyedits.config import Settings
from iftyedits.core import usage as us
from iftyedits.core.errors import (
    LlmBudgetError,
    LlmError,
    LlmFormatError,
    LlmRateLimitError,
    LlmTimeoutError,
    LlmUnavailableError,
)
from iftyedits.storage.ai import AiRepository
from iftyedits.storage.db import open_database

KEY = "sk-ant-TEST-SECRET-999"
Reply = tuple[int, bytes, dict[str, str]]


def message(
    text: str = "hello", *, inp: int | None = 11, out: int | None = 7, stop: str = "end_turn"
) -> bytes:
    data: dict[str, object] = {
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop,
    }
    if inp is not None or out is not None:
        data["usage"] = {"input_tokens": inp, "output_tokens": out}
    return json.dumps(data).encode()


class Stub:
    def __init__(self, replies: list[Reply]) -> None:
        self.replies = list(replies)
        self.seen: list[tuple[dict[str, str], dict[str, object]]] = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.seen.append(({k.lower(): v for k, v in self.headers.items()}, body))
                status, data, extra = stub.replies.pop(0) if stub.replies else (500, b"{}", {})
                self.send_response(status)
                for k, v in extra.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def serve() -> Iterator[Callable[[list[Reply]], Stub]]:
    stubs: list[Stub] = []

    def start(replies: list[Reply]) -> Stub:
        stub = Stub(replies)
        stubs.append(stub)
        return stub

    yield start
    for s in stubs:
        s.close()


def claude(stub: Stub, retries: int = 0, timeout: float = 5.0) -> AnthropicModel:
    return AnthropicModel(
        stub.url, "claude-test", KEY, timeout, max_retries=retries, sleep=lambda s: None
    )


# ------------------------------------------------------------------ Claude adapter


def test_success_sends_expected_request_and_extracts_tokens(serve: Callable[..., Stub]) -> None:
    stub = serve([(200, message("hi", inp=120, out=30), {})])
    model = claude(stub)
    assert model.complete("sys", "usr", max_tokens=500) == "hi"
    headers, body = stub.seen[0]
    assert headers["x-api-key"] == KEY and headers["anthropic-version"] == "2023-06-01"
    assert body["model"] == "claude-test" and body["max_tokens"] == 500
    assert body["system"] == "sys" and body["messages"] == [{"role": "user", "content": "usr"}]
    assert model.last_usage == us.Usage(120, 30)


def test_missing_token_counts_stay_unknown(serve: Callable[..., Stub]) -> None:
    stub = serve([(200, message("hi", inp=None, out=None), {})])
    model = claude(stub)
    model.complete("s", "u")
    assert model.last_usage == us.Usage(None, None)


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, LlmUnavailableError),
        (403, LlmUnavailableError),
        (404, LlmUnavailableError),
        (429, LlmRateLimitError),
        (500, LlmUnavailableError),
        (529, LlmUnavailableError),
        (400, LlmError),
    ],
)
def test_http_errors_map_without_leaking_key(
    serve: Callable[..., Stub], status: int, error: type[Exception]
) -> None:
    stub = serve([(status, b'{"error":{"message":"nope"}}', {})])
    with pytest.raises(error) as caught:
        claude(stub).complete("s", "u")
    assert KEY not in str(caught.value) and KEY not in repr(caught.value)


def test_auth_failure_is_not_retried(serve: Callable[..., Stub]) -> None:
    stub = serve([(401, b"{}", {}), (200, message(), {})])
    with pytest.raises(LlmUnavailableError):
        claude(stub, retries=3).complete("s", "u")
    assert len(stub.seen) == 1


def test_rate_limit_retries_then_succeeds_and_honours_retry_after(
    serve: Callable[..., Stub],
) -> None:
    stub = serve([(429, b"{}", {"retry-after": "3"}), (200, message("ok"), {})])
    waits: list[float] = []
    model = AnthropicModel(stub.url, "m", KEY, 5.0, max_retries=2, sleep=waits.append)
    assert model.complete("s", "u") == "ok"
    assert waits == [3.0] and len(stub.seen) == 2


def test_rate_limit_gives_up_after_bounded_retries(serve: Callable[..., Stub]) -> None:
    stub = serve([(429, b"{}", {})] * 5)
    with pytest.raises(LlmRateLimitError):
        claude(stub, retries=2).complete("s", "u")
    assert len(stub.seen) == 3


def test_refused_connection_is_unavailable(serve: Callable[..., Stub]) -> None:
    stub = serve([])
    model = claude(stub)
    stub.close()
    with pytest.raises(LlmUnavailableError) as caught:
        model.complete("s", "u")
    assert KEY not in str(caught.value)


def test_slow_server_times_out() -> None:
    gate = threading.Event()

    class Slow(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            gate.wait(2)

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        model = AnthropicModel(
            f"http://127.0.0.1:{server.server_address[1]}",
            "m",
            KEY,
            0.3,
            max_retries=0,
            sleep=lambda s: None,
        )
        with pytest.raises(LlmTimeoutError):
            model.complete("s", "u")
    finally:
        gate.set()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"{}",
        b'{"content": []}',
        b'{"content": [{"type":"text","text":"  "}]}',
        b'{"content": 5}',
    ],
)
def test_malformed_replies_raise_format_error(serve: Callable[..., Stub], payload: bytes) -> None:
    stub = serve([(200, payload, {})])
    with pytest.raises(LlmFormatError):
        claude(stub).complete("s", "u")


def test_truncated_reply_is_a_format_error_but_usage_is_kept(serve: Callable[..., Stub]) -> None:
    stub = serve([(200, message('{"a":', stop="max_tokens", inp=5, out=9), {})])
    model = claude(stub)
    with pytest.raises(LlmFormatError):
        model.complete("s", "u")
    assert model.last_usage == us.Usage(5, 9)


def test_missing_key_or_bad_endpoint_is_refused_up_front() -> None:
    with pytest.raises(LlmUnavailableError):
        AnthropicModel("https://api.anthropic.com", "m", "  ", 5.0)
    with pytest.raises(LlmUnavailableError):
        AnthropicModel("file:///etc/passwd", "m", KEY, 5.0)


# ------------------------------------------------------------------ fakes + router


class Fake:
    """A scripted model. ``script`` items are replies (str) or exceptions."""

    def __init__(
        self, name: str, script: list[str | Exception], usage: us.Usage | None = None
    ) -> None:
        self._name = name
        self.script = list(script)
        self.calls: list[int] = []
        self.last_usage: us.Usage | None = None
        self._usage = usage

    @property
    def name(self) -> str:
        return self._name

    def complete(self, system: str, user: str, *, max_tokens: int = 2048) -> str:
        self.calls.append(max_tokens)
        self.last_usage = self._usage
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def config(mode: str, **limits: object) -> object:
    from iftyedits.adapters.ai_router import AiConfig

    return AiConfig(
        mode=mode,
        limits=us.Limits(
            monthly_usd=float(limits.get("usd", 0)),  # type: ignore[arg-type]
            monthly_tokens=int(limits.get("tokens", 0)),  # type: ignore[call-overload]
            hard=bool(limits.get("hard", False)),
        ),
        prices=limits.get("prices", us.Prices(None, None)),  # type: ignore[arg-type]
        max_output_tokens=int(limits.get("cap", 2048)),  # type: ignore[call-overload]
    )


def routed(
    mode: str,
    claude_m: Fake | None,
    local: Fake | None,
    conn: sqlite3.Connection,
    **limits: object,
) -> RoutedModel:
    return RoutedModel(
        config(mode, **limits),  # type: ignore[arg-type]
        claude_m,
        None if claude_m else "ANTHROPIC_API_KEY is not set.",
        local,
        AiRepository(conn),
    )


def test_claude_mode_uses_claude_only_and_never_falls_back(db_conn: sqlite3.Connection) -> None:
    c = Fake("sonnet", [LlmUnavailableError("down")])
    o = Fake("llama", ["local reply"])
    model = routed(us.MODE_CLAUDE, c, o, db_conn)
    with pytest.raises(LlmUnavailableError):
        model.complete("s", "u")
    assert o.calls == []


def test_ollama_mode_never_calls_claude(db_conn: sqlite3.Connection) -> None:
    c = Fake("sonnet", ["claude reply"])
    o = Fake("llama", ["local reply"])
    model = routed(us.MODE_OLLAMA, c, o, db_conn)
    assert model.complete("s", "u") == "local reply"
    assert c.calls == [] and model.name == "ollama:llama"


def test_hybrid_uses_claude_first_and_labels_it(db_conn: sqlite3.Connection) -> None:
    c = Fake("sonnet", ["claude reply"], us.Usage(10, 4))
    o = Fake("llama", ["local reply"])
    model = routed(us.MODE_HYBRID, c, o, db_conn)
    assert model.complete("s", "u") == "claude reply"
    assert model.name == "claude:sonnet" and model.notice == "" and o.calls == []


def test_hybrid_falls_back_visibly_when_claude_is_down(db_conn: sqlite3.Connection) -> None:
    c = Fake("sonnet", [LlmRateLimitError("rate limited")])
    o = Fake("llama", ["local reply"])
    model = routed(us.MODE_HYBRID, c, o, db_conn)
    assert model.complete("s", "u") == "local reply"
    assert model.name == "ollama:llama"
    assert "Claude could not answer" in model.notice and "local model" in model.notice


def test_hybrid_does_not_fall_back_on_a_bad_claude_answer(db_conn: sqlite3.Connection) -> None:
    c = Fake("sonnet", [LlmFormatError("garbled")])
    o = Fake("llama", ["local reply"])
    with pytest.raises(LlmFormatError):
        routed(us.MODE_HYBRID, c, o, db_conn).complete("s", "u")
    assert o.calls == []


def test_hybrid_without_claude_key_uses_local_with_notice(db_conn: sqlite3.Connection) -> None:
    o = Fake("llama", ["local reply"])
    model = routed(us.MODE_HYBRID, None, o, db_conn)
    assert model.complete("s", "u") == "local reply"
    assert "ANTHROPIC_API_KEY" in model.notice


def test_hybrid_without_any_fallback_gives_useful_error(db_conn: sqlite3.Connection) -> None:
    c = Fake("sonnet", [LlmUnavailableError("down")])
    with pytest.raises(LlmUnavailableError, match="not configured"):
        routed(us.MODE_HYBRID, c, None, db_conn).complete("s", "u")


def test_claude_mode_without_key_explains_setup(db_conn: sqlite3.Connection) -> None:
    with pytest.raises(LlmUnavailableError, match="ANTHROPIC_API_KEY"):
        routed(us.MODE_CLAUDE, None, Fake("l", ["x"]), db_conn).complete("s", "u")


def test_ollama_mode_without_model_errors(db_conn: sqlite3.Connection) -> None:
    with pytest.raises(LlmUnavailableError, match="IFTYEDITS_LLM_MODEL"):
        routed(us.MODE_OLLAMA, Fake("c", ["x"]), None, db_conn).complete("s", "u")


def test_output_token_cap_is_applied(db_conn: sqlite3.Connection) -> None:
    c = Fake("sonnet", ["r"])
    routed(us.MODE_CLAUDE, c, None, db_conn, cap=700).complete("s", "u", max_tokens=2048)
    assert c.calls == [700]


# ------------------------------------------------------------------ usage + budget


def test_usage_is_recorded_per_provider_and_feature(db_conn: sqlite3.Connection) -> None:
    c = Fake("sonnet", ["a"], us.Usage(100, 25))
    o = Fake("llama", ["b"], us.Usage(40, 10))
    repo = AiRepository(db_conn)
    now = current = datetime.now(UTC).strftime("%Y-%m")
    with us.feature_scope("metadata:titles"):
        routed(us.MODE_CLAUDE, c, None, db_conn).complete("s", "u")
    with us.feature_scope("safety_review"):
        routed(us.MODE_OLLAMA, None, o, db_conn).complete("s", "u")
    assert now == current
    claude_totals = repo.totals(us.PROVIDER_CLAUDE, now)
    assert (claude_totals.calls, claude_totals.input_tokens, claude_totals.output_tokens) == (
        1,
        100,
        25,
    )
    local_totals = repo.totals(us.PROVIDER_OLLAMA, now)
    assert local_totals.input_tokens == 40
    features = {f for f, _ in repo.by_feature(us.PROVIDER_CLAUDE, now)}
    assert features == {"metadata:titles"}


def test_failed_calls_are_recorded_and_unknown_tokens_are_not_invented(
    db_conn: sqlite3.Connection,
) -> None:
    c = Fake("sonnet", [LlmUnavailableError("down")])
    with pytest.raises(LlmUnavailableError):
        routed(us.MODE_CLAUDE, c, None, db_conn).complete("s", "u")
    rows = AiRepository(db_conn).recent(5)
    assert rows[0].ok is False and rows[0].input_tokens is None and rows[0].note


def test_cost_is_unknown_without_prices_and_never_for_local() -> None:
    totals = us.UsageTotals(calls=2, input_tokens=1_000_000, output_tokens=500_000)
    assert us.estimate_cost(totals, us.Prices(None, None)) is None
    assert us.estimate_cost(totals, us.Prices(3.0, 15.0)) == pytest.approx(10.5)


def fill(
    conn: sqlite3.Connection, tokens_in: int, tokens_out: int, provider: str = "claude"
) -> None:
    AiRepository(conn).add_usage(
        provider=provider, model="m", feature="f", usage=us.Usage(tokens_in, tokens_out), ok=True
    )


def test_token_budget_warns_then_blocks_when_hard(db_conn: sqlite3.Connection) -> None:
    c = Fake("sonnet", ["a", "b"], us.Usage(1, 1))
    fill(db_conn, 450, 400)  # 850 of 1000 -> warning
    soft = routed(us.MODE_CLAUDE, c, None, db_conn, tokens=1000, hard=True)
    assert soft.budget_state().level == "warning" and not soft.budget_state().blocks
    assert soft.complete("s", "u") == "a"
    fill(db_conn, 100, 100)
    with pytest.raises(LlmBudgetError):
        soft.complete("s", "u")
    assert len(c.calls) == 1  # blocked before the call


def test_reaching_limit_without_hard_only_warns(db_conn: sqlite3.Connection) -> None:
    fill(db_conn, 900, 900)
    state = routed(us.MODE_CLAUDE, Fake("c", ["a"]), None, db_conn, tokens=1000).budget_state()
    assert state.level == "reached" and not state.blocks


def test_usd_limit_without_prices_is_unenforceable_and_fails_closed(
    db_conn: sqlite3.Connection,
) -> None:
    model = routed(us.MODE_CLAUDE, Fake("c", ["a"]), None, db_conn, usd=5.0, hard=True)
    assert model.budget_state().level == "unenforceable"
    with pytest.raises(LlmBudgetError):
        model.complete("s", "u")


def test_usd_limit_with_prices(db_conn: sqlite3.Connection) -> None:
    fill(db_conn, 1_000_000, 0)  # $3
    prices = us.Prices(3.0, 15.0)
    ok = routed(us.MODE_CLAUDE, Fake("c", ["a"]), None, db_conn, usd=10.0, prices=prices)
    assert ok.budget_state().level == "ok"
    over = routed(
        us.MODE_CLAUDE, Fake("c", ["a"]), None, db_conn, usd=3.0, hard=True, prices=prices
    )
    assert over.budget_state().blocks


def test_local_usage_never_counts_against_the_claude_budget(db_conn: sqlite3.Connection) -> None:
    fill(db_conn, 10_000_000, 10_000_000, provider="ollama")
    state = routed(
        us.MODE_CLAUDE, Fake("c", ["a"]), None, db_conn, tokens=1000, hard=True
    ).budget_state()
    assert state.level == "ok"


def test_hybrid_falls_back_when_hard_budget_blocks_claude(db_conn: sqlite3.Connection) -> None:
    fill(db_conn, 2000, 0)
    c = Fake("sonnet", ["claude"])
    o = Fake("llama", ["local"])
    model = routed(us.MODE_HYBRID, c, o, db_conn, tokens=1000, hard=True)
    assert model.complete("s", "u") == "local" and c.calls == []
    assert "limit" in model.notice


def test_last_months_usage_does_not_count(db_conn: sqlite3.Connection) -> None:
    AiRepository(db_conn).add_usage(
        provider="claude",
        model="m",
        feature="f",
        usage=us.Usage(5000, 5000),
        ok=True,
        at="2020-01-15T00:00:00+00:00",
    )
    cfg = config(us.MODE_CLAUDE, tokens=1000, hard=True)
    assert month_budget(AiRepository(db_conn), cfg).level == "ok"  # type: ignore[arg-type]


# ------------------------------------------------------------------ config + persistence


def test_saved_choices_override_env_defaults_and_survive_restart(settings: Settings) -> None:
    with open_database(settings.db_path) as conn:
        repo = AiRepository(conn)
        assert resolve_config(settings, repo).mode == "ollama"
        save_choices(
            repo,
            mode="hybrid",
            monthly_usd=12.5,
            monthly_tokens=50000,
            hard=True,
            max_output_tokens=1024,
        )
        fill(conn, 7, 3)
    conn2 = open_database(settings.db_path)
    try:
        cfg = resolve_config(settings, AiRepository(conn2))
        assert cfg.mode == "hybrid" and cfg.limits.monthly_usd == 12.5
        assert (
            cfg.limits.monthly_tokens == 50000 and cfg.limits.hard and cfg.max_output_tokens == 1024
        )
        assert (
            AiRepository(conn2).totals("claude", datetime.now(UTC).strftime("%Y-%m")).input_tokens
            == 7
        )
    finally:
        conn2.close()


def test_unknown_mode_is_rejected(db_conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        save_choices(
            AiRepository(db_conn),
            mode="gpt",
            monthly_usd=0,
            monthly_tokens=0,
            hard=False,
            max_output_tokens=2048,
        )


def test_no_secret_reaches_sqlite(settings: Settings) -> None:
    s = Settings(
        data_dir=settings.data_dir,
        models_dir=settings.models_dir,
        min_free_bytes=0,
        anthropic_api_key=KEY,
        claude_model="m",
    )
    with open_database(s.db_path) as conn:
        save_choices(
            AiRepository(conn),
            mode="claude",
            monthly_usd=0,
            monthly_tokens=0,
            hard=False,
            max_output_tokens=2048,
        )
        model = build_text_model(s, conn)
        assert model is not None
        dump = "\n".join(conn.iterdump())
    assert KEY not in dump
    assert KEY not in repr(s) and KEY not in str(s)


def test_env_key_alias_and_readiness(settings: Settings) -> None:
    assert claude_ready(settings) is not None
    ready = Settings(data_dir=settings.data_dir, anthropic_api_key=KEY)
    assert claude_ready(ready) is None


def test_build_text_model_follows_mode(settings: Settings, db_conn: sqlite3.Connection) -> None:
    s = Settings(
        data_dir=settings.data_dir, anthropic_api_key=KEY, llm_model="llama3", claude_model="c1"
    )
    repo = AiRepository(db_conn)
    save_choices(
        repo, mode="ollama", monthly_usd=0, monthly_tokens=0, hard=False, max_output_tokens=2048
    )
    local = build_text_model(s, db_conn)
    assert local is not None and local.name == "ollama:llama3"
    save_choices(
        repo, mode="claude", monthly_usd=0, monthly_tokens=0, hard=False, max_output_tokens=2048
    )
    cl = build_text_model(s, db_conn)
    assert cl is not None and cl.name == "claude:c1"
    bare = Settings(data_dir=settings.data_dir, llm_model="")
    assert build_text_model(bare, db_conn) is None  # claude mode, no key
