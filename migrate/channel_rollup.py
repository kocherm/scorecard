"""Widen sweep_runs.kind for the week-closed summary sweep.

#scorecard used to get one top-level post per metric; it now gets one roll-up
per sweep, plus a Tuesday summary that is its own sweep kind ('summary') and
therefore its own row on Admin > Setup & status. slack_threads, which holds the
parent message each roll-up threads under, is a NEW table and arrives with
init_db's CREATE TABLE IF NOT EXISTS - only the CHECK needs this, because
SQLite cannot ALTER one.

The table is rebuilt in place. Idempotent: re-running against an
already-migrated DB is a no-op.

    uv run python -m migrate.channel_rollup
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
    outcome    TEXT    NOT NULL CHECK (outcome IN ('sent','nothing','skipped')),
    detail     TEXT    NOT NULL,
    sent_count INTEGER NOT NULL DEFAULT 0
)
"""


def needs_migration(con) -> bool:
    return "'summary'" not in _table_sql(con, "sweep_runs")


def migrate(con) -> None:
    _rebuild(con, "sweep_runs", NEW_SWEEP_RUNS,
             "id, kind, ran_at, outcome, detail, sent_count",
             post_sql=("CREATE INDEX idx_sweep_runs_kind ON sweep_runs(kind, id)",))


def main() -> int:
    with dbm.get_db() as con:
        if not needs_migration(con):
            print("Already migrated: sweep_runs.kind allows 'summary'.")
            return 0
        n = con.execute("SELECT COUNT(*) AS c FROM sweep_runs").fetchone()["c"]
        migrate(con)
        print(f"Migrated sweep_runs ({n} row(s) preserved). "
              "kind now allows 'summary'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
