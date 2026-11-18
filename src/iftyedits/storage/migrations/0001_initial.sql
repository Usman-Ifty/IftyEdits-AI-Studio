-- Phase 1A: imported videos, their sources and streams, background jobs.

CREATE TABLE videos (
    id                INTEGER PRIMARY KEY,
    sha256            TEXT    NOT NULL UNIQUE CHECK (length(sha256) = 64),
    vault_relpath     TEXT    NOT NULL UNIQUE,
    original_filename TEXT    NOT NULL,
    size_bytes        INTEGER NOT NULL CHECK (size_bytes >= 0),
    status            TEXT    NOT NULL CHECK (status IN ('ready', 'probe_failed')),
    format_name       TEXT,
    duration_s        REAL,
    bit_rate          INTEGER,
    probe_json        TEXT,
    probe_error       TEXT,
    imported_at       TEXT    NOT NULL
);

CREATE INDEX idx_videos_size ON videos (size_bytes);

CREATE TABLE video_sources (
    id          INTEGER PRIMARY KEY,
    video_id    INTEGER NOT NULL REFERENCES videos (id) ON DELETE CASCADE,
    source_path TEXT    NOT NULL,
    imported_at TEXT    NOT NULL,
    UNIQUE (video_id, source_path)
);

CREATE TABLE media_streams (
    id              INTEGER PRIMARY KEY,
    video_id        INTEGER NOT NULL REFERENCES videos (id) ON DELETE CASCADE,
    stream_index    INTEGER NOT NULL,
    codec_type      TEXT    NOT NULL,
    codec_name      TEXT,
    profile         TEXT,
    width           INTEGER,
    height          INTEGER,
    pix_fmt         TEXT,
    avg_frame_rate  REAL,
    r_frame_rate    REAL,
    rotation        INTEGER NOT NULL DEFAULT 0,
    color_transfer  TEXT,
    color_primaries TEXT,
    sample_rate     INTEGER,
    channels        INTEGER,
    channel_layout  TEXT,
    language        TEXT,
    bit_rate        INTEGER,
    duration_s      REAL,
    is_attached_pic INTEGER NOT NULL DEFAULT 0,
    UNIQUE (video_id, stream_index)
);

CREATE TABLE jobs (
    id           INTEGER PRIMARY KEY,
    kind         TEXT    NOT NULL,
    payload      TEXT    NOT NULL,
    status       TEXT    NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    progress     REAL    NOT NULL DEFAULT 0,
    message      TEXT,
    error        TEXT,
    result       TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    heartbeat_at TEXT,
    worker_id    TEXT
);

CREATE INDEX idx_jobs_status ON jobs (status, id);

CREATE TABLE workers (
    worker_id  TEXT PRIMARY KEY,
    pid        INTEGER NOT NULL,
    started_at TEXT    NOT NULL,
    last_seen  TEXT    NOT NULL
);
