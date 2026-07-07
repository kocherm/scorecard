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
    is_key: bool     # leading indicator: bolded and starred
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
    from .db import get_setting
    try:
        months = max(1, min(4, int(get_setting(con, "display_months", "2"))))
    except (TypeError, ValueError):
        months = 2
    weeks = wk.window_weeks(today, months=months)
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
                val = (ei.value_numeric if ei and m["metric_type"] != "status" else None)
                # Bar height as % of target for numeric metrics (trajectory read).
                pct = None
                tgt = target_for(m["id"], w)
                if (m["metric_type"] == "numeric" and val is not None
                        and tgt is not None and tgt > 0):
                    if m["direction"] == "down":
                        ratio = 1.0 if val <= tgt else tgt / val
                    else:
                        ratio = val / tgt
                    pct = int(max(0.15, min(1.0, ratio)) * 100)
                spark.append({"state": st.value, "value": val, "pct": pct})

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
                is_key=bool(m["is_key"]),
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


# ---------------------------------------------------------------- TV view model
@dataclass
class ActionItem:
    kind: str        # 'red' | 'stale'
    badge: str       # 'RED - WEEK 2' / 'NOT UPDATED'
    name: str
    value_display: str
    target_display: str
    dri_name: str
    initials: str
    next_step: str


@dataclass
class Hero:
    name: str
    dri_name: str
    value_display: str
    target_display: str
    pct: Optional[int]   # % of target, clamped 0..120; None if unknowable
    state: str
    arrow: str           # 'up' | 'down' | 'flat' | 'none'
    week_note: str       # 'this week' | 'last week' | 'no data yet'


@dataclass
class StripRow:
    name: str
    dri_name: str
    dot: str
    is_key: bool
    metric_type: str
    last_display: str
    last_state: str
    cur_display: str
    cur_state: str
    latest_display: str   # this week if entered, else last week (for tiles)
    latest_state: str
    week_note: str        # 'this week' | 'last week' | ''
    pct: Optional[int]    # % of current target for meters; None if unknowable
    target_display: str
    spark: list
    section: str


@dataclass
class MrrHud:
    value: float
    value_display: str
    pace_display: str
    goal: float
    goal_display: str
    fill_pct: float
    milestones: list  # [{pct, label}]


@dataclass
class TvVM:
    vm: GridVM
    mrr: Optional[MrrHud]
    heroes: list
    strip: list    # every metric incl. key and status rows; views filter
    clients: list  # [{name, state, note}]
    actions: list
    more_actions: int
    view: str = "hybrid"
    prev_view: Optional[str] = None
    next_view: Optional[str] = None


def _initials(name: str) -> str:
    parts = [p for p in name.split() if p]
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper() if parts else "?"


def _ratio_pct(value: float, target: Optional[float], direction: str) -> Optional[int]:
    if target is None or target <= 0:
        return None
    if direction == "down":
        r = 1.0 if value <= target else target / value
    else:
        r = value / target
    return int(round(max(0.0, min(1.2, r)) * 100))


NEXT_STEP = {
    1: "file a 1-3-1 before the weekly sync",
    2: "15-min 1:1 with Michael this week",
    3: "structural conversation - 3+ weeks red",
}


