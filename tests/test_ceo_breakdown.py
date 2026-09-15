"""Breaking a CEO tile down into categories: expenses by category, written by a
person on the page or by an automation in one API call, and a panel that says
when the parts stop adding up to the total."""
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
        con.execute("INSERT INTO sections (id, name, sort_order, is_enabled) VALUES (1,'Money',0,0)")
        for uid, role in ((1, "admin"), (2, "editor"), (3, "viewer")):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role)
                   VALUES (?,?,'x',?,?)""", (uid, f"u{uid}@x.co", f"User {role}", role))
        exp = con.execute(
            """INSERT INTO metrics (section_id, name, metric_type, rollup, direction, unit,
                                    dri_user_id, start_week)
               VALUES (1,'Expenses','numeric','sum','down','$',2,'2026-01-05')""").lastrowid
        write_tok = auth.new_api_token(con, "books", "write", 1)
        read_tok = auth.new_api_token(con, "reader", "read", 1)
        sessions = {uid: auth.create_session(con, uid) for uid in (1, 2, 3)}

    def client(uid=None):
        c = TestClient(app)
        if uid:
            c.cookies.set(auth.SESSION_COOKIE, sessions[uid])
        return c
    client.expenses = exp
    client.write = {"Authorization": f"Bearer {write_tok}"}
    client.read = {"Authorization": f"Bearer {read_tok}"}
    return client


def add(c, name, slot="expenses"):
    return c.post(f"/admin/settings/ceo-breakdown/{slot}/add", data={"name": name},
                  follow_redirects=False)


def cats(slot="expenses"):
    with dbm.get_db() as con:
        return [(i, con.execute("SELECT * FROM metrics WHERE id = ?", (i,)).fetchone())
                for i in ceom.breakdown_ids(con, slot)]


def value(mid, week=CLOSED):
    with dbm.get_db() as con:
        r = con.execute("SELECT value_numeric FROM entries WHERE metric_id = ? AND week_start = ?",
                        (mid, week.isoformat())).fetchone()
    return r and r["value_numeric"]


def three(env):
    admin = env(1)
    for n in ("Tax", "Contractors", "Other"):
        assert add(admin, n).status_code == 303
    return admin


# ---------------------------------------------------------------- categories
def test_a_category_is_an_ordinary_metric_shaped_like_its_tile(env):
    three(env)
    got = cats()
    assert [m["name"] for _, m in got] == ["Tax", "Contractors", "Other"]
    for _, m in got:
        assert (m["section_id"], m["unit"], m["direction"], m["rollup"], m["dri_user_id"]) \
            == (1, "$", "down", "sum", 2)


def test_duplicate_and_unmapped_categories_are_refused(env):
    admin = three(env)
    r = add(admin, "tax")
    assert "bd_err=" in r.headers["location"] and len(cats()) == 3
    r = add(admin, "Referrals", slot="leads")     # no Leads metric exists
    assert "bd_err=" in r.headers["location"]
    r = add(admin, "Anything", slot="cac")        # CAC does not sum
    assert "bd_err=" in r.headers["location"]


def test_removing_unlinks_but_keeps_the_metric(env):
    admin = three(env)
    before = [i for i, _ in cats()]
    tax = before[0]
    admin.post("/admin/settings/ceo-breakdown/expenses/remove", data={"metric_id": tax})
    assert [i for i, _ in cats()] == before[1:]
    with dbm.get_db() as con:
        assert con.execute("SELECT archived_at FROM metrics WHERE id = ?", (tax,)).fetchone()[0] is None


def test_only_admins_manage_categories(env):
    add(env(2), "Tax")
    add(env(3), "Tax")
    assert cats() == []


# ----------------------------------------------------------------------- API
def test_api_lists_slots_with_their_categories(env):
    three(env)
    body = env().get("/api/v1/ceo", headers=env.read).json()
    exp = next(s for s in body["slots"] if s["slot"] == "expenses")
    assert exp["metric_id"] == env.expenses
    assert [c["name"] for c in exp["breakdown"]] == ["Tax", "Contractors", "Other"]
    assert "breakdown" not in next(s for s in body["slots"] if s["slot"] == "cac")


def test_one_call_writes_every_category_and_the_summed_total(env):
    three(env)
    ids = [i for i, _ in cats()]
    r = env().post("/api/v1/ceo/expenses/breakdown", headers=env.write, json={
        "categories": {"Tax": 2000, "contractors": 3500, str(ids[2]): 500}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["week_start"] == CLOSED.isoformat()
    assert out["missing"] == [] and out["total_written"] == 6000
    assert [value(i) for i in ids] == [2000, 3500, 500]
    assert value(env.expenses) == 6000
    with dbm.get_db() as con:
        src = con.execute("SELECT source, entered_by_token_id FROM entries WHERE metric_id = ?",
                          (env.expenses,)).fetchone()
    assert src["source"] == "api" and src["entered_by_token_id"]


def test_a_partial_week_writes_categories_but_not_a_short_total(env):
    three(env)
    out = env().post("/api/v1/ceo/expenses/breakdown", headers=env.write,
                     json={"categories": {"Tax": 2000}}).json()
    assert out["total_written"] is None and out["missing"] == ["Contractors", "Other"]
    assert value(env.expenses) is None


def test_explicit_or_no_total(env):
    three(env)
    c = env()
    c.post("/api/v1/ceo/expenses/breakdown", headers=env.write,
           json={"categories": {"Tax": 1}, "total": 9999})
    assert value(env.expenses) == 9999
    c.post("/api/v1/ceo/expenses/breakdown", headers=env.write,
           json={"categories": {"Tax": 2, "Contractors": 2, "Other": 2}, "total": None})
    assert value(env.expenses) == 9999


def test_an_unknown_name_writes_nothing_and_lists_the_valid_ones(env):
    three(env)
    r = env().post("/api/v1/ceo/expenses/breakdown", headers=env.write,
                   json={"categories": {"Tax": 2000, "Software": 99}})
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["unknown"] == ["Software"]
    assert detail["categories"] == ["Tax", "Contractors", "Other"]
    assert all(value(i) is None for i, _ in cats()) and value(env.expenses) is None


def test_api_refusals(env):
    c = env()
    r = c.post("/api/v1/ceo/expenses/breakdown", headers=env.write, json={"categories": {"x": 1}})
    assert r.status_code == 409                                    # no categories yet
    three(env)
    assert c.post("/api/v1/ceo/expenses/breakdown", headers=env.read,
                  json={"categories": {"Tax": 1}}).status_code == 403
    assert c.post("/api/v1/ceo/cac/breakdown", headers=env.write,
                  json={"categories": {"Tax": 1}}).status_code == 404
    future = (wk.current_week(NOW).toordinal() + 7)
    from datetime import date
    assert c.post("/api/v1/ceo/expenses/breakdown", headers=env.write, json={
        "week_start": date.fromordinal(future).isoformat(),
        "categories": {"Tax": 1}}).status_code == 422
    assert c.post("/api/v1/ceo/expenses/breakdown", headers=env.write,
                  json={"categories": {"Tax": 1}, "total": "lots"}).status_code == 422


# ---------------------------------------------------------------------- page
def test_page_panel_shows_shares_and_flags_a_total_that_disagrees(env):
    three(env)
    env().post("/api/v1/ceo/expenses/breakdown", headers=env.write,
               json={"categories": {"Tax": 2000, "Contractors": 1500, "Other": 500},
                     "total": 5000})
    html = env(3).get("/ceo").text
    assert 'id="bd-expenses"' in html and "3 categories" in html
    assert "don&rsquo;t add up" in html
    assert ">50%<" in html and ">38%<" in html and ">12%<" in html
    assert "Categories add up to <b>$4,000</b>" in html
    assert "Set expenses to" not in html                           # viewer: no button
    editor = env(2).get("/ceo").text
    assert 'action="/ceo/expenses/total"' in editor


def test_use_the_sum_writes_the_total_and_returns_to_the_panel(env):
    three(env)
    env().post("/api/v1/ceo/expenses/breakdown", headers=env.write,
               json={"categories": {"Tax": 2000, "Contractors": 1500, "Other": 500},
                     "total": 5000})
    r = env(2).post("/ceo/expenses/total", data={"week": CLOSED.isoformat()},
                    follow_redirects=False)
    assert r.headers["location"] == "/ceo#bd-expenses"
    assert value(env.expenses) == 4000
    assert "don&rsquo;t add up" not in env(2).get("/ceo").text


def test_use_the_sum_refuses_an_incomplete_week(env):
    three(env)
    env().post("/api/v1/ceo/expenses/breakdown", headers=env.write,
               json={"categories": {"Tax": 2000}})
    r = env(2).post("/ceo/expenses/total", data={"week": CLOSED.isoformat()})
    assert r.status_code == 422 and value(env.expenses) is None
    assert env(3).post("/ceo/expenses/total", data={"week": CLOSED.isoformat()}).status_code == 403


def test_the_tv_view_is_unchanged_by_a_breakdown(env):
    three(env)
    tv = env().get("/display/body", params={"token": "tok", "view": "ceo"}).text
    assert "categories" not in tv and "bd-" not in tv
