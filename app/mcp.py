"""Remote MCP server - the surface Claude (in Slack, or anywhere connectors
reach) uses to read the scorecard, and an agent with its own token uses to
report numbers.

The token's scope decides the tools, exactly as it decides the REST endpoints.
A read token sees the read tools only; a write, read_write or admin token also
sees report_number. Every read answers from app/api.py's builders, which run
the same pure engine as the TV and the edit grid, so Claude can never quote a
number the board disagrees with, and report_number writes through
api.apply_entry, the same path as POST /api/v1/metrics/{id}/entries - same
validation, and audited as source 'api' to the token, like any API write.

What stays read-only is the SHARED connector: Claude in Slack talks to one
URL for everyone, so a write there would be "the Slack token" rather than the
person who reported the number. That connector uses the URL form below, which
accepts read tokens only, so it never sees a write tool. Admin-scope actions
(users, metrics, targets) are REST-only on purpose - a confident agent
mistake there restructures the board, not one week's number.

Transport is MCP Streamable HTTP: one endpoint, JSON-RPC 2.0 in the POST body,
plain JSON back (no SSE - nothing here streams, and no session state is kept,
so any number of connector instances can share the endpoint).

Auth, two ways, because Claude's custom-connector form takes a URL and OAuth
credentials but no custom header:
  - Authorization: Bearer sc_...   preferred; works for curl, n8n, MCP
    clients that let you set headers.
  - POST /mcp/t/{token}            same token in the path, for clients that
    only accept a URL. The token lands in the reverse proxy's access log, so
    only a READ-scoped token is accepted here (anything else gets 401); rotate
    it on the Admin > API tokens page if the URL ever leaks.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from pydantic import ValidationError

from . import api as apim
from . import db as dbm
from .auth import api_token_from_request, api_token_from_value

log = logging.getLogger("scorecard.mcp")

router = APIRouter()

# Echoed back to the client when it does not name a version it wants.
DEFAULT_PROTOCOL = "2025-06-18"

SERVER_INFO = {"name": "scorecard", "version": "1.0.0"}

TOOLS = [
    {
        "name": "get_scorecard",
        "description": (
            "Full current state of the company scorecard: every section and "
            "metric with its target, last closed week's value and colour, "
            "current week, 4-week trend, and red streak - plus ready-made "
            "'stale' and 'red' lists. Use this to answer any question about "
            "how the company is doing, what is off track, or what a specific "
            "metric is at. Call it before answering from memory: the numbers "
            "change weekly."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_check_in_status",
        "description": (
            "Who still owes a number for the last closed week, split into "
            "'pending' (due, deadline not passed yet - these are the people "
            "worth nudging now) and 'stale' (deadline passed, already late). "
            "Each entry carries the DRI's name and Slack member ID; mention "
            "people by that ID rather than by name. Also returns "
            "'entries_due_by' and 'stale_after' so you can say how long is "
            "left. Use this for any 'who hasn't updated?' or 'chase the "
            "stragglers' request. Both lists empty means everyone is in."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_metrics",
        "description": (
            "Flat id/name/section/DRI list of metrics. Use it to resolve a "
            "metric someone named loosely ('our demo number') to a specific "
            "id before quoting or discussing it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_archived": {
                    "type": "boolean",
                    "description": "Include retired metrics. Defaults to false.",
                }
            },
            "additionalProperties": False,
        },
    },
]

# Listed only for a token that can write (see _tools_for). Mirrors POST
# /api/v1/metrics/{id}/entries and calls the same function.
WRITE_TOOLS = [
    {
        "name": "report_number",
        "description": (
            "Set one metric's value for one week. It REPLACES whatever is "
            "there, including a number a person typed, so only report a "
            "number someone actually gave you or that you read from a source "
            "of truth - never an estimate. Resolve the metric with "
            "list_metrics first and pass its id; never use an id from memory. "
            "week_start defaults to the last closed week, which is the week "
            "check-ins ask for. Numeric and binary metrics take 'value' "
            "(binary: any non-zero is yes); status metrics take 'status' "
            "(R, Y or G). Returns the week written; a refusal (unknown "
            "metric, future week, wrong field for the type) writes nothing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "metric_id": {"type": "integer",
                              "description": "Metric id from list_metrics."},
                "week_start": {"type": "string",
                               "description": "Monday of the week, YYYY-MM-DD. "
                                              "Defaults to the last closed week."},
                "value": {"type": "number",
                          "description": "For numeric and binary metrics."},
                "status": {"type": "string", "enum": ["R", "Y", "G"],
                           "description": "For status metrics."},
            },
            "required": ["metric_id"],
            "additionalProperties": False,
        },
    },
]
WRITE_TOOL_NAMES = {t["name"] for t in WRITE_TOOLS}


def _can_write(token: sqlite3.Row) -> bool:
    # The same rule api_token_from_value applies for need_write.
    return token["scope"] != "read"


def _tools_for(token: sqlite3.Row) -> list[dict]:
    return TOOLS + WRITE_TOOLS if _can_write(token) else TOOLS


def _instructions(token: sqlite3.Row) -> str:
    if _can_write(token):
        return ("Read and report numbers on the company scorecard. Call "
                "get_scorecard for the state of the board and "
                "get_check_in_status for who still owes a number. "
                "report_number writes a week's value and replaces what is "
                "there: resolve the metric with list_metrics first, and only "
                "report numbers you were given or read from a source of truth.")
    return ("Read-only access to the company scorecard. Call get_scorecard "
            "for the state of the board and get_check_in_status for who "
            "still owes a number. You cannot write numbers through this "
            "server - if someone wants to report a value, point them at "
            "their weekly check-in DM or the My Numbers page.")


# ---------------------------------------------------------------- tools
def _now() -> datetime:
    """The clock, in one place. app/weeks.py and app/scoring.py stay pure by
    taking `now` as an argument; this is where the impure value enters the MCP
    path, and the seam tests pin so "who is stale" does not depend on which
    day of the week the suite runs."""
    return datetime.now(timezone.utc)


def _call_tool(name: str, args: dict, con: sqlite3.Connection,
               token: sqlite3.Row) -> Any:
    now = _now()
    if name == "get_scorecard":
        return apim.build_scorecard(con, now)
    if name == "get_check_in_status":
        state = apim.build_scorecard(con, now)
        return {
            "last_closed_week": state["last_closed_week"],
            "entries_due_by": state["entries_due_by"],
            "stale_after": state["stale_after"],
            "pending": state["pending"],
            "stale": state["stale"],
        }
    if name == "list_metrics":
        return apim.metrics_rows(con, bool(args.get("include_archived", False)))
    if name == "report_number":
        mid = args.get("metric_id")
        if not isinstance(mid, int) or isinstance(mid, bool):
            raise HTTPException(422, "metric_id must be an integer id from list_metrics")
        body = apim.EntryIn(week_start=args.get("week_start"), value=args.get("value"),
                            status=args.get("status"))
        return apim.apply_entry(con, token, mid, body, now)
    raise KeyError(name)


def _refusal(detail: Any) -> str:
    return detail if isinstance(detail, str) else json.dumps(detail)


# ---------------------------------------------------------------- JSON-RPC
def _result(rpc_id, payload: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": payload}


def _error(rpc_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


def _dispatch(msg: dict, con: sqlite3.Connection,
              token: sqlite3.Row) -> Optional[dict]:
    """One JSON-RPC message in, one response out - or None for a notification,
    which by spec gets an empty 202 rather than a body."""
    if msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        return _error(msg.get("id"), -32600, "Invalid Request")
    method, rpc_id = msg["method"], msg.get("id")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    if method == "initialize":
        return _result(rpc_id, {
            "protocolVersion": params.get("protocolVersion") or DEFAULT_PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": _instructions(token),
        })
    if method == "ping":
        return _result(rpc_id, {})
    if method.startswith("notifications/"):
        return None
    if method == "tools/list":
        return _result(rpc_id, {"tools": _tools_for(token)})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(name, str):
            return _error(rpc_id, -32602, "Missing tool name")
        if not isinstance(args, dict):
            return _error(rpc_id, -32602, "arguments must be an object")
        # Not listed for this token, so not callable either. Said plainly
        # rather than "unknown tool", so an agent handed the wrong token
        # can tell its operator what to fix.
        if name in WRITE_TOOL_NAMES and not _can_write(token):
            return _error(rpc_id, -32602, f"{name} needs a write token; this one is read-only")
        try:
            payload = _call_tool(name, args, con, token)
        except KeyError:
            return _error(rpc_id, -32602, f"Unknown tool: {name}")
        except (HTTPException, ValidationError) as e:
            # A refusal the agent can act on (wrong id, future week, missing
            # value). Both are raised before anything is written.
            text = (_refusal(e.detail) if isinstance(e, HTTPException)
                    else "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                                   for x in e.errors()))
            return _result(rpc_id, {"content": [{"type": "text", "text": text}],
                                    "isError": True})
        except Exception:  # a broken tool is a tool-level failure, not a dead session
            log.exception("MCP tool %s failed", name)
            return _result(rpc_id, {
                "content": [{"type": "text", "text": f"{name} failed - see server logs."}],
                "isError": True,
            })
        return _result(rpc_id, {
            "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
            "isError": False,
        })
    if is_notification:
        return None
    return _error(rpc_id, -32601, f"Method not found: {method}")


async def _handle(request: Request, con: sqlite3.Connection,
                  token: sqlite3.Row) -> Response:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(_error(None, -32700, "Parse error"), status_code=400)

    # A batch is a JSON array; a single call is an object. Both are valid.
    batch = isinstance(body, list)
    msgs = body if batch else [body]
    if batch and not msgs:
        return JSONResponse(_error(None, -32600, "Invalid Request"), status_code=400)

    out = []
    for m in msgs:
        if not isinstance(m, dict):
            out.append(_error(None, -32600, "Invalid Request"))
            continue
        r = _dispatch(m, con, token)
        if r is not None:
            out.append(r)
    if not out:  # notifications only
        return Response(status_code=202)
    return JSONResponse(out if batch else out[0])


# ---------------------------------------------------------------- routes
# These handlers are async (the body has to be awaited), so they open their own
# connection rather than taking db_dep - a sync dependency runs in the
# threadpool and its sqlite handle cannot cross back to the event loop thread.
# Same pattern as the Slack and Telegram webhooks.
@router.post("/mcp")
async def mcp_endpoint(request: Request):
    with dbm.get_db() as con:
        token = api_token_from_request(request, con, need_write=False)
        return await _handle(request, con, token)


@router.post("/mcp/t/{token}")
async def mcp_endpoint_path_token(token: str, request: Request):
    """Same endpoint for connector UIs that accept only a URL. The path token
    is the credential here; any Authorization header is ignored rather than
    merged, so there is never a question of which one won.

    READ scope only: a URL ends up in proxy logs, browser history and
    connector settings screens, so a token that can write must never work
    here. Refused with the same 401 as an unknown token - this form does not
    confirm that a write token exists."""
    with dbm.get_db() as con:
        row = api_token_from_value(con, token, need_write=False)
        if row["scope"] != "read":
            raise HTTPException(status_code=401, detail="Invalid token")
        return await _handle(request, con, row)


@router.get("/mcp")
def mcp_no_stream():
    """Streamable HTTP lets a server decline the server-initiated SSE channel;
    nothing here pushes, so say so rather than holding a socket open."""
    raise HTTPException(status_code=405, detail="This MCP server does not stream")
