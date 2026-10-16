"""Data-correctness fixes, each pinned to a FIXED date so none of them follows
the calendar (see CLAUDE.md > Gotchas):

1. /m/{id} counts its red streak with each week's OWN quarter target, so the
   page and the board can never disagree across a quarter boundary.
2. A target shown beside a number is that number's week's target - at a
   quarter start and at quarter-week 7 (baseline -> stretch) - on the grid,
   the TV/CEO rows, the act cards and the API.
3. Admin > Targets' last-quarter mean skips the in-progress week.
4. display_months=1 cannot empty the grid window.
5. The admin edit form refuses a type change on a metric with entries and a
   duplicate live name, and a mismatched entry scores as no data instead of
   taking the board down.
6. "+10%" on a lower-is-better target lowers it.

Fixture quarters: Q3 2026 runs Jul 6 - Sep 28 (13 Mondays), Q4 2026 starts
Monday Oct 5; Q4 W6 is Nov 9 and W7 is Nov 16."""
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import admin_ops
from app import db as dbm
from app import grid as gridm
from app import scoring as sc
from app import weeks as wk
from app.api import build_scorecard
from app.auth import hash_password

PW = "a-fine-password-123"
TZ = wk.BUSINESS_TZ
Q3_START = date(2026, 7, 6)
Q3_LAST = date(2026, 9, 28)          # Q3 W13
Q4_START = date(2026, 10, 5)         # Q4 W1


