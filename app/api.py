"""JSON API for Hermes and other integrations. Bearer token auth.

GET  /api/v1/scorecard           full current state (same scoring as the TV)
GET  /api/v1/metrics             id/name list for writers (?include_archived=true)
POST /api/v1/metrics/{id}/entries  {"week_start": "YYYY-MM-DD" (a Monday, optional,
                                    defaults to last closed week),
                                    "value": number  OR  "status": "R"|"Y"|"G"}
POST /api/v1/metrics/{id}/archive    {"effective_week": "YYYY-MM-DD" (a Monday,
                                      optional, defaults to this week)}  admin scope
POST /api/v1/metrics/{id}/unarchive  admin scope
GET  /api/v1/ceo                 the seven CEO slots, the metric filling each,
                                 and each breakdown's categories (ids + names)
POST /api/v1/ceo/{slot}/breakdown  {"week_start": optional Monday,
                                    "categories": {"<name or id>": number, ...},
                                    "total": "sum" (default) | number | null}
                                 one week of categories in ONE call - the shape
                                 an accounting export (QuickBooks -> n8n) or a
                                 finance agent already has. All-or-nothing:
                                 every name is checked before anything is
                                 written. "sum" writes the tile's total only
                                 when every category has a number that week.

Archiving is the soft delete behind "remove this client": history is preserved,
but the row leaves every surface (board, edit grid, API, alerts) outright,
whatever its archive date - those all filter archived_at IS NULL.
effective_week records *when* the client left. It drives scoring's na-tail via
MetricInfo.archived_week, which only surfaces through build_grid's
include_archived flag; no shipped view passes that today, so treat the field as
an honest date for the record rather than a display change.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Literal, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Security
from fastapi.security import HTTPBearer
from pydantic import BaseModel, Field

from . import db as dbm
from . import grid as gridm
from . import weeks as wk
from .auth import api_token_from_request
from .db import db_dep

# Documentation only: auto_error=False, so authentication stays in
# api_token_from_request (its 401/403 messages, its scope rules). This just
# puts the bearer scheme and the lock icon into the OpenAPI schema.
bearer = HTTPBearer(auto_error=False, description="An API token (starts with sc_) "
                    "from Admin > API tokens.")
router = APIRouter(prefix="/api/v1", dependencies=[Security(bearer)])


# ------------------------------------------------ documented response shapes
# Used ONLY as `responses=` documentation, never as response_model: a model
# filters what it does not declare, and the builders here are shared with the
# MCP server, so the schema must describe the output, not reshape it.
class Error(BaseModel):
    detail: Union[str, dict, list] = Field(
        description="A message, or for unknown breakdown categories an object "
                    "listing them; request-shape errors return FastAPI's list form.")


class CellOut(BaseModel):
    state: Optional[str] = Field(None, description="green, yellow, red, stale, pending, "
                                 "no-target, or na (before the metric started)")
    value: Union[float, str, None] = Field(None, description="The number, or R/Y/G for status metrics")


class MetricStateOut(BaseModel):
    id: int
    name: str
    type: Literal["numeric", "binary", "status"]
    unit: Optional[str] = Field(None, examples=["$"])
    dri: str = Field(description="Owner's display name, '-' when unowned")
    target: str = Field(description="This week's target, formatted; '-' when none")
    last_closed_week: CellOut
    current_week: CellOut
    red_streak: int = Field(description="Consecutive closed weeks in red")
    escalation_level: int = Field(description="0 none, 1 file a 1-3-1, 2 1:1, 3 structural")
    one_three_one_filed: bool
    trend: list[str] = Field(description="States of the last four closed weeks, oldest first")


class SectionOut(BaseModel):
    name: str
    metrics: list[MetricStateOut]


class PersonRef(BaseModel):
    id: int = Field(description="Metric id")
    name: str = Field(description="Metric name")
    dri: str
    dri_slack_member_id: Optional[str] = None


class RedRef(PersonRef):
    weeks_red: int
    one_three_one_filed: bool


class ScorecardOut(BaseModel):
    current_week: str = Field(examples=["2026-09-14"])
    current_week_label: str = Field(examples=["Q3-W11"])
    last_closed_week: str = Field(examples=["2026-09-07"])
    entries_due_by: str = Field(description="Deadline for last closed week's numbers")
    stale_after: str = Field(description="When a still-missing number turns stale")
    sections: list[SectionOut]
    pending: list[PersonRef] = Field(description="Missing, deadline not yet passed")
    stale: list[PersonRef] = Field(description="Missing, deadline passed")
    red: list[RedRef]


class MetricListItem(BaseModel):
    id: int
    name: str
    metric_type: Literal["numeric", "binary", "status"]
    unit: Optional[str] = None
    archived_at: Optional[str] = None
    section: str
    dri: Optional[str] = None


class EntryOut(BaseModel):
    ok: bool = True
    metric_id: int
    week_start: str = Field(examples=["2026-09-07"])
    week_label: str = Field(examples=["Q3-W10"])


class CategoryOut(BaseModel):
    metric_id: int
    name: str
    unit: Optional[str] = None


class CeoSlotOut(BaseModel):
    slot: Literal["revenue", "expenses", "leads", "conversions", "cac", "retention", "profit"]
    label: str
    metric_id: Optional[int] = Field(None, description="null when no metric fills the tile")
    metric_name: Optional[str] = None
    unit: Optional[str] = None
    breakdown: Optional[list[CategoryOut]] = Field(
        None, description="Present only on tiles that can be broken down "
                          "(revenue, expenses, leads); empty until categories are added")


class CeoOut(BaseModel):
    slots: list[CeoSlotOut]


class WrittenOut(BaseModel):
    metric_id: int
    name: str
    value: float


class BreakdownOut(BaseModel):
    ok: bool = True
    slot: str
    week_start: str
    written: list[WrittenOut]
    missing: list[str] = Field(description="Categories with no number for this week yet")
    total_metric_id: Optional[int] = None
    total_written: Optional[float] = Field(
        None, description="The total written to the tile, or null when none was written")


class ArchiveOut(BaseModel):
    ok: bool = True
    metric_id: int
    name: str
    archived: Literal[True] = True
    was_already_archived: bool
    effective_week: str
    effective_week_label: str


class UnarchiveOut(BaseModel):
    ok: bool = True
    metric_id: int
    name: str
    archived: Literal[False] = False
    was_archived: bool


def _errors(*codes: int) -> dict:
    text = {400: "Bad request", 401: "Missing or invalid token",
            403: "Token scope too low", 404: "Not found",
            409: "Conflict with current configuration",
            422: "Invalid input (see detail)"}
    return {c: {"model": Error, "description": text[c]} for c in codes}


def _read_token(request: Request, con: sqlite3.Connection = Depends(db_dep)):
    return api_token_from_request(request, con, need_write=False)


def _slack_ids(con: sqlite3.Connection) -> dict[int, Optional[str]]:
    """user_id -> Slack member id, for the stale/red lists. Lets a caller that
    can post to Slack (the MCP server, an automation) address the DRI directly
    instead of guessing at a display name."""
    return {r["id"]: r["slack_member_id"]
            for r in con.execute("SELECT id, slack_member_id FROM users").fetchall()}


def build_scorecard(con: sqlite3.Connection, now: datetime) -> dict:
    """Full scored state. Shared by GET /api/v1/scorecard and the MCP server so
    both answer from the same engine the TV and the edit grid use."""
    vm = gridm.build_grid(con, now)
    week = wk.last_closed_week(now)
    slack = _slack_ids(con)
    out = {
        "current_week": vm.current_week.isoformat(),
        "current_week_label": vm.quarter_label,
        "last_closed_week": week.isoformat(),
        "entries_due_by": wk.entry_deadline(week).isoformat(),
        "stale_after": wk.stale_at(week).isoformat(),
        "sections": [],
        "pending": [],
        "stale": [],
        "red": [],
    }
    for s in vm.sections:
        sec = {"name": s.name, "metrics": []}
        for r in s.rows:
            closed_cell = next((c for c in r.cells if c.week == week), None)
            cur_cell = next((c for c in r.cells if c.is_current), None)
            m = {
                "id": r.metric_id,
                "name": r.name,
                "type": r.metric_type,
                "unit": r.unit,
                "dri": r.dri_name,
                "target": r.target_display,
                "last_closed_week": {
                    "state": closed_cell.state.value if closed_cell else None,
                    "value": closed_cell.raw if closed_cell else None,
                },
                "current_week": {
                    "state": cur_cell.state.value if cur_cell else None,
                    "value": cur_cell.raw if cur_cell else None,
                },
                "red_streak": r.red_streak,
                "escalation_level": r.escalation,
                "one_three_one_filed": r.has_131,
                "trend": [sp["state"] for sp in r.spark],
            }
            sec["metrics"].append(m)
            # Missing numbers split by whether the deadline has passed. Chasing
            # is only useful while "pending" is still true - once a metric is
            # stale the week is already late, so a list of stale rows answers
            # "who was late" rather than "who should I nudge now".
            if closed_cell and closed_cell.state.value == "pending":
                out["pending"].append({"id": r.metric_id, "name": r.name, "dri": r.dri_name,
                                       "dri_slack_member_id": slack.get(r.dri_user_id)})
            if closed_cell and closed_cell.state.value == "stale":
                out["stale"].append({"id": r.metric_id, "name": r.name, "dri": r.dri_name,
                                     "dri_slack_member_id": slack.get(r.dri_user_id)})
            if r.red_streak >= 1:
                out["red"].append({"id": r.metric_id, "name": r.name, "dri": r.dri_name,
                                   "dri_slack_member_id": slack.get(r.dri_user_id),
                                   "weeks_red": r.red_streak,
                                   "one_three_one_filed": r.has_131})
        out["sections"].append(sec)
    return out


@router.get("/scorecard", tags=["Scorecard"], summary="Get the scored scorecard",
            responses={200: {"model": ScorecardOut}, **_errors(401)},
            description="Every visible metric with its target, last closed and current "
                        "week, trend and red streak, plus ready-made pending, stale and "
                        "red lists. Scored by the same engine as the board and the TV. "
                        "Any token scope.")
def scorecard_state(request: Request, con: sqlite3.Connection = Depends(db_dep)):
    api_token_from_request(request, con, need_write=False)
    return build_scorecard(con, datetime.now(timezone.utc))


def metrics_rows(con: sqlite3.Connection, include_archived: bool = False) -> list[dict]:
    arch = "" if include_archived else "WHERE m.archived_at IS NULL"
    rows = con.execute(
        f"""SELECT m.id, m.name, m.metric_type, m.unit, m.archived_at,
                   s.name AS section, u.display_name AS dri
            FROM metrics m JOIN sections s ON s.id = m.section_id
            LEFT JOIN users u ON u.id = m.dri_user_id
            {arch} ORDER BY s.sort_order, m.sort_order"""
    ).fetchall()
    return [dict(r) for r in rows]


@router.get("/metrics", tags=["Metrics"], summary="List metrics",
            responses={200: {"model": list[MetricListItem]}, **_errors(401)},
            description="Ids and names to write to. Includes metrics in hidden "
                        "sections. Any token scope.")
def list_metrics(request: Request,
                 include_archived: bool = Query(False, description="Also list archived metrics"),
                 con: sqlite3.Connection = Depends(db_dep)):
    api_token_from_request(request, con, need_write=False)
    return metrics_rows(con, include_archived)


class EntryIn(BaseModel):
    week_start: Optional[str] = Field(
        None, description="Monday of the week (YYYY-MM-DD). Defaults to the last closed week.",
        examples=["2026-09-07"])
    value: Optional[float] = Field(None, description="Required for numeric and binary "
                                   "metrics (binary: any non-zero is yes)", examples=[12])
    status: Optional[str] = Field(None, description='Required for status metrics: "R", "Y" or "G"')


@router.post("/metrics/{metric_id}/entries", tags=["Metrics"], summary="Write a weekly value",
             responses={200: {"model": EntryOut}, **_errors(401, 403, 404, 422)},
             description="Sets the metric's value for one week, replacing any value "
                         "already there (including one typed by hand). Idempotent: "
                         "sending the same week again overwrites it. Needs a write, "
                         "read_write or admin token.")
def write_entry(body: EntryIn, request: Request, metric_id: int = Path(description="Metric id from GET /api/v1/metrics"),
                con: sqlite3.Connection = Depends(db_dep)):
    token = api_token_from_request(request, con, need_write=True)
    m = con.execute("SELECT * FROM metrics WHERE id = ? AND archived_at IS NULL",
                    (metric_id,)).fetchone()
    if m is None:
        raise HTTPException(404, "Unknown or archived metric")

    now = datetime.now(timezone.utc)
    week = _week_or_422(body.week_start, now)
    if week < wk.parse_week(m["start_week"]):
        raise HTTPException(422, f"Metric starts {m['start_week']}")

    if m["metric_type"] == "status":
        if body.status not in ("R", "Y", "G"):
            raise HTTPException(422, 'status must be "R", "Y" or "G"')
        dbm.upsert_entry(con, metric_id, week, value_status=body.status,
                         source="api", token_id=token["id"])
    else:
        if body.value is None:
            raise HTTPException(422, "value is required for numeric/binary metrics")
        v = float(body.value)
        if m["metric_type"] == "binary":
            v = 1.0 if v else 0.0
        dbm.upsert_entry(con, metric_id, week, value_numeric=v,
                         source="api", token_id=token["id"])
    return {"ok": True, "metric_id": metric_id, "week_start": week.isoformat(),
            "week_label": wk.quarter_label(week)}


def _week_or_422(week_start: Optional[str], now: datetime):
    if week_start:
        try:
            week = wk.parse_week(week_start)
        except ValueError as e:
            raise HTTPException(422, str(e))
    else:
        week = wk.last_closed_week(now)
    if week > wk.current_week(now):
        raise HTTPException(422, "Cannot write a future week")
    return week


# ------------------------------------------------------------ CEO metrics
def ceo_state(con: sqlite3.Connection) -> dict:
    from . import ceo as ceom
    mapping = ceom.resolve_slots(con)
    names = {r["id"]: r for r in con.execute(
        "SELECT id, name, unit FROM metrics WHERE archived_at IS NULL")}
    slots = []
    for s in ceom.SLOTS:
        mid = mapping.get(s.key)
        slot = {"slot": s.key, "label": s.label,
                "metric_id": mid, "metric_name": names[mid]["name"] if mid in names else None,
                "unit": names[mid]["unit"] if mid in names else s.unit}
        if s.key in ceom.BREAKDOWN_SLOTS:
            slot["breakdown"] = [{"metric_id": i, "name": names[i]["name"],
                                  "unit": names[i]["unit"]}
                                 for i in ceom.breakdown_ids(con, s.key) if i in names]
        slots.append(slot)
    return {"slots": slots}


@router.get("/ceo", tags=["CEO metrics"], summary="List CEO tiles and categories",
            responses={200: {"model": CeoOut}, **_errors(401)},
            description="The seven CEO tiles, the metric filling each, and the "
                        "categories of tiles that are broken down. Use it to find the "
                        "names a breakdown call accepts. Any token scope.")
def ceo_slots(request: Request, con: sqlite3.Connection = Depends(db_dep)):
    api_token_from_request(request, con, need_write=False)
    return ceo_state(con)


class BreakdownIn(BaseModel):
    week_start: Optional[str] = Field(
        None, description="Monday of the week (YYYY-MM-DD). Defaults to the last closed week.",
        examples=["2026-09-07"])
    categories: dict[str, Optional[float]] = Field(
        description="Category name (case-insensitive) or metric id -> amount. Send 0 "
                    "for none. Categories left out keep their current value.",
        examples=[{"Payroll": 18000, "Contractors": 6500, "Other": 1200}])
    total: Union[float, Literal["sum"], None] = Field(
        "sum", description='"sum" writes the tile total as the categories added up, '
                           "only once every category has a number for the week; a "
                           "number writes that total; null leaves the total alone.")


@router.post("/ceo/{slot}/breakdown", tags=["CEO metrics"],
             summary="Write a week of categories",
             responses={200: {"model": BreakdownOut}, **_errors(401, 403, 404, 409, 422)},
             description="Writes one week of a tile's categories in a single call, and "
                         "optionally its total. All-or-nothing: every category name is "
                         "checked first, and an unknown name writes nothing and returns "
                         "the valid names. Names are never created. `slot` is revenue, "
                         "expenses or leads. Needs a write, read_write or admin token.")
def write_breakdown(body: BreakdownIn, request: Request,
                    slot: str = Path(description="The tile: revenue, expenses or leads",
                                     examples=["expenses"]),
                    con: sqlite3.Connection = Depends(db_dep)):
    """Write one week of a tile's categories, and optionally its total.

    Validates EVERYTHING before writing anything: a nightly export that names
    one category wrong must fail loudly and leave the week as it was, not land
    two of three numbers and a total that no longer adds up. Unknown names are
    refused, never created - a typo in an n8n mapping must not grow a fourth
    category. Every write is an ordinary API entry, audited to the token."""
    from . import ceo as ceom
    token = api_token_from_request(request, con, need_write=True)
    if slot not in ceom.BREAKDOWN_SLOTS:
        raise HTTPException(404, f"No breakdown for {slot!r}; "
                                 f"one of {', '.join(ceom.BREAKDOWN_SLOTS)}")
    now = datetime.now(timezone.utc)
    week = _week_or_422(body.week_start, now)
    ids = ceom.breakdown_ids(con, slot)
    if not ids:
        raise HTTPException(409, f"{slot} has no categories yet - add them in "
                                 "Admin > Settings > CEO view")
    if not body.categories:
        raise HTTPException(422, "categories is empty")
    resolved, unknown = {}, []
    for ref, value in body.categories.items():
        mid = ceom.resolve_category(con, slot, ref)
        if mid is None:
            unknown.append(ref)
        elif value is None:
            raise HTTPException(422, f"{ref}: value is required (send 0 for none)")
        else:
            resolved[mid] = float(value)
    if unknown:
        valid = [r["name"] for r in con.execute(
            f"SELECT name FROM metrics WHERE id IN ({','.join('?' * len(ids))})", ids)]
        raise HTTPException(422, {"error": "unknown categories", "unknown": unknown,
                                  "categories": valid})
    total = body.total
    if not (total is None or total == "sum" or isinstance(total, (int, float))):
        raise HTTPException(422, 'total must be "sum", a number, or null')
    parent_id = ceom.resolve_slots(con).get(slot)
    starts = {r["id"]: r for r in con.execute(
        f"SELECT id, name, start_week FROM metrics WHERE id IN ({','.join('?' * (len(ids) + 1))})",
        [*ids, parent_id or 0])}
    for mid in [*resolved, *([parent_id] if total is not None and parent_id else [])]:
        if week < wk.parse_week(starts[mid]["start_week"]):
            raise HTTPException(422, f"{starts[mid]['name']} starts "
                                     f"{starts[mid]['start_week']}; nothing was written")

    for mid, v in resolved.items():
        dbm.upsert_entry(con, mid, week, value_numeric=v, source="api", token_id=token["id"])

    have = {r["metric_id"]: r["value_numeric"] for r in con.execute(
        f"""SELECT metric_id, value_numeric FROM entries WHERE week_start = ?
            AND value_numeric IS NOT NULL AND metric_id IN ({','.join('?' * len(ids))})""",
        [week.isoformat(), *ids])}
    missing = [i for i in ids if i not in have]
    written_total = None
    if total is not None and parent_id is not None:
        if total == "sum":
            if not missing:
                written_total = sum(have.values())
        else:
            written_total = float(total)
        if written_total is not None:
            dbm.upsert_entry(con, parent_id, week, value_numeric=written_total,
                             source="api", token_id=token["id"])
    names = {r["id"]: r["name"] for r in con.execute(
        f"SELECT id, name FROM metrics WHERE id IN ({','.join('?' * len(ids))})", ids)}
    return {"ok": True, "slot": slot, "week_start": week.isoformat(),
            "written": [{"metric_id": m, "name": names[m], "value": v}
                        for m, v in resolved.items()],
            "missing": [names[i] for i in missing],
            "total_metric_id": parent_id, "total_written": written_total}


class ArchiveIn(BaseModel):
    effective_week: Optional[str] = Field(
        None, description="Monday the metric stopped being tracked. Defaults to this week.",
        examples=["2026-06-29"])


def _metric_row(con: sqlite3.Connection, metric_id: int) -> sqlite3.Row:
    m = con.execute("SELECT * FROM metrics WHERE id = ?", (metric_id,)).fetchone()
    if m is None:
        raise HTTPException(404, "Unknown metric")
    return m


@router.post("/metrics/{metric_id}/archive", tags=["Admin"], summary="Archive a metric",
             responses={200: {"model": ArchiveOut}, **_errors(401, 403, 404, 422)},
             description="Takes a metric (for example a churned client) off every "
                         "surface while keeping its history. Calling it again moves the "
                         "effective week. Needs an admin token.")
def archive_metric(request: Request, metric_id: int = Path(description="Metric id from GET /api/v1/metrics"),
                   body: Optional[ArchiveIn] = None,
                   con: sqlite3.Connection = Depends(db_dep)):
    """Take a metric off the board from effective_week onward, keeping history.

    Re-archiving an already-archived metric moves the effective week, so a
    wrong churn date is corrected by calling this again.
    """
    api_token_from_request(request, con, need_admin=True)
    m = _metric_row(con, metric_id)
    body = body or ArchiveIn()

    now = datetime.now(timezone.utc)
    if body.effective_week:
        try:
            week = wk.parse_week(body.effective_week)
        except ValueError as e:
            raise HTTPException(422, str(e))
    else:
        week = wk.current_week(now)
    # A future effective week would drop the row from the board immediately while
    # scoring still counted the weeks in between - refuse rather than half-archive.
    if week > wk.current_week(now):
        raise HTTPException(422, "Cannot archive effective a future week")
    if week < wk.parse_week(m["start_week"]):
        raise HTTPException(422, f"Metric starts {m['start_week']}")

    was_archived = m["archived_at"] is not None
    con.execute("UPDATE metrics SET archived_at = ? WHERE id = ?",
                (week.isoformat(), metric_id))
    return {"ok": True, "metric_id": metric_id, "name": m["name"],
            "archived": True, "was_already_archived": was_archived,
            "effective_week": week.isoformat(),
            "effective_week_label": wk.quarter_label(week)}


@router.post("/metrics/{metric_id}/unarchive", tags=["Admin"], summary="Restore a metric",
             responses={200: {"model": UnarchiveOut}, **_errors(401, 403, 404)},
             description="Puts an archived metric back. Needs an admin token.")
def unarchive_metric(request: Request, metric_id: int = Path(description="Metric id from GET /api/v1/metrics"),
                     con: sqlite3.Connection = Depends(db_dep)):
    api_token_from_request(request, con, need_admin=True)
    m = _metric_row(con, metric_id)
    was_archived = m["archived_at"] is not None
    con.execute("UPDATE metrics SET archived_at = NULL WHERE id = ?", (metric_id,))
    return {"ok": True, "metric_id": metric_id, "name": m["name"],
            "archived": False, "was_archived": was_archived}
