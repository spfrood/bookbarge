-- Server-side sessions: the cookie holds only a random token; all session
-- state lives here, so logout/revocation is authoritative.

CREATE TABLE sessions (
    token      TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT    NOT NULL
);
CREATE INDEX idx_sessions_user ON sessions(user_id);
