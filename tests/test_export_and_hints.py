"""Three ways the board explains itself: a readiness warning when a quarter has
no targets (which silently stops every alert), the "why is this red?" sentence
on each cell, and CSV exports that score exactly as the board does.

Page-level fixtures are built inside the quarter of wk.last_closed_week(now),
with targets for the previous week's quarter too, so they hold in a quarter's
first weeks (CLAUDE.md, Gotchas). Pure checks take a fixed `now`."""
import csv
import io
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import auth
from app import db as dbm
from app import export as exportm
from app import grid as gridm
from app import readiness
from app import weeks as wk

NOW = datetime.now(timezone.utc)
D = wk.last_closed_week(NOW)          # the due week
PREV = D - timedelta(days=7)


def _target(con, mid, week, value):
    y, q = wk.quarter_of(week)
    con.execute("""INSERT OR REPLACE INTO targets (metric_id, year, quarter,
                   baseline_value, stretch_value) VALUES (?,?,?,?,?)""",
                (mid, y, q, value, value))


def _metric(con, mid, section, name, mtype="numeric", start="2026-01-05",
            dri=2, unit=None, archived=None):
    con.execute(
        """INSERT INTO metrics (id, section_id, name, metric_type, rollup, direction,
                                unit, dri_user_id, start_week, archived_at)
           VALUES (?,?,?,?,?,'up',?,?,?,?)""",
        (mid, section, name, mtype, "sum" if mtype == "numeric" else None,
         unit, dri, start, archived))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app

    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        con.execute("INSERT INTO sections (id, name, sort_order, is_enabled) "
                    "VALUES (2,'Secret',1,0)")
        for uid, name, role in ((1, "Boss", "admin"), (2, "@Eddie", "editor"),
                                (3, "Val", "viewer")):
            con.execute("""INSERT INTO users (id, email, password_hash, display_name, role)
                           VALUES (?,?,'x',?,?)""", (uid, f"u{uid}@x.co", name, role))
        start = (PREV - timedelta(days=7)).isoformat()
        _metric(con, 1, 1, "Calls", start=start)
        _metric(con, 2, 1, "Client A", mtype="status", start=start)
        _metric(con, 3, 1, '=HYPERLINK("http://x","y")', start=start)
        _metric(con, 4, 2, "Hidden number", start=start)
        for mid in (1, 3, 4):
            for w in (PREV, D):
                _target(con, mid, w, 10)
        for w, v in ((PREV, 2.0), (D, 3.0)):          # two red weeks
            dbm.upsert_entry(con, 1, w, value_numeric=v, source="manual", user_id=2)
        dbm.upsert_entry(con, 2, D, value_status="R", source="manual", user_id=2)
        dbm.upsert_entry(con, 3, D, value_numeric=-5.0, source="manual", user_id=2)
        dbm.upsert_entry(con, 4, D, value_numeric=99.0, source="manual", user_id=2)
        sessions = {uid: auth.create_session(con, uid) for uid in (1, 3)}

    def client(uid=3):
        c = TestClient(app)
        if uid:
            c.cookies.set(auth.SESSION_COOKIE, sessions[uid])
        return c
    return client


def rows_of(resp):
    return list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))))


# ------------------------------------------------------------- readiness
def _readiness_db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "r.db"))
    con = dbm.connect()
    dbm.init_db(con)
    con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
    con.execute("INSERT INTO sections (id, name, sort_order, is_enabled) VALUES (2,'Off',1,0)")
    con.execute("""INSERT INTO users (id, email, password_hash, display_name, role)
                   VALUES (2,'e@x.co','x','Eddie','editor')""")
    _metric(con, 1, 1, "Calls")
    _metric(con, 2, 1, "Client A", mtype="status")      # status: no target needed
    _metric(con, 3, 1, "Shipped", mtype="binary")       # binary: no target needed
    _metric(con, 4, 2, "Hidden")                        # hidden section
    _metric(con, 5, 1, "Gone", archived="2026-02-02")   # archived
    _metric(con, 6, 1, "Later", start="2027-04-05")     # starts after Q4 and Q1
    return con


def _checks(con, now):
    return {c.key: c for c in readiness.local_checks(con, now)}


def test_current_quarter_without_targets_warns_and_links_to_that_quarter(tmp_path, monkeypatch):
    con = _readiness_db(tmp_path, monkeypatch)
    now = datetime(2026, 10, 15, 15, tzinfo=timezone.utc)
    c = _checks(con, now)
    t = c["targets"]
    assert t.state == readiness.WARN
    assert t.detail.startswith("1 metric has no Q4 2026 target.")
    assert "no colour" in t.detail and "red streaks" in t.detail
    assert t.fix_url == "/admin/targets?year=2026&quarter=4"
    assert [i["name"] for i in t.items] == ["Calls"]
    assert "targets_next" not in c, "Q1 2027 is 81 days off - not yet"

    con.execute("INSERT INTO targets (metric_id, year, quarter, baseline_value, "
                "stretch_value) VALUES (1, 2026, 4, 5, 8)")
    assert _checks(con, now)["targets"].state == readiness.OK
    con.close()


def test_next_quarter_warns_inside_fourteen_days(tmp_path, monkeypatch):
    con = _readiness_db(tmp_path, monkeypatch)
    now = datetime(2026, 12, 22, 15, tzinfo=timezone.utc)   # Q1 2027 starts Jan 4
    nxt = _checks(con, now)["targets_next"]
    assert nxt.state == readiness.WARN
    assert "1 metric has no Q1 2027 target - it starts in 13 days" in nxt.detail
    assert nxt.fix_url == "/admin/targets?year=2027&quarter=1"

    con.execute("INSERT INTO targets (metric_id, year, quarter, baseline_value, "
                "stretch_value) VALUES (1, 2027, 1, 5, 8)")
    assert _checks(con, now)["targets_next"].state == readiness.OK
    con.close()


