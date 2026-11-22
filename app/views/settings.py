"""Settings: where things are stored, how processing is configured and tool diagnostics.

Configuration is read from environment variables and ``.env`` (``IFTYEDITS_*``); this page
shows what is in effect and does not edit it. The one setting it can change is the welcome
card on the Dashboard.
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing

import streamlit as st

from iftyedits.adapters.ai_router import (
    check_claude,
    check_local,
    claude_ready,
    current_month,
    month_budget,
    ollama_ready,
    probe_ollama,
    resolve_config,
    save_choices,
)
from iftyedits.adapters.llm import endpoint_host, is_local
from iftyedits.config import Settings
from iftyedits.core import usage as ai_us
from iftyedits.core.usage import estimate_cost
from iftyedits.pipeline.jobs import JobQueue
from iftyedits.services.preferences import Preferences
from iftyedits.storage.ai import AiRepository
from iftyedits.storage.db import open_database, schema_version
from iftyedits.ui import theme
from iftyedits.ui.formatting import human_size

settings = Settings.from_env()
prefs = Preferences(settings.data_dir)


MODE_LABELS = {
    ai_us.MODE_OLLAMA: "Local model only (Ollama)",
    ai_us.MODE_CLAUDE: "Claude only",
    ai_us.MODE_HYBRID: "Hybrid: Claude first, local model as fallback",
}


def _tokens(n: int | None) -> str:
    return "unknown" if n is None else f"{n:,}"


def _ai_panel(conn: sqlite3.Connection) -> None:
    """Provider choice, status, usage and limits. Secrets are never shown or stored."""
    repo = AiRepository(conn)
    config = resolve_config(settings, repo)
    claude_problem = claude_ready(settings)
    local_problem = ollama_ready(settings)
    st.write(
        "Titles, descriptions, tags and the content review use an AI model. Choose where it "
        "runs. Without any model you can still write everything by hand."
    )
    modes = list(MODE_LABELS)
    mode = st.radio(
        "AI provider",
        modes,
        index=modes.index(config.mode),
        format_func=lambda m: MODE_LABELS[m],
        key="ai_mode",
    )
    if mode == ai_us.MODE_HYBRID:
        st.info(
            "Hybrid is your explicit permission to use the local model when Claude cannot "
            "answer. Every result says which provider wrote it."
        )
    if mode == ai_us.MODE_OLLAMA:
        st.caption("Nothing is sent to Anthropic in this mode.")
    else:
        st.warning(
            "Claude is an online service. Clip transcripts are sent to Anthropic whenever you "
            "generate metadata or run a content review."
        )

    left, right = st.columns(2)
    with left:
        st.markdown("**Claude**")
        st.markdown(
            theme.pill(
                "Key set, not tested" if claude_problem is None else "Not configured",
                "good" if claude_problem is None else "muted",
            ),
            unsafe_allow_html=True,
        )
        st.caption(f"Model: {settings.claude_model}")
        st.caption("API key: set (hidden)" if claude_problem is None else f"{claude_problem}")
        if claude_problem is None and st.button("Test Claude (tiny request)", key="ai_claude_test"):
            with st.spinner("Asking Claude to say OK…"):
                st.session_state["ai_claude_result"] = check_claude(settings)
        if "ai_claude_result" in st.session_state:
            st.caption(f"Test: {st.session_state['ai_claude_result']}")
        if claude_problem is not None:
            st.caption(
                "Add ANTHROPIC_API_KEY to .env and restart. A Claude chat subscription is not "
                "API access: you need an API key and API billing at console.anthropic.com."
            )
    with right:
        st.markdown("**Local model (Ollama)**")
        st.markdown(
            theme.pill(
                "Model named, not tested" if local_problem is None else "Not configured",
                "good" if local_problem is None else "muted",
            ),
            unsafe_allow_html=True,
        )
        st.caption(f"Model: {settings.llm_model or '(not set)'}")
        st.caption(f"Endpoint: {endpoint_host(settings.llm_base_url)}")
        if not is_local(settings.llm_base_url):
            st.warning("This endpoint is not on your computer.")
        if st.button("Check local connection", key="ai_probe"):
            st.session_state["ai_probe_result"] = probe_ollama(settings)
        if local_problem is None and st.button(
            "Test local model (tiny request)", key="ai_local_test"
        ):
            with st.spinner("Asking the local model to say OK…"):
                st.session_state["ai_probe_result"] = check_local(settings)
        if "ai_probe_result" in st.session_state:
            st.caption(f"Connection: {st.session_state['ai_probe_result']}")
        if local_problem is not None:
            st.caption(
                "Install Ollama yourself, pull a model, then set IFTYEDITS_LLM_MODEL in .env."
            )

    st.markdown("**Spending limits (Claude only)**")
    c1, c2 = st.columns(2)
    monthly_usd = c1.number_input(
        "Monthly budget (USD, 0 = none)",
        min_value=0.0,
        value=float(config.limits.monthly_usd),
        step=1.0,
        key="ai_usd",
    )
    monthly_tokens = c2.number_input(
        "Monthly token limit (0 = none)",
        min_value=0,
        value=int(config.limits.monthly_tokens),
        step=10_000,
        key="ai_tok",
    )
    hard = st.checkbox(
        "Stop Claude requests when the limit is reached",
        value=config.limits.hard,
        key="ai_hard",
        help="Without this, reaching the limit only shows a warning.",
    )
    max_out = st.number_input(
        "Largest reply per request (tokens)",
        min_value=256,
        value=int(config.max_output_tokens),
        step=256,
        key="ai_maxout",
    )
    if monthly_usd > 0 and not config.prices.known:
        st.warning(
            "A USD budget needs prices. Set IFTYEDITS_CLAUDE_PRICE_INPUT_PER_MTOK and "
            "IFTYEDITS_CLAUDE_PRICE_OUTPUT_PER_MTOK (from Anthropic's pricing page). Until "
            "then the USD limit cannot be checked; a token limit works without prices."
        )
    if st.button("Save AI settings", type="primary", key="ai_save"):
        save_choices(
            repo,
            mode=str(mode),
            monthly_usd=float(monthly_usd),
            monthly_tokens=int(monthly_tokens),
            hard=bool(hard),
            max_output_tokens=int(max_out),
        )
        st.success("Saved. New requests use these choices.")
        st.rerun()

    month = current_month()
    totals = repo.totals(ai_us.PROVIDER_CLAUDE, month)
    state = month_budget(repo, config)
    st.markdown(f"**Usage this month ({month})**")
    if state.level in ("reached", "unenforceable"):
        st.error(state.message)
    elif state.level == "warning":
        st.warning(state.message)
    else:
        st.caption(state.message)
    cost = estimate_cost(totals, config.prices)
    local_totals = repo.totals(ai_us.PROVIDER_OLLAMA, month)
    usage_rows: list[dict[str, object]] = [
        {
            "Provider": "Claude (Anthropic)",
            "Requests": totals.calls,
            "No token count": totals.calls_without_usage,
            "Input tokens": _tokens(totals.input_tokens),
            "Output tokens": _tokens(totals.output_tokens),
            "Estimated cost": f"${cost:.4f}" if cost is not None else "unknown (no prices set)",
        },
        {
            "Provider": "Local model (Ollama)",
            "Requests": local_totals.calls,
            "No token count": local_totals.calls_without_usage,
            "Input tokens": _tokens(local_totals.input_tokens),
            "Output tokens": _tokens(local_totals.output_tokens),
            "Estimated cost": "no charge",
        },
    ]
    st.dataframe(usage_rows, hide_index=True, width="stretch")
    recent = repo.recent(8)
    if recent:
        st.caption("Latest requests (no prompt text is stored)")
        st.dataframe(
            [
                {
                    "When (UTC)": r.created_at[:19].replace("T", " "),
                    "Provider": r.provider,
                    "Model": r.model,
                    "Feature": r.feature,
                    "Tokens in/out": f"{_tokens(r.input_tokens)} / {_tokens(r.output_tokens)}",
                    "Result": "ok" if r.ok else f"failed ({r.note})",
                }
                for r in recent
            ],
            hide_index=True,
            width="stretch",
        )
    st.caption(
        "Token counts come from the provider's own reply. Estimated cost uses the prices you "
        "configured and is not an invoice."
    )


st.markdown(
    theme.hero(
        "Settings",
        "What the studio is using right now. Change values with IFTYEDITS_ environment "
        "variables or the .env file, then restart the app.",
    ),
    unsafe_allow_html=True,
)

tab_storage, tab_processing, tab_ai, tab_diag = st.tabs(
    ["Storage", "Processing", "AI writing and review", "Diagnostics"]
)

with tab_storage:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(settings.data_dir)
    c1, c2, c3 = st.columns(3)
    c1.metric("Free disk space", human_size(usage.free))
    c2.metric("Total disk", human_size(usage.total))
    c3.metric("Minimum free to import", human_size(settings.min_free_bytes))
    if usage.free < settings.min_free_bytes:
        st.error("Free space is below the minimum, so new imports will be refused.")
    st.code(
        f"data      {settings.data_dir}\n"
        f"originals {settings.originals_dir}  (read-only vault)\n"
        f"derived   {settings.derived_dir}  (audio, proxies, thumbnails)\n"
        f"exports   {settings.exports_dir}  (finished Shorts, transcript exports)\n"
        f"models    {settings.models_dir}  (speech models)\n"
        f"database  {settings.db_path}",
        language="text",
        wrap_lines=True,
    )
    st.caption("The studio never deletes originals automatically, even when space is low.")
    shown = not prefs.get("welcome_dismissed")
    welcome = st.toggle(
        "Show the welcome card on the Dashboard (when no videos exist)",
        value=shown,
        key="settings-welcome",
    )
    if welcome != shown:
        prefs.set("welcome_dismissed", not welcome)
        st.rerun()

with tab_processing:
    clip_length = f"{settings.clip_min_s:g} / {settings.clip_target_s:g} / {settings.clip_max_s:g}"
    st.markdown("**Transcription**")
    st.dataframe(
        [
            {"Setting": "Model", "Value": settings.transcription_model},
            {"Setting": "Language", "Value": settings.transcription_language},
            {"Setting": "Compute type", "Value": settings.transcription_compute_type},
            {"Setting": "Word timestamps", "Value": str(settings.transcription_word_timestamps)},
            {"Setting": "Voice activity filter", "Value": str(settings.transcription_vad)},
        ],
        hide_index=True,
        width="stretch",
    )
    st.markdown("**Clips and video**")
    st.dataframe(
        [
            {"Setting": "Clip length (min / target / max, s)", "Value": clip_length},
            {"Setting": "Most candidates per video", "Value": str(settings.clip_max_candidates)},
            {"Setting": "Use loudness signal", "Value": str(settings.clip_use_audio)},
            {"Setting": "Use scene changes", "Value": str(settings.clip_use_scenes)},
            {"Setting": "Export encoder", "Value": settings.export_encoder},
            {"Setting": "Largest download (MB)", "Value": str(settings.url_max_filesize_mb)},
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption("Per-run options (clip count, minimum score, caption style) are on Create Shorts.")

with tab_ai, closing(open_database(settings.db_path)) as conn:
    _ai_panel(conn)

with tab_diag:
    with closing(open_database(settings.db_path)) as conn:
        workers = JobQueue(conn).live_workers()
        version = schema_version(conn)
    ffmpeg, ffprobe = shutil.which(settings.ffmpeg_path), shutil.which(settings.ffprobe_path)
    rows = [
        ("FFmpeg", ffmpeg or "not found", bool(ffmpeg)),
        ("FFprobe", ffprobe or "not found", bool(ffprobe)),
        ("Background worker", ", ".join(workers) or "not running", bool(workers)),
        ("Database schema version", str(version), True),
    ]
    for name, value, ok in rows:
        a, b = st.columns([2, 5])
        a.markdown(theme.pill(name, "good" if ok else "bad"), unsafe_allow_html=True)
        b.code(value, language="text", wrap_lines=True)
    st.caption("A full toolchain check: `uv run iftyedits check`.")
