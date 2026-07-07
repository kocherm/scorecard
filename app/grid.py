"""Assemble the scorecard view model. One code path feeds the TV page,
the edit grid, and the JSON API."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from . import scoring as sc
from . import weeks as wk


@dataclass
class Cell:
    week: date
    state: sc.CellState
    display: str          # formatted value or ""
    raw: Optional[float | str]
    is_current: bool
    editable: bool        # current or past week, metric live


@dataclass
class Row:
    metric_id: int
    last_state: str  # cell state of the last closed week, for the row dot
    name: str
    metric_type: str
    rollup: Optional[str]
    direction: str
    unit: Optional[str]
    dri_name: str
    dri_user_id: Optional[int]
    cells: list[Cell]
    band_subtotals: list[Optional[float | str]]  # one per month band
    target_display: str
    actual_display: str   # latest closed week's value
    spark: list[dict]     # last 4 closed weeks: {state, value}
    red_streak: int
    escalation: int
    has_131: bool
    latest_131_week: Optional[str]


@dataclass
class SectionVM:
    id: int
    name: str
    icon: str
    rows: list[Row] = field(default_factory=list)


@dataclass
class Summary:
    green: int = 0
    yellow: int = 0
    red: int = 0
    stale: int = 0
    pending: int = 0
    other: int = 0
    red_names: list[str] = field(default_factory=list)
    stale_names: list[str] = field(default_factory=list)


@dataclass
class GridVM:
    weeks: list[date]
    bands: list[wk.MonthBand]
    current_week: date
    last_closed: date
    sections: list[SectionVM]
    quarter_label: str
    summary: Summary


def fmt_value(metric_type: str, unit: Optional[str], value) -> str:
    if value is None:
        return ""
    if metric_type == "status":
        return {"G": "G", "Y": "Y", "R": "R"}.get(value, "")
    if metric_type == "binary":
        return "Yes" if value else "No"
    v = float(value)
    s = f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"
    if unit == "$":
        return f"${s}"
    return s


def _metric_info(m: sqlite3.Row) -> sc.MetricInfo:
    archived_week = None
    if m["archived_at"]:
        archived_week = wk.monday_of(date.fromisoformat(m["archived_at"][:10]))
    return sc.MetricInfo(
        id=m["id"], metric_type=m["metric_type"], direction=m["direction"],
        start_week=date.fromisoformat(m["start_week"]), archived_week=archived_week,
    )


def build_grid(con: sqlite3.Connection, now: datetime,
               include_archived: bool = False) -> GridVM:
    tz = wk.BUSINESS_TZ
    today = now.astimezone(tz).date()
    weeks = wk.window_weeks(today)
    bands = wk.month_bands(weeks)
    cur_week = wk.monday_of(today)
    week_keys = [w.isoformat() for w in weeks]

    sections = con.execute(
        "SELECT * FROM sections WHERE is_enabled = 1 ORDER BY sort_order, id"
    ).fetchall()
    metrics_sql = """SELECT m.*, u.display_name AS dri_name FROM metrics m
                     LEFT JOIN users u ON u.id = m.dri_user_id
                     WHERE m.section_id = ? {arch} ORDER BY m.sort_order, m.id"""
    arch_clause = "" if include_archived else "AND m.archived_at IS NULL"

    entries = {}
    for e in con.execute(
        f"SELECT * FROM entries WHERE week_start IN ({','.join('?' * len(week_keys))})",
        week_keys,
    ):
        entries[(e["metric_id"], e["week_start"])] = e

    # Also need entries slightly before the window for streaks/sparklines.
    prior_weeks = [(weeks[0] - timedelta(days=7 * i)).isoformat() for i in range(1, 9)]
    for e in con.execute(
        f"SELECT * FROM entries WHERE week_start IN ({','.join('?' * len(prior_weeks))})",
        prior_weeks,
    ):
        entries[(e["metric_id"], e["week_start"])] = e

    targets: dict[tuple[int, int, int], sc.QuarterTargets] = {}
    for t in con.execute("SELECT * FROM targets"):
        targets[(t["metric_id"], t["year"], t["quarter"])] = sc.QuarterTargets(
            baseline=t["baseline_value"], stretch=t["stretch_value"])

    otos = {(o["metric_id"], o["week_start"]) for o in con.execute(
        "SELECT metric_id, week_start FROM one_three_ones")}

    def entry_info(mid: int, w: date) -> Optional[sc.EntryInfo]:
        e = entries.get((mid, w.isoformat()))
        if e is None:
            return None
        return sc.EntryInfo(value_numeric=e["value_numeric"], value_status=e["value_status"])

    def target_for(mid: int, w: date) -> Optional[float]:
        y, q = wk.quarter_of(w)
        return sc.target_for_week(w, targets.get((mid, y, q)))

    summary = Summary()
    section_vms: list[SectionVM] = []
    for s in sections:
        vm = SectionVM(id=s["id"], name=s["name"], icon=s["icon"] or "chart")
        for m in con.execute(metrics_sql.format(arch=arch_clause), (s["id"],)):
            info = _metric_info(m)
            cells = []
            for w in weeks:
                ei = entry_info(m["id"], w)
                state = sc.cell_state(info, w, ei, target_for(m["id"], w), now, tz)
                raw = None
                if ei is not None:
                    raw = ei.value_status if m["metric_type"] == "status" else ei.value_numeric
                cells.append(Cell(
                    week=w, state=state,
                    display=fmt_value(m["metric_type"], m["unit"], raw),
                    raw=raw, is_current=(w == cur_week),
                    editable=(state != sc.CellState.NA),
                ))
            band_subs = []
            for b in bands:
                vals = [entry_info(m["id"], w) for w in b.weeks]
                sub = sc.month_subtotal(m["metric_type"], m["rollup"], vals)
                if isinstance(sub, float):
                    sub = fmt_value("numeric", m["unit"], sub)
                band_subs.append(sub)

            # Streak over closed weeks, newest first, back 8 weeks.
            closed = wk.last_closed_week(now, tz)
            states_desc = []
            for i in range(8):
                w = closed - timedelta(days=7 * i)
                if w < info.start_week:
                    break
                states_desc.append(sc.cell_state(info, w, entry_info(m["id"], w),
                                                 target_for(m["id"], w), now, tz))
            streak = sc.consecutive_red_weeks(states_desc)

            spark = []
            for i in range(3, -1, -1):
                w = closed - timedelta(days=7 * i)
                ei = entry_info(m["id"], w)
                st = sc.cell_state(info, w, ei, target_for(m["id"], w), now, tz)
                spark.append({
                    "state": st.value,
                    "value": (ei.value_numeric if ei and m["metric_type"] != "status"
                              else None),
                })

            cur_target = target_for(m["id"], cur_week)
            closed_entry = entry_info(m["id"], closed)
            actual_raw = None
            if closed_entry:
                actual_raw = (closed_entry.value_status if m["metric_type"] == "status"
                              else closed_entry.value_numeric)

            closed_state = sc.cell_state(info, closed, closed_entry,
                                         target_for(m["id"], closed), now, tz)
            if closed_state == sc.CellState.GREEN:
                summary.green += 1
            elif closed_state == sc.CellState.YELLOW:
                summary.yellow += 1
            elif closed_state == sc.CellState.RED:
                summary.red += 1
                summary.red_names.append(m["name"])
            elif closed_state == sc.CellState.STALE:
                summary.stale += 1
                summary.stale_names.append(m["name"])
            elif closed_state == sc.CellState.PENDING:
                summary.pending += 1
            else:
                summary.other += 1

            has_131 = (m["id"], closed.isoformat()) in otos
            vm.rows.append(Row(
                metric_id=m["id"], last_state=closed_state.value,
                name=m["name"], metric_type=m["metric_type"],
                rollup=m["rollup"], direction=m["direction"], unit=m["unit"],
                dri_name=m["dri_name"] or "-", dri_user_id=m["dri_user_id"],
                cells=cells, band_subtotals=band_subs,
                target_display=(fmt_value("numeric", m["unit"], cur_target)
                                if cur_target is not None else
                                ("G" if m["metric_type"] == "status" else "-")),
                actual_display=fmt_value(m["metric_type"], m["unit"], actual_raw) or "-",
                spark=spark, red_streak=streak,
                escalation=sc.escalation_level(streak), has_131=has_131,
                latest_131_week=closed.isoformat() if streak >= 1 else None,
            ))
        section_vms.append(vm)

    return GridVM(weeks=weeks, bands=bands, current_week=cur_week,
                  last_closed=wk.last_closed_week(now, tz),
                  sections=section_vms, quarter_label=wk.quarter_label(cur_week),
                  summary=summary)
