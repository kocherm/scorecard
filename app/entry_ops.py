"""Shared entry write/score helpers used by the edit grid, the My Numbers
check-in page, and the Slack reply handler. Lives outside main.py so
app.slack can import it without a circular import."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Optional

from . import db as dbm
from . import grid as gridm
from . import scoring as sc
from . import weeks as wk


def save_value(con: sqlite3.Connection, metric: sqlite3.Row, week: date,
               raw: str, *, source: str, user_id: Optional[int]) -> None:
    """Validate one typed value against the metric's type and store it.
    Accepts human forms ("$1,500", "80%", "yes", "g"); empty clears a numeric
    entry. Raises ValueError with a user-showable message on bad input."""
    if metric["metric_type"] == "status":
        v = raw.strip().upper()
        if v not in ("R", "Y", "G"):
            raise ValueError("Use R, Y, or G")
        dbm.upsert_entry(con, metric["id"], week, value_status=v,
                         source=source, user_id=user_id)
        return
    cleaned = raw.strip().replace("$", "").replace(",", "").replace("%", "")
    if cleaned == "":
        dbm.delete_entry(con, metric["id"], week, user_id=user_id)
        return
    if metric["metric_type"] == "binary" and cleaned.lower() in ("yes", "no", "y", "n"):
        v = 1.0 if cleaned.lower() in ("yes", "y") else 0.0
    else:
        try:
            v = float(cleaned)
        except ValueError:
            raise ValueError(f"Not a number: {raw.strip()!r}")
        if metric["metric_type"] == "binary":
            v = 1.0 if v else 0.0
    dbm.upsert_entry(con, metric["id"], week, value_numeric=v,
                     source=source, user_id=user_id)


def state_for(con: sqlite3.Connection, metric: sqlite3.Row, week: date,
              now: datetime) -> sc.CellState:
    """Score a single cell without building the whole grid (Slack confirmations)."""
    info = gridm._metric_info(metric)
    e = con.execute(
        "SELECT value_numeric, value_status FROM entries WHERE metric_id=? AND week_start=?",
        (metric["id"], week.isoformat())).fetchone()
    entry = sc.EntryInfo(e["value_numeric"], e["value_status"]) if e else None
    y, q = wk.quarter_of(week)
    t = con.execute(
        "SELECT baseline_value, stretch_value FROM targets WHERE metric_id=? AND year=? AND quarter=?",
        (metric["id"], y, q)).fetchone()
    target = sc.target_for_week(
        week, sc.QuarterTargets(t["baseline_value"], t["stretch_value"]) if t else None)
    return sc.cell_state(info, week, entry, target, now)


def target_hint(con: sqlite3.Connection, m: sqlite3.Row, week: date) -> str:
    """' (target 25)' suffix for nudge/help lists; '' when no target is set."""
    if m["metric_type"] == "status":
        return " (G/Y/R - target G)"
    y, q = wk.quarter_of(week)
    t = con.execute(
        "SELECT baseline_value, stretch_value FROM targets "
        "WHERE metric_id=? AND year=? AND quarter=?", (m["id"], y, q)).fetchone()
    tv = sc.target_for_week(
        week, sc.QuarterTargets(t["baseline_value"], t["stretch_value"]) if t else None)
    return f" (target {gridm.fmt_value('numeric', m['unit'], tv)})" if tv is not None else ""


def missing_due_metrics(con: sqlite3.Connection, user_id: int,
                        now: datetime) -> list[sqlite3.Row]:
    """Live metrics this user owns that still have no entry for the due week
    (the last closed week). Drives /checkin emphasis, the login redirect, the
    nav badge, and the Slack nudge DMs."""
    due = wk.last_closed_week(now).isoformat()
    return con.execute(
        """SELECT m.* FROM metrics m
           JOIN sections s ON s.id = m.section_id
           WHERE m.dri_user_id = ? AND m.archived_at IS NULL
             AND s.is_enabled = 1 AND m.start_week <= ?
             AND NOT EXISTS (SELECT 1 FROM entries e
                             WHERE e.metric_id = m.id AND e.week_start = ?)
           ORDER BY s.sort_order, m.sort_order, m.id""",
        (user_id, due, due)).fetchall()
