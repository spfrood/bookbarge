-- Automatic paragraph pacing (PROJECT_BIBLE.md §14 → §7, 2026-07-17).
-- Set on the last chunk of each paragraph at chunking time; assembly
-- inserts a small silence after flagged chunks when stitching.
ALTER TABLE chunks ADD COLUMN paragraph_gap_after INTEGER NOT NULL DEFAULT 0;