def build_tv(con: sqlite3.Connection, now: datetime) -> TvVM:
    from .db import get_setting
    vm = build_grid(con, now)
    heroes, strip, clients, actions = [], [], [], []

    def find_cell(row: Row, week: date) -> Optional[Cell]:
        return next((c for c in row.cells if c.week == week), None)

    targets_by_metric: dict[int, Optional[float]] = {}
    for t in con.execute("SELECT metric_id, year, quarter, baseline_value, stretch_value FROM targets"):
        y, q = wk.quarter_of(vm.current_week)
        if t["year"] == y and t["quarter"] == q:
            targets_by_metric[t["metric_id"]] = sc.target_for_week(
                vm.current_week,
                sc.QuarterTargets(t["baseline_value"], t["stretch_value"]))

    for section in vm.sections:
        for row in section.rows:
            cur = find_cell(row, vm.current_week)
            closed = find_cell(row, vm.last_closed)

            if row.metric_type == "status":
                st = row.last_state if row.last_state in ("green", "yellow", "red") else "stale"
                note = ""
                if row.red_streak >= 2:
                    note = f"week {row.red_streak}"
                clients.append({"name": row.name, "state": st, "note": note})
            if row.metric_type != "status" and row.is_key:
                # Prefer this week's number; fall back to last week's.
                use, week_note = (cur, "this week")
                if cur is None or cur.raw is None:
                    use, week_note = (closed, "last week")
                val = use.raw if use and use.raw is not None else None
                tgt = targets_by_metric.get(row.metric_id)
                pct = _ratio_pct(float(val), tgt, row.direction) if val is not None else None
                nums = [s["value"] for s in row.spark if s["value"] is not None]
                if val is not None:
                    nums = nums + [float(val)] if week_note == "this week" else nums
                arrow = "none"
                if len(nums) >= 2:
                    arrow = "up" if nums[-1] > nums[-2] else ("down" if nums[-1] < nums[-2] else "flat")
                heroes.append(Hero(
                    name=row.name, dri_name=row.dri_name,
                    value_display=(use.display if use and use.display else "-"),
                    target_display=row.target_display,
                    pct=pct,
                    state=(use.state.value if use and use.raw is not None else
                           (closed.state.value if closed else "pending")),
                    arrow=arrow,
                    week_note=(week_note if val is not None else "no data yet")))

            use, wnote = (cur, "this week")
            if cur is None or cur.raw is None:
                use, wnote = (closed, "last week")
            latest_raw = use.raw if use and use.raw is not None else None
            if row.metric_type == "numeric" and latest_raw is not None:
                row_pct = _ratio_pct(float(latest_raw),
                                     targets_by_metric.get(row.metric_id), row.direction)
            else:
                row_pct = None
            strip.append(StripRow(
                name=row.name, dri_name=row.dri_name, dot=row.last_state,
                is_key=row.is_key, metric_type=row.metric_type,
                last_display=(closed.display if closed and closed.display else "-"),
                last_state=(closed.state.value if closed else "pending"),
                cur_display=(cur.display if cur and cur.display else ""),
                cur_state=(cur.state.value if cur else "pending"),
                latest_display=(use.display if use and use.display else "-"),
                latest_state=(use.state.value if use and use.raw is not None else
                              (closed.state.value if closed else "pending")),
                week_note=(wnote if latest_raw is not None else ""),
                pct=row_pct,
                target_display=row.target_display, spark=row.spark,
                section=section.name))

            if row.red_streak >= 1:
                lvl = min(row.red_streak, 3)
                step = NEXT_STEP[lvl]
                if lvl == 1 and row.has_131:
                    step = "1-3-1 filed - review in sync"
                actions.append(ActionItem(
                    kind="red", badge=f"RED - WEEK {row.red_streak}",
                    name=row.name,
                    value_display=(closed.display if closed and closed.display else "R"),
                    target_display=row.target_display,
                    dri_name=row.dri_name, initials=_initials(row.dri_name),
                    next_step=step))
            elif row.last_state == "stale":
                actions.append(ActionItem(
                    kind="stale", badge="NOT UPDATED",
                    name=row.name, value_display="-",
                    target_display=row.target_display,
                    dri_name=row.dri_name, initials=_initials(row.dri_name),
                    next_step="enter last week's number"))

    actions.sort(key=lambda a: (a.kind != "red", a.name))
    more = max(0, len(actions) - 4)

    mrr = None
    mid_s = get_setting(con, "hud_mrr_metric_id")
    if mid_s:
        try:
            goal = float(get_setting(con, "mrr_goal", "100000"))
            row_e = con.execute(
                """SELECT value_numeric FROM entries WHERE metric_id = ?
                   AND value_numeric IS NOT NULL ORDER BY week_start DESC LIMIT 1""",
                (int(mid_s),)).fetchone()
            if row_e:
                val = row_e["value_numeric"]
                pace = targets_by_metric.get(int(mid_s))
                miles = []
                for part in (get_setting(con, "mrr_milestones", "") or "").split(";"):
                    if ":" in part:
                        amt, label = part.split(":", 1)
                        miles.append({"pct": min(99.0, float(amt) / goal * 100),
                                      "label": label.strip()})
                mrr = MrrHud(
                    value=val, value_display=fmt_value("numeric", "$", val),
                    pace_display=(fmt_value("numeric", "$", pace) if pace else "-"),
                    goal=goal, goal_display=fmt_value("numeric", "$", goal),
                    fill_pct=max(3.0, min(100.0, val / goal * 100)),
                    milestones=miles)
        except (TypeError, ValueError):
            mrr = None

    return TvVM(vm=vm, mrr=mrr, heroes=heroes, strip=strip, clients=clients,
                actions=actions[:4], more_actions=more)
