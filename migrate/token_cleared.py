"""Add api_tokens.cleared_at to a DB created before dead tokens could be
cleared off Admin > API tokens.

Additive and nullable, so safe on a live database with no table rebuild:
every existing token stays on the page until an admin clears it.

Idempotent: re-running against a migrated DB is a no-op.

    uv run python -m migrate.token_cleared
"""
from __future__ import annotations

import sys

from app import db as dbm

ADD_CLEARED = "ALTER TABLE api_tokens ADD COLUMN cleared_at TEXT"


def needs_migration(con) -> bool:
    return not any(r["name"] == "cleared_at" for r in
                   con.execute("PRAGMA table_info(api_tokens)").fetchall())


def migrate(con) -> list[str]:
    if not needs_migration(con):
        return []
    con.execute(ADD_CLEARED)
    return ["api_tokens.cleared_at added."]


def main() -> int:
    with dbm.get_db() as con:
        done = migrate(con)
        for line in done:
            print(line)
        if not done:
            print("Already migrated: dead tokens can be cleared.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
