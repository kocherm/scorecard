"""Users: the table carries information, the drawer carries the controls.

The column that matters is "metrics owned" - it is how you see that one person
carries most of the board, which a list of names can never show."""
from datetime import datetime, timedelta, timezone

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
    due = wk.last_closed_week(datetime.now(timezone.utc))
    start = (due - timedelta(days=28)).isoformat()
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        for uid, email, name, role in ((1, "b@x.co", "Boss", "admin"),
                                       (2, "e@x.co", "Eddie", "editor")):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role,
                                      must_change_password) VALUES (?,?,?,?,?,0)""",
                (uid, email, hash_password(PW), name, role))
        # Boss owns three, Eddie one, and one metric has nobody at all.
        for mid, dri in ((1, 1), (2, 1), (3, 1), (4, 2), (5, None)):
            con.execute("""INSERT INTO metrics (id, section_id, name, metric_type,
                                                rollup, start_week, dri_user_id)
                           VALUES (?,1,?, 'numeric','sum',?,?)""",
                        (mid, f"M{mid}", start, dri))
        dbm.upsert_entry(con, 1, due, value_numeric=5.0, source="manual", user_id=1)
    c = TestClient(app)
    c.post("/login", data={"email": "b@x.co", "password": PW})
    return c


def test_the_page_shows_how_the_board_is_distributed(env):
    body = env.get("/admin/users").text
    assert "Metrics owned" in body
    assert 'class="usr-share"' in body        # the share bar, not just a count
    assert "1 live metric has no owner" in body


def test_sorting_by_owned_puts_the_imbalance_first(env):
    rows = env.get("/admin/users?sort=owned").text.split('class="usr-name"')
    assert "Boss" in rows[1] and "Eddie" in rows[2]
    # Default order is alphabetical.
    alpha = env.get("/admin/users").text.split('class="usr-name"')
    assert "Boss" in alpha[1] and "Eddie" in alpha[2]


def test_last_entry_reports_whether_the_account_is_used(env):
    body = env.get("/admin/users").text
    assert "never" in body                    # Eddie has written nothing


def test_controls_live_in_the_drawer_not_the_row(env):
    """Five columns of controls and two of data was the shape being fixed."""
    body = env.get("/admin/users").text
    head = body.split("<tbody>")[0]
    assert "<select" not in head and "Reset password" not in head
    # Eddie's drawer, not the admin's own: View-as and Deactivate are
    # deliberately absent on yourself.
    drawer = next(d for d in body.split('class="usr-drawer"')[1:]
                  if "/admin/users/2/role" in d)
    for control in ("notify_channel", "Reset password", "View as", "Deactivate"):
        assert control in drawer


def test_you_cannot_change_your_own_role(env):
    drawers = env.get("/admin/users").text.split('class="usr-drawer"')
    boss = next(d for d in drawers[1:] if "/admin/users/1/role" in d)
    assert "disabled" in boss
    # And the guard is on the server too, not only in the markup.
    env.post("/admin/users/1/role", data={"role": "viewer"})
    with dbm.get_db() as con:
        assert con.execute("SELECT role FROM users WHERE id=1").fetchone()["role"] == "admin"
