"""Revenue per employee on /ceo: one number per CLOSED month - the Revenue
slot's metric rolled up by its own rollup over the weeks whose Monday falls
in the month, divided by the headcount in effect that month - with an
annualized view, its change on the month and the year before, and a
twelve-month trend. Headcount is a dated history in settings, carried
forward, never a weekly metric."""
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import auth
from app import ceo as ceom
from app import db as dbm
from app import weeks as wk

NOW = datetime.now(timezone.utc)
LAST = wk.last_closed_month(NOW)
PREV = wk.month_shift(LAST, -1)
YEAR_AGO = wk.month_shift(LAST, -12)


def ym_key(ym):
    return f"{ym[0]:04d}-{ym[1]:02d}"


# ---------------------------------------------------------------- pure parts
def test_a_month_is_the_mondays_in_it():
    assert wk.mondays_in_month(2026, 6) == [date(2026, 6, d) for d in (1, 8, 15, 22, 29)]
    assert wk.mondays_in_month(2026, 2) == [date(2026, 2, d) for d in (2, 9, 16, 23)]


def test_a_month_closes_when_its_last_week_does():
    # Thursday Oct 15 2026: the week of Oct 5 has closed, October has not.
    assert wk.last_closed_month(datetime(2026, 10, 15, 15, tzinfo=timezone.utc)) == (2026, 9)
    # Wednesday Oct 7: September's last week (Mon Sep 28) closed on Sunday Oct 4.
    assert wk.last_closed_month(datetime(2026, 10, 7, 15, tzinfo=timezone.utc)) == (2026, 9)
    # Thursday Oct 1: the week of Sep 28 is still running, so August.
    assert wk.last_closed_month(datetime(2026, 10, 1, 15, tzinfo=timezone.utc)) == (2026, 8)


def test_month_arithmetic_crosses_years():
    assert wk.month_shift((2026, 1), -1) == (2025, 12)
    assert wk.month_shift((2025, 12), 13) == (2027, 1)
    assert wk.months_ending((2026, 2), 3) == [(2025, 12), (2026, 1), (2026, 2)]


def test_headcount_carries_forward_from_the_month_it_took_effect():
    hist = [("2026-01", 10.0), ("2026-06", 14.5)]
    assert ceom.headcount_for(hist, (2025, 12)) is None
    assert ceom.headcount_for(hist, (2026, 1)) == 10
    assert ceom.headcount_for(hist, (2026, 5)) == 10
    assert ceom.headcount_for(hist, (2026, 6)) == 14.5
    assert ceom.headcount_for(hist, (2027, 3)) == 14.5


