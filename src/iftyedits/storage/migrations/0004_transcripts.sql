-- Phase 1C: transcripts with segment and word timings, and full-text search.
--
-- A transcript is written once, in a single transaction, when transcription succeeds.
-- Run state (queued, running, failed, cancelled, progress, errors) lives on the job.
-- One transcript per (video, parameter hash): re-running with identical parameters
-- replaces it; other parameters (another model or language) add a transcript.
-- Exactly one transcript per video can be current; later phases read the current one.

-- AUTOINCREMENT: ids appear in job results and exports, so they are never reused.
CREATE TABLE transcripts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id             INTEGER NOT NULL REFERENCES videos (id) ON DELETE CASCADE,
    job_id               INTEGER REFERENCES jobs (id) ON DELETE SET NULL,
    audio_sha256         TEXT    NOT NULL CHECK (length(audio_sha256) = 64),
    engine               TEXT    NOT NULL,
    engine_version       TEXT    NOT NULL,
    model                TEXT    NOT NULL,
    model_revision       TEXT,
    device               TEXT    NOT NULL,
    compute_type         TEXT    NOT NULL,
    language_requested   TEXT,
    language             TEXT,
    language_probability REAL CHECK (language_probability IS NULL
                                     OR (language_probability >= 0 AND language_probability <= 1)),
    duration_s           REAL CHECK (duration_s IS NULL OR duration_s >= 0),
    params_hash          TEXT    NOT NULL,
    params_json          TEXT    NOT NULL,
    segment_count        INTEGER NOT NULL DEFAULT 0 CHECK (segment_count >= 0),
    word_count           INTEGER NOT NULL DEFAULT 0 CHECK (word_count >= 0),
    dropped_segments     INTEGER NOT NULL DEFAULT 0 CHECK (dropped_segments >= 0),
    processing_s         REAL CHECK (processing_s IS NULL OR processing_s >= 0),
    is_current           INTEGER NOT NULL DEFAULT 0 CHECK (is_current IN (0, 1)),
    created_at           TEXT    NOT NULL,
    UNIQUE (video_id, params_hash)
);

CREATE UNIQUE INDEX idx_transcripts_current ON transcripts (video_id) WHERE is_current = 1;

CREATE TABLE transcript_segments (
    id             INTEGER PRIMARY KEY,
    transcript_id  INTEGER NOT NULL REFERENCES transcripts (id) ON DELETE CASCADE,
    position       INTEGER NOT NULL CHECK (position >= 0),
    start_s        REAL    NOT NULL CHECK (start_s >= 0),
    end_s          REAL    NOT NULL CHECK (end_s >= start_s),
    text           TEXT    NOT NULL CHECK (length(text) > 0),
    avg_logprob    REAL,
    no_speech_prob REAL,
    UNIQUE (transcript_id, position)
);

CREATE TABLE transcript_words (
    id          INTEGER PRIMARY KEY,
    segment_id  INTEGER NOT NULL REFERENCES transcript_segments (id) ON DELETE CASCADE,
    position    INTEGER NOT NULL CHECK (position >= 0),
    start_s     REAL    NOT NULL CHECK (start_s >= 0),
    end_s       REAL    NOT NULL CHECK (end_s >= start_s),
    text        TEXT    NOT NULL CHECK (length(text) > 0),
    probability REAL,
    UNIQUE (segment_id, position)
);

-- Full-text index over segment text, kept in sync by triggers.
-- unicode61 with diacritics removed: "cafe" matches "café". Languages written without
-- spaces (Chinese, Japanese, Thai) are indexed as whole runs; see docs/DECISIONS.md D17.
CREATE VIRTUAL TABLE transcript_search USING fts5 (
    text,
    content = 'transcript_segments',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER transcript_segments_ai AFTER INSERT ON transcript_segments BEGIN
    INSERT INTO transcript_search (rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER transcript_segments_ad AFTER DELETE ON transcript_segments BEGIN
    INSERT INTO transcript_search (transcript_search, rowid, text)
    VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER transcript_segments_au AFTER UPDATE ON transcript_segments BEGIN
    INSERT INTO transcript_search (transcript_search, rowid, text)
    VALUES ('delete', old.id, old.text);
    INSERT INTO transcript_search (rowid, text) VALUES (new.id, new.text);
END;