def at(y, m, d, h=12):
    return datetime(y, m, d, h, 0, tzinfo=TZ)


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    with dbm.get_db() as c:
        dbm.init_db(c)
        c.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        c.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role,
                                  must_change_password) VALUES (1,'b@x.co',?,'Boss','admin',0)""",
            (hash_password(PW),))
        yield c


def metric(c, mid, name="Calls", mtype="numeric", direction="up", start=Q3_START):
    c.execute("""INSERT INTO metrics (id, section_id, name, metric_type, rollup, direction,
                                      start_week, dri_user_id, sort_order)
                 VALUES (?,1,?,?,?,?,?,1,?)""",
              (mid, name, mtype, "sum" if mtype == "numeric" else None, direction,
               start.isoformat(), mid))


def target(c, mid, year, quarter, base, stretch):
    admin_ops.save_target(c, mid, year, quarter, base, stretch)


def entry(c, mid, week, v=None, status=None):
    dbm.upsert_entry(c, mid, week, value_numeric=v, value_status=status,
                     source="manual", user_id=1)


def grid_row(vm, mid):
    return next(r for s in vm.sections for r in s.rows if r.metric_id == mid)


def board_row(c, vm, mid):
    return next(r for r in gridm._board_rows(c, vm) if r.metric_id == mid)


# ---------------------------------------------------------------- 1. streak
def test_metric_page_streak_uses_each_weeks_own_quarter_target(con):
    """Q3 target 10, Q4 target 100. 9 in the last two Q3 weeks is yellow
    against Q3's 10; judged against Q4's 100 (the old bug) it was red, and the
    page claimed a 3-week streak while the board said 1."""
    metric(con, 1)
    target(con, 1, 2026, 3, 10, 10)
    target(con, 1, 2026, 4, 100, 100)
    entry(con, 1, Q3_LAST - timedelta(days=7), 9)
    entry(con, 1, Q3_LAST, 9)
    entry(con, 1, Q4_START, 50)                    # red against 100
    now = at(2026, 10, 14)                         # last closed = Q4 W1
    board = grid_row(gridm.build_grid(con, now), 1)
    page = gridm.build_metric(con, 1, now)
    assert board.red_streak == 1
    assert page.red_streak == board.red_streak
    assert page.escalation == board.escalation


def test_streak_helper_is_shared_and_stops_at_the_start_week():
    info = sc.MetricInfo(id=1, metric_type="numeric", direction="up",
                         start_week=date(2026, 9, 21), archived_week=None)
    states = gridm.closed_states_desc(
        info, Q4_START, lambda w: sc.EntryInfo(1.0, None), lambda w: 10.0, at(2026, 10, 14))
    assert len(states) == 3 and all(s == sc.CellState.RED for s in states)


# --------------------------------------------------- 2. target beside a value
def _quarter_start_board(con):
    metric(con, 1, "Calls")
    metric(con, 2, "Demos")
    target(con, 1, 2026, 3, 10, 20)
    target(con, 1, 2026, 4, 100, 120)
    target(con, 2, 2026, 3, 30, 40)
    target(con, 2, 2026, 4, 300, 400)
    entry(con, 1, Q3_LAST, 2)        # red against Q3 stretch 20; nothing for Demos
    return at(2026, 10, 7)           # current = Q4 W1, last closed = Q3 W13


def test_quarter_start_pairs_last_quarters_value_with_last_quarters_target(con):
    now = _quarter_start_board(con)
    vm = gridm.build_grid(con, now)
    row = grid_row(vm, 1)
    assert row.actual_display == "2" and row.target_display == "20"   # not Q4's 100
    br = board_row(con, vm, 1)
    assert br.week_note == "last week"
    assert br.target_display == "20" and br.target_raw == 20


def test_this_weeks_number_takes_this_weeks_target(con):
    now = _quarter_start_board(con)
    entry(con, 1, Q4_START, 30)
    br = board_row(con, gridm.build_grid(con, now), 1)
    assert br.week_note == "this week"
    assert br.target_display == "100" and br.target_raw == 100


def test_red_and_stale_cards_carry_the_same_weeks_target(con):
    now = _quarter_start_board(con)
    acts = {a.name: a for a in gridm.build_actions(con, gridm.build_grid(con, now))}
    assert acts["Calls"].kind == "red" and acts["Calls"].target_display == "20"
    assert acts["Demos"].kind == "stale" and acts["Demos"].target_display == "40"


def test_quarter_week_7_does_not_show_stretch_beside_a_baseline_week(con):
    """W6's 90 was judged against baseline 100 (yellow); the CEO ring must
    read 90% of 100, not 75% of the W7 stretch 120."""
    metric(con, 1, "Revenue")
    target(con, 1, 2026, 4, 100, 120)
    entry(con, 1, date(2026, 11, 9), 90)           # Q4 W6
    now = at(2026, 11, 18)                         # current = Q4 W7
    br = board_row(con, gridm.build_grid(con, now), 1)
    assert br.target_display == "100" and br.target_raw == 100
    assert gridm._share_of_target(br) == 90


def test_api_target_is_the_last_closed_weeks_and_each_week_carries_its_own(con):
    now = _quarter_start_board(con)
    entry(con, 1, Q4_START, 30)
    m = next(x for s in build_scorecard(con, now)["sections"]
             for x in s["metrics"] if x["id"] == 1)
    assert m["target"] == "20"
    assert m["last_closed_week"]["target"] == 20
    assert m["current_week"]["target"] == 100


# ---------------------------------------------- 3. targets page, closed only
def test_last_quarter_mean_skips_the_in_progress_week(con):
    metric(con, 1, start=Q4_START)
    target(con, 1, 2026, 4, 10, 10)
    entry(con, 1, Q4_START, 10)
    entry(con, 1, date(2026, 10, 12), 10)
    entry(con, 1, date(2026, 10, 19), 1)           # in progress, partial
    rows = gridm.build_target_rows(con, 2027, 1, at(2026, 10, 21))
    r = next(r for r in rows if r.metric["id"] == 1)
    assert r.prev_actual == 10.0                   # not (10+10+1)/3
    assert r.scored_weeks == 2


# ------------------------------------------------- 4. display window
def test_one_month_window_cannot_empty_the_grid(con):
    """Thu Oct 1: October's first Monday is Oct 5, so a one-month window had
    no weeks and build_grid raised IndexError."""
    metric(con, 1)
    dbm.set_setting(con, "display_months", "1")
    now = at(2026, 10, 1)
    vm = gridm.build_grid(con, now)
    assert vm.weeks and vm.weeks[-1] == date(2026, 9, 28)
    assert len(vm.weeks) > 1                       # clamped to the 2 the form offers


def test_empty_window_falls_back_to_the_current_week(con, monkeypatch):
    metric(con, 1)
    dbm.set_setting(con, "display_months", "1")
    monkeypatch.setattr(gridm, "clamp_display_months", lambda n: n)
    vm = gridm.build_grid(con, at(2026, 10, 1))
    assert vm.weeks == [date(2026, 9, 28)]


# ------------------------------------------- 5. type change / scoring safety
@pytest.fixture
def client(con):
    from app.main import app
    con.commit()
    c = TestClient(app)
    c.post("/login", data={"email": "b@x.co", "password": PW})
    return c


def _edit(client, mid, **over):
    data = {"section_id": 1, "name": "Calls", "metric_type": "numeric",
            "rollup": "sum", "direction": "up", "start_week": Q3_START.isoformat(),
            "sort_order": 0, **over}
    return client.post(f"/admin/metrics/{mid}", data=data, follow_redirects=False)


def _type(mid):
    with dbm.get_db() as c:
        return c.execute("SELECT name, metric_type FROM metrics WHERE id=?",
                         (mid,)).fetchone()


def test_form_refuses_a_type_change_once_numbers_exist(con, client):
    metric(con, 1)
    entry(con, 1, Q3_START, 5)
    con.commit()
    r = _edit(client, 1, metric_type="status")
    assert r.status_code == 409
    assert "type cannot change" in r.text
    assert _type(1)["metric_type"] == "numeric"


def test_form_allows_a_type_fix_before_anything_is_entered(con, client):
    metric(con, 1)
    con.commit()
    assert _edit(client, 1, metric_type="status").status_code == 303
    assert _type(1)["metric_type"] == "status"


def test_form_refuses_a_second_live_name_in_the_section(con, client):
    metric(con, 1, "Calls")
    metric(con, 2, "Demos")
    con.commit()
    r = _edit(client, 2, name=" calls ")
    assert r.status_code == 409 and "already exists" in r.text
    assert _type(2)["name"] == "Demos"


def test_a_mismatched_entry_scores_as_no_data_not_a_crash(con):
    info = sc.MetricInfo(id=1, metric_type="status", direction="up",
                         start_week=Q3_START, archived_week=None)
    late = at(2026, 10, 14)
    assert sc.cell_state(info, Q4_START, sc.EntryInfo(5.0, None), None, late) \
        == sc.CellState.STALE
    num = sc.MetricInfo(id=2, metric_type="numeric", direction="up",
                        start_week=Q3_START, archived_week=None)
    assert sc.cell_state(num, Q4_START, sc.EntryInfo(None, "G"), 10.0, late) \
        == sc.CellState.STALE
    # And a whole board with such a row still builds (legacy data).
    metric(con, 1)
    entry(con, 1, Q4_START, 5)
    con.execute("UPDATE metrics SET metric_type='status', rollup=NULL WHERE id=1")
    vm = gridm.build_grid(con, late)
    assert grid_row(vm, 1).last_state == "stale"
    gridm.build_tv(con, late)
    build_scorecard(con, late)


# --------------------------------------------------- 6. "+10%" direction
def test_better_by_ten_percent_lowers_a_budget(con, client):
    metric(con, 1, "Calls")
    metric(con, 2, "Expenses", direction="down")
    for mid in (1, 2):
        entry(con, mid, Q3_START, 100)
    con.commit()
    body = client.get("/admin/targets?year=2026&quarter=4").text
    assert body.count('data-tg-bump="1.1"') == 1
    assert body.count('data-tg-bump="0.9"') == 1
    assert "&minus;10%" in body
