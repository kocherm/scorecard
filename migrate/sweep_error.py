"""Widen sweep_runs.outcome to allow 'error'.

A sweep that crashed used to leave no row at all: its run record was written
through the sweep's own connection and rolled back with the exception, so
Admin > Setup & status showed the previous week's healthy run. The scheduler
wrapper (alerts.guarded) now records the crash on a separate connection, and a
send that failed outright records 'error' too - which the boot catch-up treats
as "not run", so a restart retries it.

The table is rebuilt in place (SQLite cannot ALTER a CHECK). Idempotent:
re-running against an already-migrated DB is a no-op.

    uv run python -m migrate.sweep_error
"""
from __future__ import annotations

import sys

from app import db as dbm

from .slack_two_way import _rebuild, _table_sql

NEW_SWEEP_RUNS = """
CREATE TABLE sweep_runs_new (
    id         INTEGER PRIMARY KEY,
    kind       TEXT    NOT NULL CHECK (kind IN
                 ('nudge1','nudge2','summary','stale','red')),
    ran_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    outcome    TEXT    NOT NULL CHECK (outcome IN
                 ('sent','nothing','skipped','error')),
    detail     TEXT    NOT NULL,
    sent_count INTEGER NOT NULL DEFAULT 0
)
"""


def needs_migration(con) -> bool:
    return "'error'" not in _table_sql(con, "sweep_runs")


def migrate(con) -> None:
    _rebuild(con, "sweep_runs", NEW_SWEEP_RUNS,
             "id, kind, ran_at, outcome, detail, sent_count",
             post_sql=("CREATE INDEX idx_sweep_runs_kind ON sweep_runs(kind, id)",))


def main() -> int:
    with dbm.get_db() as con:
        if not needs_migration(con):
            print("Already migrated: sweep_runs.outcome allows 'error'.")
            return 0
        migrate(con)
        print("Migrated sweep_runs: outcome now allows 'error'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
