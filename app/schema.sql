PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    display_name  TEXT    NOT NULL,
    role          TEXT    NOT NULL CHECK (role IN ('admin','editor','viewer')),
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    slack_member_id TEXT,
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0,1)),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT NOT NULL,
    last_seen_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS api_tokens (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL,
    token_hash   TEXT    NOT NULL UNIQUE,
    scope        TEXT    NOT NULL CHECK (scope IN ('read','write','read_write','admin')),
    created_by   INTEGER REFERENCES users(id),
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT,
    revoked_at   TEXT
);

CREATE TABLE IF NOT EXISTS sections (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    icon       TEXT,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0,1))
);

CREATE TABLE IF NOT EXISTS metrics (
    id          INTEGER PRIMARY KEY,
    section_id  INTEGER NOT NULL REFERENCES sections(id),
    name        TEXT    NOT NULL,
    metric_type TEXT    NOT NULL CHECK (metric_type IN ('numeric','binary','status')),
    rollup      TEXT    CHECK (rollup IN ('sum','average')),
    direction   TEXT    NOT NULL DEFAULT 'up' CHECK (direction IN ('up','down')),
    unit        TEXT,
    dri_user_id INTEGER REFERENCES users(id),
    start_week  TEXT    NOT NULL CHECK (strftime('%w', start_week) = '1'),
    is_key      INTEGER NOT NULL DEFAULT 0 CHECK (is_key IN (0,1)),
    sort_order  INTEGER NOT NULL DEFAULT 0,
    archived_at TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK ((metric_type = 'numeric') = (rollup IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_metrics_section ON metrics(section_id, sort_order);

CREATE TABLE IF NOT EXISTS targets (
    id             INTEGER PRIMARY KEY,
    metric_id      INTEGER NOT NULL REFERENCES metrics(id) ON DELETE CASCADE,
    year           INTEGER NOT NULL,
    quarter        INTEGER NOT NULL CHECK (quarter BETWEEN 1 AND 4),
    baseline_value REAL    NOT NULL,
    stretch_value  REAL    NOT NULL,
    UNIQUE (metric_id, year, quarter)
);

CREATE TABLE IF NOT EXISTS entries (
    id            INTEGER PRIMARY KEY,
    metric_id     INTEGER NOT NULL REFERENCES metrics(id) ON DELETE CASCADE,
    week_start    TEXT    NOT NULL CHECK (strftime('%w', week_start) = '1'),
    value_numeric REAL,
    value_status  TEXT    CHECK (value_status IN ('R','Y','G')),
    source        TEXT    NOT NULL CHECK (source IN ('manual','api')),
    entered_by_user_id  INTEGER REFERENCES users(id),
    entered_by_token_id INTEGER REFERENCES api_tokens(id),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (metric_id, week_start),
    CHECK ((value_numeric IS NULL) <> (value_status IS NULL)),
    CHECK ((source = 'manual' AND entered_by_user_id IS NOT NULL)
        OR (source = 'api'    AND entered_by_token_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_entries_week ON entries(week_start);

CREATE TABLE IF NOT EXISTS entry_audit (
    id            INTEGER PRIMARY KEY,
    metric_id     INTEGER NOT NULL,
    week_start    TEXT    NOT NULL,
    old_numeric   REAL,
    old_status    TEXT,
    new_numeric   REAL,
    new_status    TEXT,
    source        TEXT    NOT NULL,
    actor_user_id INTEGER,
    actor_token_id INTEGER,
    changed_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS one_three_ones (
    id             INTEGER PRIMARY KEY,
    metric_id      INTEGER NOT NULL REFERENCES metrics(id) ON DELETE CASCADE,
    week_start     TEXT    NOT NULL CHECK (strftime('%w', week_start) = '1'),
    problem        TEXT    NOT NULL,
    options_json   TEXT    NOT NULL,
    recommendation TEXT    NOT NULL,
    created_by     INTEGER NOT NULL REFERENCES users(id),
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    resolved_at    TEXT,
    UNIQUE (metric_id, week_start)
);

CREATE TABLE IF NOT EXISTS alerts_sent (
    id         INTEGER PRIMARY KEY,
    metric_id  INTEGER NOT NULL REFERENCES metrics(id) ON DELETE CASCADE,
    week_start TEXT    NOT NULL,
    alert_type TEXT    NOT NULL CHECK (alert_type IN
                 ('stale','red_week1','red_week2','red_week3')),
    sent_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (metric_id, week_start, alert_type)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
