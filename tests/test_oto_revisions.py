"""A 1-3-1 can be revised, and the version it replaces is kept.

It used INSERT OR IGNORE against a UNIQUE(metric_id, week_start): a second
filing was silently discarded, which was the one place in this app where a
write disappeared without saying so."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db as dbm
from app import weeks as wk
from app.auth import hash_password
from migrate import oto_revisions

PW = "a-fine-password-123"


def due():
    return wk.last_closed_week(datetime.now(timezone.utc))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app
    d = due()
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        con.execute("""INSERT INTO users (id, email, password_hash, display_name,
                       role, must_change_password) VALUES (1,'b@x.co',?,'Boss','admin',0)""",
                    (hash_password(PW),))
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                       start_week, dri_user_id) VALUES (1,1,'Calls','numeric','sum',?,1)""",
                    ((d - timedelta(days=56)).isoformat(),))
        con.execute("""INSERT INTO targets (metric_id, year, quarter, baseline_value,
                       stretch_value) VALUES (1,?,?,10,10)""", wk.quarter_of(d))
        oto_revisions.migrate(con)        # what startup does, done here too
        for i in range(3):
            dbm.upsert_entry(con, 1, d - timedelta(days=7 * i), value_numeric=1.0,
                             source="manual", user_id=1)
    c = TestClient(app)
    c.post("/login", data={"email": "b@x.co", "password": PW})
    return c


def file_one(client, problem="Leads dried up", rec="Call the last 20 lost deals"):
    return client.post(f"/131/1/{due().isoformat()}", data={
        "problem": problem, "option1": "a", "option2": "b", "option3": "c",
        "recommendation": rec})


def test_the_form_carries_the_metric_data_it_is_about(env):
    body = env.get(f"/131/1/{due().isoformat()}").text
    assert "What happened" in body
    assert "Red streak" in body and "Weeks on target" in body
    assert "/m/1" in body                      # and a way to the full history


def test_a_revision_supersedes_and_keeps_the_original(env):
    file_one(env)
    file_one(env, problem="Actually it is pricing", rec="Re-quote the pipeline")
    with dbm.get_db() as con:
        rows = con.execute(
            "SELECT problem, superseded_at FROM one_three_ones "
            "WHERE metric_id=1 ORDER BY id").fetchall()
    assert len(rows) == 2
    assert rows[0]["problem"] == "Leads dried up" and rows[0]["superseded_at"]
    assert rows[1]["problem"] == "Actually it is pricing"
    assert rows[1]["superseded_at"] is None    # exactly one live draft

    body = env.get(f"/131/1/{due().isoformat()}").text
    assert "Actually it is pricing" in body
    assert "Earlier versions" in body and "Leads dried up" in body


def test_only_one_live_draft_can_exist(env):
    """The partial index is what replaced UNIQUE(metric_id, week_start)."""
    file_one(env)
    with dbm.get_db() as con:
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                """INSERT INTO one_three_ones (metric_id, week_start, problem,
                   options_json, recommendation, created_by)
                   VALUES (1,?,'dup','[]','dup',1)""", (due().isoformat(),))


def test_filing_lands_on_the_metric_page(env):
    r = file_one(env, problem="p", rec="r")
    assert str(r.url).endswith("/m/1")


def test_the_migration_is_idempotent(env):
    with dbm.get_db() as con:
        assert not oto_revisions.needs_migration(con)
        oto_revisions.main()                            # a no-op, must not raise


# ------------------------------------------------- migrating a real old DB
OLD_OTO = """
CREATE TABLE one_three_ones (
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
)
"""


@pytest.fixture
def old_db(tmp_path, monkeypatch):
    """Shaped like production before this migration: the frozen table, with a
    1-3-1 already filed in it."""
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    d = due()
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("DROP INDEX IF EXISTS idx_oto_live")
        con.execute("DROP TABLE one_three_ones")
        con.execute(OLD_OTO)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'S',0)")
        con.execute("""INSERT INTO users (id, email, password_hash, display_name, role)
                       VALUES (1,'b@x.co','x','Boss','admin')""")
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type,
                       rollup, start_week) VALUES (1,1,'M','numeric','sum',?)""",
                    (d.isoformat(),))
        con.execute("""INSERT INTO one_three_ones (metric_id, week_start, problem,
                       options_json, recommendation, created_by)
                       VALUES (1,?,'old problem','["a","b","c"]','old rec',1)""",
                    (d.isoformat(),))
    yield


def test_migration_keeps_the_filed_document_and_lifts_the_freeze(old_db):
    import sqlite3
    with dbm.get_db() as con:
        assert oto_revisions.needs_migration(con)
        # Before: a second filing is refused outright.
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("""INSERT INTO one_three_ones (metric_id, week_start, problem,
                           options_json, recommendation, created_by)
                           VALUES (1,?,'second','[]','r',1)""", (due().isoformat(),))

    with dbm.get_db() as con:
        oto_revisions.migrate(con)

    with dbm.get_db() as con:
        assert not oto_revisions.needs_migration(con)
        row = con.execute("SELECT * FROM one_three_ones").fetchone()
        assert row["problem"] == "old problem"          # data survived
        assert row["superseded_at"] is None             # and is still the live one
        # After: superseding first, then inserting, is allowed.
        con.execute("UPDATE one_three_ones SET superseded_at = datetime('now') "
                    "WHERE metric_id=1 AND superseded_at IS NULL")
        con.execute("""INSERT INTO one_three_ones (metric_id, week_start, problem,
                       options_json, recommendation, created_by)
                       VALUES (1,?,'second','[]','r',1)""", (due().isoformat(),))
        assert con.execute("SELECT COUNT(*) c FROM one_three_ones").fetchone()["c"] == 2


def test_startup_survives_a_database_that_predates_this_change(old_db):
    """The bug this guards: schema.sql is replayed on EVERY startup, before the
    migrations. A partial index naming superseded_at, declared there, failed
    against the not-yet-migrated table and killed the app on boot - the
    migration meant to fix the table never got to run."""
    from app.main import app
    with TestClient(app):                     # runs the lifespan, as uvicorn does
        pass
    with dbm.get_db() as con:
        assert not oto_revisions.needs_migration(con)
        assert oto_revisions._has_index(con)
        assert con.execute(
            "SELECT problem FROM one_three_ones").fetchone()["problem"] == "old problem"


def test_a_fresh_database_ends_up_with_the_index_too(tmp_path, monkeypatch):
    """The other direction: schema.sql creates the column but cannot create the
    index, so the migration has to notice and finish the job."""
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "fresh.db"))
    from app.main import app
    with dbm.get_db() as con:
        dbm.init_db(con)
        assert oto_revisions.needs_migration(con)      # column yes, index no
    with TestClient(app):
        pass
    with dbm.get_db() as con:
        assert oto_revisions._has_index(con)
