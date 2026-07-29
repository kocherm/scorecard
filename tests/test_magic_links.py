"""Magic links: a Slack-delivered token signs the DRI straight into /checkin,
stays valid until expiry (Slack's crawler must not burn it), and dies cleanly."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import auth
from app import db as dbm
from app.auth import create_magic_link, hash_password

PW = "a-fine-password-123"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app

    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'S',0)")
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role,
                                  must_change_password) VALUES (2,'ed@x.co',?,'Eddie','editor',0)""",
            (hash_password(PW),))
    yield app


def mint(days=7):
    with dbm.get_db() as con:
        return create_magic_link(con, 2, days=days)


def test_magic_link_signs_in_and_lands_on_checkin(env):
    token = mint()
    client = TestClient(env)  # no cookie
    r = client.get("/checkin", params={"t": token})
    assert r.status_code == 200
    assert str(r.url).endswith("/checkin")      # clean URL after the exchange
    assert "My numbers" in r.text               # the page, not the login form
    assert client.get("/").status_code == 200   # full session: grid works too


def test_magic_link_is_multi_use_within_expiry(env):
    token = mint()
    for _ in range(2):  # e.g. Slack's crawler hit it first
        assert "My numbers" in TestClient(env).get("/checkin", params={"t": token}).text


def test_bad_or_expired_token_goes_to_login(env):
    client = TestClient(env)
    r = client.get("/checkin", params={"t": "garbage"})
    assert str(r.url).endswith("/login")
    expired = mint(days=-1)
    r = TestClient(env).get("/checkin", params={"t": expired})
    assert str(r.url).endswith("/login")


def test_deactivated_user_link_is_dead(env):
    token = mint()
    with dbm.get_db() as con:
        con.execute("UPDATE users SET is_active = 0 WHERE id = 2")
    r = TestClient(env).get("/checkin", params={"t": token})
    assert str(r.url).endswith("/login")


def test_consume_stamps_last_used(env):
    token = mint()
    TestClient(env).get("/checkin", params={"t": token})
    with dbm.get_db() as con:
        row = con.execute("SELECT last_used_at FROM magic_links WHERE token_hash = ?",
                          (auth._sha256(token),)).fetchone()
    assert row["last_used_at"] is not None
