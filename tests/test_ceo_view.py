"""The "7 metrics every CEO should track" template and its TV view.

The template is seven ORDINARY metrics created at once and a slot mapping in
settings; the view is an arrangement of the same BoardRows the board renders.
These tests pin the promises that matter to an admin: one click adds what is
missing and never duplicates what exists, the view stays out of the rotation
until it has something to show, and the derived ratios only combine numbers
from the same week.
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import auth
from app import ceo as ceom
from app import db as dbm
from app import weeks as wk

NOW = datetime.now(timezone.utc)
CLOSED = wk.last_closed_week(NOW)
CUR = wk.current_week(NOW)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app

    with dbm.get_db() as con:
        dbm.init_db(con)
        dbm.set_setting(con, "display_token", "tok")
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role)
               VALUES (1,'a@x.co','x','Ada Lovelace','admin')""")
        sess = auth.create_session(con, 1)
    c = TestClient(app)
    c.cookies.set(auth.SESSION_COOKIE, sess)
    return c


def body(client, **params):
    params.setdefault("token", "tok")
    return client.get("/display/body", params=params).text


def setting(key):
    with dbm.get_db() as con:
        return dbm.get_setting(con, key)


def install(client):
    return client.post("/admin/metrics/templates/ceo", follow_redirects=False)


def add_metric(con, name, section=1, unit="$", rollup="sum", direction="up",
               start="2026-01-05"):
    cur = con.execute(
        """INSERT INTO metrics (section_id, name, metric_type, rollup, direction,
                                unit, start_week)
           VALUES (?,?,?,?,?,?,?)""",
        (section, name, "numeric", rollup, direction, unit, start))
    return cur.lastrowid


def enter(con, mid, week, value):
    con.execute(
        """INSERT INTO entries (metric_id, week_start, value_numeric, source,
                                entered_by_user_id) VALUES (?,?,?,'manual',1)""",
        (mid, week.isoformat(), value))


def target(con, mid, value):
    y, q = wk.quarter_of(CUR)
    con.execute(
        """INSERT INTO targets (metric_id, year, quarter, baseline_value, stretch_value)
           VALUES (?,?,?,?,?)""", (mid, y, q, value, value))


# ------------------------------------------------------------- the template
def test_one_click_adds_seven_ordinary_metrics_and_leaves_the_tv_alone(env):
    r = install(env)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/metrics?template=ceo&created=7&skipped=0"
    with dbm.get_db() as con:
        rows = con.execute(
            """SELECT m.name, m.rollup, m.direction, m.unit, m.dri_user_id, m.start_week
               FROM metrics m JOIN sections s ON s.id = m.section_id
               WHERE s.name = ? ORDER BY m.sort_order""", (ceom.SECTION_NAME,)).fetchall()
    assert [m["name"] for m in rows] == [
        "Revenue", "Expenses", "Leads", "Conversions", "CAC", "Retention", "Profit"]
    by = {m["name"]: m for m in rows}
    # Point-in-time ratios average so the in-progress week is not paced;
    # money out and cost per customer score lower-is-better.
    assert by["CAC"]["rollup"] == "average" and by["Retention"]["rollup"] == "average"
    assert by["Expenses"]["direction"] == "down" and by["CAC"]["direction"] == "down"
    assert by["Retention"]["unit"] == "%" and by["Leads"]["unit"] is None
    assert all(m["dri_user_id"] == 1 for m in rows), "owned by whoever pressed the button"
    assert all(m["start_week"] == CUR.isoformat() for m in rows)
    # Opt-in on the TV: adding metrics never changes what the wall shows.
    assert (setting("display_views") or "board") == "board"
    for s in ceom.SLOTS:
        assert setting(ceom.setting_key(s.key)), f"{s.key} slot left unmapped"
    page = env.get("/admin/metrics?template=ceo&created=7&skipped=0").text
    assert "Added 7 CEO metrics" in page


def test_a_metric_you_already_track_is_mapped_not_duplicated(env):
    with dbm.get_db() as con:
        rid = add_metric(con, "Revenue")
    r = install(env)
    assert r.headers["location"].endswith("created=6&skipped=1")
    assert setting("ceo_slot_revenue") == str(rid)
    with dbm.get_db() as con:
        n = con.execute("SELECT COUNT(*) AS n FROM metrics WHERE name = 'Revenue'").fetchone()["n"]
    assert n == 1


def test_installing_twice_adds_nothing(env):
    install(env)
    r = install(env)
    assert r.headers["location"].endswith("created=0&skipped=7")
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) AS n FROM metrics").fetchone()["n"] == 7
        assert con.execute("SELECT COUNT(*) AS n FROM sections WHERE name = ?",
                           (ceom.SECTION_NAME,)).fetchone()["n"] == 1


def test_installing_keeps_the_views_already_in_rotation(env):
    with dbm.get_db() as con:
        dbm.set_setting(con, "display_views", "board,key")
    install(env)
    assert setting("display_views") == "board,key"


