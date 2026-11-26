-- Phase 3: Short editing and rendering.
--
-- A short project is the editable record for one suggested clip: the user's settings live
-- here as one JSON document, so the Phase 2 suggestion (clip_candidates) is never changed.
-- The original AI boundaries are copied in so "back to the suggestion" works even after the
-- video is re-analysed. Projects are keyed by (video, candidate_key), the same stable key the
-- user decisions use, so they survive re-analysis.
--
-- A short render is one preview or export attempt. It stores a snapshot of the settings it
-- used and a hash of them, so an identical request is detected and reused instead of rendered
-- twice. A render row is only 'succeeded' after the output file was verified.

CREATE TABLE short_projects (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id         INTEGER NOT NULL REFERENCES videos (id) ON DELETE CASCADE,
    candidate_key    TEXT    NOT NULL CHECK (length(candidate_key) > 0),
    title            TEXT    NOT NULL DEFAULT '',
    original_start_s REAL    NOT NULL CHECK (original_start_s >= 0),
    original_end_s   REAL    NOT NULL CHECK (original_end_s > original_start_s),
    config_json      TEXT    NOT NULL,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    UNIQUE (video_id, candidate_key)
);

CREATE TABLE short_renders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES short_projects (id) ON DELETE CASCADE,
    kind         TEXT    NOT NULL CHECK (kind IN ('preview', 'export')),
    config_hash  TEXT    NOT NULL,
    config_json  TEXT    NOT NULL,
    status       TEXT    NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    job_id       INTEGER REFERENCES jobs (id) ON DELETE SET NULL,
    output_path  TEXT,
    width        INTEGER,
    height       INTEGER,
    duration_s   REAL,
    size_bytes   INTEGER,
    has_audio    INTEGER CHECK (has_audio IS NULL OR has_audio IN (0, 1)),
    encoder      TEXT,
    error        TEXT,
    created_at   TEXT    NOT NULL,
    finished_at  TEXT
);

CREATE INDEX idx_short_renders_project ON short_renders (project_id, kind, id);
CREATE UNIQUE INDEX idx_short_renders_active
    ON short_renders (project_id, kind, config_hash) WHERE status IN ('queued', 'running');
