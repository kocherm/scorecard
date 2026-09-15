"""Assemble the scorecard view model. One code path feeds the TV page,
the edit grid, and the JSON API."""
from __future__ import annotations

import math
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
    if unit == "%":
        return f"{s}%"
    return s


def plain_value(value) -> str:
    """A stored value as a person would TYPE it back: 5, not 5.0; 3.5 stays
    3.5; a status stays its letter. fmt_value is for reading - it adds the $
    and the thousands separators, which is exactly what a number input must
    not be pre-filled with."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    v = float(value)
    return str(int(v)) if v == int(v) else str(v)


def _metric_info(m: sqlite3.Row) -> sc.MetricInfo:
    archived_week = None
    if m["archived_at"]:
        archived_week = wk.monday_of(date.fromisoformat(m["archived_at"][:10]))
    return sc.MetricInfo(
        id=m["id"], metric_type=m["metric_type"], direction=m["direction"],
        start_week=date.fromisoformat(m["start_week"]), archived_week=archived_week,
        rollup=m["rollup"],
    )


def build_grid(con: sqlite3.Connection, now: datetime,
               include_archived: bool = False, hidden: bool = False) -> GridVM:
    """`hidden=True` builds the HIDDEN sections instead of the visible ones -
    only for the CEO view, whose slots may point into a section kept off the
    board (see ceo_rows). Everything else reads the visible board."""
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
        "SELECT * FROM sections WHERE is_enabled = ? ORDER BY sort_order, id",
        (0 if hidden else 1,)).fetchall()
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
    metric_id: int
    week: str        # last closed week, for the 1-3-1 link
    has_131: bool
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
    # The number behind latest_display and the current-week target behind
    # target_display, for views that derive (a ratio, a share of target).
    # None when the metric is not numeric or nothing is entered.
    latest_raw: Optional[float] = None
    target_raw: Optional[float] = None
    direction: str = "up"
    unit: Optional[str] = None


@dataclass
class BoardSection:
    name: str
    rows: list
    hidden: list = field(default_factory=list)  # folded rows behind the "+N" summary
    overflow_state: str = ""                    # worst hidden state, colors the +N chip
    overflow_label: str = ""                    # e.g. "all green" / "3 green · 1 no data"
    subcols: int = 1                            # rows flow across this many sub-columns


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
    # Every live row, unfolded and unfiltered - the goal metric and folded
    # greens are removed from `columns`, but a view that looks rows up by id
    # (the CEO view) needs the whole board.
    rows: list = field(default_factory=list)
    ceo: Optional["CeoVM"] = None
    # The "Clients" view: every row of every roster section (all R/Y/G),
    # worst-first and never folded, with the tile-grid column count that
    # fills a 16:9 panel for this many tiles.
    roster: list = field(default_factory=list)
    roster_cols: int = 1
    roster_rotates: bool = False  # set by main._tv_view: Clients is in rotation


def _initials(name: str) -> str:
    parts = [p for p in name.split() if p]
    return (parts[0][0] + (parts[1][0] if len(parts) > 1 else "")).upper() if parts else "?"


NEXT_STEP = {
    1: "file a 1-3-1 before sync",
    2: "15-min 1:1 this week",
    3: "structural conversation",
}


# ---- board layout: the type scale never shrinks below legibility to absorb
# an unbounded list. Status-only sections (client health) sort worst-first.
# When a column would exceed COL_CAP_UNITS, the largest status section in it
# first WIDENS - its rows flow into up to MAX_SUBCOLS narrow sub-columns, which
# is cheap for a status row (a colour, a name, a trend; no number or target) -
# and only once that is exhausted do its greenest tail rows fold into one "+N"
# summary cell. Widening comes first because a folded client is one nobody in
# the room can see; a narrower name costs nothing. Curated numeric sections
# never widen or fold, and the edit grid always shows the complete list.
HDR_UNITS = 0.6       # a section label costs this fraction of a row's height
COL_CAP_UNITS = 11.0  # ~10 rows + labels per column, keeps rows >= ~6.4vh
MAX_SUBCOLS = 3       # a board column is ~46vw; a third of it still fits a name

_SEVERITY = {"red": 0, "yellow": 1, "stale": 2, "pending": 3, "green": 5}

_STATE_WORD = {"red": "red", "yellow": "yellow", "stale": "no data",
               "pending": "pending", "green": "green"}


def _severity_key(r: BoardRow) -> tuple[int, int]:
    # An active red streak outranks everything, even when this week's cell
    # is still awaiting entry (streaks skip stale/pending weeks by design).
    if r.red_streak > 0:
        return (0, -r.red_streak)
    return (_SEVERITY.get(r.latest_state, 4), 0)


def _lines(g: BoardSection) -> int:
    """Row-heights the section's cells occupy: the "+N" summary is a cell
    like any other, so it shares a line when there is room beside it."""
    cells = len(g.rows) + (1 if g.hidden else 0)
    return -(-cells // g.subcols)


def _units(g: BoardSection) -> float:
    return _lines(g) + HDR_UNITS


def _overflow_label(hidden: list) -> str:
    counts: dict[str, int] = {}
    for r in sorted(hidden, key=_severity_key):
        word = _STATE_WORD.get(r.latest_state, "no target")
        counts[word] = counts.get(word, 0) + 1
    if set(counts) == {"green"}:
        return "all green"
    return " · ".join(f"{n} {w}" for w, n in counts.items())


def _is_status(g: BoardSection) -> bool:
    return bool(g.rows) and all(r.metric_type == "status" for r in g.rows)


def _widen_one(groups: list[BoardSection]) -> bool:
    """Give the tallest widenable status section one more sub-column."""
    target = None
    for g in groups:
        if (_is_status(g) and g.subcols < MAX_SUBCOLS and _lines(g) > 1
                and (target is None or _lines(g) > _lines(target))):
            target = g
    if target is None:
        return False
    target.subcols += 1
    return True


def _fold_one(groups: list[BoardSection]) -> bool:
    """Hide the greenest row of the largest foldable status section."""
    target = None
    for g in groups:
        if (len(g.rows) > 1 and _is_status(g)
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
        return [[BoardSection(g.name, g.rows[:half], subcols=g.subcols)],
                [BoardSection("", g.rows[half:], hidden=g.hidden,
                              overflow_state=g.overflow_state,
                              overflow_label=g.overflow_label,
                              subcols=g.subcols)]]
    if len(groups) == 1:
        return [groups]
    best_k, best_gap = 1, None
    for k in range(1, len(groups)):
        gap = abs(sum(map(_units, groups[:k])) - sum(map(_units, groups[k:])))
        if best_gap is None or gap < best_gap:
            best_k, best_gap = k, gap
    return [groups[:best_k], groups[best_k:]]


ROSTER_TILE_RATIO = 3.0  # tile width : height that fits a name and a trend


def _roster_cols(n: int) -> int:
    """Columns for n tiles on the Clients view. The panel below the header is
    about 1920x940, so tiles of ROSTER_TILE_RATIO come out when
    rows / cols = ratio * 940 / 1920; solve for cols. 8 clients get 3
    columns, 30 get 5, 60 get 7 - the grid grows with the list, never scrolls,
    never folds."""
    if n <= 1:
        return 1
    rows_per_col = ROSTER_TILE_RATIO * 940 / 1920
    return max(2, math.ceil(math.sqrt(n / rows_per_col)))


def _layout_board(groups: list[BoardSection]) -> tuple[list, int, int]:
    for g in groups:
        if _is_status(g):
            g.rows.sort(key=_severity_key)
    while True:
        columns = _split_columns(groups)
        worst = max(columns, key=lambda col: sum(map(_units, col)), default=[])
        if sum(map(_units, worst)) <= COL_CAP_UNITS:
            break
        # Act on the overfull column first; its sections are the originals
        # except in the one-section split, whose copies the fallback covers.
        own = [g for g in groups if any(g is s for s in worst)]
        if not (_widen_one(own) or _widen_one(groups)
                or _fold_one(own) or _fold_one(groups)):
            break
    board_rows = max((sum(_lines(g) for g in col) for col in columns), default=1)
    board_secs = max((sum(1 for g in col if g.name) for col in columns), default=0)
    return columns, max(board_rows, 1), board_secs


def build_actions(con: sqlite3.Connection, vm: GridVM) -> list[ActionItem]:
    """The last closed week's reds and stales as decision cards: the escalation
    step it has reached, who owns it, and the number that earned it.

    ONE builder, shared by the TV's "Act on this" view and the board page. It
    reads a GridVM that is already built rather than re-querying, so the cards
    can never disagree with the grid under them - and it lives here, not in
    build_tv, because the board needed this layer as much as the television did
    and had no way to get it.

    Reds first, then stales, each alphabetical: an order that does not depend on
    the clock, so a card cannot move between two polls of the same board."""
    actions: list[ActionItem] = []
    closed_targets: dict[int, Optional[float]] = {}
    for t in con.execute("SELECT metric_id, year, quarter, baseline_value, "
                         "stretch_value FROM targets"):
        if (t["year"], t["quarter"]) == wk.quarter_of(vm.last_closed):
            closed_targets[t["metric_id"]] = sc.target_for_week(
                vm.last_closed, sc.QuarterTargets(t["baseline_value"],
                                                  t["stretch_value"]))
    for section in vm.sections:
        for row in section.rows:
            closed = next((c for c in row.cells if c.week == vm.last_closed), None)
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
                    metric_id=row.metric_id, name=row.name,
                    value_display=(closed.display if closed and closed.display else "R"),
                    target_display=ct_display,
                    dri_name=row.dri_name, initials=_initials(row.dri_name),
                    has_131=row.has_131,
                    week=vm.last_closed.isoformat(),
                    next_step=step))
            elif row.last_state == "stale":
                actions.append(ActionItem(
                    kind="stale", badge="NO DATA",
                    metric_id=row.metric_id, name=row.name, value_display="-",
                    target_display=row.target_display,
                    dri_name=row.dri_name, initials=_initials(row.dri_name),
                    has_131=row.has_131,
                    week=vm.last_closed.isoformat(),
                    next_step="enter last week's number"))
    actions.sort(key=lambda a: (a.kind != "red", a.name))
    return actions


def _board_rows(con: sqlite3.Connection, vm: GridVM) -> list[BoardRow]:
    """The GridVM's rows as BoardRows: latest number, state, spark, target.
    Shared by build_tv and ceo_rows so a tile can never read differently from
    the same metric on the board."""
    def find_cell(row: Row, week: date) -> Optional[Cell]:
        return next((c for c in row.cells if c.week == week), None)

    targets_by_metric = _current_targets(con, vm)
    rows: list[BoardRow] = []
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
                section=section.name,
                latest_raw=(float(latest_raw)
                            if row.metric_type == "numeric"
                            and isinstance(latest_raw, (int, float)) else None),
                target_raw=(targets_by_metric.get(row.metric_id)
                            if row.metric_type == "numeric" else None),
                direction=row.direction, unit=row.unit))
    return rows


def _current_targets(con: sqlite3.Connection, vm: GridVM) -> dict[int, Optional[float]]:
    out: dict[int, Optional[float]] = {}
    for t in con.execute("SELECT metric_id, year, quarter, baseline_value, stretch_value FROM targets"):
        if (t["year"], t["quarter"]) == wk.quarter_of(vm.current_week):
            out[t["metric_id"]] = sc.target_for_week(
                vm.current_week, sc.QuarterTargets(t["baseline_value"], t["stretch_value"]))
    return out


def ceo_rows(con: sqlite3.Connection, now: datetime, rows: list[BoardRow]) -> list[BoardRow]:
    """`rows` plus any CEO-slot metric that lives in a HIDDEN section.

    Hiding a section keeps it off the board, the check-in page and the
    nudges; it does not mean the CEO cannot see it. Companies park revenue,
    expenses and profit in a hidden section precisely because they are not
    board material for everyone, and the CEO view is where they are read.
    The hidden grid is built only when a slot actually points there."""
    from . import ceo as ceom
    have = {r.metric_id for r in rows}
    wanted = {mid for mid in ceom.resolve_slots(con).values() if mid is not None}
    for ids in ceom.all_breakdown_ids(con).values():
        wanted.update(ids)
    missing = wanted - have
    if not missing:
        return rows
    hvm = build_grid(con, now, hidden=True)
    return rows + [r for r in _board_rows(con, hvm) if r.metric_id in missing]


def build_tv(con: sqlite3.Connection, now: datetime) -> TvVM:
    from .db import get_setting
    vm = build_grid(con, now)

    rows = _board_rows(con, vm)
    targets_by_metric = _current_targets(con, vm)

    actions = build_actions(con, vm)

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
    roster = sorted((r for g in groups if _is_status(g) for r in g.rows),
                    key=_severity_key)
    columns, board_rows, board_secs = _layout_board(groups)

    return TvVM(vm=vm, mrr=mrr, columns=columns,
                board_rows=board_rows, board_secs=board_secs,
                actions=actions[:3], more_actions=max(0, len(actions) - 3),
                rows=rows, ceo=build_ceo(con, ceo_rows(con, now, rows)),
                roster=roster, roster_cols=_roster_cols(len(roster)))


# ---------------------------------------------------- the CEO view model
@dataclass
class CeoTile:
    slot: str
    number: str          # "01".."07", the order the template names them in
    label: str
    row: Optional[BoardRow]   # None = slot not mapped to any live metric
    pct: Optional[float]      # share of target 0-100, drives the ring
    pct_display: str          # "82%" / ""
    dash: Optional[float]     # stroke-dasharray length for a r=45 ring
    pct_note: str             # "of target" / "within budget" / ""


@dataclass
class CeoVM:
    tiles: dict           # slot -> CeoTile, all seven always present
    mapped: int           # how many slots found a live metric
    close_rate: str       # conversions / leads, "" when either is missing
    per_conversion: str   # revenue / conversions
    margin: str           # profit / revenue
    margin_state: str
    bars: list            # bottom-line bars: {label, display, pct, cls}
    bars_note: str        # which week the bars read from


@dataclass
class BreakdownItem:
    row: BoardRow
    counted: bool          # has a number for the tile's week
    share_pct: float       # of the categories' sum, 0 when not counted
    share_display: str


@dataclass
class Breakdown:
    slot: str
    label: str
    week_note: str         # the week every number in the panel is read for
    week: str              # its Monday, for "use the sum"
    items: list            # BreakdownItem, in saved order
    sum_display: str
    sum_raw: Optional[float]
    complete: bool         # every category has a number for that week
    total_display: str     # the tile's own number, '' when not entered
    mismatch: bool         # complete, total entered, and they disagree


_RING = 2 * 3.14159265 * 45   # circumference of the r=45 ring in _view_ceo.html


def _share_of_target(r: BoardRow) -> Optional[float]:
    """0-100: how much of the week's target this number is. A lower-is-better
    metric inverts, so 'under budget' reads as full, not as empty."""
    if r.latest_raw is None or not r.target_raw or r.target_raw <= 0:
        return None
    if r.direction == "down":
        ratio = 1.0 if r.latest_raw <= r.target_raw else r.target_raw / r.latest_raw
    else:
        ratio = r.latest_raw / r.target_raw
    return max(0.0, min(100.0, ratio * 100))


def build_breakdowns(con: sqlite3.Connection, ceo: "CeoVM", rows: list[BoardRow],
                     now: datetime) -> dict:
    """slot -> Breakdown for every tile that has categories.

    Every number is read for ONE week - the week the tile itself shows - so
    shares and the sum-vs-total check never mix this week's tax with last
    week's contractors. A category not entered for that week is listed but not
    counted, and the sum is marked incomplete rather than quietly low."""
    from . import ceo as ceom
    by_id = {r.metric_id: r for r in rows}
    out = {}
    for slot, ids in ceom.all_breakdown_ids(con).items():
        tile = ceo.tiles.get(slot)
        cats = [by_id[i] for i in ids if i in by_id]
        if tile is None or tile.row is None or not cats:
            continue
        parent = tile.row
        note = parent.week_note or "last week"
        week = (wk.current_week(now) if note == "this week" else wk.last_closed_week(now))
        counted = [r for r in cats if r.week_note == note and r.latest_raw is not None]
        total = sum(r.latest_raw for r in counted) if counted else None
        items = []
        for r in cats:
            ok = r in counted
            pct = (abs(r.latest_raw) / sum(abs(c.latest_raw) for c in counted) * 100
                   if ok and total and any(c.latest_raw for c in counted) else 0.0)
            items.append(BreakdownItem(row=r, counted=ok, share_pct=round(pct, 1),
                                       share_display=(f"{pct:.0f}%" if ok else "")))
        complete = len(counted) == len(cats)
        has_total = parent.latest_raw is not None and parent.week_note == note
        out[slot] = Breakdown(
            slot=slot, label=tile.label, week_note=note, week=week.isoformat(),
            items=items,
            sum_display=(fmt_value("numeric", parent.unit, total) if total is not None else "-"),
            sum_raw=total, complete=complete,
            total_display=(parent.latest_display if has_total else ""),
            mismatch=bool(complete and has_total and total is not None
                          and abs(parent.latest_raw - total) >= 0.5))
    return out


def build_ceo(con: sqlite3.Connection, rows: list[BoardRow]) -> CeoVM:
    """Arrange the board's rows into the seven CEO slots (app/ceo.py owns the
    slots and the mapping). Derived numbers - close rate, revenue per new
    customer, margin - are computed here at render time and never stored, and
    only when both inputs come from the SAME week: a close rate of this week's
    leads over last week's conversions is a number nobody asked for."""
    from . import ceo as ceom
    by_id = {r.metric_id: r for r in rows}
    mapping = ceom.resolve_slots(con)
    tiles: dict[str, CeoTile] = {}
    for i, slot in enumerate(ceom.SLOTS, start=1):
        r = by_id.get(mapping.get(slot.key) or -1)
        pct = _share_of_target(r) if r else None
        note = ""
        if pct is not None:
            note = ("on budget" if r.direction == "down" and pct >= 100
                    else "of budget" if r.direction == "down" else "of target")
        tiles[slot.key] = CeoTile(
            slot=slot.key, number=f"{i:02d}", label=slot.label, row=r, pct=pct,
            pct_display=(f"{pct:.0f}%" if pct is not None else ""),
            dash=(round(pct / 100 * _RING, 1) if pct is not None else None),
            pct_note=note)

    def same_week(*keys: str) -> Optional[list[BoardRow]]:
        rs = [tiles[k].row for k in keys]
        if any(r is None or r.latest_raw is None for r in rs):
            return None
        if len({r.week_note for r in rs}) != 1:
            return None
        return rs

    close_rate = per_conversion = margin = ""
    margin_state = ""
    pair = same_week("leads", "conversions")
    if pair and pair[0].latest_raw > 0:
        close_rate = f"{pair[1].latest_raw / pair[0].latest_raw * 100:.1f}%"
    pair = same_week("conversions", "revenue")
    if pair and pair[0].latest_raw > 0:
        per_conversion = fmt_value("numeric", pair[1].unit,
                                   round(pair[1].latest_raw / pair[0].latest_raw))
    pair = same_week("revenue", "profit")
    if pair and pair[0].latest_raw > 0:
        m = pair[1].latest_raw / pair[0].latest_raw * 100
        margin = f"{m:.0f}%"
        margin_state = pair[1].latest_state

    # Bottom-line bars scale to the biggest of the three so revenue is the
    # full width and expenses/profit read as shares of it.
    bars, bars_note = [], ""
    trio = [tiles[k].row for k in ("revenue", "expenses", "profit")]
    live = [r for r in trio if r is not None and r.latest_raw is not None]
    if len(live) >= 2 and len({r.week_note for r in live}) == 1:
        top = max(abs(r.latest_raw) for r in live) or 1.0
        for key, r in zip(("revenue", "expenses", "profit"), trio):
            if r is None or r.latest_raw is None:
                continue
            cls = key if key != "profit" else (r.latest_state or "no-target")
            bars.append({"label": r.name, "display": r.latest_display,
                         "pct": round(max(1.5, abs(r.latest_raw) / top * 100), 1),
                         "cls": cls, "negative": r.latest_raw < 0})
        bars_note = live[0].week_note

    return CeoVM(tiles=tiles, mapped=sum(1 for t in tiles.values() if t.row),
                 close_rate=close_rate, per_conversion=per_conversion,
                 margin=margin, margin_state=margin_state,
                 bars=bars, bars_note=bars_note)


