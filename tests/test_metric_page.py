"""/m/{id}: one metric, one quarter, scored through the same path as the board.
The page exists so "why is this red?" has an address; the tests that matter are
that it agrees with the board and that it never offers an action it cannot do."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db as dbm
from app import grid as gridm
from app import weeks as wk
from app.auth import hash_password

PW = "a-fine-password-123"


def due():
    return wk.last_closed_week(datetime.now(timezone.utc))


# Calls over the last three closed weeks: green, then two reds - against THIS
# quarter's target of 10. Earlier quarters get a target of 2, so a week that
# falls in the previous quarter scores differently depending on which
# quarter's target it is read against. In the first weeks of a quarter that is
# exactly the case the page and the board must agree on.
VALUES = (12.0, 3.0, 2.0)
CUR_TARGET, PREV_TARGET = 10, 2


def prev_quarter(year, quarter):
    return (year, quarter - 1) if quarter > 1 else (year - 1, 4)


def quarters_between(first, last):
    """Every (year, quarter) from the week `first` to the week `last`."""
    out, w = [], first
    while w <= last:
        if wk.quarter_of(w) not in out:
            out.append(wk.quarter_of(w))
        w += timedelta(days=7)
    return out


def entry_weeks():
    d = due()
    return [(d - timedelta(days=7 * (2 - i)), v) for i, v in enumerate(VALUES)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app

    d = due()
    # Start at the top of the PREVIOUS quarter, so every week of it is a real
    # (catch-up-able) week whatever today's date - not just "8 weeks back".
    first = wk.first_monday_of_quarter(*prev_quarter(*wk.quarter_of(d)))
    start = first.isoformat()
    cur = wk.current_week(datetime.now(timezone.utc))
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        for uid, email, name, role in ((1, "b@x.co", "Boss", "admin"),
                                       (2, "e@x.co", "Eddie", "editor"),
                                       (3, "v@x.co", "Val", "viewer")):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role,
                                      must_change_password) VALUES (?,?,?,?,?,0)""",
                (uid, email, hash_password(PW), name, role))
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                            start_week, dri_user_id)
                       VALUES (1,1,'Calls','numeric','sum',?,2)""", (start,))
        # A target row for EVERY quarter the data and the window touch.
        for y, q in quarters_between(first, cur):
            t = CUR_TARGET if (y, q) == wk.quarter_of(d) else PREV_TARGET
            con.execute("""INSERT INTO targets (metric_id, year, quarter, baseline_value,
                                                stretch_value) VALUES (1,?,?,?,?)""",
                        (y, q, t, t))
        for w, v in entry_weeks():
            dbm.upsert_entry(con, 1, w, value_numeric=v, source="manual", user_id=2)
    return TestClient(app)


def login(client, email="b@x.co"):
    client.post("/login", data={"email": email, "password": PW})
    return client


def test_page_shows_the_quarter_the_owner_and_the_history(env):
    r = login(env).get("/m/1")
    assert r.status_code == 200
    assert "Calls" in r.text and "Eddie" in r.text
    assert "week by week" in r.text
    assert "Every write to this metric" in r.text
    # The page shows the due week's quarter, which early in a quarter holds
    # only some of the three entries - count what it should hold.
    q = wk.quarter_of(due())
    mine = [v for w, v in entry_weeks() if wk.quarter_of(w) == q]
    hit = sum(v >= CUR_TARGET for v in mine)
    assert f"{hit} / {len(mine)}" in r.text.replace("<small>", " ").replace("</small>", "")


def test_it_scores_exactly_what_the_board_scores(env):
    """A metric that read red here and green on the board would be worse than
    no page at all - both go through sc.cell_state on the same GridVM data."""
    login(env)
    with dbm.get_db() as con:
        now = datetime.now(timezone.utc)
        vm = gridm.build_grid(con, now)
        row = vm.sections[0].rows[0]
        mvm = gridm.build_metric(con, 1, now)
    board_state = row.last_state
    assert mvm.latest.state == board_state
    assert mvm.red_streak == row.red_streak


def test_board_and_page_agree_across_a_quarter_boundary(env):
    """Pinned to Q4 2026 week 2, so the streak always crosses into Q3 whatever
    today's date: each week must be scored against ITS OWN quarter's target.
    Read against Q4's 10, Sep 28's 3 is a second red week; against Q3's 2 it
    is green and the streak is one week long."""
    with dbm.get_db() as con:
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                            start_week, dri_user_id)
                       VALUES (2,1,'Pinned','numeric','sum','2026-08-03',2)""")
        for y, q, t in ((2026, 3, PREV_TARGET), (2026, 4, CUR_TARGET)):
            con.execute("""INSERT INTO targets (metric_id, year, quarter, baseline_value,
                                                stretch_value) VALUES (2,?,?,?,?)""",
                        (y, q, t, t))
        for w, v in (("2026-09-21", 12.0), ("2026-09-28", 3.0), ("2026-10-05", 2.0)):
            dbm.upsert_entry(con, 2, wk.parse_week(w), value_numeric=v,
                             source="manual", user_id=2)
        now = datetime(2026, 10, 15, 15, 0, tzinfo=timezone.utc)
        vm = gridm.build_grid(con, now)
        row = next(r for s in vm.sections for r in s.rows if r.metric_id == 2)
        mvm = gridm.build_metric(con, 2, now)
    assert mvm.latest.state == row.last_state == "red"
    assert row.red_streak == 1
    assert mvm.red_streak == row.red_streak


def test_unknown_metric_is_404(env):
    assert login(env).get("/m/999").status_code == 404


def test_enter_is_offered_only_to_the_person_it_would_work_for(env):
    """/checkin lists the metrics you own, so offering an admin who is not the
    DRI an "Enter" link would send them to a page the metric is missing from."""
    boss = login(env).get("/m/1").text
    assert "mx-edit" not in boss                  # admin, but not the DRI
    env.post("/logout")
    login(env, "e@x.co")
    assert "mx-edit" in env.get("/m/1").text
    # Weeks older than the due week go to catch-up. The due week's own quarter
    # may hold none yet (quarter week 1-2), so read the previous quarter, which
    # the fixture starts at the top of: every week there is an older week.
    y, q = prev_quarter(*wk.quarter_of(due()))
    older = env.get(f"/m/1?year={y}&quarter={q}").text
    assert "mx-edit" in older and "/checkin/catch-up" in older


def test_viewers_can_read_it_but_are_offered_nothing(env):
    r = login(env, "v@x.co").get("/m/1")
    assert r.status_code == 200 and "Calls" in r.text
    assert "mx-edit" not in r.text and "File a 1-3-1" not in r.text


def test_an_empty_quarter_renders_rather_than_dividing_by_zero(env):
    """Every bar height is a fraction of the largest value or target; a quarter
    with neither must still draw."""
    r = login(env).get("/m/1?year=2020&quarter=1")
    assert r.status_code == 200 and "Q1 2020" in r.text
