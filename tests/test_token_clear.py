"""Clearing dead tokens off Admin > API tokens: revoked and expired rows can
go, a secret that still authenticates never can, and every number a cleared
token wrote keeps naming it on Activity and the Sections & metrics page."""
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import auth
from app import db as dbm
from app import weeks as wk
from app.main import _metric_writers, _token_lineages
from migrate import token_cleared


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app  # imported late so DB_PATH is already patched

    start = (wk.last_closed_week(datetime.now(timezone.utc))
             - timedelta(days=28)).isoformat()
    with dbm.get_db() as con:
        dbm.init_db(con)
        for uid, email, name, role in ((1, "a@example.com", "Admin", "admin"),
                                       (2, "e@example.com", "Eddie", "editor")):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role)
                   VALUES (?,?,'x',?,?)""", (uid, email, name, role))
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'S',0)")
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                            start_week, dri_user_id)
                       VALUES (1, 1, 'Leads', 'numeric', 'sum', ?, 2)""", (start,))
        auth.new_api_token(con, "hermes", "read_write", 1)
    client = TestClient(app)
    login(client, 1)
    yield client


def login(client, uid):
    with dbm.get_db() as con:
        client.cookies.set(auth.SESSION_COOKIE, auth.create_session(con, uid))


def hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


def token(tid):
    with dbm.get_db() as con:
        return con.execute("SELECT * FROM api_tokens WHERE id = ?", (tid,)).fetchone()


def visible():
    with dbm.get_db() as con:
        rows = con.execute("SELECT * FROM api_tokens WHERE cleared_at IS NULL "
                           "ORDER BY created_at DESC, id DESC").fetchall()
    return _token_lineages(rows, datetime.now(timezone.utc))


def sql(stmt, *args):
    with dbm.get_db() as con:
        con.execute(stmt, args)


def test_revoked_token_can_be_deleted_and_leaves_the_list(env):
    client = env
    client.post("/admin/tokens/1/revoke")
    r = client.post("/admin/tokens/1/delete", follow_redirects=True)
    assert r.status_code == 200
    assert "Cleared 1 dead token" in r.text
    assert token(1)["cleared_at"] is not None
    assert visible() == []
    assert "hermes" not in r.text.split("<h3>New token</h3>")[0].split("<table")[1]


def test_active_token_is_never_deletable(env):
    """The id is in the URL, so the route cannot trust the page to have hidden
    the button: a live token is refused on the server."""
    client = env
    r = client.post("/admin/tokens/1/delete")
    assert "still authenticates" in r.text
    assert token(1)["cleared_at"] is None
    assert len(visible()) == 1
    # and "Clear revoked" does not sweep it up either
    client.post("/admin/tokens/clear")
    assert token(1)["cleared_at"] is None


def test_unknown_id_is_refused(env):
    client = env
    r = client.post("/admin/tokens/999/delete")
    assert "No such token" in r.text


def test_a_live_page_shows_no_delete_or_clear_control(env):
    client = env
    r = client.get("/admin/tokens")
    assert "/admin/tokens/1/delete" not in r.text
    assert "/admin/tokens/clear" not in r.text
    client.post("/admin/tokens/1/revoke")
    r = client.get("/admin/tokens")
    assert "/admin/tokens/1/delete" in r.text
    assert "Clear revoked (1)" in r.text


def test_previous_secret_in_grace_cannot_be_deleted_but_expired_can(env):
    client = env
    with dbm.get_db() as con:
        auth.rotate_api_token(con, 1, 7, 1)          # 1 -> 2, 1 in grace
    client.post("/admin/tokens/1/delete")
    assert token(1)["cleared_at"] is None, "it still authenticates"

    sql("UPDATE api_tokens SET expires_at = datetime('now','-1 day') WHERE id=1")
    assert "/admin/tokens/1/delete" in client.get("/admin/tokens").text
    client.post("/admin/tokens/1/delete")
    assert token(1)["cleared_at"] is not None
    assert token(2)["cleared_at"] is None, "the live replacement stays"
    [g] = visible()
    assert g["head"]["t"]["id"] == 2 and g["prior"] == []


