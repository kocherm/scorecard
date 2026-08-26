"""slack_two_way migrations: old-CHECK tables rebuild in place, data survives,
the widened CHECKs accept the new values and still reject bad attribution."""
import sqlite3

import pytest

from app import db as dbm
from migrate import slack_two_way as m2w

OLD_ENTRIES = """
CREATE TABLE entries (
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
)
"""

OLD_ALERTS = """
CREATE TABLE alerts_sent (
    id         INTEGER PRIMARY KEY,
    metric_id  INTEGER NOT NULL REFERENCES metrics(id) ON DELETE CASCADE,
    week_start TEXT    NOT NULL,
    alert_type TEXT    NOT NULL CHECK (alert_type IN
                 ('stale','red_week1','red_week2','red_week3')),
    sent_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (metric_id, week_start, alert_type)
)
"""


@pytest.fixture
def old_db(tmp_path, monkeypatch):
    """A DB shaped like production before this migration: current schema for
    everything except entries/alerts_sent, which get the old CHECKs."""
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("DROP TABLE entries")
        con.execute(OLD_ENTRIES)
        con.execute("CREATE INDEX idx_entries_week ON entries(week_start)")
        con.execute("DROP TABLE alerts_sent")
        con.execute(OLD_ALERTS)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'S',0)")
        con.execute("""INSERT INTO users (id, email, password_hash, display_name, role)
                       VALUES (1,'a@b.c','x','A','admin')""")
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type, rollup, start_week)
                       VALUES (1, 1, 'M', 'numeric', 'sum', '2026-01-05')""")
        con.execute("""INSERT INTO entries (metric_id, week_start, value_numeric,
                                            source, entered_by_user_id)
                       VALUES (1, '2026-01-05', 42.0, 'manual', 1)""")
        con.execute("""INSERT INTO alerts_sent (metric_id, week_start, alert_type)
                       VALUES (1, '2026-01-05', 'stale')""")
    yield


def test_migrates_and_preserves_data(old_db):
    with dbm.get_db() as con:
        assert m2w.needs_entries_migration(con)
        assert m2w.needs_alerts_migration(con)
        m2w.migrate_entries(con)
        m2w.migrate_alerts(con)

        e = con.execute("SELECT * FROM entries").fetchone()
        assert (e["value_numeric"], e["source"], e["entered_by_user_id"]) == (42.0, "manual", 1)
        a = con.execute("SELECT * FROM alerts_sent").fetchone()
        assert a["alert_type"] == "stale"

        # new values accepted (every message channel)
        for week, src in (("2026-01-12", "slack"), ("2026-01-26", "telegram"),
                          ("2026-02-02", "sms"), ("2026-02-09", "whatsapp")):
            con.execute("""INSERT INTO entries (metric_id, week_start, value_numeric,
                                                source, entered_by_user_id)
                           VALUES (1, ?, 7.0, ?, 1)""", (week, src))
        con.execute("""INSERT INTO alerts_sent (metric_id, week_start, alert_type)
                       VALUES (1, '2026-01-12', 'nudge1')""")

        # slack without a user is still rejected
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("""INSERT INTO entries (metric_id, week_start, value_numeric, source)
                           VALUES (1, '2026-01-19', 1.0, 'slack')""")

        # the index was recreated with the table
        idx = con.execute("SELECT name FROM sqlite_master WHERE type='index' "
                          "AND name='idx_entries_week'").fetchone()
        assert idx is not None

        # idempotent
        assert not m2w.needs_entries_migration(con)
        assert not m2w.needs_alerts_migration(con)


def test_fresh_schema_needs_no_migration(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    with dbm.get_db() as con:
        dbm.init_db(con)
        assert not m2w.needs_entries_migration(con)
        assert not m2w.needs_alerts_migration(con)
        # fresh DBs also carry the new tables and session column
        assert con.execute("SELECT 1 FROM magic_links WHERE 0").fetchall() == []
        assert con.execute("SELECT 1 FROM slack_prompts WHERE 0").fetchall() == []
        cols = [r["name"] for r in con.execute("PRAGMA table_info(sessions)")]
        assert "impersonate_user_id" in cols


# ------------------------------------------------- channel roll-up migration
OLD_SWEEP_RUNS = """
CREATE TABLE sweep_runs (
    id         INTEGER PRIMARY KEY,
    kind       TEXT    NOT NULL CHECK (kind IN ('nudge1','nudge2','stale','red')),
    ran_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    outcome    TEXT    NOT NULL CHECK (outcome IN ('sent','nothing','skipped')),
    detail     TEXT    NOT NULL,
    sent_count INTEGER NOT NULL DEFAULT 0
)
"""


@pytest.fixture
def pre_rollup_db(tmp_path, monkeypatch):
    """A DB from before the week-closed summary existed: sweep_runs still has
    the four-kind CHECK, and slack_threads has never been created. Built and
    committed here, like old_db, so no transaction is open when the rebuild
    issues its own BEGIN."""
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("DROP TABLE sweep_runs")
        con.execute(OLD_SWEEP_RUNS)
        con.execute("CREATE INDEX idx_sweep_runs_kind ON sweep_runs(kind, id)")
        con.execute("DROP TABLE slack_threads")
        con.execute("""INSERT INTO sweep_runs (kind, outcome, detail, sent_count)
                       VALUES ('stale','sent','Alerted 3 metrics.',3)""")
    yield


def test_the_summary_sweep_cannot_record_a_run_before_the_migration(pre_rollup_db):
    with dbm.get_db() as con, pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO sweep_runs (kind, outcome, detail) "
                    "VALUES ('summary','sent','x')")


def test_migrating_keeps_the_history_and_accepts_the_new_kind(pre_rollup_db):
    from migrate import channel_rollup

    with dbm.get_db() as con:
        assert channel_rollup.needs_migration(con)
        channel_rollup.migrate(con)
        assert not channel_rollup.needs_migration(con)

        kept = con.execute("SELECT * FROM sweep_runs").fetchall()
        assert [(r["kind"], r["sent_count"]) for r in kept] == [("stale", 3)]
        con.execute("INSERT INTO sweep_runs (kind, outcome, detail) "
                    "VALUES ('summary','sent','x')")
        # The index rides on the dropped table and has to come back with it.
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_sweep_runs_kind'").fetchone()


def test_slack_threads_arrives_with_init_db_not_a_migration(pre_rollup_db):
    """New TABLES need no migration - init_db's CREATE TABLE IF NOT EXISTS makes
    them on the next boot. Only the CHECK could not be altered in place."""
    with dbm.get_db() as con:
        assert not con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='slack_threads'").fetchone()
        dbm.init_db(con)
        assert con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='slack_threads'").fetchone()
