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


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app

    d = due()
    start = (d - timedelta(days=56)).isoformat()
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
        con.execute("""INSERT INTO targets (metric_id, year, quarter, baseline_value,
                                            stretch_value) VALUES (1,?,?,10,10)""",
                    wk.quarter_of(d))
        for i, v in enumerate((12.0, 3.0, 2.0)):       # green, then two reds
            dbm.upsert_entry(con, 1, d - timedelta(days=7 * (2 - i)),
                             value_numeric=v, source="manual", user_id=2)
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
    assert "1 / 3" in r.text.replace("<small>", " ").replace("</small>", "")


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


def test_unknown_metric_is_404(env):
    assert login(env).get("/m/999").status_code == 404


def test_enter_is_offered_only_to_the_person_it_would_work_for(env):
    """/checkin lists the metrics you own, so offering an admin who is not the
    DRI an "Enter" link would send them to a page the metric is missing from."""
    boss = login(env).get("/m/1").text
    assert "mx-edit" not in boss                  # admin, but not the DRI
    env.post("/logout")
    eddie = login(env, "e@x.co").get("/m/1").text
    assert "mx-edit" in eddie and "/checkin/catch-up" in eddie


def test_viewers_can_read_it_but_are_offered_nothing(env):
    r = login(env, "v@x.co").get("/m/1")
    assert r.status_code == 200 and "Calls" in r.text
    assert "mx-edit" not in r.text and "File a 1-3-1" not in r.text


def test_an_empty_quarter_renders_rather_than_dividing_by_zero(env):
    """Every bar height is a fraction of the largest value or target; a quarter
    with neither must still draw."""
    r = login(env).get("/m/1?year=2020&quarter=1")
    assert r.status_code == 200 and "Q1 2020" in r.text
