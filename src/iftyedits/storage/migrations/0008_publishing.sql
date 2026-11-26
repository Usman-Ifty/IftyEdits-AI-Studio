-- Phase 6: upload preparation (metadata, content review, rights, readiness).
--
-- Additive only: no existing table is changed. Everything hangs off a Short project
-- (short_projects) except rights, which belong to the source video so every clip made from
-- it inherits them. The tables hold the owner's own review work, so nothing here is derived
-- data that could be recomputed: metadata edits, flag resolutions, rights evidence and the
-- readiness confirmation. Content reviews and rights changes are append-only history.
--
-- Nothing in this schema uploads or publishes anything.

-- One editable metadata package per Short. `doc_json` is the metadata; `provenance_json`
-- records, per field, whether it came from the model ('ai') or the owner ('manual').
-- `context_hash` fingerprints the clip content the text was written for, so a later edit of
-- the clip is noticed instead of silently keeping stale metadata.
CREATE TABLE publish_metadata (
    project_id      INTEGER PRIMARY KEY REFERENCES short_projects (id) ON DELETE CASCADE,
    doc_json        TEXT NOT NULL,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    model           TEXT NOT NULL DEFAULT '',
    context_hash    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Every content review ever run, newest last. Nothing is overwritten, so earlier concerns are
-- never silently cleared by a later "all clear".
CREATE TABLE safety_reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES short_projects (id) ON DELETE CASCADE,
    status       TEXT NOT NULL CHECK (status IN ('ok', 'manual', 'unavailable', 'failed')),
    risk_level   TEXT NOT NULL CHECK (risk_level IN ('low', 'review', 'high', 'unable')),
    model        TEXT NOT NULL DEFAULT '',
    context_hash TEXT NOT NULL,
    result_json  TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);
CREATE INDEX idx_safety_reviews_project ON safety_reviews (project_id, id);

-- The owner's answer to one flag. Appended; the newest row per (project, flag_key) counts.
CREATE TABLE safety_resolutions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES short_projects (id) ON DELETE CASCADE,
    flag_key   TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('dismissed', 'acknowledged', 'reopened')),
    note       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_safety_resolutions_project ON safety_resolutions (project_id, flag_key, id);

-- What the owner recorded about the right to reuse a source video.
CREATE TABLE source_rights (
    video_id   INTEGER PRIMARY KEY REFERENCES videos (id) ON DELETE CASCADE,
    rights_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Append-only audit trail of rights changes (status, license, evidence, notes).
CREATE TABLE source_rights_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id    INTEGER NOT NULL REFERENCES videos (id) ON DELETE CASCADE,
    old_status  TEXT,
    new_status  TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_rights_history_video ON source_rights_history (video_id, id);

-- The owner's checklist ticks, upload-setting choices and final confirmation for one Short.
-- `confirmed_fingerprint` is a hash of everything the confirmation depended on (export file
-- settings, clip content, metadata text, rights). If any of it changes the stored value no
-- longer matches and the confirmation stops counting.
CREATE TABLE publish_readiness (
    project_id            INTEGER PRIMARY KEY REFERENCES short_projects (id) ON DELETE CASCADE,
    checks_json           TEXT NOT NULL DEFAULT '[]',
    settings_json         TEXT NOT NULL DEFAULT '{}',
    rights_ack            INTEGER NOT NULL DEFAULT 0 CHECK (rights_ack IN (0, 1)),
    confirmed_at          TEXT,
    confirmed_render_id   INTEGER,
    confirmed_fingerprint TEXT,
    updated_at            TEXT NOT NULL
);

CREATE TABLE publish_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES short_projects (id) ON DELETE CASCADE,
    event      TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_publish_events_project ON publish_events (project_id, id);
