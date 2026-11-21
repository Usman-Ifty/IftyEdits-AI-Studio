-- Phase 2: clip intelligence (content signals, ranked clip suggestions, user decisions).
--
-- An analysis is written once, in a single transaction, when a run succeeds, so a failed or
-- cancelled run leaves nothing behind. Run state lives on the job. One analysis per
-- (video, parameter hash): re-running identical parameters replaces it. Exactly one
-- analysis per video is current. Suggestions are recommendations: scoring is algorithmic.
--
-- User decisions (favorite / rejected, optional adjusted boundaries) live in their own
-- table keyed by the suggestion's stable key (video SHA-256 + boundaries), with a snapshot
-- of the span and category, so they survive re-analysis and can nudge later rankings.

CREATE TABLE clip_analyses (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id         INTEGER NOT NULL REFERENCES videos (id) ON DELETE CASCADE,
    transcript_id    INTEGER NOT NULL REFERENCES transcripts (id) ON DELETE CASCADE,
    job_id           INTEGER REFERENCES jobs (id) ON DELETE SET NULL,
    analysis_version INTEGER NOT NULL CHECK (analysis_version >= 1),
    scoring_version  INTEGER NOT NULL CHECK (scoring_version >= 1),
    params_hash      TEXT    NOT NULL,
    params_json      TEXT    NOT NULL,
    duration_s       REAL    NOT NULL CHECK (duration_s >= 0),
    sources_json     TEXT    NOT NULL,
    stats_json       TEXT    NOT NULL,
    signal_count     INTEGER NOT NULL DEFAULT 0 CHECK (signal_count >= 0),
    candidate_count  INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
    processing_s     REAL CHECK (processing_s IS NULL OR processing_s >= 0),
    is_current       INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1)),
    created_at       TEXT    NOT NULL,
    UNIQUE (video_id, params_hash)
);

CREATE UNIQUE INDEX idx_clip_analyses_current ON clip_analyses (video_id) WHERE is_current = 1;

CREATE TABLE clip_signals (
    id            INTEGER PRIMARY KEY,
    analysis_id   INTEGER NOT NULL REFERENCES clip_analyses (id) ON DELETE CASCADE,
    position      INTEGER NOT NULL CHECK (position >= 0),
    kind          TEXT    NOT NULL,
    source        TEXT    NOT NULL CHECK (source IN ('transcript', 'audio', 'video')),
    start_s       REAL    NOT NULL CHECK (start_s >= 0),
    end_s         REAL    NOT NULL CHECK (end_s >= start_s),
    strength      REAL    NOT NULL CHECK (strength >= 0 AND strength <= 1),
    confidence    REAL    NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    label         TEXT    NOT NULL,
    evidence_json TEXT    NOT NULL,
    UNIQUE (analysis_id, position)
);

CREATE TABLE clip_candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id         INTEGER NOT NULL REFERENCES clip_analyses (id) ON DELETE CASCADE,
    video_id            INTEGER NOT NULL REFERENCES videos (id) ON DELETE CASCADE,
    candidate_key       TEXT    NOT NULL CHECK (length(candidate_key) > 0),
    rank                INTEGER NOT NULL CHECK (rank >= 1),
    start_s             REAL    NOT NULL CHECK (start_s >= 0),
    end_s               REAL    NOT NULL CHECK (end_s > start_s),
    category            TEXT    NOT NULL,
    methods_json        TEXT    NOT NULL,
    title               TEXT    NOT NULL,
    excerpt             TEXT    NOT NULL,
    hook_json           TEXT    NOT NULL,
    context_json        TEXT,
    payoff_json         TEXT    NOT NULL,
    score               REAL    NOT NULL CHECK (score >= 0 AND score <= 100),
    confidence          REAL    NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    components_json     TEXT    NOT NULL,
    strengths_json      TEXT    NOT NULL,
    weaknesses_json     TEXT    NOT NULL,
    explanation         TEXT    NOT NULL,
    signal_positions_json TEXT  NOT NULL,
    evidence_json       TEXT    NOT NULL,
    UNIQUE (analysis_id, candidate_key),
    UNIQUE (analysis_id, rank)
);

CREATE INDEX idx_clip_candidates_video ON clip_candidates (video_id, analysis_id);

CREATE TABLE clip_decisions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id         INTEGER NOT NULL REFERENCES videos (id) ON DELETE CASCADE,
    candidate_key    TEXT    NOT NULL,
    decision         TEXT CHECK (decision IS NULL OR decision IN ('favorite', 'rejected')),
    start_s          REAL    NOT NULL CHECK (start_s >= 0),
    end_s            REAL    NOT NULL CHECK (end_s > start_s),
    category         TEXT    NOT NULL,
    adjusted_start_s REAL CHECK (adjusted_start_s IS NULL OR adjusted_start_s >= 0),
    adjusted_end_s   REAL,
    decided_at       TEXT    NOT NULL,
    UNIQUE (video_id, candidate_key),
    CHECK ((adjusted_start_s IS NULL) = (adjusted_end_s IS NULL)),
    CHECK (adjusted_start_s IS NULL OR adjusted_end_s > adjusted_start_s)
);
