-- Editable audiobook metadata: narrator credit for the M4B tags, and
-- per-chapter display titles for chapter markers (falling back to the
-- upload filename stem when unset).

ALTER TABLE projects ADD COLUMN narrator TEXT;
ALTER TABLE chapters ADD COLUMN title TEXT;