# --------------------------------------------------------------- resolving
def test_name_fallback_prefers_the_exact_name_and_never_takes_a_new_flow(env):
    with dbm.get_db() as con:
        add_metric(con, "New MRR")
        mrr = add_metric(con, "MRR")
        add_metric(con, "Monthly costs", direction="down")
        slots = ceom.resolve_slots(con)
    assert slots["revenue"] == mrr
    assert slots["expenses"] is not None
    assert slots["leads"] is None


def test_an_explicit_slot_wins_and_a_dangling_one_is_stored_as_auto(env):
    with dbm.get_db() as con:
        a = add_metric(con, "Revenue")
        b = add_metric(con, "Bookings")
    r = env.post("/admin/settings/ceo-view",
                 data={"slot_revenue": str(b), "slot_leads": "999"},
                 follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/settings?tab=display&saved=ceo-view#ceo-view"
    assert setting("ceo_slot_revenue") == str(b)
    assert setting("ceo_slot_leads") == ""
    with dbm.get_db() as con:
        assert ceom.resolve_slots(con)["revenue"] == b
    page = env.get("/admin/settings?tab=display").text
    assert 'id="ceo-view"' in page
    assert f'value="{b}" selected' in page


# ------------------------------------------------------------------ the view
def test_the_view_stays_out_of_the_rotation_until_a_slot_is_mapped(env):
    with dbm.get_db() as con:
        dbm.set_setting(con, "display_views", "ceo")
        dbm.set_setting(con, "display_rotate_seconds", "30")
    assert "Company Scorecard" in body(env)
    assert "Growth engine" not in body(env, view="ceo")


def seed_full_week(con, values: dict, targets: dict):
    """Install, then give every slot a number for the last closed week and a
    target for the quarter. start_week is moved back: a metric created this
    week has no scored history yet, same as one added by hand."""
    res = ceom.install_template(con, NOW, dri_user_id=1)
    assert len(res.created) == 7
    # Nothing enables the view but Settings > TV views; do it as the admin would.
    dbm.set_setting(con, "display_views", "board,ceo")
    con.execute("UPDATE metrics SET start_week = '2026-01-05'")
    ids = {k: int(dbm.get_setting(con, ceom.setting_key(k))) for k in values}
    for k, v in values.items():
        enter(con, ids[k], CLOSED, v)
        target(con, ids[k], targets[k])
    return ids


def test_the_view_shows_the_seven_with_ratios_derived_at_render_time(env):
    with dbm.get_db() as con:
        seed_full_week(
            con,
            {"leads": 100, "conversions": 8, "revenue": 20000, "expenses": 14000,
             "profit": 6000, "cac": 500, "retention": 92},
            {"leads": 80, "conversions": 10, "revenue": 18000, "expenses": 15000,
             "profit": 5000, "cac": 600, "retention": 90})
    html = body(env, view="ceo")
    for label in ("Growth engine", "Unit economics", "Bottom line",
                  "Leads", "Conversions", "Revenue", "Expenses", "CAC", "Retention", "Profit"):
        assert label in html
    assert "8.0%" in html and "close rate" in html          # 8 / 100
    assert "$2,500" in html and "per new customer" in html  # 20000 / 8
    assert "30% margin" in html                             # 6000 / 20000
    assert "92%" in html                                    # the % unit formats
    assert "on budget" in html                          # expenses under target
    assert "of target" in html
    assert 'class="vc-num yellow"' in html                  # conversions 8 vs 10
    assert "Not tracked" not in html
    assert "Ada" in html                                    # the owner, on every tile


def test_ratios_never_mix_weeks(env):
    with dbm.get_db() as con:
        ids = seed_full_week(
            con,
            {"leads": 100, "conversions": 8, "revenue": 20000, "expenses": 14000,
             "profit": 6000, "cac": 500, "retention": 92},
            {"leads": 80, "conversions": 10, "revenue": 18000, "expenses": 15000,
             "profit": 5000, "cac": 600, "retention": 90})
        # This week's leads are in; this week's conversions are not, so the
        # tile falls back to last week's - a close rate across the two would
        # be a number nobody asked for.
        enter(con, ids["leads"], CUR, 40)
    html = body(env, view="ceo")
    # The connector stays, its ratio does not: neither 8/100 nor 8/40.
    assert "close rate" not in html and "8.0%" not in html and "20.0%" not in html
    assert "$2,500" in html and "per new customer" in html  # revenue & conversions still agree


def test_an_unmapped_slot_is_a_hint_on_a_live_board(env):
    with dbm.get_db() as con:
        rid = add_metric(con, "Revenue")
        enter(con, rid, CLOSED, 1000)
        dbm.set_setting(con, "display_views", "ceo")
    html = body(env, view="ceo")
    assert "CEO metrics" in html
    assert html.count("Not tracked") == 6
    assert "$1,000" in html


def test_the_settings_panel_lists_every_slot_with_its_detected_metric(env):
    with dbm.get_db() as con:
        add_metric(con, "Revenue")
    page = env.get("/admin/settings?tab=display").text
    for s in ceom.SLOTS:
        assert f'name="slot_{s.key}"' in page
    assert "currently Revenue" in page
    assert "no match yet" in page
