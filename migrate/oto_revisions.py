"""Let a 1-3-1 be revised instead of frozen at its first draft.

A 1-3-1 is written the week a metric goes red, usually before anyone has had
the conversation it exists to start. The route used INSERT OR IGNORE against a
UNIQUE(metric_id, week_start), so a second filing was silently discarded - the
one place in this app where a write disappeared without saying so.

Revisions supersede rather than overwrite: the old row keeps its text and gains
a superseded_at stamp, exactly as entry_audit keeps the number you corrected.
That means the UNIQUE has to go, replaced by a partial index that still allows
only ONE live draft per metric-week. SQLite cannot drop a constraint, so the
table is rebuilt.

Idempotent: re-running against an already-migrated DB is a no-op.

    uv run python -m migrate.oto_revisions
"""
from __future__ import annotations

import sys

from app import db as dbm

from .slack_two_way import _rebuild, _table_sql

NEW_OTO = """
CREATE TABLE one_three_ones_new (
    id             INTEGER PRIMARY KEY,
    metric_id      INTEGER NOT NULL REFERENCES metrics(id) ON DELETE CASCADE,
    week_start     TEXT    NOT NULL CHECK (strftime('%w', week_start) = '1'),
    problem        TEXT    NOT NULL,
    options_json   TEXT    NOT NULL,
    recommendation TEXT    NOT NULL,
    created_by     INTEGER NOT NULL REFERENCES users(id),
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    resolved_at    TEXT,
    superseded_at  TEXT
)
"""

# One LIVE draft per metric-week; superseded rows are unlimited and keep their
# text. A plain UNIQUE could not express that.
LIVE_INDEX = ("CREATE UNIQUE INDEX IF NOT EXISTS idx_oto_live ON one_three_ones "
              "(metric_id, week_start) WHERE superseded_at IS NULL")


def _has_index(con) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_oto_live'"
    ).fetchone() is not None


def needs_migration(con) -> bool:
    """True while either half is missing.

    Two paths reach this, and the index is why it cannot live in schema.sql:
    an OLD database has neither the column nor the index, while a FRESH one is
    created by schema.sql with the column but no index - schema.sql is replayed
    on every startup, before the migrations run, so an index naming
    superseded_at would fail against a not-yet-migrated table and take the app
    down on boot rather than migrating it."""
    if "superseded_at" not in _table_sql(con, "one_three_ones"):
        return True
    return not _has_index(con)


def migrate(con) -> None:
    if "superseded_at" not in _table_sql(con, "one_three_ones"):
        _rebuild(con, "one_three_ones", NEW_OTO,
                 "id, metric_id, week_start, problem, options_json, "
                 "recommendation, created_by, created_at, resolved_at",
                 post_sql=(LIVE_INDEX,))
        return
    con.execute(LIVE_INDEX)          # fresh DB: only the index is outstanding


def main() -> int:
    with dbm.get_db() as con:
        if not needs_migration(con):
            print("Already migrated - nothing to do.")
            return 0
        migrate(con)
        print("one_three_ones now supports revisions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
