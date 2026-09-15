"""Top-level navigation: three labelled destinations grouped by cadence, with
the seven admin screens one level down. Guards the regression that made it a
route table - nine unlabelled glyphs, seven of them quarterly."""
import pytest
from fastapi.testclient import TestClient

from app import db as dbm
from app.auth import hash_password

PW = "a-fine-password-123"

ADMIN_HREFS = ["/admin/status", "/admin/metrics", "/admin/targets", "/admin/users",
               "/admin/settings", "/admin/activity", "/admin/tokens"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app
    with dbm.get_db() as con:
        dbm.init_db(con)
        for uid, email, name, role in ((1, "b@x.co", "Boss", "admin"),
                                       (2, "e@x.co", "Eddie", "editor"),
                                       (3, "v@x.co", "Val", "viewer")):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role,
                                      must_change_password) VALUES (?,?,?,?,?,0)""",
                (uid, email, hash_password(PW), name, role))
    return TestClient(app)


def login(client, email):
    client.post("/login", data={"email": email, "password": PW})
    return client


def test_rail_holds_four_destinations_not_nine(env):
    body = login(env, "b@x.co").get("/").text
    rail = body.split('<aside class="sidenav">')[1].split("</aside>")[0]
    # Board, CEO metrics, My numbers, Admin, account, sign out
    assert rail.count('class="nav-item') == 6
    for label in ("Board", "CEO metrics", "My numbers", "Admin"):
        assert f"<span>{label}</span>" in rail
    # Every admin screen is behind the single Admin entry, not in the rail.
    for href in ADMIN_HREFS:
        assert href not in rail


def test_admin_lands_on_health_not_the_metric_editor(env):
    r = login(env, "b@x.co").get("/admin", follow_redirects=False)
    assert r.headers["location"] == "/admin/status"


def test_admin_screens_carry_their_own_second_level_nav(env):
    body = login(env, "b@x.co").get("/admin/targets").text
    sub = body.split('<nav class="subnav"')[1].split("</nav>")[0]
    for href in ADMIN_HREFS:
        assert href in sub
    assert 'class="on"' in sub                      # the current page is marked


def test_non_admin_screens_have_no_subnav(env):
    assert 'class="subnav"' not in login(env, "b@x.co").get("/").text


def test_editors_and_viewers_see_only_what_they_can_use(env):
    ed = login(env, "e@x.co").get("/").text
    assert "<span>My numbers</span>" in ed and "<span>Admin</span>" not in ed
    env.post("/logout")
    vi = login(env, "v@x.co").get("/").text
    assert "<span>My numbers</span>" not in vi and "<span>Admin</span>" not in vi
