"""Re-owning a metric is a pick, not a trip through the edit form: the owner on
Admin > Sections & metrics and on the metric's own page is a dropdown that saves
on change."""
import pytest
from fastapi.testclient import TestClient

from app import db as dbm
from app.auth import hash_password

PW = "a-fine-password-123"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app
    with dbm.get_db() as con:
        dbm.init_db(con)
        for uid, email, name, role, active in ((1, "b@x.co", "Boss", "admin", 1),
                                               (2, "m@x.co", "Mo", "editor", 1),
                                               (3, "g@x.co", "Gone", "editor", 0)):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role,
                                      must_change_password, is_active)
                   VALUES (?,?,?,?,?,0,?)""",
                (uid, email, hash_password(PW), name, role, active))
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1, 'Sales', 1)")
        con.execute(
            """INSERT INTO metrics (id, section_id, name, metric_type, rollup, direction,
                                    dri_user_id, start_week, sort_order)
               VALUES (7, 1, 'Calls', 'numeric', 'sum', 'up', NULL, '2026-01-05', 1)""")
    return TestClient(app)


def login(c, email):
    c.post("/login", data={"email": email, "password": PW})
    return c


def owner():
    with dbm.get_db() as con:
        return con.execute("SELECT dri_user_id FROM metrics WHERE id = 7").fetchone()[0]


def test_metrics_list_owner_is_a_picker(env):
    body = login(env, "b@x.co").get("/admin/metrics").text
    form = body.split('action="/admin/metrics/7/owner"')[1].split("</form>")[0]
    assert 'onchange="this.form.submit()"' in form
    assert '<option value="2">Mo</option>' in form
    assert "Gone" not in form  # inactive people cannot be picked


def test_set_owner_saves_and_returns_to_the_section(env):
    r = login(env, "b@x.co").post("/admin/metrics/7/owner", data={"dri_user_id": "2"},
                                  follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/metrics#sec-1"
    assert owner() == 2
    env.post("/admin/metrics/7/owner", data={"dri_user_id": ""})
    assert owner() is None


def test_set_owner_follows_next_but_never_off_site(env):
    c = login(env, "b@x.co")
    r = c.post("/admin/metrics/7/owner", data={"dri_user_id": "2", "next": "/m/7"},
               follow_redirects=False)
    assert r.headers["location"] == "/m/7"
    r = c.post("/admin/metrics/7/owner", data={"dri_user_id": "2", "next": "//evil.example"},
               follow_redirects=False)
    assert r.headers["location"] == "/admin/metrics#sec-1"


def test_inactive_or_unknown_owner_is_refused(env):
    c = login(env, "b@x.co")
    assert c.post("/admin/metrics/7/owner", data={"dri_user_id": "3"}).status_code == 422
    assert c.post("/admin/metrics/7/owner", data={"dri_user_id": "99"}).status_code == 422
    assert c.post("/admin/metrics/99/owner", data={"dri_user_id": "2"}).status_code == 404
    assert owner() is None


def test_only_admins_can_reassign(env):
    c = login(env, "m@x.co")
    r = c.post("/admin/metrics/7/owner", data={"dri_user_id": "2"}, follow_redirects=False)
    assert r.status_code in (303, 403) and owner() is None


def test_metric_page_offers_the_picker_to_admins_only(env):
    c = login(env, "b@x.co")
    assert 'action="/admin/metrics/7/owner"' in c.get("/m/7").text
    c.post("/logout")
    c.cookies.clear()
    assert 'action="/admin/metrics/7/owner"' not in login(c, "m@x.co").get("/m/7").text
