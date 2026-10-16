"""Operational edges: the health check, boot-time column migrations, and the
schema's replay safety. Small, but each one is what an operator relies on when
something is already going wrong."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db as dbm


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app
    with dbm.get_db() as con:
        dbm.init_db(con)
    return TestClient(app)


def test_healthz_needs_no_login_and_says_only_three_things(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"ok", "db", "scheduler"}
    assert body["ok"] is True and body["db"] == "ok"
    # Another test's lifespan may share the module-level scheduler; either
    # answer is a fixed word, never anything more.
    assert body["scheduler"] in ("running", "stopped")


def test_healthz_is_503_when_the_database_cannot_answer(client, monkeypatch):
    def broken(*a, **k):
        raise sqlite3.OperationalError("unable to open database file /secret/path")
    monkeypatch.setattr(dbm, "connect", broken)
    r = client.get("/healthz")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False and body["db"] == "error"
    assert set(body) == {"ok", "db", "scheduler"}
    assert "secret" not in r.text            # no error text to the unauthenticated


def test_boot_migration_ignores_only_an_existing_column():
    from app.main import _add_column
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (a INTEGER)")
    _add_column(con, "ALTER TABLE t ADD COLUMN b TEXT")
    _add_column(con, "ALTER TABLE t ADD COLUMN b TEXT")     # replay: fine
    with pytest.raises(sqlite3.OperationalError):           # typo: stop the boot
        _add_column(con, "ALTER TABLE nope ADD COLUMN b TEXT")

    class Locked:
        def execute(self, sql):
            raise sqlite3.OperationalError("database is locked")
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        _add_column(Locked(), "ALTER TABLE t ADD COLUMN c TEXT")


def test_entry_audit_is_indexed_by_metric_and_the_schema_replays(tmp_path):
    con = dbm.connect(str(tmp_path / "s.db"))
    dbm.init_db(con)
    dbm.init_db(con)                  # replayed on every boot
    plan = " ".join(r[3] for r in con.execute(
        "EXPLAIN QUERY PLAN SELECT source FROM entry_audit "
        "WHERE metric_id = 1 ORDER BY id DESC LIMIT 1"))
    assert "idx_entry_audit_metric" in plan
    con.close()
