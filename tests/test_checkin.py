"""My Numbers check-in: DRI-scoped list, one-tap saves, the login redirect,
the nav badge, and demo-mode isolation."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db as dbm
from app import demo
from app import weeks as wk
from app.auth import hash_password

PW = "a-fine-password-123"


def due_week():
    return wk.last_closed_week(datetime.now(timezone.utc)).isoformat()


def cur_week():
    return wk.current_week(datetime.now(timezone.utc)).isoformat()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app

    start = (wk.parse_week(due_week()) - timedelta(days=28)).isoformat()
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        for uid, email, name, role in ((1, "boss@x.co", "Boss", "admin"),
                                       (2, "ed@x.co", "Eddie", "editor"),
                                       (3, "vi@x.co", "Val", "viewer")):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role,
                                      must_change_password) VALUES (?,?,?,?,?,0)""",
                (uid, email, hash_password(PW), name, role))
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                            start_week, dri_user_id)
                       VALUES (1, 1, 'Eddie Calls', 'numeric', 'sum', ?, 2)""", (start,))
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type,
                                            start_week, dri_user_id)
                       VALUES (2, 1, 'Eddie Client', 'status', ?, 2)""", (start,))
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                            start_week, dri_user_id)
                       VALUES (3, 1, 'Boss Metric', 'numeric', 'sum', ?, 1)""", (start,))
    yield TestClient(app)


def login(client, email="ed@x.co"):
    return client.post("/login", data={"email": email, "password": PW})


def test_login_lands_on_checkin_when_numbers_are_missing(env):
    client = env
    r = login(client)
    assert r.status_code == 200
    assert str(r.url).endswith("/checkin")
    assert "My numbers" in r.text
    assert "Eddie Calls" in r.text and "Eddie Client" in r.text
    assert "Boss Metric" not in r.text          # only own metrics
    assert "is-missing" in r.text               # missing emphasis
    assert "0</b> of 2 entered" in r.text       # progress counter


def test_save_due_and_status_then_login_goes_home(env):
    client = env
    login(client)
    r = client.post(f"/checkin/1/{due_week()}", data={"value": "12"})
    assert r.status_code == 200 and "12" in r.text
    r = client.post(f"/checkin/2/{due_week()}", data={"value": "G"})
    assert r.status_code == 200
    with dbm.get_db() as con:
        e = con.execute("SELECT * FROM entries WHERE metric_id=1").fetchone()
        assert (e["value_numeric"], e["source"], e["entered_by_user_id"]) == (12.0, "manual", 2)
        assert con.execute("SELECT value_status FROM entries WHERE metric_id=2")\
                  .fetchone()["value_status"] == "G"
    client.post("/logout")
    r = login(client)
    assert not str(r.url).endswith("/checkin")  # all caught up -> home


def test_human_number_forms_are_accepted(env):
    client = env
    login(client)
    r = client.post(f"/checkin/1/{due_week()}", data={"value": "$1,500"})
    assert r.status_code == 200
    with dbm.get_db() as con:
        assert con.execute("SELECT value_numeric FROM entries WHERE metric_id=1")\
                  .fetchone()["value_numeric"] == 1500.0


def test_bad_input_and_future_week_rejected(env):
    client = env
    login(client)
    assert client.post(f"/checkin/1/{due_week()}", data={"value": "twelve"}).status_code == 422
    future = (wk.parse_week(cur_week()) + timedelta(days=7)).isoformat()
    assert client.post(f"/checkin/1/{future}", data={"value": "3"}).status_code == 422


def test_viewer_is_403_and_never_redirected(env):
    client = env
    r = login(client, "vi@x.co")
    assert not str(r.url).endswith("/checkin")
    assert client.get("/checkin").status_code == 403


def test_nav_badge_counts_missing(env):
    client = env
    login(client)
    assert "nav-badge" in client.get("/").text
    client.post(f"/checkin/1/{due_week()}", data={"value": "5"})
    client.post(f"/checkin/2/{due_week()}", data={"value": "G"})
    assert "nav-badge" not in client.get("/").text


def test_backfill_lives_on_the_catch_up_page_not_the_weekly_one(env):
    """Earlier weeks are a separate room: a two-month-old gap must not reopen
    on the weekly page every Monday, which is what made it unusable."""
    client = env
    login(client)
    gap = (wk.parse_week(due_week()) - timedelta(days=14)).isoformat()

    weekly = client.get("/checkin").text
    assert gap not in weekly                    # not offered on the weekly page
    assert "catch up" in weekly                 # but the way there is

    cu = client.get("/checkin/catch-up").text
    assert f"v:1:{gap}" in cu and "Eddie Calls" in cu
    assert "Boss Metric" not in cu              # still DRI-scoped


def test_catchup_saves_changed_cells_with_audit(env):
    client = env
    login(client)
    gap = (wk.parse_week(due_week()) - timedelta(days=14)).isoformat()
    older = (wk.parse_week(due_week()) - timedelta(days=21)).isoformat()

    r = client.post("/checkin/catch-up", data={
        f"v:1:{gap}": "7", f"o:1:{gap}": "",
        f"v:1:{older}": "", f"o:1:{older}": "",      # untouched blank
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/checkin?saved=1"

    with dbm.get_db() as con:
        e = con.execute("SELECT * FROM entries WHERE metric_id=1 AND week_start=?",
                        (gap,)).fetchone()
        a = con.execute("SELECT * FROM entry_audit WHERE metric_id=1 AND week_start=?",
                        (gap,)).fetchone()
        untouched = con.execute(
            "SELECT COUNT(*) c FROM entry_audit WHERE week_start=?", (older,)
        ).fetchone()["c"]
    assert e["value_numeric"] == 7.0
    assert a["old_numeric"] is None and a["new_numeric"] == 7.0   # first entry
    assert untouched == 0        # unchanged field wrote nothing, audited nothing


def test_catchup_skips_unchanged_values_and_rejects_other_peoples_metrics(env):
    client = env
    login(client)
    gap = (wk.parse_week(due_week()) - timedelta(days=14)).isoformat()
    client.post("/checkin/catch-up", data={f"v:1:{gap}": "7", f"o:1:{gap}": ""})

    # Re-submitting the same number is a no-op: no second audit row.
    client.post("/checkin/catch-up", data={f"v:1:{gap}": "7", f"o:1:{gap}": "7"})
    with dbm.get_db() as con:
        assert con.execute(
            "SELECT COUNT(*) c FROM entry_audit WHERE metric_id=1 AND week_start=?",
            (gap,)).fetchone()["c"] == 1

    # Metric 3 belongs to the boss; the field name is not authorisation.
    r = client.post("/checkin/catch-up",
                    data={f"v:3:{gap}": "9", f"o:3:{gap}": ""})
    assert r.status_code == 403
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) c FROM entries WHERE metric_id=3")\
                  .fetchone()["c"] == 0


def test_current_week_is_a_tab_not_a_second_field_on_every_row(env):
    client = env
    login(client)
    due, cur = due_week(), cur_week()

    weekly = client.get("/checkin").text
    assert f'/checkin/1/{due}' in weekly and f'/checkin/1/{cur}' not in weekly

    early = client.get(f"/checkin?week={cur}").text
    assert f'/checkin/1/{cur}' in early and f'/checkin/1/{due}' not in early

    # An unknown week falls back to the due week rather than 404ing.
    fallback = client.get("/checkin?week=not-a-week").text
    assert f'/checkin/1/{due}' in fallback


def test_saving_swaps_the_row_and_the_counter_together(env):
    client = env
    login(client)
    r = client.post(f"/checkin/1/{due_week()}", data={"value": "12"})
    assert r.status_code == 200
    assert 'id="ck-1"' in r.text                       # the row
    assert 'hx-swap-oob="true"' in r.text               # the counter, out of band
    assert "1</b> of 2 entered" in r.text


def test_correction_keeps_old_value_in_the_audit_trail(env):
    client = env
    login(client)
    client.post(f"/checkin/1/{due_week()}", data={"value": "12"})
    client.post(f"/checkin/1/{due_week()}", data={"value": "15"})
    with dbm.get_db() as con:
        assert con.execute("SELECT value_numeric FROM entries WHERE metric_id=1")\
                  .fetchone()["value_numeric"] == 15.0
        audits = con.execute(
            "SELECT * FROM entry_audit WHERE metric_id=1 ORDER BY id").fetchall()
    assert len(audits) == 2
    assert audits[1]["old_numeric"] == 12.0 and audits[1]["new_numeric"] == 15.0


def test_demo_mode_isolates_checkin_and_suppresses_redirect(env):
    client = env
    with dbm.get_db() as con:
        dbm.set_setting(con, "display_demo_data", "1")
    r = login(client)
    assert not str(r.url).endswith("/checkin")  # redirect suppressed in demo
    before = dbm.connect().execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
    # a demo metric id (conversations=1 exists in demo db too); save goes to demo.db
    r = client.post(f"/checkin/1/{due_week()}", data={"value": "777"})
    assert r.status_code == 200
    after = dbm.connect().execute("SELECT COUNT(*) c FROM entries").fetchone()["c"]
    assert after == before                       # real DB untouched
    with dbm.connect(str(demo.demo_path())) as dcon:
        row = dcon.execute("SELECT value_numeric FROM entries WHERE metric_id=1 AND week_start=?",
                           (due_week(),)).fetchone()
    assert row["value_numeric"] == 777.0