# ---------------------------------------------------- one metric, in full
@dataclass
class MetricPoint:
    week: date
    label: str            # "Q3-W6"
    date_label: str       # "Aug 10"
    state: str
    display: str
    raw: Optional[float | str]
    target: Optional[float]
    target_display: str
    editable: bool
    is_current: bool


@dataclass
class MetricVM:
    metric_id: int
    name: str
    section: str
    metric_type: str
    rollup: Optional[str]
    direction: str
    unit: Optional[str]
    is_key: bool
    archived: bool
    dri_name: str
    dri_user_id: Optional[int]
    quarter_label: str
    year: int
    quarter: int
    points: list[MetricPoint]     # every week of the quarter to date
    last_closed: date
    current_week: date
    latest: Optional[MetricPoint]  # the last closed week
    baseline: Optional[float]
    stretch: Optional[float]
    red_streak: int
    escalation: int
    quarter_total_display: str
    hit_weeks: int                # closed weeks scored green
    scored_weeks: int             # closed weeks with a number and a target


def build_metric(con: sqlite3.Connection, metric_id: int, now: datetime,
                 year: Optional[int] = None,
                 quarter: Optional[int] = None) -> Optional[MetricVM]:
    """One metric across one quarter: every week, its target, and how it scored.

    This is the address the product never had. "Why is this red?" used to mean
    reading the board, then Activity, then Targets, then the 1-3-1, and holding
    the four in your head. Scores through sc.cell_state exactly as the grid and
    the TV do - a metric that reads red here and green on the board would be
    worse than no page at all."""
    tz = wk.BUSINESS_TZ
    m = con.execute(
        """SELECT m.*, s.name AS section_name, u.display_name AS dri_name
           FROM metrics m
           JOIN sections s ON s.id = m.section_id
           LEFT JOIN users u ON u.id = m.dri_user_id
           WHERE m.id = ?""", (metric_id,)).fetchone()
    if m is None:
        return None

    today = now.astimezone(tz).date()
    cur_week = wk.monday_of(today)
    last_closed = wk.last_closed_week(now, tz)
    if year is None or quarter is None:
        year, quarter = wk.quarter_of(last_closed)

    # Every Monday of the quarter, stopping at the current week: a quarter that
    # has not happened yet is not evidence.
    start = wk.first_monday_of_quarter(year, quarter)
    weeks: list[date] = []
    w = start
    while wk.quarter_of(w) == (year, quarter) and w <= cur_week:
        weeks.append(w)
        w += timedelta(days=7)

    t = con.execute(
        """SELECT baseline_value, stretch_value FROM targets
           WHERE metric_id = ? AND year = ? AND quarter = ?""",
        (metric_id, year, quarter)).fetchone()
    qt = sc.QuarterTargets(t["baseline_value"], t["stretch_value"]) if t else None

    entries = {r["week_start"]: sc.EntryInfo(r["value_numeric"], r["value_status"])
               for r in con.execute(
                   "SELECT week_start, value_numeric, value_status FROM entries "
                   "WHERE metric_id = ?", (metric_id,))}
    info = _metric_info(m)

    points, values = [], []
    hit = scored = 0
    for w in weeks:
        ei = entries.get(w.isoformat())
        target = sc.target_for_week(w, qt)
        state = sc.cell_state(info, w, ei, target, now, tz)
        raw = None
        if ei is not None:
            raw = ei.value_status if m["metric_type"] == "status" else ei.value_numeric
        if w <= last_closed and raw is not None and target is not None:
            scored += 1
            if state == sc.CellState.GREEN:
                hit += 1
        if raw is not None and m["metric_type"] != "status":
            values.append(sc.EntryInfo(ei.value_numeric, None))
        points.append(MetricPoint(
            week=w, label=wk.quarter_label(w), date_label=w.strftime("%b %-d"),
            state=state.value,
            display=fmt_value(m["metric_type"], m["unit"], raw), raw=raw,
            target=target,
            target_display=(fmt_value("numeric", m["unit"], target)
                            if target is not None else
                            ("G" if m["metric_type"] == "status" else "-")),
            editable=(state != sc.CellState.NA), is_current=(w == cur_week)))

    states_desc = []
    for i in range(8):
        w = last_closed - timedelta(days=7 * i)
        if w < info.start_week:
            break
        states_desc.append(sc.cell_state(info, w, entries.get(w.isoformat()),
                                         sc.target_for_week(w, qt), now, tz))
    streak = sc.consecutive_red_weeks(states_desc)

    total = sc.month_subtotal(m["metric_type"], m["rollup"], values)
    if isinstance(total, float):
        total = fmt_value("numeric", m["unit"], total)

    return MetricVM(
        metric_id=m["id"], name=m["name"], section=m["section_name"],
        metric_type=m["metric_type"], rollup=m["rollup"], direction=m["direction"],
        unit=m["unit"], is_key=bool(m["is_key"]), archived=bool(m["archived_at"]),
        dri_name=m["dri_name"] or "-", dri_user_id=m["dri_user_id"],
        quarter_label=f"Q{quarter} {year}", year=year, quarter=quarter,
        points=points, last_closed=last_closed, current_week=cur_week,
        latest=next((p for p in points if p.week == last_closed), None),
        baseline=(t["baseline_value"] if t else None),
        stretch=(t["stretch_value"] if t else None),
        red_streak=streak, escalation=sc.escalation_level(streak),
        quarter_total_display=(total if isinstance(total, str) else "-"),
        hit_weeks=hit, scored_weeks=scored)


