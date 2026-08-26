"""Concurrent requests must not 500.

Found while adding the TV preview to Settings, which puts a second request on
the page: /display returned 500 for 79 of 80 concurrent calls, on the deployed
commit as well. A sync generator dependency does not choose its threads -
Starlette runs the `yield` and the teardown as two separate to_thread calls -
so the per-request sqlite connection was being CLOSED on a different worker
than opened it. With one client (the TV, polling alone) the pool hands back the
same thread and nothing ever shows.
"""
from concurrent.futures import ThreadPoolExecutor
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
    d = wk.last_closed_week(datetime.now(timezone.utc))
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'S',0)")
        con.execute("""INSERT INTO users (id, email, password_hash, display_name,
                       role, must_change_password) VALUES (1,'b@x.co',?,'Boss','admin',0)""",
                    (hash_password(PW),))
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                       start_week, dri_user_id) VALUES (1,1,'Calls','numeric','sum',?,1)""",
                    ((d - timedelta(days=28)).isoformat(),))
        dbm.upsert_entry(con, 1, d, value_numeric=4.0, source="manual", user_id=1)
        token = "tv-token-for-this-test"
        dbm.set_setting(con, "display_token", token)
    c = TestClient(app)
    c.post("/login", data={"email": "b@x.co", "password": PW})
    return c, token


def test_the_tv_survives_being_watched_while_someone_uses_the_app(env):
    """The exact shape the Settings preview creates: the board polling while an
    admin page loads beside it."""
    client, token = env
    urls = ([f"/display?token={token}", f"/display/body?token={token}"] * 12
            + ["/", "/admin/settings"] * 6)

    with ThreadPoolExecutor(max_workers=12) as pool:
        codes = [r.status_code for r in pool.map(client.get, urls)]

    assert all(c == 200 for c in codes), \
        f"{sum(1 for c in codes if c != 200)} of {len(codes)} failed: {sorted(set(codes))}"


def test_a_connection_is_never_shared_between_two_requests(env):
    """check_same_thread=False is safe only because each request gets its own
    connection. If that ever changes, this fails."""
    client, _ = env
    seen = []
    real_connect = dbm.connect

    def spy(*a, **kw):
        con = real_connect(*a, **kw)
        seen.append(id(con))
        return con

    dbm.connect = spy
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(client.get, ["/admin/users"] * 6))
    finally:
        dbm.connect = real_connect
    assert len(seen) == len(set(seen)), "a connection object was reused"
