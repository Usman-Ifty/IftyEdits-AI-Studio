-- Phase 8: optional performance tracking for uploaded Shorts.
--
-- Additive only. `tracked_videos` links a YouTube video to the Short it came from (when known);
-- `analytics_snapshots` keeps what Google reported at a point in time. No tokens are stored.

CREATE TABLE tracked_videos (
    youtube_video_id TEXT PRIMARY KEY,
    project_id       INTEGER REFERENCES short_projects(id) ON DELETE SET NULL,
    source           TEXT NOT NULL CHECK (source IN ('upload', 'manual')),
    added_at         TEXT NOT NULL
);

CREATE TABLE analytics_snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    youtube_video_id   TEXT NOT NULL
                       REFERENCES tracked_videos(youtube_video_id) ON DELETE CASCADE,
    fetched_at         TEXT NOT NULL,
    title              TEXT NOT NULL DEFAULT '',
    privacy            TEXT NOT NULL DEFAULT '',
    published_at       TEXT NOT NULL DEFAULT '',
    views              INTEGER,
    likes              INTEGER,
    comments           INTEGER,
    -- the next columns come from the optional Analytics report for the window below
    window_start       TEXT,
    window_end         TEXT,
    watch_minutes      REAL,
    avg_view_seconds   REAL,
    avg_view_percent   REAL,
    shares             INTEGER,
    subscribers_gained INTEGER
);

CREATE INDEX analytics_snapshots_video ON analytics_snapshots (youtube_video_id, id);
