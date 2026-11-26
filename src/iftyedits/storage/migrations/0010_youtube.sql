-- Phase 7: optional YouTube upload tracking.
--
-- Additive only. One row per approved or attempted upload. It stores the exact details that were
-- approved (as JSON), their fingerprint and the outcome. It never stores OAuth tokens or the
-- client secret: those live in a separate 0600 file outside the repository.

CREATE TABLE youtube_uploads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES short_projects(id),
    render_id       INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN
                        ('draft', 'approved', 'uploading', 'uploaded', 'failed', 'cancelled')),
    details         TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    approved_at     TEXT,
    started_at      TEXT,
    finished_at     TEXT,
    bytes_sent      INTEGER NOT NULL DEFAULT 0,
    youtube_video_id TEXT,
    error_kind      TEXT,
    error           TEXT,
    job_id          INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX youtube_uploads_project ON youtube_uploads (project_id, id);
CREATE INDEX youtube_uploads_day ON youtube_uploads (started_at);
