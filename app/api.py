"""JSON API for Hermes and other integrations. Bearer token auth.

GET  /api/v1/scorecard           full current state (same scoring as the TV)
GET  /api/v1/metrics             id/name list for writers (?include_archived=true)
POST /api/v1/metrics/{id}/entries  {"week_start": "YYYY-MM-DD" (a Monday, optional,
                                    defaults to last closed week),
                                    "value": number  OR  "status": "R"|"Y"|"G"}
POST /api/v1/metrics/{id}/archive    {"effective_week": "YYYY-MM-DD" (a Monday,
                                      optional, defaults to this week)}  admin scope
POST /api/v1/metrics/{id}/unarchive  admin scope

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
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from . import db as dbm
from . import grid as gridm
from . import weeks as wk
from .auth import api_token_from_request
from .db import db_dep

router = APIRouter(prefix="/api/v1")


def _read_token(request: Request, con: sqlite3.Connection = Depends(db_dep)):
    return api_token_from_request(request, con, need_write=False)


@router.get("/scorecard")
def scorecard_state(request: Request, con: sqlite3.Connection = Depends(db_dep)):
    api_token_from_request(request, con, need_write=False)
    now = datetime.now(timezone.utc)
    vm = gridm.build_grid(con, now)
    week = wk.last_closed_week(now)
    out = {
        "current_week": vm.current_week.isoformat(),
        "current_week_label": vm.quarter_label,
        "last_closed_week": week.isoformat(),
        "entries_due_by": wk.entry_deadline(week).isoformat(),
        "stale_after": wk.stale_at(week).isoformat(),
        "sections": [],
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
            if closed_cell and closed_cell.state.value == "stale":
                out["stale"].append({"id": r.metric_id, "name": r.name, "dri": r.dri_name})
            if r.red_streak >= 1:
                out["red"].append({"id": r.metric_id, "name": r.name, "dri": r.dri_name,
                                   "weeks_red": r.red_streak,
                                   "one_three_one_filed": r.has_131})
        out["sections"].append(sec)
    return out


@router.get("/metrics")
def list_metrics(request: Request, include_archived: bool = False,
                 con: sqlite3.Connection = Depends(db_dep)):
    api_token_from_request(request, con, need_write=False)
    arch = "" if include_archived else "WHERE m.archived_at IS NULL"
    rows = con.execute(
        f"""SELECT m.id, m.name, m.metric_type, m.unit, m.archived_at,
                   s.name AS section, u.display_name AS dri
            FROM metrics m JOIN sections s ON s.id = m.section_id
            LEFT JOIN users u ON u.id = m.dri_user_id
            {arch} ORDER BY s.sort_order, m.sort_order"""
    ).fetchall()
    return [dict(r) for r in rows]


class EntryIn(BaseModel):
    week_start: Optional[str] = None
    value: Optional[float] = None
    status: Optional[str] = None


@router.post("/metrics/{metric_id}/entries")
def write_entry(metric_id: int, body: EntryIn, request: Request,
                con: sqlite3.Connection = Depends(db_dep)):
    token = api_token_from_request(request, con, need_write=True)
    m = con.execute("SELECT * FROM metrics WHERE id = ? AND archived_at IS NULL",
                    (metric_id,)).fetchone()
    if m is None:
        raise HTTPException(404, "Unknown or archived metric")

    now = datetime.now(timezone.utc)
    if body.week_start:
        try:
            week = wk.parse_week(body.week_start)
        except ValueError as e:
            raise HTTPException(422, str(e))
    else:
        week = wk.last_closed_week(now)
    if week > wk.current_week(now):
        raise HTTPException(422, "Cannot write a future week")
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


class ArchiveIn(BaseModel):
    effective_week: Optional[str] = None


def _metric_row(con: sqlite3.Connection, metric_id: int) -> sqlite3.Row:
    m = con.execute("SELECT * FROM metrics WHERE id = ?", (metric_id,)).fetchone()
    if m is None:
        raise HTTPException(404, "Unknown metric")
    return m


@router.post("/metrics/{metric_id}/archive")
def archive_metric(metric_id: int, request: Request,
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


@router.post("/metrics/{metric_id}/unarchive")
def unarchive_metric(metric_id: int, request: Request,
                     con: sqlite3.Connection = Depends(db_dep)):
    api_token_from_request(request, con, need_admin=True)
    m = _metric_row(con, metric_id)
    was_archived = m["archived_at"] is not None
    con.execute("UPDATE metrics SET archived_at = NULL WHERE id = ?", (metric_id,))
    return {"ok": True, "metric_id": metric_id, "name": m["name"],
            "archived": False, "was_archived": was_archived}
