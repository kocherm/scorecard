"""The MCP server Claude connects to: handshake, tool discovery, tool calls,
auth on both the header and path-token forms, and the read-only guarantee."""
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
        tokens = {s: auth.new_api_token(con, s, s, 1) for s in ("read", "read_write")}
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


def test_tools_list_is_read_only(env):
    client, tk = env
    tools = rpc(client, tk["read"], "tools/list").json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"get_scorecard", "get_check_in_status", "list_metrics"}
    # Nothing that mutates: writes would land in the audit trail as the shared
    # connector token rather than a person.
    assert not any(w in n for t in tools for n in [t["name"]]
                   for w in ("write", "set", "update", "archive", "delete"))
    for t in tools:
        assert t["description"] and t["inputSchema"]["type"] == "object"


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
    assert {t["name"] for t in r.json()["result"]["tools"]} == {
        "get_scorecard", "get_check_in_status", "list_metrics"}
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
