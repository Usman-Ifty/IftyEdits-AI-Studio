-- Phase 8: where views came from, per fetched snapshot. Additive; '' means not fetched.
ALTER TABLE analytics_snapshots ADD COLUMN traffic_json TEXT NOT NULL DEFAULT '';
