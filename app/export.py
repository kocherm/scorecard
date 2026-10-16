"""CSV exports: the board's window (GET /export.csv) and one metric's whole
history (GET /m/{id}.csv).

Both score through sc.cell_state with each week's OWN quarter's target - the
board export reads the cells build_grid already scored, the history walks the
same function week by week - so a state in the file is the colour on screen.

Free text (section, metric, owner, unit, source, writer) is guarded against
formula injection: a cell starting with = + - @ (or a tab/CR, which some
spreadsheets strip first) is prefixed with a single quote. Numbers are written
as numbers - a float rendered by plain_value cannot carry a formula, and
quoting -5 would turn it into text in every spreadsheet that opens the file.
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Iterable, Optional

from . import scoring as sc
from . import weeks as wk
from .grid import GridVM, _metric_info, plain_value

BOARD_HEADER = ["section", "metric", "owner", "week_start", "value", "target",
                "state", "unit"]
HISTORY_HEADER = ["week_start", "value", "target", "state", "source",
                  "entered_at", "entered_by"]

_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def safe_text(v: Optional[str]) -> str:
    s = "" if v is None else str(v)
    return "'" + s if s.startswith(_FORMULA_LEAD) else s


def _num(v) -> str:
    return "" if v is None else plain_value(v)


def to_csv(header: list[str], rows: Iterable[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(header)
    w.writerows(rows)
    # BOM: Excel otherwise reads UTF-8 as the local code page and mangles names.
    return buf.getvalue().encode("utf-8-sig")


def filename_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "metric"


def board_rows(vm: GridVM) -> Iterable[list]:
    """One row per metric per week of the board's window, visible sections
    only (build_grid already leaves hidden ones out). Weeks outside a metric's
    life (state 'na') are skipped, as the board leaves those cells blank."""
    for s in vm.sections:
        for r in s.rows:
            for c in r.cells:
                if c.state == sc.CellState.NA:
                    continue
                yield [safe_text(s.name), safe_text(r.name),
                       safe_text(r.dri_name if r.dri_name != "-" else ""),
                       c.week.isoformat(), _num(c.raw),
                       _num(c.target) if r.metric_type == "numeric" else "",
                       c.state.value, safe_text(r.unit)]


def history_rows(con: sqlite3.Connection, m: sqlite3.Row,
                 now: datetime) -> list[list]:
    """Every week from the metric's start to the current week (or its archive
    week), oldest first - gaps included, because a missing number is part of
    the history. Value, target and state are what the board would show."""
    tz = wk.BUSINESS_TZ
    info = _metric_info(m)
    targets = {(t["year"], t["quarter"]): sc.QuarterTargets(t["baseline_value"],
                                                             t["stretch_value"])
               for t in con.execute("SELECT * FROM targets WHERE metric_id = ?",
                                    (m["id"],))}
    entries = {e["week_start"]: e for e in con.execute(
        """SELECT e.*, u.display_name AS user_name, t.name AS token_name
           FROM entries e
           LEFT JOIN users u ON u.id = e.entered_by_user_id
           LEFT JOIN api_tokens t ON t.id = e.entered_by_token_id
           WHERE e.metric_id = ?""", (m["id"],))}
    out = []
    w, cur = info.start_week, wk.current_week(now, tz)
    while w <= cur:
        if info.archived_week is not None and w >= info.archived_week:
            break
        e = entries.get(w.isoformat())
        ei = sc.EntryInfo(e["value_numeric"], e["value_status"]) if e else None
        target = sc.target_for_week(w, targets.get(wk.quarter_of(w)))
        state = sc.cell_state(info, w, ei, target, now, tz)
        value = None
        if e is not None:
            value = e["value_status"] if m["metric_type"] == "status" else e["value_numeric"]
        out.append([w.isoformat(), _num(value),
                    _num(target) if m["metric_type"] == "numeric" else "",
                    state.value,
                    safe_text(e["source"]) if e else "",
                    e["updated_at"] if e else "",
                    safe_text((e["user_name"] or e["token_name"] or "")) if e else ""])
        w += timedelta(days=7)
    return out
