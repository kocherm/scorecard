"""Widen two CHECK constraints for two-way messaging:

- entries.source gains the message channels ('slack','telegram','sms',
  'whatsapp') - a typed reply writes the scorecard, attributed to the
  replying user, so the attribution CHECK becomes user-required for every
  non-API source.
- alerts_sent.alert_type gains 'nudge1'/'nudge2' (the Monday/Tuesday
  check-in DMs dedupe per metric+week like every other alert).

SQLite cannot ALTER a CHECK constraint, so each table is rebuilt in place.
Idempotent: re-running against an already-migrated DB is a no-op.

    uv run python -m migrate.slack_two_way
"""
from __future__ import annotations

import sys

from app import db as dbm

NEW_ENTRIES = """
CREATE TABLE entries_new (
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
)
"""

NEW_ALERTS_SENT = """
CREATE TABLE alerts_sent_new (
    id         INTEGER PRIMARY KEY,
    metric_id  INTEGER NOT NULL REFERENCES metrics(id) ON DELETE CASCADE,
    week_start TEXT    NOT NULL,
    alert_type TEXT    NOT NULL CHECK (alert_type IN
                 ('stale','red_week1','red_week2','red_week3','nudge1','nudge2')),
    sent_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (metric_id, week_start, alert_type)
)
"""


def _table_sql(con, name: str) -> str:
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"No {name} table - run migrate.seed first.")
    return row["sql"]


def needs_entries_migration(con) -> bool:
    return "'slack'" not in _table_sql(con, "entries")


def needs_alerts_migration(con) -> bool:
    return "'nudge1'" not in _table_sql(con, "alerts_sent")


def _rebuild(con, table: str, new_ddl: str, columns: str,
             post_sql: tuple[str, ...] = ()) -> None:
    # Same swap pattern as migrate.add_admin_scope: FK enforcement off for the
    # drop+rename, legacy_alter_table so the RENAME does not rewrite other
    # tables' REFERENCES clauses, verified by foreign_key_check before COMMIT.
    con.execute("PRAGMA foreign_keys = OFF")
    con.execute("PRAGMA legacy_alter_table = ON")
    try:
        con.execute("BEGIN")
        con.execute(new_ddl)
        con.execute(f"INSERT INTO {table}_new ({columns}) SELECT {columns} FROM {table}")
        con.execute(f"DROP TABLE {table}")
        con.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
        for sql in post_sql:  # indexes die with the dropped table
            con.execute(sql)
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


def migrate_entries(con) -> None:
    _rebuild(con, "entries", NEW_ENTRIES,
             "id, metric_id, week_start, value_numeric, value_status, source, "
             "entered_by_user_id, entered_by_token_id, created_at, updated_at",
             post_sql=("CREATE INDEX idx_entries_week ON entries(week_start)",))


def migrate_alerts(con) -> None:
    _rebuild(con, "alerts_sent", NEW_ALERTS_SENT,
             "id, metric_id, week_start, alert_type, sent_at")


def main() -> int:
    with dbm.get_db() as con:
        did = 0
        if needs_entries_migration(con):
            n = con.execute("SELECT COUNT(*) AS c FROM entries").fetchone()["c"]
            migrate_entries(con)
            print(f"Migrated entries ({n} row(s) preserved). source now allows 'slack'.")
            did += 1
        if needs_alerts_migration(con):
            n = con.execute("SELECT COUNT(*) AS c FROM alerts_sent").fetchone()["c"]
            migrate_alerts(con)
            print(f"Migrated alerts_sent ({n} row(s) preserved). "
                  "alert_type now allows 'nudge1'/'nudge2'.")
            did += 1
        if not did:
            print("Already migrated: slack source and nudge alert types allowed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
