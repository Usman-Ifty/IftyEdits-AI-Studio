-- Phase 1B: cancellable jobs, processing stages and generated artifacts.
--
-- SQLite cannot change a CHECK constraint in place, so `jobs` is rebuilt with the
-- new `cancelled` status and a `cancel_requested_at` column. Existing rows and ids
-- are copied unchanged. Nothing references `jobs` before this migration.

CREATE TABLE jobs_v3 (
    id                  INTEGER PRIMARY KEY,
    kind                TEXT    NOT NULL,
    payload             TEXT    NOT NULL,
    status              TEXT    NOT NULL
                        CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    progress            REAL    NOT NULL DEFAULT 0,
    message             TEXT,
    error               TEXT,
    result              TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT    NOT NULL,
    started_at          TEXT,
    finished_at         TEXT,
    heartbeat_at        TEXT,
    worker_id           TEXT,
    cancel_requested_at TEXT
);

INSERT INTO jobs_v3 (
    id, kind, payload, status, progress, message, error, result, attempts,
    created_at, started_at, finished_at, heartbeat_at, worker_id
)
SELECT
    id, kind, payload, status, progress, message, error, result, attempts,
    created_at, started_at, finished_at, heartbeat_at, worker_id
FROM jobs;

DROP INDEX idx_jobs_status;
DROP TABLE jobs;
ALTER TABLE jobs_v3 RENAME TO jobs;
CREATE INDEX idx_jobs_status ON jobs (status, id);

-- Latest state of each processing stage for each video (one row per video and stage).
CREATE TABLE processing_stages (
    id           INTEGER PRIMARY KEY,
    video_id     INTEGER NOT NULL REFERENCES videos (id) ON DELETE CASCADE,
    stage        TEXT    NOT NULL CHECK (stage IN ('audio', 'proxy', 'thumbnail')),
    status       TEXT    NOT NULL
                 CHECK (status IN ('running', 'succeeded', 'failed', 'skipped', 'cancelled')),
    job_id       INTEGER REFERENCES jobs (id) ON DELETE SET NULL,
    params_hash  TEXT    NOT NULL,
    params_json  TEXT    NOT NULL,
    progress     REAL    NOT NULL DEFAULT 0,
    message      TEXT,
    error        TEXT,
    started_at   TEXT    NOT NULL,
    finished_at  TEXT,
    UNIQUE (video_id, stage)
);

-- Files generated from a video. One current artifact per video and kind.
-- `relpath` is relative to the derived-media directory.
CREATE TABLE artifacts (
    id            INTEGER PRIMARY KEY,
    video_id      INTEGER NOT NULL REFERENCES videos (id) ON DELETE CASCADE,
    stage_id      INTEGER NOT NULL REFERENCES processing_stages (id) ON DELETE CASCADE,
    job_id        INTEGER REFERENCES jobs (id) ON DELETE SET NULL,
    kind          TEXT    NOT NULL CHECK (kind IN ('audio_wav', 'proxy_mp4', 'thumbnail_jpg')),
    relpath       TEXT    NOT NULL UNIQUE,
    size_bytes    INTEGER NOT NULL CHECK (size_bytes > 0),
    sha256        TEXT    NOT NULL CHECK (length(sha256) = 64),
    params_hash   TEXT    NOT NULL,
    metadata_json TEXT    NOT NULL DEFAULT '{}',
    created_at    TEXT    NOT NULL,
    UNIQUE (video_id, kind)
);

CREATE INDEX idx_artifacts_stage ON artifacts (stage_id);
