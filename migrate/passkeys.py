"""Add password-reset links and passkeys to a DB created before either existed.

Three additive changes, all safe on a live database (no table rebuild, so no
FK dance like migrate.slack_two_way needs):

- magic_links.purpose - existing rows are check-in links, which is exactly what
  the DEFAULT backfills them to. Without it every outstanding check-in link
  would also be a password-reset link.
- users.webauthn_handle - NULL until the user registers a passkey.
- webauthn_credentials / webauthn_challenges - new tables.

Idempotent: re-running against a migrated DB is a no-op.

    uv run python -m migrate.passkeys
"""
from __future__ import annotations

import sys

from app import db as dbm

ADD_PURPOSE = ("ALTER TABLE magic_links ADD COLUMN purpose TEXT NOT NULL "
               "DEFAULT 'checkin' CHECK (purpose IN ('checkin','reset'))")
ADD_HANDLE = "ALTER TABLE users ADD COLUMN webauthn_handle TEXT"
HANDLE_INDEX = ("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_webauthn_handle "
                "ON users(webauthn_handle) WHERE webauthn_handle IS NOT NULL")

CREDENTIALS = """
CREATE TABLE IF NOT EXISTS webauthn_credentials (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    credential_id TEXT    NOT NULL UNIQUE,
    public_key    BLOB    NOT NULL,
    sign_count    INTEGER NOT NULL DEFAULT 0,
    transports    TEXT,
    name          TEXT    NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    last_used_at  TEXT
)
"""
CREDENTIALS_INDEX = ("CREATE INDEX IF NOT EXISTS idx_webauthn_user "
                     "ON webauthn_credentials(user_id)")
CHALLENGES = """
CREATE TABLE IF NOT EXISTS webauthn_challenges (
    challenge  TEXT PRIMARY KEY,
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    purpose    TEXT NOT NULL CHECK (purpose IN ('register','login')),
    expires_at TEXT NOT NULL
)
"""


def _has_column(con, table: str, column: str) -> bool:
    return any(r["name"] == column
               for r in con.execute(f"PRAGMA table_info({table})").fetchall())


def needs_purpose(con) -> bool:
    return not _has_column(con, "magic_links", "purpose")


def needs_handle(con) -> bool:
    return not _has_column(con, "users", "webauthn_handle")


def migrate(con) -> list[str]:
    done = []
    if needs_purpose(con):
        n = con.execute("SELECT COUNT(*) AS c FROM magic_links").fetchone()["c"]
        con.execute(ADD_PURPOSE)
        done.append(f"magic_links.purpose added ({n} existing link(s) kept as "
                    "'checkin').")
    if needs_handle(con):
        con.execute(ADD_HANDLE)
        done.append("users.webauthn_handle added.")
    con.execute(HANDLE_INDEX)
    before = con.execute(
        "SELECT COUNT(*) AS c FROM sqlite_master WHERE type='table' "
        "AND name IN ('webauthn_credentials','webauthn_challenges')").fetchone()["c"]
    con.execute(CREDENTIALS)
    con.execute(CREDENTIALS_INDEX)
    con.execute(CHALLENGES)
    if before < 2:
        done.append("webauthn_credentials and webauthn_challenges created.")
    return done


def main() -> int:
    with dbm.get_db() as con:
        done = migrate(con)
        for line in done:
            print(line)
        if not done:
            print("Already migrated: reset links and passkeys are available.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