# ------------------------------------------------- setting targets, with evidence
@dataclass
class TargetRow:
    metric: sqlite3.Row
    baseline: Optional[float]
    stretch: Optional[float]
    prev_label: str                  # "Q2 2026"
    prev_baseline_display: str
    prev_actual: Optional[float]
    prev_actual_display: str
    prev_delta_pct: Optional[int]    # actual vs baseline, signed
    hit_weeks: int
    scored_weeks: int


def build_target_rows(con: sqlite3.Connection, year: int, quarter: int,
                      now: datetime) -> list[TargetRow]:
    """Every numeric metric's target for one quarter, next to what actually
    happened last quarter.

    Setting a target is the most consequential thing an admin does - it defines
    red, yellow and green for thirteen weeks, for everyone - and the page for it
    used to be empty number boxes with no context at all. "You hit this 2 weeks
    out of 13" is the fact that should drive the number, and it is already in
    the database."""
    tz = wk.BUSINESS_TZ
    py, pq = (year - 1, 4) if quarter == 1 else (year, quarter - 1)
    prev_weeks = []
    w = wk.first_monday_of_quarter(py, pq)
    while wk.quarter_of(w) == (py, pq):
        prev_weeks.append(w)
        w += timedelta(days=7)
    last_closed = wk.last_closed_week(now, tz)

    metrics = con.execute(
        """SELECT m.*, s.name AS section_name, u.display_name AS dri_name
           FROM metrics m
           JOIN sections s ON s.id = m.section_id
           LEFT JOIN users u ON u.id = m.dri_user_id
           WHERE m.archived_at IS NULL AND m.metric_type = 'numeric'
           ORDER BY s.sort_order, m.sort_order""").fetchall()

    rows = []
    for m in metrics:
        t = con.execute(
            "SELECT * FROM targets WHERE metric_id=? AND year=? AND quarter=?",
            (m["id"], year, quarter)).fetchone()
        pt = con.execute(
            "SELECT * FROM targets WHERE metric_id=? AND year=? AND quarter=?",
            (m["id"], py, pq)).fetchone()
        pqt = (sc.QuarterTargets(pt["baseline_value"], pt["stretch_value"])
               if pt else None)
        entries = {r["week_start"]: sc.EntryInfo(r["value_numeric"], r["value_status"])
                   for r in con.execute(
                       "SELECT week_start, value_numeric, value_status FROM entries "
                       "WHERE metric_id = ?", (m["id"],))}
        info = _metric_info(m)

        vals, hit, scored = [], 0, 0
        for pw in prev_weeks:
            ei = entries.get(pw.isoformat())
            if ei is not None and ei.value_numeric is not None:
                vals.append(ei.value_numeric)
            if pw > last_closed:
                continue
            target = sc.target_for_week(pw, pqt)
            if ei is None or ei.value_numeric is None or target is None:
                continue
            scored += 1
            if sc.cell_state(info, pw, ei, target, now, tz) == sc.CellState.GREEN:
                hit += 1

        # The weekly MEAN, not the quarter total: a weekly target is compared
        # with a weekly number, whatever the metric rolls up to.
        actual = (sum(vals) / len(vals)) if vals else None
        pb = pt["baseline_value"] if pt else None
        delta = (round((actual - pb) / pb * 100) if actual is not None
                 and pb not in (None, 0) else None)

        rows.append(TargetRow(
            metric=m,
            baseline=(t["baseline_value"] if t else None),
            stretch=(t["stretch_value"] if t else None),
            prev_label=f"Q{pq} {py}",
            prev_baseline_display=(fmt_value("numeric", m["unit"], pb)
                                   if pb is not None else "-"),
            prev_actual=actual,
            prev_actual_display=(fmt_value("numeric", m["unit"], actual)
                                 if actual is not None else "-"),
            prev_delta_pct=delta, hit_weeks=hit, scored_weeks=scored))
    return rows
