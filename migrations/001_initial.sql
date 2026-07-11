-- Bookbarge initial schema (PROJECT_BIBLE.md §5).
-- TOTP columns retained per 2026-07-11 decision (no 2FA flow built for now,
-- but the columns cost nothing and keep the option open).

CREATE TABLE users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT    NOT NULL UNIQUE,
    password_hash  TEXT    NOT NULL,
    totp_secret    TEXT,
    totp_confirmed INTEGER NOT NULL DEFAULT 0,
    is_approved    INTEGER NOT NULL DEFAULT 0,
    approved_at    TEXT,
    approved_by    INTEGER REFERENCES users(id),
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    is_admin       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    title      TEXT    NOT NULL,
    author     TEXT,
    status     TEXT    NOT NULL DEFAULT 'draft'
               CHECK (status IN ('draft','generating','reviewing','assembled')),
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_projects_user ON projects(user_id);

CREATE TABLE chapters (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id           INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    chapter_number       INTEGER NOT NULL,
    filename             TEXT,
    raw_text             TEXT,
    version              INTEGER NOT NULL DEFAULT 1,
    processing_status    TEXT    NOT NULL DEFAULT 'pending'
                         CHECK (processing_status IN
                             ('pending','generating','ready_for_review','recasting','error')),
    approved             INTEGER NOT NULL DEFAULT 0,
    approved_at          TEXT,
    assembled_audio_path TEXT,
    assembled_at         TEXT
);
CREATE INDEX idx_chapters_project ON chapters(project_id);

CREATE TABLE chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chapter_id      INTEGER NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
    chapter_version INTEGER NOT NULL,
    chunk_index     INTEGER NOT NULL,
    text            TEXT    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','processing','done','error')),
    runpod_job_id   TEXT,
    audio_file_path TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_chunks_chapter ON chunks(chapter_id);
CREATE INDEX idx_chunks_status ON chunks(status);

CREATE TABLE voice_references (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename    TEXT    NOT NULL,
    file_path   TEXT    NOT NULL,
    uploaded_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX idx_voice_refs_project ON voice_references(project_id);

CREATE TABLE generation_jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    -- NULL chapter_id = full-project generation; set = single-chapter recast
    chapter_id       INTEGER REFERENCES chapters(id) ON DELETE CASCADE,
    started_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at     TEXT,
    total_chunks     INTEGER NOT NULL DEFAULT 0,
    completed_chunks INTEGER NOT NULL DEFAULT 0,
    status           TEXT    NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running','completed','error'))
);
CREATE INDEX idx_generation_jobs_project ON generation_jobs(project_id);
