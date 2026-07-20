"""Widen api_tokens.scope to allow 'admin'.

SQLite cannot ALTER a CHECK constraint, so the table is rebuilt in place.
Idempotent: re-running against an already-migrated DB is a no-op.

    uv run python -m migrate.add_admin_scope
"""
from __future__ import annotations

import sys

from app import db as dbm

NEW_TABLE = """
CREATE TABLE api_tokens_new (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL,
    token_hash   TEXT    NOT NULL UNIQUE,
    scope        TEXT    NOT NULL CHECK (scope IN ('read','write','read_write','admin')),
    created_by   INTEGER REFERENCES users(id),
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT,
    revoked_at   TEXT
)
"""


def needs_migration(con) -> bool:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='api_tokens'"
    ).fetchone()
    if row is None:
        raise SystemExit("No api_tokens table - run migrate.seed first.")
    return "'admin'" not in row["sql"]


def migrate(con) -> None:
    # entries.entered_by_token_id references api_tokens; the rebuild drops and
    # recreates that table under the same name with the same ids, so references
    # stay valid as long as FK enforcement is off for the swap. legacy_alter_table
    # keeps the RENAME from rewriting other tables' REFERENCES clauses.
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("PRAGMA legacy_alter_table = ON")
    try:
        con.execute("BEGIN")
        con.execute(NEW_TABLE)
        con.execute(
            """INSERT INTO api_tokens_new
                 (id, name, token_hash, scope, created_by, created_at,
                  last_used_at, revoked_at)
               SELECT id, name, token_hash, scope, created_by, created_at,
                      last_used_at, revoked_at
               FROM api_tokens"""
        )
        con.execute("DROP TABLE api_tokens")
        con.execute("ALTER TABLE api_tokens_new RENAME TO api_tokens")
        bad = con.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            con.execute("ROLLBACK")
            raise SystemExit(f"Foreign key check failed, rolled back: {bad}")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.execute("PRAGMA legacy_alter_table = OFF")
        con.execute("PRAGMA foreign_keys = ON")


def main() -> int:
    with dbm.get_db() as con:
        if not needs_migration(con):
            print("Already migrated: api_tokens.scope allows 'admin'.")
            return 0
        n = con.execute("SELECT COUNT(*) AS c FROM api_tokens").fetchone()["c"]
        migrate(con)
        print(f"Migrated api_tokens ({n} token(s) preserved). scope now allows 'admin'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
