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
    red_names: list[str] = field(default_factory=list)    # "Metric (DRI)"
    stale_names: list[str] = field(default_factory=list)  # "Metric (DRI)"


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
            owned_name = (f"{m['name']} ({m['dri_name']})" if m["dri_name"]
                          else m["name"])
            if closed_state == sc.CellState.GREEN:
                summary.green += 1
            elif closed_state == sc.CellState.YELLOW:
                summary.yellow += 1
            elif closed_state == sc.CellState.RED:
                summary.red += 1
                summary.red_names.append(owned_name)
            elif closed_state == sc.CellState.STALE:
                summary.stale += 1
                summary.stale_names.append(owned_name)
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
    badge: str       # 'RED WK 2' / 'NO DATA'
    name: str
    value_display: str
    target_display: str
    dri_name: str
    initials: str
    next_step: str


@dataclass
class BoardRow:
    metric_id: int
    name: str
    metric_type: str
    is_key: bool
    dri_name: str
    dri_first: str
    initials: str
    latest_display: str   # this week if entered, else last closed week
    latest_state: str
    week_note: str        # 'this week' | 'last week' | ''
    cur_state: str        # current-week cell state, drives the trend ring
    target_display: str
    spark: list           # last 4 closed weeks: {state, value, pct}
    red_streak: int
    section: str


@dataclass
class BoardSection:
    name: str
    rows: list
    hidden: list = field(default_factory=list)  # folded rows behind the "+N" summary
    overflow_state: str = ""                    # worst hidden state, colors the +N chip
    overflow_label: str = ""                    # e.g. "all green" / "3 green · 1 no data"


@dataclass
class MrrHud:
    value_display: str
    asof_label: str            # "wk of Jun 15" (week of the newest entry)
    asof_stale: bool           # newest entry is older than the last closed week
    pace_display: str          # this week's ramp target, '-' if none
    pace_pct: Optional[float]  # pace position on the goal track, 0-100
    pace_state: str            # green/yellow/red of value vs pace
    delta_display: str         # signed gap to pace
    goal_display: str
    fill_pct: float
    milestones: list           # [{pct, label}]
    dri_name: str
    initials: str


@dataclass
class TvVM:
    vm: GridVM
    mrr: Optional[MrrHud]
    columns: list        # 1-2 lists of BoardSection, balanced by row count
    board_rows: int      # rows in the fullest column (drives the vh type scale)
    board_secs: int      # section labels in the fullest column
    actions: list        # top escalations for the footer line
    more_actions: int


def _initials(name: str) -> str:
    parts = [p for p in name.split() if p]
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper() if parts else "?"


NEXT_STEP = {
    1: "file a 1-3-1 before sync",
    2: "15-min 1:1 this week",
    3: "structural conversation",
}


# ---- board layout: the type scale never shrinks below legibility to absorb
# an unbounded list. Status-only sections (client health) sort worst-first;
# when a column would exceed COL_CAP_UNITS, their greenest tail rows fold
# into one "+N" summary row. Curated numeric sections never fold, and the
# edit grid always shows the complete list.
HDR_UNITS = 0.6       # a section label costs this fraction of a row's height
COL_CAP_UNITS = 11.0  # ~10 rows + labels per column, keeps rows >= ~6.4vh

_SEVERITY = {"red": 0, "yellow": 1, "stale": 2, "pending": 3, "green": 5}

_STATE_WORD = {"red": "red", "yellow": "yellow", "stale": "no data",
               "pending": "pending", "green": "green"}


def _severity_key(r: BoardRow) -> tuple[int, int]:
    # An active red streak outranks everything, even when this week's cell
    # is still awaiting entry (streaks skip stale/pending weeks by design).
    if r.red_streak > 0:
        return (0, -r.red_streak)
    return (_SEVERITY.get(r.latest_state, 4), 0)


def _units(g: BoardSection) -> float:
    return len(g.rows) + HDR_UNITS + (1 if g.hidden else 0)


def _overflow_label(hidden: list) -> str:
    counts: dict[str, int] = {}
    for r in sorted(hidden, key=_severity_key):
        word = _STATE_WORD.get(r.latest_state, "no target")
        counts[word] = counts.get(word, 0) + 1
    if set(counts) == {"green"}:
        return "all green"
    return " · ".join(f"{n} {w}" for w, n in counts.items())


