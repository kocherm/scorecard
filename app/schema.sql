PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    email         TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT    NOT NULL,
    display_name  TEXT    NOT NULL,
    role          TEXT    NOT NULL CHECK (role IN ('admin','editor','viewer')),
    is_active     INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
    slack_member_id TEXT,
    -- Where check-in nudges reach this user: 'slack' (default, uses
    -- slack_member_id) or teams/gchat/sms/whatsapp/telegram (notify_address
    -- holds the phone number / Telegram chat ID; unused for teams/gchat).
    notify_channel  TEXT,
    notify_address  TEXT,
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0,1)),
    -- Opaque per-user handle sent to authenticators as the WebAuthn user id.
    -- Not the row id: it is stored on the passkey and readable by any site the
    -- authenticator is later asked about, so it must carry no meaning. Minted
    -- lazily on first passkey registration; NULL until then.
    webauthn_handle TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);
-- The unique index on webauthn_handle is created by migrate/passkeys.py, NOT
-- here. This file runs against existing databases too, where CREATE TABLE IF
-- NOT EXISTS is a no-op and so users has no webauthn_handle column yet - an
-- index over it here fails the whole script, and init_db runs before any
-- migration, so the app would not start at all. The migration owns the column
-- and the index together, and runs unconditionally from lifespan.

CREATE TABLE IF NOT EXISTS sessions (
    token_hash   TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT NOT NULL,
    last_seen_at TEXT,
    -- set while an admin is "viewing as" another user; audit stays on user_id
    impersonate_user_id INTEGER REFERENCES users(id)
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
    revoked_at   TEXT,
    -- Rotation: the replacement token points back at the one it supersedes,
    -- and the superseded token gets an expires_at grace deadline so both
    -- secrets work until the integration has been switched over.
    expires_at      TEXT,
    rotated_from_id INTEGER REFERENCES api_tokens(id)
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
    source        TEXT    NOT NULL CHECK (source IN
                    ('manual','api','slack','telegram','sms','whatsapp')),
    entered_by_user_id  INTEGER REFERENCES users(id),
    entered_by_token_id INTEGER REFERENCES api_tokens(id),
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (metric_id, week_start),
    CHECK ((value_numeric IS NULL) <> (value_status IS NULL)),
    CHECK ((source = 'api' AND entered_by_token_id IS NOT NULL)
        OR (source <> 'api' AND entered_by_user_id IS NOT NULL))
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
                 ('stale','red_week1','red_week2','red_week3','nudge1','nudge2')),
    sent_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (metric_id, week_start, alert_type)
);

-- Pre-authenticated links delivered over a private message channel. Multi-use
-- until expiry (Slack's link crawler would burn single-use tokens); hash stored.
-- purpose is load-bearing, not descriptive: a 'checkin' link is DM'd weekly and
-- lives for 7 days, a 'reset' link authorises setting a new password and lives
-- for two hours, and neither may ever be redeemed at the other's endpoint.
CREATE TABLE IF NOT EXISTS magic_links (
    id           INTEGER PRIMARY KEY,
    token_hash   TEXT    NOT NULL UNIQUE,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose      TEXT    NOT NULL DEFAULT 'checkin'
                 CHECK (purpose IN ('checkin','reset')),
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT    NOT NULL,
    last_used_at TEXT
);

-- Passkeys (WebAuthn). Strictly additive: every user keeps a password, because
-- a passkey lives on one device and the recovery path for a lost one is the
-- password path. Nothing here can be used to sign in on its own - a credential
-- only proves possession of the private key for a challenge this server issued.
CREATE TABLE IF NOT EXISTS webauthn_credentials (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    credential_id TEXT    NOT NULL UNIQUE,   -- base64url, as the browser sends it
    public_key    BLOB    NOT NULL,
    -- Authenticators that keep a counter bump it every assertion; a value that
    -- goes backwards means a cloned credential. Platform passkeys (iCloud,
    -- Google Password Manager) sync and always report 0, so 0 means "no signal".
    sign_count    INTEGER NOT NULL DEFAULT 0,
    transports    TEXT,                      -- JSON array, for allowCredentials hints
    name          TEXT    NOT NULL,          -- user-supplied: "MacBook Touch ID"
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    last_used_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_webauthn_user ON webauthn_credentials(user_id);

-- Outstanding WebAuthn challenges. Server-side and single-use by deletion: the
-- whole point of the ceremony is that the server chose the challenge, so it
-- cannot be carried in anything the client could pick (a cookie it can set, a
-- form field it can edit). user_id is NULL for sign-in, which is usernameless -
-- the credential itself names the account.
CREATE TABLE IF NOT EXISTS webauthn_challenges (
    challenge  TEXT PRIMARY KEY,             -- base64url
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    purpose    TEXT NOT NULL CHECK (purpose IN ('register','login')),
    expires_at TEXT NOT NULL
);

-- The numbered metric list each user was shown in their last nudge message
-- (named for Slack, shared by every two-way channel: Telegram, SMS/WhatsApp).
-- Reply indices resolve against THIS list, never a recomputed one, so a
-- metric filled on the web between nudge and reply can't shift the numbering.
CREATE TABLE IF NOT EXISTS slack_prompts (
    user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    week_start TEXT NOT NULL,
    metric_ids TEXT NOT NULL,  -- JSON array, 1-based order as numbered in the DM
    sent_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per sweep run, including the runs that send nothing. A sweep that
-- stopped because a switch was off, or because the public base URL was empty,
-- is indistinguishable from one that never fired unless it says so somewhere,
-- and the scheduler has nobody to tell. Admin > Setup & status reads the
-- latest row per kind.
CREATE TABLE IF NOT EXISTS sweep_runs (
    id         INTEGER PRIMARY KEY,
    kind       TEXT    NOT NULL CHECK (kind IN ('nudge1','nudge2','stale','red')),
    ran_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    outcome    TEXT    NOT NULL CHECK (outcome IN ('sent','nothing','skipped')),
    detail     TEXT    NOT NULL,
    sent_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sweep_runs_kind ON sweep_runs(kind, id);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
