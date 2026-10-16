"""A check-in link signs in with a LIMITED session: numbers, own 1-3-1s and
reading the board - for half a day. Never the owner's full role, so an admin's
weekly nudge is not an admin session (or a passkey) for whoever opens it."""
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from app import auth
from app import db as dbm
from app import weeks as wk
from app.auth import create_magic_link, hash_password
from migrate import session_scope

PW = "a-fine-password-123"


def due_week():
    return wk.last_closed_week(datetime.now(timezone.utc)).isoformat()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app

    start = (wk.parse_week(due_week()) - timedelta(days=28)).isoformat()
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'S',0)")
        for uid, email, role in ((1, "boss@x.co", "admin"), (2, "ed@x.co", "editor")):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role,
                                      must_change_password) VALUES (?,?,?,?,?,0)""",
                (uid, email, hash_password(PW), email.split("@")[0], role))
        for mid, dri in ((1, 1), (2, 2)):
            con.execute("""INSERT INTO metrics (id, section_id, name, metric_type,
                                                rollup, start_week, dri_user_id)
                           VALUES (?, 1, ?, 'numeric', 'sum', ?, ?)""",
                        (mid, f"Metric {mid}", start, dri))
    yield app


def via_link(app, uid=1):
    with dbm.get_db() as con:
        token = create_magic_link(con, uid)
    c = TestClient(app)
    r = c.get("/checkin", params={"t": token})
    assert r.status_code == 200 and "My numbers" in r.text
    return c


def bounced(r, next_path=None):
    assert r.status_code == 303, r.status_code
    loc = urlparse(r.headers["location"])
    q = parse_qs(loc.query)
    assert loc.path == "/login" and q["why"] == ["full"]
    if next_path:
        assert q["next"] == [next_path]
    return True


def test_admin_link_session_can_enter_their_numbers(env):
    c = via_link(env)
    r = c.post(f"/checkin/1/{due_week()}", data={"value": "7"})
    assert r.status_code == 200
    with dbm.get_db() as con:
        assert con.execute("SELECT value_numeric FROM entries WHERE metric_id=1"
                           ).fetchone()["value_numeric"] == 7.0
    assert c.get("/checkin/catch-up").status_code == 200
    assert c.get("/").status_code == 200          # read the board
    assert c.get("/m/1").status_code == 200       # and a metric page


def test_admin_link_session_is_bounced_from_admin_and_credentials(env):
    c = via_link(env)
    nr = {"follow_redirects": False}
    assert bounced(c.get("/admin/settings", **nr), "/admin/settings")
    assert bounced(c.get("/admin/tokens", **nr), "/admin/tokens")
    assert bounced(c.post("/admin/tokens", data={"name": "x", "scope": "admin"}, **nr))
    assert bounced(c.get("/admin/users", **nr))
    assert bounced(c.post("/account/passkeys/begin", **nr))
    assert bounced(c.post("/account/passkeys/finish", json={}, **nr))
    assert bounced(c.get("/account", **nr))
    assert bounced(c.post("/account/password",
                          data={"current": PW, "new": "z" * 20}, **nr))
    assert bounced(c.post("/admin/users/2/impersonate", **nr))
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) FROM api_tokens").fetchone()[0] == 0
        assert auth.verify_password(PW, con.execute(
            "SELECT password_hash FROM users WHERE id=1").fetchone()["password_hash"])
        assert con.execute("SELECT COUNT(*) FROM sessions "
                           "WHERE impersonate_user_id IS NOT NULL").fetchone()[0] == 0
    # The login page says why.
    assert "Sign in with your password to do that" in c.get(
        "/login", params={"why": "full", "next": "/admin/settings"}).text


def test_htmx_refusal_navigates_instead_of_swapping_the_login_form(env):
    c = via_link(env)
    r = c.get("/admin/settings", headers={"HX-Request": "true"},
              follow_redirects=False)
    assert r.status_code == 403
    assert r.headers["HX-Redirect"].startswith("/login?")


def test_board_edits_are_outside_the_link_scope(env):
    c = via_link(env, uid=2)  # an editor, not just admins
    assert bounced(c.get("/quick/2", follow_redirects=False))
    assert bounced(c.post(f"/cell/2/{due_week()}", data={"value": "3"},
                          follow_redirects=False))


def test_one_three_one_only_for_a_metric_you_own(env):
    c = via_link(env, uid=2)
    w = due_week()
    form = {"problem": "p", "option1": "a", "option2": "b", "option3": "c",
            "recommendation": "r"}
    assert c.get(f"/131/2/{w}").status_code == 200
    assert c.post(f"/131/2/{w}", data=form, follow_redirects=False).status_code == 303
    assert bounced(c.get(f"/131/1/{w}", follow_redirects=False))
    assert bounced(c.post(f"/131/1/{w}", data=form, follow_redirects=False))
    with dbm.get_db() as con:
        rows = con.execute("SELECT metric_id FROM one_three_ones").fetchall()
    assert [r["metric_id"] for r in rows] == [2]


def test_link_session_is_short_and_marked(env):
    with dbm.get_db() as con:
        token = create_magic_link(con, 1)
    c = TestClient(env)
    r = c.get("/checkin", params={"t": token}, follow_redirects=False)
    assert "max-age=43200" in r.headers["set-cookie"].lower()
    with dbm.get_db() as con:
        s = con.execute("SELECT scope, expires_at FROM sessions").fetchone()
    assert s["scope"] == "checkin"
    left = datetime.fromisoformat(s["expires_at"]) - datetime.now(timezone.utc)
    assert timedelta(hours=11) < left <= timedelta(hours=auth.LINK_SESSION_HOURS)


def test_activity_does_not_extend_it_and_expiry_ends_it(env):
    c = via_link(env)
    with dbm.get_db() as con:
        before = con.execute("SELECT expires_at FROM sessions").fetchone()[0]
    c.get("/checkin")
    c.get("/")
    with dbm.get_db() as con:
        assert con.execute("SELECT expires_at FROM sessions").fetchone()[0] == before
        con.execute("UPDATE sessions SET expires_at = ?",
                    ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),))
    assert str(c.get("/checkin").url).endswith("/login")


def test_password_login_replaces_the_limited_session(env):
    c = via_link(env)
    assert bounced(c.get("/admin/settings", follow_redirects=False))
    c.post("/login", data={"email": "boss@x.co", "password": PW})
    assert c.get("/admin/settings", follow_redirects=False).status_code == 200
    assert c.get("/admin/tokens", follow_redirects=False).status_code == 200
    assert c.post("/account/passkeys/begin").status_code == 200
    with dbm.get_db() as con:
        scopes = sorted(r[0] for r in con.execute("SELECT scope FROM sessions"))
    assert "full" in scopes


def test_password_and_passkey_sessions_are_full(env):
    c = TestClient(env)
    c.post("/login", data={"email": "boss@x.co", "password": PW})
    with dbm.get_db() as con:
        s = con.execute("SELECT scope, expires_at FROM sessions").fetchone()
    assert s["scope"] == "full"
    left = datetime.fromisoformat(s["expires_at"]) - datetime.now(timezone.utc)
    assert left > timedelta(days=29)


def test_migration_adds_scope_to_an_old_sessions_table(tmp_path):
    con = sqlite3.connect(tmp_path / "old.db")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY)")
    con.execute("""CREATE TABLE sessions (token_hash TEXT PRIMARY KEY,
                   user_id INTEGER NOT NULL, created_at TEXT, expires_at TEXT NOT NULL,
                   last_seen_at TEXT, impersonate_user_id INTEGER)""")
    con.execute("INSERT INTO sessions (token_hash, user_id, expires_at) "
                "VALUES ('h', 1, '2099-01-01')")
    assert session_scope.needs_migration(con)
    assert session_scope.migrate(con)
    assert con.execute("SELECT scope FROM sessions").fetchone()[0] == "full"
    assert session_scope.migrate(con) == []          # idempotent
    # schema.sql replays cleanly over the migrated table (boot order).
    con.executescript(dbm.SCHEMA_PATH.read_text())
