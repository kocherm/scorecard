"""Token rotation: the old secret keeps authenticating for its grace window,
stops the moment the window closes, and the admin page shows which generation
is still being called so you know when it is safe to revoke."""
import re

import pytest
from fastapi.testclient import TestClient

from app import auth
from app import db as dbm
from app.main import _token_lineages

from datetime import datetime, timezone


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app  # imported late so DB_PATH is already patched

    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role)
               VALUES (1,'a@example.com','x','Admin','admin')""")
        old = auth.new_api_token(con, "hermes", "read_write", 1)
    yield TestClient(app), old


def hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


def rotate(grace_days=7, tid=1):
    with dbm.get_db() as con:
        return auth.rotate_api_token(con, tid, grace_days, 1)


def rows():
    with dbm.get_db() as con:
        return con.execute("SELECT * FROM api_tokens ORDER BY created_at DESC, id DESC").fetchall()


def test_both_secrets_work_during_grace(env):
    client, old = env
    new = rotate(grace_days=7)
    assert new != old
    assert client.get("/api/v1/scorecard", headers=hdr(old)).status_code == 200
    assert client.get("/api/v1/scorecard", headers=hdr(new)).status_code == 200


def test_replacement_keeps_name_and_scope(env):
    client, old = env
    rotate()
    r = rows()
    assert len(r) == 2
    assert r[0]["name"] == "hermes" and r[0]["scope"] == "read_write"
    assert r[0]["rotated_from_id"] == 1
    assert r[0]["expires_at"] is None       # the replacement has no clock
    assert r[1]["expires_at"] is not None   # the old one does


def test_old_secret_dies_when_grace_expires(env):
    client, old = env
    new = rotate(grace_days=7)
    with dbm.get_db() as con:  # wind the deadline back into the past
        con.execute("UPDATE api_tokens SET expires_at = datetime('now','-1 day') WHERE id=1")
    assert client.get("/api/v1/scorecard", headers=hdr(old)).status_code == 401
    assert client.get("/api/v1/scorecard", headers=hdr(new)).status_code == 200


def test_zero_grace_cuts_over_immediately(env):
    client, old = env
    new = rotate(grace_days=0)
    assert client.get("/api/v1/scorecard", headers=hdr(old)).status_code == 401
    assert client.get("/api/v1/scorecard", headers=hdr(new)).status_code == 200


def test_rotating_twice_is_refused_while_grace_is_open(env):
    client, old = env
    rotate()
    with pytest.raises(ValueError):
        rotate(tid=1)


def test_revoked_token_cannot_be_rotated(env):
    client, old = env
    with dbm.get_db() as con:
        con.execute("UPDATE api_tokens SET revoked_at = datetime('now') WHERE id=1")
    with pytest.raises(ValueError):
        rotate(tid=1)


def test_grace_is_capped(env):
    client, old = env
    rotate(grace_days=9999)
    with dbm.get_db() as con:
        days = con.execute(
            "SELECT julianday(expires_at) - julianday('now') AS d FROM api_tokens WHERE id=1"
        ).fetchone()["d"]
    assert days <= auth.ROTATE_GRACE_MAX + 1


def test_still_in_use_flags_an_unswitched_integration(env):
    client, old = env
    rotate()
    now = datetime.now(timezone.utc)
    assert _token_lineages(rows(), now)[0]["prior"][0]["still_in_use"] is False
    client.get("/api/v1/scorecard", headers=hdr(old))  # integration still on the old secret
    assert _token_lineages(rows(), now)[0]["prior"][0]["still_in_use"] is True


def test_connector_url_is_shown_with_a_fresh_token(env):
    """Tokens are hashed, so create/rotate is the only moment the ready-to-paste
    MCP URL can exist. Assembling it by hand is what dropped a character last
    time; the page must hand it over whole."""
    client, old = env
    with dbm.get_db() as con:
        dbm.set_setting(con, "public_base_url", "https://example.test")
        sess = auth.create_session(con, 1)
    client.cookies.set(auth.SESSION_COOKIE, sess)

    r = client.post("/admin/tokens", data={"name": "claude", "scope": "read"},
                    follow_redirects=True)
    raw = re.search(r"(sc_[A-Za-z0-9_\-]+)", r.text).group(1)
    assert f"https://example.test/mcp/t/{raw}" in r.text

    # ...and again on rotate, since the connector needs the new value pasted in
    tid = rows()[0]["id"]
    r = client.post(f"/admin/tokens/{tid}/rotate", data={"grace_days": "7"},
                    follow_redirects=True)
    new = re.search(r"(sc_[A-Za-z0-9_\-]+)", r.text).group(1)
    assert new != raw and f"https://example.test/mcp/t/{new}" in r.text

    # A plain page load must not carry a real token URL - only the documented
    # shape, "/mcp/t/<token>". Tokens are hashed; a live one here would mean we
    # had started storing them.
    r = client.get("/admin/tokens")
    assert "/mcp/t/sc_" not in r.text
    assert "/mcp/t/&lt;token&gt;" in r.text, "the URL shape is still documented"


def test_lineage_nests_the_previous_generation_and_counts_the_rest(env):
    client, old = env
    rotate(grace_days=0)          # gen 1 -> 2
    with dbm.get_db() as con:
        auth.rotate_api_token(con, 2, 0, 1)   # gen 2 -> 3
    groups = _token_lineages(rows(), datetime.now(timezone.utc))
    assert len(groups) == 1                    # one lineage, not three rows
    assert groups[0]["head"]["t"]["id"] == 3
    assert [p["t"]["id"] for p in groups[0]["prior"]] == [2]
    assert groups[0]["retired"] == 1
