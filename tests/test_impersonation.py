"""View-as-user: the admin session renders as the target (role and all),
the banner names both parties, writes are audited as the real admin, and
every fallback (exit, deactivation, demotion, logout) restores safety."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db as dbm
from app import weeks as wk
from app.auth import hash_password

PW = "a-fine-password-123"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app

    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        for uid, email, name, role in ((1, "boss@x.co", "Boss", "admin"),
                                       (2, "ed@x.co", "Eddie Editor", "editor"),
                                       (3, "vi@x.co", "Val Viewer", "viewer"),
                                       (4, "ad2@x.co", "Second Admin", "admin")):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role,
                                      must_change_password) VALUES (?,?,?,?,?,0)""",
                (uid, email, hash_password(PW), name, role))
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                            start_week, dri_user_id)
                       VALUES (1, 1, 'Calls', 'numeric', 'sum', '2026-01-05', 2)""")
    client = TestClient(app)
    client.post("/login", data={"email": "boss@x.co", "password": PW})
    yield client


def week():
    return wk.current_week(datetime.now(timezone.utc)).isoformat()


def test_admin_views_as_editor_and_back(env):
    client = env
    r = client.post("/admin/users/2/impersonate")
    assert r.status_code == 200  # followed 303 to /

    r = client.get("/")
    assert "Viewing as" in r.text and "Eddie Editor" in r.text and "Boss" in r.text
    assert "/admin/settings" not in r.text          # admin nav gone
    assert client.get("/admin/users").status_code == 403  # effective role enforced

    r = client.post("/impersonate/stop")
    assert r.status_code == 200  # followed to /admin/users
    r = client.get("/")
    assert "Viewing as" not in r.text and "/admin/settings" in r.text


def test_writes_while_impersonating_audit_the_real_admin(env):
    client = env
    client.post("/admin/users/2/impersonate")
    assert client.post(f"/cell/1/{week()}", data={"value": "9"}).status_code == 200
    with dbm.get_db() as con:
        e = con.execute("SELECT * FROM entries WHERE metric_id=1").fetchone()
        a = con.execute("SELECT * FROM entry_audit WHERE metric_id=1").fetchone()
    assert e["entered_by_user_id"] == 1  # Boss, not Eddie
    assert a["actor_user_id"] == 1


def test_viewer_target_gets_read_only_surface(env):
    client = env
    client.post("/admin/users/3/impersonate")
    r = client.get("/")
    assert "Read-only view" in r.text
    assert client.post(f"/cell/1/{week()}", data={"value": "5"}).status_code == 403


def test_non_admin_cannot_impersonate(env):
    client = env
    client.post("/logout")
    client.post("/login", data={"email": "ed@x.co", "password": PW})
    assert client.post("/admin/users/3/impersonate").status_code == 403


def test_impersonating_admin_is_allowed_but_chaining_switches(env):
    client = env
    client.post("/admin/users/4/impersonate")
    r = client.get("/")
    assert "Second Admin" in r.text and "Viewing as" in r.text


def test_deactivated_target_falls_back_to_admin(env):
    client = env
    client.post("/admin/users/2/impersonate")
    with dbm.get_db() as con:
        con.execute("UPDATE users SET is_active = 0 WHERE id = 2")
    r = client.get("/")
    assert "Viewing as" not in r.text and "/admin/settings" in r.text


def test_inactive_target_404s(env):
    client = env
    with dbm.get_db() as con:
        con.execute("UPDATE users SET is_active = 0 WHERE id = 3")
    assert client.post("/admin/users/3/impersonate").status_code == 404
    assert client.post("/admin/users/99/impersonate").status_code == 404


def test_logout_ends_impersonation_with_the_session(env):
    client = env
    client.post("/admin/users/2/impersonate")
    client.post("/logout")
    client.post("/login", data={"email": "boss@x.co", "password": PW})
    r = client.get("/")
    assert "Viewing as" not in r.text
