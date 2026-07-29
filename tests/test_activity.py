"""Admin > Activity: the audit trail made visible - old -> new values, actors,
and a LATE chip on anything written after the week's staleness deadline."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db as dbm
from app import weeks as wk
from app.auth import hash_password

PW = "a-fine-password-123"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app

    start = (wk.last_closed_week(datetime.now(timezone.utc))
             - timedelta(days=28)).isoformat()
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'S',0)")
        for uid, email, name, role in ((1, "boss@x.co", "Boss", "admin"),
                                       (2, "ed@x.co", "Eddie", "editor")):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role,
                                      must_change_password) VALUES (?,?,?,?,?,0)""",
                (uid, email, hash_password(PW), name, role))
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                            start_week, dri_user_id)
                       VALUES (1, 1, 'Discovery calls', 'numeric', 'sum', ?, 2)""",
                    (start,))
    yield TestClient(app)


def test_activity_shows_edits_actors_and_late_flag(env):
    client = env
    client.post("/login", data={"email": "ed@x.co", "password": PW})
    old_week = (wk.last_closed_week(datetime.now(timezone.utc))
                - timedelta(days=14)).isoformat()
    client.post(f"/checkin/1/{old_week}", data={"value": "9"})   # late back-fill
    client.post(f"/checkin/1/{old_week}", data={"value": "11"})  # correction
    client.post("/logout")

    client.post("/login", data={"email": "boss@x.co", "password": PW})
    r = client.get("/admin/activity")
    assert r.status_code == 200
    assert "Discovery calls" in r.text and "Eddie" in r.text
    assert "entered 9" in r.text          # the back-fill
    assert "9 &rarr; 11" in r.text        # the correction, old value preserved
    assert "LATE" in r.text               # two weeks past the deadline


def test_activity_is_admin_only(env):
    client = env
    client.post("/login", data={"email": "ed@x.co", "password": PW})
    assert client.get("/admin/activity").status_code == 403
