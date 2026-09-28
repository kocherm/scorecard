"""People, sections, metric definitions and targets over the API: the admin
gate, that admin accounts are out of the API's reach, and that each write
keeps the rule its admin screen enforces."""
import pytest
from fastapi.testclient import TestClient

from app import auth
from app import db as dbm

START = "2026-01-05"  # a Monday


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app  # imported late so DB_PATH is already patched

    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Client Health',0)")
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (2,'Revenue',1)")
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role)
               VALUES (1,'boss@example.com','x','Boss','admin'),
                      (2,'ed@example.com','x','Ed','editor')""")
        con.execute(
            """INSERT INTO metrics (id, section_id, name, metric_type, dri_user_id, start_week)
               VALUES (1, 1, 'Acme Co', 'status', 1, ?)""", (START,))
        con.execute(
            """INSERT INTO metrics (id, section_id, name, metric_type, rollup, dri_user_id,
                                    start_week)
               VALUES (2, 2, 'New MRR', 'numeric', 'sum', 1, ?)""", (START,))
        tokens = {s: auth.new_api_token(con, s, s, 1) for s in ("read", "read_write", "admin")}
    yield TestClient(app), tokens


def hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


def row(sql, *args):
    with dbm.get_db() as con:
        return con.execute(sql, args).fetchone()


# ---------------- the gate

@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/v1/users", None),
    ("post", "/api/v1/users", {"display_name": "X", "email": "x@example.com"}),
    ("patch", "/api/v1/users/2", {"display_name": "X"}),
    ("get", "/api/v1/sections", None),
    ("post", "/api/v1/sections", {"name": "X"}),
    ("post", "/api/v1/metrics", {"section_id": 1, "name": "X", "metric_type": "status"}),
    ("patch", "/api/v1/metrics/1", {"dri_user_id": 2}),
    ("put", "/api/v1/metrics/2/targets", {"year": 2026, "quarter": 4, "baseline": 1, "stretch": 2}),
])
def test_admin_endpoints_refuse_lower_scopes(env, method, path, body):
    client, tokens = env
    for scope in ("read", "read_write"):
        kw = {"headers": hdr(tokens[scope])}
        if body is not None:
            kw["json"] = body
        r = getattr(client, method)(path, **kw)
        assert r.status_code == 403, (scope, path)
    assert row("SELECT dri_user_id FROM metrics WHERE id=1")["dri_user_id"] == 1
    assert row("SELECT COUNT(*) c FROM users")["c"] == 2


# ---------------- people

def test_add_a_person_returns_a_one_time_password_that_signs_in(env):
    client, tokens = env
    r = client.post("/api/v1/users", headers=hdr(tokens["admin"]),
                    json={"display_name": " Jordan Lee ", "email": "jordan@example.com",
                          "slack_member_id": "U0123"})
    assert r.status_code == 200
    out = r.json()
    assert out["display_name"] == "Jordan Lee" and out["role"] == "editor"
    assert out["slack_member_id"] == "U0123" and out["metrics_owned"] == 0
    u = row("SELECT * FROM users WHERE id=?", out["id"])
    assert u["must_change_password"] == 1
    assert auth.verify_password(out["temp_password"], u["password_hash"])
    assert out["temp_password"] not in u["password_hash"]


def test_duplicate_email_is_a_conflict(env):
    client, tokens = env
    r = client.post("/api/v1/users", headers=hdr(tokens["admin"]),
                    json={"display_name": "Ed again", "email": "ED@example.com"})
    assert r.status_code == 409


def test_the_api_cannot_mint_an_admin(env):
    client, tokens = env
    r = client.post("/api/v1/users", headers=hdr(tokens["admin"]),
                    json={"display_name": "X", "email": "x@example.com", "role": "admin"})
    assert r.status_code == 422
    r = client.patch("/api/v1/users/2", headers=hdr(tokens["admin"]), json={"role": "admin"})
    assert r.status_code == 422
    assert row("SELECT role FROM users WHERE id=2")["role"] == "editor"


def test_admin_accounts_are_read_only_here(env):
    # Re-pointing an admin's Slack id is a password-reset link delivered to
    # someone else: the one edit that turns a token into a browser admin.
    client, tokens = env
    for body in ({"slack_member_id": "UATTACKER"}, {"is_active": False},
                 {"role": "viewer"}, {"email": "new@example.com"}):
        r = client.patch("/api/v1/users/1", headers=hdr(tokens["admin"]), json=body)
        assert r.status_code == 409, body
    u = row("SELECT * FROM users WHERE id=1")
    assert (u["slack_member_id"], u["is_active"], u["role"], u["email"]) == \
        (None, 1, "admin", "boss@example.com")


def test_patch_changes_only_what_was_sent(env):
    client, tokens = env
    r = client.patch("/api/v1/users/2", headers=hdr(tokens["admin"]),
                     json={"slack_member_id": "U999"})
    assert r.status_code == 200
    u = row("SELECT * FROM users WHERE id=2")
    assert (u["display_name"], u["role"], u["slack_member_id"]) == ("Ed", "editor", "U999")
    client.patch("/api/v1/users/2", headers=hdr(tokens["admin"]), json={"slack_member_id": None})
    assert row("SELECT slack_member_id FROM users WHERE id=2")["slack_member_id"] is None


def test_deactivating_signs_the_person_out(env):
    client, tokens = env
    with dbm.get_db() as con:
        con.execute("INSERT INTO sessions (token_hash, user_id, expires_at) "
                    "VALUES ('h', 2, datetime('now', '+1 day'))")
    r = client.patch("/api/v1/users/2", headers=hdr(tokens["admin"]), json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert row("SELECT COUNT(*) c FROM sessions WHERE user_id=2")["c"] == 0


def test_list_people_counts_what_they_own(env):
    client, tokens = env
    people = {u["display_name"]: u for u in
              client.get("/api/v1/users", headers=hdr(tokens["admin"])).json()}
    assert people["Boss"]["metrics_owned"] == 2 and people["Ed"]["metrics_owned"] == 0


# ---------------- metrics

def test_reassign_an_owner(env):
    client, tokens = env
    r = client.patch("/api/v1/metrics/1", headers=hdr(tokens["admin"]), json={"dri_user_id": 2})
    assert r.status_code == 200
    out = r.json()
    assert (out["dri_user_id"], out["dri"], out["name"]) == (2, "Ed", "Acme Co")
    listed = {m["id"]: m for m in client.get("/api/v1/metrics", headers=hdr(tokens["read"])).json()}
    assert listed[1]["dri_user_id"] == 2 and listed[1]["dri"] == "Ed"


def test_clearing_and_refusing_owners(env):
    client, tokens = env
    assert client.patch("/api/v1/metrics/1", headers=hdr(tokens["admin"]),
                        json={"dri_user_id": 99}).status_code == 422
    with dbm.get_db() as con:
        con.execute("UPDATE users SET is_active = 0 WHERE id = 2")
    assert client.patch("/api/v1/metrics/1", headers=hdr(tokens["admin"]),
                        json={"dri_user_id": 2}).status_code == 422
    r = client.patch("/api/v1/metrics/1", headers=hdr(tokens["admin"]), json={"dri_user_id": None})
    assert r.status_code == 200 and r.json()["dri"] is None


def test_add_a_client_refuses_a_duplicate_name(env):
    client, tokens = env
    r = client.post("/api/v1/metrics", headers=hdr(tokens["admin"]),
                    json={"section_id": 1, "name": "  Beta   Inc ", "metric_type": "status",
                          "dri_user_id": 2})
    assert r.status_code == 200
    out = r.json()
    assert out["name"] == "Beta Inc" and out["rollup"] is None and out["dri"] == "Ed"
    r = client.post("/api/v1/metrics", headers=hdr(tokens["admin"]),
                    json={"section_id": 1, "name": "acme co", "metric_type": "status"})
    assert r.status_code == 409


def test_numeric_metric_gets_a_rollup_and_a_monday_start(env):
    client, tokens = env
    r = client.post("/api/v1/metrics", headers=hdr(tokens["admin"]),
                    json={"section_id": 2, "name": "Cash", "metric_type": "numeric",
                          "unit": "$", "start_week": "2026-01-12"})
    assert r.status_code == 200 and r.json()["rollup"] == "sum"
    assert r.json()["start_week"] == "2026-01-12"
    r = client.post("/api/v1/metrics", headers=hdr(tokens["admin"]),
                    json={"section_id": 2, "name": "Cash 2", "metric_type": "numeric",
                          "start_week": "2026-01-13"})
    assert r.status_code == 422


def test_rename_into_a_taken_name_is_refused(env):
    client, tokens = env
    r = client.patch("/api/v1/metrics/2", headers=hdr(tokens["admin"]),
                     json={"section_id": 1, "name": "ACME CO"})
    assert r.status_code == 409
    assert row("SELECT section_id FROM metrics WHERE id=2")["section_id"] == 2


def test_rollup_is_numeric_only(env):
    client, tokens = env
    assert client.patch("/api/v1/metrics/1", headers=hdr(tokens["admin"]),
                        json={"rollup": "average"}).status_code == 422


# ---------------- sections and targets

def test_add_a_section_once(env):
    client, tokens = env
    r = client.post("/api/v1/sections", headers=hdr(tokens["admin"]), json={"name": "Pipeline"})
    assert r.status_code == 200 and r.json()["metrics"] == 0
    assert client.post("/api/v1/sections", headers=hdr(tokens["admin"]),
                       json={"name": "pipeline"}).status_code == 409
    names = [s["name"] for s in client.get("/api/v1/sections", headers=hdr(tokens["admin"])).json()]
    assert names == ["Client Health", "Revenue", "Pipeline"]


def test_target_is_a_pair_and_idempotent(env):
    client, tokens = env
    body = {"year": 2026, "quarter": 4, "baseline": 8, "stretch": 10}
    r = client.put("/api/v1/metrics/2/targets", headers=hdr(tokens["admin"]), json=body)
    assert r.status_code == 200 and r.json()["changed"] is True
    r = client.put("/api/v1/metrics/2/targets", headers=hdr(tokens["admin"]), json=body)
    assert r.json()["changed"] is False
    t = row("SELECT * FROM targets WHERE metric_id=2")
    assert (t["baseline_value"], t["stretch_value"]) == (8, 10)
    assert client.put("/api/v1/metrics/2/targets", headers=hdr(tokens["admin"]),
                      json={"year": 2026, "quarter": 4, "baseline": 8}).status_code == 422


def test_status_metrics_have_no_targets(env):
    client, tokens = env
    r = client.put("/api/v1/metrics/1/targets", headers=hdr(tokens["admin"]),
                   json={"year": 2026, "quarter": 4, "baseline": 1, "stretch": 1})
    assert r.status_code == 422
