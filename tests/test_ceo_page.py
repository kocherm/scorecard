"""The CEO metrics page (/ceo): the TV view's seven numbers as a page in the
sidebar, for everyone who can see the board - including slots that point into
a HIDDEN section, which is where companies tend to park profit and expenses."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import auth
from app import ceo as ceom
from app import db as dbm
from app import weeks as wk

NOW = datetime.now(timezone.utc)
CLOSED = wk.last_closed_week(NOW)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app

    with dbm.get_db() as con:
        dbm.init_db(con)
        dbm.set_setting(con, "display_token", "tok")
        con.execute("INSERT INTO sections (id, name, sort_order, is_enabled) VALUES (1,'Sales',0,1)")
        con.execute("INSERT INTO sections (id, name, sort_order, is_enabled) VALUES (2,'Money',1,0)")
        for uid, role in ((1, "admin"), (2, "editor"), (3, "viewer")):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role)
                   VALUES (?,?,'x',?,?)""", (uid, f"u{uid}@x.co", f"User {role}", role))
        ids = {}
        for sec, name, unit, direction in ((1, "New leads", None, "up"),
                                           (1, "New customers (conversions)", None, "up"),
                                           (2, "Revenue", "$", "up"),
                                           (2, "Expenses", "$", "down"),
                                           (2, "Profit", "$", "up"),
                                           (1, "New MRR", "$", "up")):
            ids[name] = con.execute(
                """INSERT INTO metrics (section_id, name, metric_type, rollup, direction,
                                        unit, dri_user_id, start_week)
                   VALUES (?,?,'numeric','sum',?,?,2,'2026-01-05')""",
                (sec, name, direction, unit)).lastrowid
        for name, v in (("Revenue", 10000), ("Expenses", 7000), ("Profit", 3000),
                        ("New leads", 40), ("New customers (conversions)", 4)):
            con.execute(
                """INSERT INTO entries (metric_id, week_start, value_numeric, source,
                                        entered_by_user_id) VALUES (?,?,?,'manual',1)""",
                (ids[name], CLOSED.isoformat(), v))
        sessions = {uid: auth.create_session(con, uid) for uid in (1, 2, 3)}

    def client(uid):
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, sessions[uid])
        return c
    client.ids = ids
    return client


def test_new_leads_and_new_customers_fill_their_slots_new_mrr_still_not_revenue(env):
    with dbm.get_db() as con:
        slots = ceom.resolve_slots(con)
    assert slots["leads"] == env.ids["New leads"]
    assert slots["conversions"] == env.ids["New customers (conversions)"]
    assert slots["revenue"] == env.ids["Revenue"]


def test_everyone_who_sees_the_board_gets_the_page_and_the_sidebar_entry(env):
    for uid in (1, 2, 3):
        html = env(uid).get("/ceo").text
        assert "<h1>CEO metrics</h1>" in html
        assert 'href="/ceo" class="nav-item active"' in html
    assert 'href="/ceo" class="nav-item' in env(3).get("/").text


def test_hidden_section_slots_read_on_the_page_and_the_tv(env):
    html = env(3).get("/ceo").text
    # Revenue, expenses and profit live in the hidden section.
    assert "$10,000" in html and "$7,000" in html and "$3,000" in html
    assert "30% margin" in html
    assert "10.0%" in html            # close rate: 4 of 40 leads
    # ...and still stay off the board itself.
    assert "Expenses" not in env(3).get("/").text
    tv = env(1).get("/display/body", params={"token": "tok", "view": "ceo"}).text
    assert "$10,000" in tv and "30% margin" in tv


def test_pencils_for_editors_not_viewers(env):
    assert 'hx-get="/quick/' in env(2).get("/ceo").text
    viewer = env(3).get("/ceo").text
    assert 'hx-get="/quick/' not in viewer and 'id="quickedit"' not in viewer


def test_saving_from_the_page_reloads_it_even_for_a_hidden_metric(env):
    c = env(2)
    pid = env.ids["Profit"]
    dlg = c.get(f"/quick/{pid}", params={"origin": "ceo"}).text
    assert 'name="origin" value="ceo"' in dlg
    r = c.post(f"/quick/{pid}/{CLOSED.isoformat()}", data={"value": "3500", "origin": "ceo"})
    assert r.status_code == 200 and r.headers.get("HX-Refresh") == "true"
    assert "$3,500" in c.get("/ceo").text
    # A bad number re-renders the dialog and keeps the origin.
    r = c.post(f"/quick/{pid}/{CLOSED.isoformat()}", data={"value": "lots", "origin": "ceo"})
    assert 'name="origin" value="ceo"' in r.text and "HX-Refresh" not in r.headers


def test_an_unknown_origin_is_ignored(env):
    dlg = env(2).get(f"/quick/{env.ids['New leads']}", params={"origin": "evil"}).text
    assert 'name="origin"' not in dlg


def test_show_on_tv_pins_the_view_even_when_it_is_not_in_the_rotation(env):
    c = env(1)
    assert "view=ceo" in c.get("/ceo").text
    with dbm.get_db() as con:
        assert (dbm.get_setting(con, "display_views") or "board") == "board"
    assert "CEO metrics" in c.get("/display/body", params={"token": "tok", "view": "ceo"}).text
    r = c.get("/tv", params={"view": "ceo"}, follow_redirects=False)
    assert r.headers["location"] == "/display?token=tok&view=ceo"
    assert c.get("/tv", params={"view": "nope"},
                 follow_redirects=False).headers["location"] == "/display?token=tok"
