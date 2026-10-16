"""The "why is this red?" sentence for each board cell (title + aria-label).

Built from what the cell was actually scored on (grid.Cell.target / .pace,
set in build_grid beside the cell_state call), never re-derived, so the
sentence cannot disagree with the colour. Colour alone was the only way the
board said red; this is the words for hover and for screen readers.
"""
from __future__ import annotations

from datetime import date

from . import scoring as sc
from .grid import Cell, Row, fmt_value

_STATUS_WORD = {"G": "green", "Y": "yellow", "R": "red"}


def _streak(row: Row, cell: Cell, last_closed: date) -> int:
    """Consecutive red closed weeks ending at this cell, counted the way the
    alerts count them (stale/pending skipped). The board's own row streak
    looks further back than the window, so it wins for the due week."""
    if cell.week > last_closed or cell.state != sc.CellState.RED:
        return 0
    desc = [c.state for c in reversed(row.cells) if c.week <= cell.week]
    n = sc.consecutive_red_weeks(desc)
    return max(n, row.red_streak) if cell.week == last_closed else n


def _numeric(row: Row, cell: Cell) -> str:
    v = cell.raw
    show = lambda x: fmt_value("numeric", row.unit, x)  # noqa: E731
    if cell.target is None:
        return f"{show(v)} - no target this quarter"
    target = cell.target
    paced = (cell.is_current and cell.pace is not None
             and sc.is_paced(sc.MetricInfo(row.metric_id, "numeric", row.direction,
                                           cell.week, None, row.rollup)))
    if paced:
        due = target * cell.pace
        if due <= 0:
            return f"{show(v)} so far - nothing due until Tuesday (target {show(target)})"
        pct = round(v / due * 100)
        return (f"{show(v)} vs {show(due)} due so far ({pct}% of pace, "
                f"week target {show(target)})")
    if row.direction == "down":
        if v <= target:
            return f"{show(v)} vs target {show(target)}, lower is better (within target)"
        over = round((v / target - 1) * 100) if target > 0 else None
        tail = f" ({over}% over)" if over is not None else ""
        return f"{show(v)} vs target {show(target)}, lower is better{tail}"
    pct = round(v / target * 100) if target > 0 else None
    return f"{show(v)} vs target {show(target)}" + (f" ({pct}%)" if pct is not None else "")


def cell_hint(row: Row, cell: Cell, last_closed: date) -> str:
    """'' for a cell outside the metric's life; otherwise what was compared,
    the resulting state, and the red streak it extends."""
    st = cell.state
    if st == sc.CellState.NA:
        return ""
    if st == sc.CellState.PENDING:
        return "no number yet"
    if st == sc.CellState.STALE:
        return "no number - past the Wednesday deadline"
    if row.metric_type == "status":
        parts = [f"status set to {_STATUS_WORD.get(cell.raw, cell.raw)} by the owner"]
    elif row.metric_type == "binary":
        parts = ["Yes" if cell.raw else "No", st.value]
    elif st == sc.CellState.NO_TARGET:
        parts = [_numeric(row, cell)]
    else:
        parts = [_numeric(row, cell), st.value]
    n = _streak(row, cell, last_closed)
    if n >= 2:
        parts.append(f"week {n} red in a row")
    elif n == 1:
        parts.append("first red week")
    return " · ".join(parts)