# ---------------------------------------------------------------- the page
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app

    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order, is_enabled) VALUES (1,'Money',0,0)")
        for uid, role in ((1, "admin"), (3, "viewer")):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role)
                   VALUES (?,?,'x',?,?)""", (uid, f"u{uid}@x.co", f"User {role}", role))
        rev = con.execute(
            """INSERT INTO metrics (section_id, name, metric_type, rollup, direction,
                                    unit, dri_user_id, start_week)
               VALUES (1,'Revenue','numeric','sum','up','$',1,'2020-01-06')""").lastrowid
        sessions = {uid: auth.create_session(con, uid) for uid in (1, 3)}

    def client(uid=1):
        c = TestClient(app)
        c.cookies.set(auth.SESSION_COOKIE, sessions[uid])
        return c
    client.rev = rev
    return client


def fill_month(rev, ym, weekly, skip=0):
    """`weekly` revenue in every week of the month except the last `skip`."""
    mondays = wk.mondays_in_month(*ym)
    with dbm.get_db() as con:
        for d in mondays[:len(mondays) - skip]:
            con.execute(
                """INSERT OR REPLACE INTO entries (metric_id, week_start, value_numeric,
                                                   source, entered_by_user_id)
                   VALUES (?,?,?,'manual',1)""", (rev, d.isoformat(), weekly))
    return len(mondays) - skip


def headcount(month, n):
    with dbm.get_db() as con:
        ceom.set_headcount(con, month, str(n))


def money(v):
    return f"${v:,.0f}"


def test_without_a_headcount_the_section_asks_for_one(env):
    fill_month(env.rev, LAST, 1000)
    html = env().get("/ceo").text
    assert "Revenue per employee" in html
    assert "Needs a headcount" in html
    assert 'href="/admin/settings?tab=display#ceo-headcount"' in html
    assert "An admin can add it" in env(3).get("/ceo").text


def test_monthly_and_annualized_read_the_last_closed_month(env):
    fill_month(env.rev, LAST, 12000)
    fill_month(env.rev, PREV, 8000)
    headcount(ym_key(PREV), 4)
    month = 12000 * 52 / 12          # the average week x 4.33
    html = env().get("/ceo").text
    assert date(*LAST, 1).strftime("%B %Y") in html
    assert money(month / 4) in html and "per employee per month" in html
    assert f"{money(month)} revenue &divide; 4 employees" in html

    html = env().get("/ceo?rpe=year").text
    assert money(12000 * 52 / 4) in html and "per employee per year" in html
    assert "&times; 12" in html and "run rate" in html
    assert 'href="/ceo?rpe=year#rpe" class="on"' in html


def test_change_on_the_month_and_the_year_before(env):
    fill_month(env.rev, LAST, 1000)
    fill_month(env.rev, PREV, 1000)
    fill_month(env.rev, YEAR_AGO, 1000)
    headcount(ym_key(YEAR_AGO), 2)
    headcount(ym_key(PREV), 2)
    headcount(ym_key(LAST), 1)   # same revenue, half the team
    html = env().get("/ceo").text
    assert "+100.0%" in html and "on the month before" in html
    assert "+100.0%</span> <span class=\"note\">on a year ago" in html


def test_five_monday_months_do_not_read_high(env):
    # Same weekly revenue in a four- and a five-Monday month: same figure.
    with dbm.get_db() as con:
        con.execute("UPDATE metrics SET start_week = '2000-01-03'")
    headcount("2000-01", 1)
    fill_month(env.rev, LAST, 1000)
    fill_month(env.rev, PREV, 1000)
    html = env().get("/ceo").text
    assert "+0.0%</span> <span class=\"note\">on the month before" in html
    assert money(1000 * 52 / 12) in html


def test_a_month_missing_a_week_is_estimated_and_says_so(env):
    entered = fill_month(env.rev, LAST, 1000, skip=1)
    headcount(ym_key(LAST), 1)
    html = env().get("/ceo").text
    assert f"{entered} of {entered + 1} weeks entered" in html
    assert "rpe-col latest partial" in html
    assert "estimated from the weeks entered" in html
    assert money(1000 * 52 / 12) in html   # scaled up, not read low


def test_the_month_in_progress_is_left_out(env):
    cur = wk.month_shift(LAST, 1)
    headcount(ym_key(LAST), 1)
    fill_month(env.rev, cur, 99999)
    assert "No closed month has both revenue and a headcount" in env().get("/ceo").text


def test_a_mrr_metric_averages_instead_of_summing(env):
    with dbm.get_db() as con:
        con.execute("UPDATE metrics SET rollup = 'average', name = 'MRR' WHERE id = ?", (env.rev,))
    fill_month(env.rev, LAST, 30000)
    headcount(ym_key(LAST), 3)
    assert money(10000) in env().get("/ceo").text


def test_admin_adds_and_removes_headcount_in_settings(env):
    c = env()
    r = c.post("/admin/settings/ceo-headcount/add",
               data={"month": "2026-03", "headcount": "12.5"}, follow_redirects=False)
    assert r.status_code == 303 and "saved=ceo-headcount#ceo-headcount" in r.headers["location"]
    html = c.get("/admin/settings?tab=display").text
    assert "<b>12.5</b>" in html and "from Mar 2026" in html
    r = c.post("/admin/settings/ceo-headcount/add",
               data={"month": "2026-03", "headcount": "0"}, follow_redirects=False)
    assert "hc_err=" in r.headers["location"]
    c.post("/admin/settings/ceo-headcount/remove", data={"month": "2026-03"})
    with dbm.get_db() as con:
        assert ceom.headcount_history(con) == []


def test_only_admins_edit_headcount(env):
    r = env(3).post("/admin/settings/ceo-headcount/add",
                    data={"month": "2026-03", "headcount": "5"}, follow_redirects=False)
    assert r.status_code in (303, 401, 403)
    with dbm.get_db() as con:
        assert ceom.headcount_history(con) == []
