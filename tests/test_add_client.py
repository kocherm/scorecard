"""Adding a client is a name and nothing else: a roster section (every live row
R/Y/G) carries its own one-field form with the type fixed and the owner
defaulted, and the board links straight to it."""
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
        for uid, email, name, role in ((1, "b@x.co", "Boss", "admin"),
                                       (2, "m@x.co", "Mo", "editor")):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role,
                                      must_change_password) VALUES (?,?,?,?,?,0)""",
                (uid, email, hash_password(PW), name, role))
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1, 'Sales', 1)")
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (2, 'Client Health', 2)")
        for name, sec, mtype, dri in (("Calls", 1, "numeric", 1),
                                      ("Client A", 2, "status", 2),
                                      ("Client B", 2, "status", 2)):
            con.execute(
                """INSERT INTO metrics (section_id, name, metric_type, rollup, direction,
                                        dri_user_id, start_week, sort_order)
                   VALUES (?,?,?,?,'up',?,'2026-01-05',1)""",
                (sec, name, mtype, "sum" if mtype == "numeric" else None, dri))
    c = TestClient(app)
    c.post("/login", data={"email": "b@x.co", "password": PW})
    return c


def live(name):
    with dbm.get_db() as con:
        return con.execute("SELECT * FROM metrics WHERE name = ? AND archived_at IS NULL",
                           (name,)).fetchall()


def test_roster_section_gets_the_one_field_form_numeric_does_not(env):
    body = env.get("/admin/metrics").text
    assert body.count('class="met-quick"') == 1
    form = body.split('class="met-quick"')[1].split("</form>")[0]
    assert 'name="section_id" value="2"' in form
    assert 'name="metric_type" value="status"' in form
    assert "Add a client" in form
    # The owner defaults to whoever owns the rest of the roster.
    assert '<option value="2" selected>Mo</option>' in form


def test_add_client_creates_a_status_row_and_returns_to_the_form(env):
    r = env.post("/admin/metrics", data={"section_id": "2", "metric_type": "status",
                                         "name": "  New   Client ", "dri_user_id": "2",
                                         "again": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/metrics?added=New+Client&add=2#sec-2"
    (m,) = live("New Client")
    assert (m["section_id"], m["metric_type"], m["dri_user_id"]) == (2, "status", 2)
    assert m["rollup"] is None
    body = env.get("/admin/metrics?added=New+Client&add=2").text
    assert "Added <strong>New Client</strong>" in body
    assert 'id="quick-2" name="name" required' in body and "autofocus" in body


def test_duplicate_client_name_is_refused(env):
    r = env.post("/admin/metrics", data={"section_id": "2", "metric_type": "status",
                                         "name": "client a", "again": "1"},
                 follow_redirects=False)
    assert "dup=client+a" in r.headers["location"]
    assert len(live("Client A")) == 1 and not live("client a")


def test_board_links_admins_to_the_form(env):
    body = env.get("/").text
    assert body.count('class="section-add"') == 1
    assert 'href="/admin/metrics?add=2#sec-2">+ Add client' in body


def test_board_hides_the_link_from_editors(env):
    env.post("/logout")
    env.cookies.clear()
    env.post("/login", data={"email": "m@x.co", "password": PW})
    assert 'class="section-add"' not in env.get("/").text