def _fold_one(groups: list[BoardSection]) -> bool:
    """Hide the greenest row of the largest foldable status section."""
    target = None
    for g in groups:
        if (len(g.rows) > 1
                and all(r.metric_type == "status" for r in g.rows)
                and (target is None or len(g.rows) > len(target.rows))):
            target = g
    if target is None:
        return False
    target.hidden.append(target.rows.pop())
    target.overflow_state = min(target.hidden, key=_severity_key).latest_state
    target.overflow_label = _overflow_label(target.hidden)
    return True


def _split_columns(groups: list[BoardSection]) -> list[list[BoardSection]]:
    if not groups:
        return []
    if len(groups) == 1 and len(groups[0].rows) > 8:
        g = groups[0]
        half = (len(g.rows) + 1) // 2
        return [[BoardSection(g.name, g.rows[:half])],
                [BoardSection("", g.rows[half:], hidden=g.hidden,
                              overflow_state=g.overflow_state,
                              overflow_label=g.overflow_label)]]
    if len(groups) == 1:
        return [groups]
    best_k, best_gap = 1, None
    for k in range(1, len(groups)):
        gap = abs(sum(map(_units, groups[:k])) - sum(map(_units, groups[k:])))
        if best_gap is None or gap < best_gap:
            best_k, best_gap = k, gap
    return [groups[:best_k], groups[best_k:]]


def _layout_board(groups: list[BoardSection]) -> tuple[list, int, int]:
    for g in groups:
        if g.rows and all(r.metric_type == "status" for r in g.rows):
            g.rows.sort(key=_severity_key)
    while True:
        columns = _split_columns(groups)
        worst = max((sum(_units(g) for g in col) for col in columns), default=0.0)
        if worst <= COL_CAP_UNITS or not _fold_one(groups):
            break
    board_rows = max((sum(len(g.rows) + (1 if g.hidden else 0) for g in col)
                      for col in columns), default=1)
    board_secs = max((sum(1 for g in col if g.name) for col in columns), default=0)
    return columns, max(board_rows, 1), board_secs


