-- Phase 6.10: AI provider choices and usage accounting.
--
-- Additive only. `ai_settings` holds the owner's non-secret choices (provider mode, limits).
-- API keys are never stored here: they come from environment variables only.
-- `ai_usage` has one row per model request with the token counts the provider reported
-- (NULL when it reported none). No prompt or transcript text is stored, and no cost is stored:
-- cost is computed from the owner's configured prices when it is displayed.

CREATE TABLE ai_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE ai_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    provider      TEXT NOT NULL CHECK (provider IN ('claude', 'ollama')),
    model         TEXT NOT NULL,
    feature       TEXT NOT NULL DEFAULT '',
    input_tokens  INTEGER,
    output_tokens INTEGER,
    ok            INTEGER NOT NULL DEFAULT 1,
    note          TEXT NOT NULL DEFAULT ''
);

CREATE INDEX ai_usage_month ON ai_usage (provider, created_at);
