"""Add sessions.scope to a DB created before check-in links made LIMITED
sessions.

A session minted by redeeming a /checkin?t= link is scope 'checkin': short
lived, and refused by require_admin, require_self and every route outside the
check-in allowlist (auth.LINK_SCOPE_*). Every other session is 'full'.

Additive with a default, so safe on a live database with no table rebuild:
every existing session reads as 'full', exactly what it was before. A
check-in session issued before this ran is indistinguishable from a password
one and stays full until it expires; that window closes on its own.

Owned here rather than by schema.sql's CREATE TABLE alone because schema.sql
is a no-op against an existing sessions table. schema.sql also carries the
column so a fresh database is born with it; nothing in schema.sql may index
or otherwise depend on it (it is replayed BEFORE this migration on boot).

Idempotent: re-running against a migrated DB is a no-op.

    uv run python -m migrate.session_scope
"""
from __future__ import annotations

import sys

from app import db as dbm

ADD_SCOPE = "ALTER TABLE sessions ADD COLUMN scope TEXT NOT NULL DEFAULT 'full'"


def needs_migration(con) -> bool:
    return not any(r["name"] == "scope" for r in
                   con.execute("PRAGMA table_info(sessions)").fetchall())


def migrate(con) -> list[str]:
    if not needs_migration(con):
        return []
    con.execute(ADD_SCOPE)
    return ["sessions.scope added (check-in links now sign in with a limited session)."]


def main() -> int:
    with dbm.get_db() as con:
        done = migrate(con)
        for line in done:
            print(line)
        if not done:
            print("Already migrated: sessions carry a scope.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