# ------------------------------------------------------------- cell hints
def _td(html, mid, week):
    m = re.search(rf'<td id="cell-{mid}-{week.isoformat()}"[^>]*>', html)
    assert m, f"no cell {mid}/{week}"
    return m.group(0)


def test_red_cell_says_what_it_was_compared_with_and_the_streak(env):
    html = env().get("/").text
    td = _td(html, 1, D)
    hint = "3 vs target 10 (30%) · red · week 2 red in a row"
    assert f'title="{hint}"' in td
    assert f'aria-label="Calls, {wk.quarter_label(D)}: {hint}"' in td
    assert "first red week" in _td(html, 1, PREV)


def test_status_cell_just_says_the_status(env):
    td = _td(env().get("/").text, 2, D)
    assert "status set to red by the owner · first red week" in td
    assert "vs target" not in td


def test_export_links_are_on_the_board_and_the_metric_page(env):
    assert 'href="/export.csv"' in env().get("/").text
    assert 'href="/m/1.csv"' in env().get("/m/1").text


# ------------------------------------------------------------- CSV
def test_board_csv_matches_the_board_and_leaves_hidden_sections_out(env):
    r = env().get("/export.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert re.fullmatch(r'attachment; filename="scorecard-\d{4}-\d\d-\d\d-to-'
                        r'\d{4}-\d\d-\d\d\.csv"', r.headers["content-disposition"])
    rows = rows_of(r)
    assert list(rows[0]) == exportm.BOARD_HEADER
    assert not any(x["section"] == "Secret" or x["metric"] == "Hidden number" for x in rows)

    calls = {x["week_start"]: x for x in rows if x["metric"] == "Calls"}
    assert calls[D.isoformat()] == {
        "section": "Sales", "metric": "Calls", "owner": "'@Eddie",
        "week_start": D.isoformat(), "value": "3", "target": "10",
        "state": "red", "unit": ""}
    assert calls[PREV.isoformat()]["state"] == "red"
    assert {x["state"] for x in rows if x["metric"] == "Client A"} >= {"red"}

    # Same states as the board, cell for cell.
    with dbm.get_db() as con:
        vm = gridm.build_grid(con, datetime.now(timezone.utc))
    board = {(r_.name, c.week.isoformat()): c.state.value
             for s in vm.sections for r_ in s.rows for c in r_.cells}
    for x in rows:
        name = x["metric"].lstrip("'")
        assert board[(name, x["week_start"])] == x["state"]


def test_csv_guards_against_formula_injection_but_keeps_numbers(env):
    rows = rows_of(env().get("/export.csv"))
    evil = [x for x in rows if "HYPERLINK" in x["metric"]]
    assert evil and all(x["metric"].startswith("'=HYPERLINK") for x in evil)
    assert all(x["owner"] == "'@Eddie" for x in evil)
    assert next(x for x in evil if x["week_start"] == D.isoformat())["value"] == "-5"
    assert exportm.safe_text("+1") == "'+1" and exportm.safe_text("-x") == "'-x"
    assert exportm.safe_text("plain") == "plain"


def test_metric_csv_is_the_whole_history(env):
    r = env().get("/m/1.csv")
    assert r.status_code == 200
    assert r.headers["content-disposition"] == 'attachment; filename="calls-history.csv"'
    rows = rows_of(r)
    assert list(rows[0]) == exportm.HISTORY_HEADER
    by = {x["week_start"]: x for x in rows}
    assert by[D.isoformat()]["value"] == "3" and by[D.isoformat()]["state"] == "red"
    assert by[D.isoformat()]["source"] == "manual"
    assert by[D.isoformat()]["entered_by"] == "'@Eddie"
    assert by[D.isoformat()]["entered_at"]
    assert env().get("/m/999.csv").status_code == 404
    assert env().get("/m/1").status_code == 200


def test_history_scores_each_week_on_its_own_quarters_target(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "h.db"))
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        con.execute("""INSERT INTO users (id, email, password_hash, display_name, role)
                       VALUES (2,'e@x.co','x','Eddie','editor')""")
        _metric(con, 1, 1, "Calls", start="2026-09-21")
        con.execute("INSERT INTO targets (metric_id, year, quarter, baseline_value, "
                    "stretch_value) VALUES (1,2026,3,30,30), (1,2026,4,20,20)")
        for w, v in (("2026-09-21", 30.0), ("2026-09-28", 21.0), ("2026-10-05", 21.0)):
            dbm.upsert_entry(con, 1, date.fromisoformat(w), value_numeric=v,
                             source="manual", user_id=2)
        m = con.execute("SELECT * FROM metrics WHERE id = 1").fetchone()
        rows = exportm.history_rows(con, m, datetime(2026, 10, 15, 15, tzinfo=timezone.utc))
    assert [(r[0], r[2], r[3]) for r in rows] == [
        ("2026-09-21", "30", "green"),
        ("2026-09-28", "30", "yellow"),     # 21 of Q3's 30
        ("2026-10-05", "20", "green"),      # the same 21 against Q4's 20
        ("2026-10-12", "20", "pending"),
    ]


def test_a_viewer_can_export_and_an_anonymous_visitor_cannot(env):
    assert env(3).get("/export.csv").status_code == 200
    assert env(3).get("/m/1.csv").status_code == 200
    for path in ("/export.csv", "/m/1.csv"):
        r = env(None).get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert "text/csv" not in r.headers.get("content-type", "")
