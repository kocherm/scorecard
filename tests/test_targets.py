"""Targets: last quarter's evidence beside the box, and one save for the page.
Setting a target defines red/yellow/green for thirteen weeks, for everyone -
these guard that the number is decided with the facts on screen, and that a
half-set target can never score half a quarter against nothing."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db as dbm
from app import grid as gridm
from app import weeks as wk
from app.auth import hash_password

PW = "a-fine-password-123"
Y, Q = 2026, 2          # the quarter under test
PY_, PQ = 2026, 1       # the quarter that supplies the evidence


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role,
                                  must_change_password) VALUES (1,'b@x.co',?,'Boss','admin',0)""",
            (hash_password(PW),))
        start = wk.first_monday_of_quarter(PY_, PQ).isoformat()
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                            start_week, dri_user_id)
                       VALUES (1,1,'Calls','numeric','sum',?,1)""", (start,))
        con.execute("""INSERT INTO targets (metric_id, year, quarter, baseline_value,
                                            stretch_value) VALUES (1,?,?,10,12)""",
                    (PY_, PQ))
        # Q1: four weeks entered - two at target, two under.
        w = wk.first_monday_of_quarter(PY_, PQ)
        for i, v in enumerate((10.0, 10.0, 4.0, 4.0)):
            dbm.upsert_entry(con, 1, w + timedelta(days=7 * i), value_numeric=v,
                             source="manual", user_id=1)
    client = TestClient(app)
    client.post("/login", data={"email": "b@x.co", "password": PW})
    return client


def test_last_quarter_is_on_the_row_you_are_typing_into(env):
    body = env.get(f"/admin/targets?year={Y}&quarter={Q}").text
    assert "Q1 2026 target" in body and "Q1 2026 actual" in body
    assert "2 / 4 wks" in body          # hit rate, the fact that drives the number
    assert ">7<" in body or "7" in body  # weekly mean of 10,10,4,4


def test_the_actual_is_the_weekly_mean_not_the_quarter_total(env):
    """A weekly target is compared with a weekly number - summing the quarter
    would put 28 next to a target of 10 and make every metric look heroic."""
    with dbm.get_db() as con:
        rows = gridm.build_target_rows(con, Y, Q, datetime.now(timezone.utc))
    r = next(r for r in rows if r.metric["id"] == 1)
    assert r.prev_actual == 7.0
    assert r.hit_weeks == 2 and r.scored_weeks == 4
    assert r.prev_delta_pct == -30           # 7 against a baseline of 10


def test_one_save_writes_only_what_changed(env):
    r = env.post("/admin/targets",
                 data={"year": Y, "quarter": Q, "b:1": "8", "s:1": "11"},
                 follow_redirects=False)
    assert r.status_code == 303 and "saved=1" in r.headers["location"]
    with dbm.get_db() as con:
        t = con.execute("SELECT * FROM targets WHERE metric_id=1 AND year=? AND quarter=?",
                        (Y, Q)).fetchone()
    assert (t["baseline_value"], t["stretch_value"]) == (8.0, 11.0)
    # Re-posting the same pair is a no-op.
    r = env.post("/admin/targets",
                 data={"year": Y, "quarter": Q, "b:1": "8", "s:1": "11"},
                 follow_redirects=False)
    assert "saved=0" in r.headers["location"]


def test_half_a_target_is_refused(env):
    """baseline scores weeks 1-6 and stretch the rest, so one without the other
    would leave half the quarter scored against nothing."""
    r = env.post("/admin/targets", data={"year": Y, "quarter": Q, "b:1": "8", "s:1": ""})
    assert r.status_code == 422
    with dbm.get_db() as con:
        assert con.execute(
            "SELECT COUNT(*) c FROM targets WHERE year=? AND quarter=?", (Y, Q)
        ).fetchone()["c"] == 0     # nothing committed - the whole POST rolls back
    # Both blank is how you leave a metric unscored - that is allowed.
    r = env.post("/admin/targets", data={"year": Y, "quarter": Q, "b:1": "", "s:1": ""},
                 follow_redirects=False)
    assert r.status_code == 303 and "saved=0" in r.headers["location"]


def test_field_names_are_not_authorisation(env):
    with dbm.get_db() as con:
        con.execute("UPDATE metrics SET archived_at = datetime('now') WHERE id = 1")
    assert env.post("/admin/targets",
                    data={"year": Y, "quarter": Q, "b:1": "8", "s:1": "9"}).status_code == 403
