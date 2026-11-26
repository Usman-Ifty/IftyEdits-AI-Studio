-- Phase 1A: record each stream's time base (e.g. 1/15360) for frame-accurate timing.

ALTER TABLE media_streams ADD COLUMN time_base TEXT;
