"""The MCP server Claude connects to: handshake, tool discovery, tool calls,
auth on both the header and path-token forms, and the rule that a token's
scope decides the tools - read tokens (and so the shared URL-form connector)
never see a write."""
import json
from datetime import datetime, timezone

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
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role,
                                  slack_member_id)
               VALUES (1,'dri@example.com','x','Dana','editor','U0DRI')""")
        con.execute(
            """INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                    dri_user_id, start_week)
               VALUES (1, 1, 'Demos booked', 'numeric', 'sum', 1, ?)""", (START,))
        tokens = {s: auth.new_api_token(con, s, s, 1)
                  for s in ("read", "write", "read_write", "admin")}
    yield TestClient(app), tokens


def rpc(client, token, method, params=None, rpc_id=1, path="/mcp"):
    body = {"jsonrpc": "2.0", "id": rpc_id, "method": method}
    if params is not None:
        body["params"] = params
    if rpc_id is None:
        body.pop("id")
    return client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})


def tool(client, token, name, args=None):
    r = rpc(client, token, "tools/call", {"name": name, "arguments": args or {}})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def test_initialize_echoes_client_protocol_version(env):
    client, tk = env
    r = rpc(client, tk["read"], "initialize", {"protocolVersion": "2025-03-26"})
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-03-26"     # echo what the client asked for
    assert res["serverInfo"]["name"] == "scorecard"
    assert "tools" in res["capabilities"]


def test_initialize_falls_back_when_client_names_no_version(env):
    client, tk = env
    res = rpc(client, tk["read"], "initialize", {}).json()["result"]
    assert res["protocolVersion"]


READ_TOOLS = {"get_scorecard", "get_check_in_status", "list_metrics"}


def test_a_read_token_sees_only_read_tools(env):
    client, tk = env
    tools = rpc(client, tk["read"], "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == READ_TOOLS
    # Nothing that mutates: the shared Slack connector holds a read token, and
    # a write there would land in the audit trail as that token, not a person.
    assert not any(w in t["name"] for t in tools
                   for w in ("write", "set", "update", "archive", "delete", "report"))
    for t in tools:
        assert t["description"] and t["inputSchema"]["type"] == "object"


@pytest.mark.parametrize("scope", ["write", "read_write", "admin"])
def test_a_token_that_can_write_also_sees_report_number(env, scope):
    client, tk = env
    tools = rpc(client, tk[scope], "tools/list").json()["result"]["tools"]
    assert {t["name"] for t in tools} == READ_TOOLS | {"report_number"}
    # Admin-scope actions stay REST-only, whatever the token can do there.
    assert not any(w in t["name"] for t in tools
                   for w in ("archive", "user", "target", "section"))


def test_instructions_match_what_the_token_can_do(env):
    client, tk = env
    ro = rpc(client, tk["read"], "initialize", {}).json()["result"]["instructions"]
    rw = rpc(client, tk["write"], "initialize", {}).json()["result"]["instructions"]
    assert "cannot write" in ro and "report_number" not in ro
    assert "report_number" in rw


def entry(metric_id, week):
    with dbm.get_db() as con:
        return con.execute(
            """SELECT value_numeric, value_status, source, entered_by_token_id
               FROM entries WHERE metric_id = ? AND week_start = ?""",
            (metric_id, week)).fetchone()


def token_id(name):
    with dbm.get_db() as con:
        return con.execute("SELECT id FROM api_tokens WHERE name = ?",
                           (name,)).fetchone()["id"]


def test_report_number_writes_through_the_api_path(env, monkeypatch):
    client, tk = env
    pin(monkeypatch, AFTER_DEADLINE)
    out = tool(client, tk["write"], "report_number", {"metric_id": 1, "value": 7})
    assert out["ok"] and out["week_start"] == "2026-02-23", "defaults to last closed week"
    row = entry(1, "2026-02-23")
    assert row["value_numeric"] == 7
    # Audited exactly like a REST write: to the token, as source 'api'.
    assert row["source"] == "api" and row["entered_by_token_id"] == token_id("write")
    with dbm.get_db() as con:
        audit = con.execute("SELECT source, actor_token_id FROM entry_audit").fetchall()
    assert [tuple(a) for a in audit] == [("api", token_id("write"))]
    # ...and the board reads it back: the number is no longer outstanding.
    st = tool(client, tk["write"], "get_check_in_status")
    assert st["stale"] == [] and st["pending"] == []


def test_report_number_replaces_and_takes_an_explicit_week(env, monkeypatch):
    client, tk = env
    pin(monkeypatch, AFTER_DEADLINE)
    tool(client, tk["admin"], "report_number",
         {"metric_id": 1, "week_start": "2026-02-16", "value": 3})
    tool(client, tk["admin"], "report_number",
         {"metric_id": 1, "week_start": "2026-02-16", "value": 4})
    assert entry(1, "2026-02-16")["value_numeric"] == 4


def add_status_metric():
    with dbm.get_db() as con:
        con.execute(
            """INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                    dri_user_id, start_week)
               VALUES (2, 1, 'Pipeline health', 'status', NULL, 1, ?)""", (START,))


def test_report_number_status_metric(env, monkeypatch):
    client, tk = env
    pin(monkeypatch, AFTER_DEADLINE)
    add_status_metric()
    tool(client, tk["read_write"], "report_number", {"metric_id": 2, "status": "Y"})
    assert entry(2, "2026-02-23")["value_status"] == "Y"


def refused(client, token, args):
    r = rpc(client, token, "tools/call", {"name": "report_number", "arguments": args})
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    assert result["isError"] is True, result
    return result["content"][0]["text"]


@pytest.mark.parametrize("args, says", [
    ({"metric_id": 99, "value": 1}, "Unknown or archived metric"),
    ({"metric_id": 1}, "value is required"),
    ({"metric_id": 2, "status": "purple"}, "status must be"),
    ({"metric_id": 1, "value": 1, "week_start": "2026-03-16"}, "future week"),
    ({"metric_id": 1, "value": 1, "week_start": "2025-12-29"}, "Metric starts"),
    ({"metric_id": "1", "value": 1}, "metric_id must be an integer"),
    ({"metric_id": True, "value": 1}, "metric_id must be an integer"),
    ({"metric_id": 1, "value": "lots"}, "value"),
])
def test_report_number_refusals_are_tool_errors_and_write_nothing(env, monkeypatch,
                                                                  args, says):
    client, tk = env
    pin(monkeypatch, AFTER_DEADLINE)
    add_status_metric()
    assert says in refused(client, tk["write"], args)
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM entry_audit").fetchone()[0] == 0


def test_report_number_refuses_an_archived_metric(env, monkeypatch):
    client, tk = env
    pin(monkeypatch, AFTER_DEADLINE)
    with dbm.get_db() as con:
        con.execute("UPDATE metrics SET archived_at = datetime('now') WHERE id=1")
    assert "archived" in refused(client, tk["write"], {"metric_id": 1, "value": 1})


def test_a_read_token_cannot_call_report_number(env):
    client, tk = env
    err = rpc(client, tk["read"], "tools/call",
              {"name": "report_number",
               "arguments": {"metric_id": 1, "value": 5}}).json()["error"]
    assert err["code"] == -32602 and "read-only" in err["message"]
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0


def test_the_url_form_never_offers_a_write(env):
    """The shared connector's route. It already refuses any token but a read
    one, so it can never list or call report_number."""
    client, tk = env
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    assert {t["name"] for t in client.post(f"/mcp/t/{tk['read']}", json=body)
            .json()["result"]["tools"]} == READ_TOOLS
    for scope in ("write", "read_write", "admin"):
        assert client.post(f"/mcp/t/{tk[scope]}", json=body).status_code == 401


def test_arguments_must_be_an_object(env):
    client, tk = env
    err = rpc(client, tk["write"], "tools/call",
              {"name": "report_number", "arguments": [1, 5]}).json()["error"]
    assert err["code"] == -32602


def test_get_scorecard_returns_scored_state(env):
    client, tk = env
    state = tool(client, tk["read"], "get_scorecard")
    assert state["sections"][0]["metrics"][0]["name"] == "Demos booked"
    assert "stale" in state and "red" in state


def pin(monkeypatch, dt):
    """Pin the MCP clock. Whether the last closed week counts as late depends
    on the day of the week, so a wall-clock test would pass Wed-Sun and fail
    Mon-Tue."""
    from app import mcp as mcpm
    monkeypatch.setattr(mcpm, "_now", lambda: dt)


# 2026-02-23 is the last closed week from either of these vantage points; its
# deadline falls between them.
BEFORE_DEADLINE = datetime(2026, 3, 3, 18, 0, tzinfo=timezone.utc)   # Tue
AFTER_DEADLINE = datetime(2026, 3, 6, 18, 0, tzinfo=timezone.utc)    # Fri


def test_missing_number_is_pending_before_the_deadline(env, monkeypatch):
    """The whole point of the split: on Monday and Tuesday - when chasing
    someone is still useful - the metric is pending, not yet late."""
    client, tk = env
    pin(monkeypatch, BEFORE_DEADLINE)
    st = tool(client, tk["read"], "get_check_in_status")
    assert st["last_closed_week"] == "2026-02-23"
    assert [p["name"] for p in st["pending"]] == ["Demos booked"]
    assert st["stale"] == [], "not late yet"
    assert st["pending"][0]["dri_slack_member_id"] == "U0DRI"
    assert st["entries_due_by"] and st["stale_after"]


def test_the_same_metric_is_stale_once_the_deadline_passes(env, monkeypatch):
    client, tk = env
    pin(monkeypatch, AFTER_DEADLINE)
    st = tool(client, tk["read"], "get_check_in_status")
    assert [s["name"] for s in st["stale"]] == ["Demos booked"]
    assert st["pending"] == [], "it has moved out of pending, not been duplicated"
    assert st["stale"][0]["dri"] == "Dana"
    assert st["stale"][0]["dri_slack_member_id"] == "U0DRI"


def test_both_lists_empty_once_the_number_is_in(env, monkeypatch):
    client, tk = env
    pin(monkeypatch, AFTER_DEADLINE)
    with dbm.get_db() as con:
        con.execute(
            """INSERT INTO entries (metric_id, week_start, value_numeric, source,
                                    entered_by_user_id)
               VALUES (1, '2026-02-23', 12, 'manual', 1)""")
    st = tool(client, tk["read"], "get_check_in_status")
    assert st["stale"] == [] and st["pending"] == []


def test_list_metrics_and_archived_flag(env):
    client, tk = env
    assert [m["name"] for m in tool(client, tk["read"], "list_metrics")] == ["Demos booked"]
    with dbm.get_db() as con:
        con.execute("UPDATE metrics SET archived_at = datetime('now') WHERE id=1")
    assert tool(client, tk["read"], "list_metrics") == []
    assert len(tool(client, tk["read"], "list_metrics", {"include_archived": True})) == 1


def test_unknown_tool_is_a_protocol_error(env):
    client, tk = env
    err = rpc(client, tk["read"], "tools/call",
              {"name": "write_entry", "arguments": {}}).json()["error"]
    assert err["code"] == -32602


def test_unknown_method_is_method_not_found(env):
    client, tk = env
    assert rpc(client, tk["read"], "resources/list").json()["error"]["code"] == -32601


def test_notification_gets_202_and_no_body(env):
    client, tk = env
    r = rpc(client, tk["read"], "notifications/initialized", rpc_id=None)
    assert r.status_code == 202 and not r.content


def test_batch_returns_array(env):
    client, tk = env
    r = client.post("/mcp", headers={"Authorization": f"Bearer {tk['read']}"}, json=[
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    body = r.json()
    assert isinstance(body, list) and [m["id"] for m in body] == [1, 2]


def test_auth_required(env):
    client, _ = env
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                     "method": "tools/list"}).status_code == 401
    assert rpc(client, "sc_not_a_real_token", "tools/list").status_code == 401


def test_path_token_form_works_and_rejects_a_bad_token(env):
    client, tk = env
    r = client.post(f"/mcp/t/{tk['read']}",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200
    assert {t["name"] for t in r.json()["result"]["tools"]} == READ_TOOLS
    bad = client.post("/mcp/t/sc_nope",
                      json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert bad.status_code == 401


def test_path_token_ignores_a_stale_authorization_header(env):
    client, tk = env
    r = client.post(f"/mcp/t/{tk['read']}", headers={"Authorization": "Bearer sc_nope"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200, "the path token is the credential on this route"


def test_revoked_token_loses_access(env):
    client, tk = env
    with dbm.get_db() as con:
        con.execute("UPDATE api_tokens SET revoked_at = datetime('now') WHERE name='read'")
    assert rpc(client, tk["read"], "tools/list").status_code == 401


def test_parse_error(env):
    client, tk = env
    r = client.post("/mcp", headers={"Authorization": f"Bearer {tk['read']}",
                                     "Content-Type": "application/json"},
                    content=b"{not json")
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700


def test_get_does_not_hold_a_stream_open(env):
    client, tk = env
    assert client.get("/mcp", headers={"Authorization": f"Bearer {tk['read']}"}
                      ).status_code == 405