def build_tv(con: sqlite3.Connection, now: datetime) -> TvVM:
    from .db import get_setting
    vm = build_grid(con, now)

    def find_cell(row: Row, week: date) -> Optional[Cell]:
        return next((c for c in row.cells if c.week == week), None)

    targets_by_metric: dict[int, Optional[float]] = {}
    closed_targets: dict[int, Optional[float]] = {}
    for t in con.execute("SELECT metric_id, year, quarter, baseline_value, stretch_value FROM targets"):
        qt = sc.QuarterTargets(t["baseline_value"], t["stretch_value"])
        if (t["year"], t["quarter"]) == wk.quarter_of(vm.current_week):
            targets_by_metric[t["metric_id"]] = sc.target_for_week(vm.current_week, qt)
        if (t["year"], t["quarter"]) == wk.quarter_of(vm.last_closed):
            closed_targets[t["metric_id"]] = sc.target_for_week(vm.last_closed, qt)

    rows: list[BoardRow] = []
    actions: list[ActionItem] = []
    for section in vm.sections:
        for row in section.rows:
            cur = find_cell(row, vm.current_week)
            closed = find_cell(row, vm.last_closed)
            # Prefer this week's number; fall back to last closed week's.
            use, wnote = (cur, "this week")
            if cur is None or cur.raw is None:
                use, wnote = (closed, "last week")
            latest_raw = use.raw if use and use.raw is not None else None
            rows.append(BoardRow(
                metric_id=row.metric_id, name=row.name,
                metric_type=row.metric_type, is_key=row.is_key,
                dri_name=row.dri_name,
                dri_first=(row.dri_name.split()[0] if row.dri_name != "-" else ""),
                initials=(_initials(row.dri_name) if row.dri_name != "-" else ""),
                latest_display=(use.display if use and use.display else "-"),
                latest_state=(use.state.value if use and use.raw is not None else
                              (closed.state.value
                               if closed and closed.state != sc.CellState.NA
                               else "pending")),
                week_note=(wnote if latest_raw is not None else ""),
                cur_state=(cur.state.value if cur else "pending"),
                target_display=row.target_display,
                spark=row.spark, red_streak=row.red_streak,
                section=section.name))

            if row.red_streak >= 1:
                lvl = min(row.red_streak, 3)
                step = NEXT_STEP[lvl]
                if lvl == 1 and row.has_131:
                    step = "1-3-1 filed - review in sync"
                ct = closed_targets.get(row.metric_id)
                ct_display = (fmt_value("numeric", row.unit, ct) if ct is not None
                              else ("G" if row.metric_type == "status" else "-"))
                actions.append(ActionItem(
                    kind="red", badge=f"RED WK {row.red_streak}",
                    name=row.name,
                    value_display=(closed.display if closed and closed.display else "R"),
                    target_display=ct_display,
                    dri_name=row.dri_name, initials=_initials(row.dri_name),
                    next_step=step))
            elif row.last_state == "stale":
                actions.append(ActionItem(
                    kind="stale", badge="NO DATA",
                    name=row.name, value_display="-",
                    target_display=row.target_display,
                    dri_name=row.dri_name, initials=_initials(row.dri_name),
                    next_step="enter last week's number"))

    actions.sort(key=lambda a: (a.kind != "red", a.name))

    # ---- goal band: explicit setting wins, else detect a metric named "MRR"
    mrr = None
    mrr_metric_id = None
    mid_s = get_setting(con, "hud_mrr_metric_id")
    if mid_s:
        try:
            mrr_metric_id = int(mid_s)
        except ValueError:
            mrr_metric_id = None
    if mrr_metric_id is None:
        m = con.execute(
            """SELECT id FROM metrics WHERE archived_at IS NULL
               AND metric_type = 'numeric' AND lower(name) LIKE '%mrr%'
               AND lower(name) NOT LIKE '%new%' ORDER BY id LIMIT 1""").fetchone()
        mrr_metric_id = m["id"] if m else None

    if mrr_metric_id is not None:
        try:
            goal = float(get_setting(con, "mrr_goal") or 100000)
        except (TypeError, ValueError):
            goal = 100000.0
        e = con.execute(
            """SELECT week_start, value_numeric FROM entries
               WHERE metric_id = ? AND value_numeric IS NOT NULL
               ORDER BY week_start DESC LIMIT 1""", (mrr_metric_id,)).fetchone()
        d = con.execute(
            """SELECT u.display_name AS dri FROM metrics m
               LEFT JOIN users u ON u.id = m.dri_user_id WHERE m.id = ?""",
            (mrr_metric_id,)).fetchone()
        if e and goal > 0:
            val = e["value_numeric"]
            week_e = date.fromisoformat(e["week_start"])
            pace = targets_by_metric.get(mrr_metric_id)
            pace_pct, pace_state, delta_display = None, "no-target", "-"
            if pace and pace > 0:
                pace_pct = max(0.0, min(100.0, pace / goal * 100))
                ratio = val / pace
                pace_state = ("green" if ratio >= 1.0 else
                              "yellow" if ratio >= sc.YELLOW_FLOOR else "red")
                gap = val - pace
                delta_display = ("+" if gap >= 0 else "-") + fmt_value("numeric", "$", abs(gap))
            miles = []
            for part in (get_setting(con, "mrr_milestones", "") or "").split(";"):
                if ":" in part:
                    amt, label = part.split(":", 1)
                    try:
                        miles.append({"pct": min(99.0, float(amt) / goal * 100),
                                      "label": label.strip()})
                    except ValueError:
                        continue
            dri_name = d["dri"] if d and d["dri"] else "-"
            mrr = MrrHud(
                value_display=fmt_value("numeric", "$", val),
                asof_label="wk of " + week_e.strftime("%b %-d"),
                asof_stale=week_e < vm.last_closed,
                pace_display=(fmt_value("numeric", "$", pace) if pace else "-"),
                pace_pct=pace_pct, pace_state=pace_state, delta_display=delta_display,
                goal_display=fmt_value("numeric", "$", goal),
                fill_pct=max(1.5, min(100.0, val / goal * 100)),
                milestones=miles, dri_name=dri_name,
                initials=(_initials(dri_name) if dri_name != "-" else ""))

    # ---- group into sections and lay out two balanced columns; worst-first
    # sort and green-overflow folding happen in _layout_board. The goal
    # metric keeps its row only when the band cannot render.
    board = [r for r in rows if not (mrr and r.metric_id == mrr_metric_id)]
    groups: list[BoardSection] = []
    for r in board:
        if not groups or groups[-1].name != r.section:
            groups.append(BoardSection(name=r.section, rows=[]))
        groups[-1].rows.append(r)
    columns, board_rows, board_secs = _layout_board(groups)

    return TvVM(vm=vm, mrr=mrr, columns=columns,
                board_rows=board_rows, board_secs=board_secs,
                actions=actions[:3], more_actions=max(0, len(actions) - 3))