def test_dead_head_with_a_live_previous_secret_stays(env):
    """Revoking the replacement while the old secret is still in grace leaves a
    secret that authenticates nested under a dead row. Clearing the row would
    hide that secret, so both the row and the sweep leave it alone."""
    client = env
    with dbm.get_db() as con:
        auth.rotate_api_token(con, 1, 7, 1)          # 1 -> 2, 1 in grace
    client.post("/admin/tokens/2/revoke")
    r = client.get("/admin/tokens")
    assert "/admin/tokens/2/delete" not in r.text
    assert "/admin/tokens/clear" not in r.text

    r = client.post("/admin/tokens/2/delete")
    assert "still authenticates" in r.text
    client.post("/admin/tokens/clear")
    assert token(1)["cleared_at"] is None and token(2)["cleared_at"] is None


def test_clearing_a_previous_secret_takes_its_older_generations(env):
    """Otherwise the generation before it would surface as a stray row of its
    own - the list refilling with exactly what was just cleared."""
    client = env
    with dbm.get_db() as con:
        auth.rotate_api_token(con, 1, 0, 1)          # 1 -> 2
        auth.rotate_api_token(con, 2, 0, 1)          # 2 -> 3
    client.post("/admin/tokens/2/delete")
    assert token(1)["cleared_at"] and token(2)["cleared_at"]
    [g] = visible()
    assert g["head"]["t"]["id"] == 3
    assert g["prior"] == [] and g["retired"] == 0


def test_clear_revoked_sweeps_every_dead_row_and_nothing_live(env):
    client = env
    with dbm.get_db() as con:
        auth.new_api_token(con, "old-n8n", "write", 1)          # 2, revoked below
        auth.new_api_token(con, "claude", "read", 1)            # 3, stays live
        auth.rotate_api_token(con, 3, 0, 1)                     # 3 -> 4, 3 expired
        auth.new_api_token(con, "agent", "read", 1)             # 5
        auth.rotate_api_token(con, 5, 7, 1)                     # 5 -> 6, 5 in grace
    client.post("/admin/tokens/2/revoke")
    r = client.post("/admin/tokens/clear", follow_redirects=True)
    assert "Cleared 2 dead tokens" in r.text
    cleared = {i for i in range(1, 7) if token(i)["cleared_at"]}
    assert cleared == {2, 3}
    # the page's remaining rows: hermes, claude (new), agent (new + in-grace old)
    assert {g["head"]["t"]["id"] for g in visible()} == {1, 4, 6}


def test_cleared_token_still_names_the_numbers_it_wrote(env):
    """The reason this is a flag and not a DELETE."""
    client = env
    with dbm.get_db() as con:
        raw = auth.new_api_token(con, "n8n-leads", "read_write", 1)   # id 2
    r = client.post("/api/v1/metrics/1/entries", headers=hdr(raw), json={"value": 12})
    assert r.status_code == 200, r.text

    # A hard delete is not an option: the live entry holds the token by FK...
    with pytest.raises(sqlite3.IntegrityError):
        sql("DELETE FROM api_tokens WHERE id = 2")

    client.post("/admin/tokens/2/revoke")
    client.post("/admin/tokens/2/delete")
    assert token(2)["cleared_at"] is not None

    # ...and after clearing, both places that name a writer still do.
    assert "n8n-leads" in client.get("/admin/activity").text
    with dbm.get_db() as con:
        w = _metric_writers(con)[1]
    assert w["by"] == "n8n-leads" and w["api_token"] == "n8n-leads"
    assert "n8n-leads writes this through the API" in client.get("/admin/metrics").text
    # and the cleared secret still does not authenticate
    assert client.get("/api/v1/scorecard", headers=hdr(raw)).status_code == 401


def test_clearing_is_admin_only_and_post_only(env):
    client = env
    client.post("/admin/tokens/1/revoke")
    assert client.get("/admin/tokens/1/delete").status_code == 405
    assert client.get("/admin/tokens/clear").status_code == 405
    login(client, 2)
    assert client.post("/admin/tokens/1/delete").status_code == 403
    assert client.post("/admin/tokens/clear").status_code == 403
    assert token(1)["cleared_at"] is None


def test_migration_adds_the_column_to_an_older_db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "old.db"))
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("ALTER TABLE api_tokens DROP COLUMN cleared_at")
        auth.new_api_token(con, "hermes", "read", None)
    with dbm.get_db() as con:
        dbm.init_db(con)                      # the startup replay must not raise
        assert token_cleared.needs_migration(con)
        assert token_cleared.migrate(con) == ["api_tokens.cleared_at added."]
        assert token_cleared.migrate(con) == []                  # idempotent
        row = con.execute("SELECT cleared_at FROM api_tokens").fetchone()
        assert row["cleared_at"] is None      # existing tokens stay on the page
