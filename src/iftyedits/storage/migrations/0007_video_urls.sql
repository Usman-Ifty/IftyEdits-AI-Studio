-- Phase 4: videos fetched from a URL.
--
-- One row per pasted video (identified by its canonical source key such as
-- 'youtube:<id>'). It records what the site reported at fetch time and which stored original
-- the download became, so pasting the same link twice reuses the original instead of
-- downloading again. The row survives a failed download (video_id stays NULL) so the
-- failure reason is visible, and is never the source of truth for the media itself:
-- the vault copy and its SHA-256 in `videos` are.

CREATE TABLE video_urls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_key  TEXT    NOT NULL UNIQUE CHECK (length(source_key) > 0),
    url         TEXT    NOT NULL,
    video_id    INTEGER REFERENCES videos (id) ON DELETE SET NULL,
    title       TEXT    NOT NULL DEFAULT '',
    uploader    TEXT    NOT NULL DEFAULT '',
    duration_s  REAL,
    upload_date TEXT,
    license     TEXT,
    status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'downloaded', 'failed')),
    error       TEXT,
    fetched_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL
);

CREATE INDEX idx_video_urls_video ON video_urls (video_id);
