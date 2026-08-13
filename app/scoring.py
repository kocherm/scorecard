"""Scoring rules as pure functions. No I/O, no clock reads: `now` is a parameter.

Bands (Dan Martell methodology, SPEC.md):
  numeric: green >= 100% of target, yellow 70-99%, red < 70%
           direction='down' metrics (churn risk $, open bugs) invert the ratio
  binary:  green or red only
  status:  R/Y/G passthrough (client health)
  stale (gray) means "no data", distinct from red which means "bad number".

The IN-PROGRESS week judges accumulating counts on PACE, not on the full
target - see pace_fraction. Without it every "do N things this week" metric
reads red from Monday until it happens to cross 70%, which is noise, not
signal: nobody is behind on Monday morning. Only rollup='sum' + direction='up'
metrics are paced, because they are the ones that accumulate; a point-in-time
value (rollup='average': MRR, churn risk, a percentage) is already whole every
day it is read, so scaling its target would turn "MRR is 5k of 25k" green on
Tuesday and be a lie. Closed weeks are never paced.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from . import weeks as wk

YELLOW_FLOOR = 0.70

# The week's work is due by the end of Saturday, so pace is measured over six
# days (Mon-Sat) rather than seven. Sunday owes the whole target.
PACE_DAYS = 6


class CellState(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    STALE = "stale"        # deadline + grace passed with no entry
    PENDING = "pending"    # no entry yet, still within grace
    NO_TARGET = "no-target"  # value present, no target configured
    NA = "na"              # before metric start / after archive


@dataclass(frozen=True)
class MetricInfo:
    id: int
    metric_type: Literal["numeric", "binary", "status"]
    direction: Literal["up", "down"]
    start_week: date
    archived_week: Optional[date]  # Monday of archive week, or None
    # 'sum' | 'average' | None. Only meaningful for numeric metrics, and the
    # sole input to whether the in-progress week is paced. Defaulted so every
    # existing caller and test keeps the old (unpaced) behaviour.
    rollup: Optional[str] = None


@dataclass(frozen=True)
class EntryInfo:
    value_numeric: Optional[float]
    value_status: Optional[str]


@dataclass(frozen=True)
class QuarterTargets:
    baseline: float
    stretch: float


def target_for_week(week: date, targets: Optional[QuarterTargets]) -> Optional[float]:
    """Baseline applies quarter-weeks 1-6, stretch from week 7 on (incl. W14)."""
    if targets is None:
        return None
    return targets.baseline if wk.quarter_week_index(week) <= 6 else targets.stretch


def score_numeric(value: float, target: float, direction: str) -> CellState:
    if target <= 0:
        # App-level validation forbids this for direction='up'; treat defensively.
        if direction == "down":
            return CellState.GREEN if value <= target else CellState.RED
        return CellState.NO_TARGET
    if direction == "down":
        if value <= target:
            return CellState.GREEN
        ratio = target / value
    else:
        ratio = value / target
    if ratio >= 1.0:
        return CellState.GREEN
    if ratio >= YELLOW_FLOOR:
        return CellState.YELLOW
    return CellState.RED


def is_paced(metric: MetricInfo) -> bool:
    """Does this metric accumulate through the week, so a partial number
    part-way through is expected rather than bad?"""
    return (metric.metric_type == "numeric"
            and metric.rollup == "sum"
            and metric.direction == "up")


def pace_fraction(week: date, now: datetime,
                  tz: ZoneInfo = wk.BUSINESS_TZ) -> float:
    """How much of `week`'s target should already be done, as 0.0-1.0.

    Measured against the close of YESTERDAY, not the current instant: you are
    asked for what the days you have finished owed, never for credit against a
    day still in progress. So Monday owes nothing (the week has not had a full
    day yet), Tuesday owes 1/6, and by Sunday the whole target is due. The
    alternative - pro-rating continuously by the hour - makes a cell change
    colour while someone is looking at it and calls you behind at 5pm for work
    you have until Saturday to finish.

    A day is the smallest unit anyone reasons about here, which is why this
    reads the date and not the clock.
    """
    today = now.astimezone(tz).date()
    days_done = (today - week).days
    if days_done <= 0:          # Monday, or the week has not started
        return 0.0
    return min(days_done, PACE_DAYS) / PACE_DAYS


def score_binary(value: float) -> CellState:
    return CellState.GREEN if value else CellState.RED


def score_status(status: str) -> CellState:
    return {"G": CellState.GREEN, "Y": CellState.YELLOW, "R": CellState.RED}[status]


def cell_state(metric: MetricInfo, week: date, entry: Optional[EntryInfo],
               target: Optional[float], now: datetime,
               tz: ZoneInfo = wk.BUSINESS_TZ) -> CellState:
    """The single scoring entry point used by TV, edit grid, API, and alerts."""
    if week < metric.start_week:
        return CellState.NA
    if metric.archived_week is not None and week >= metric.archived_week:
        return CellState.NA
    if entry is None:
        return CellState.STALE if now >= wk.stale_at(week, tz) else CellState.PENDING
    if metric.metric_type == "status":
        return score_status(entry.value_status)
    if metric.metric_type == "binary":
        return score_binary(entry.value_numeric)
    if target is None:
        return CellState.NO_TARGET
    if week == wk.current_week(now, tz) and is_paced(metric):
        # Judge the in-progress week against what the finished days owed.
        due = target * pace_fraction(week, now, tz)
        if due <= 0:
            # Monday: nothing was due yet, so any number is on pace. Falling
            # through to score_numeric would hit its target<=0 guard and
            # report NO_TARGET, which reads as "misconfigured" on the board.
            return CellState.GREEN
        return score_numeric(entry.value_numeric, due, metric.direction)
    return score_numeric(entry.value_numeric, target, metric.direction)


def consecutive_red_weeks(states_desc: list[CellState]) -> int:
    """Count leading REDs walking backward from the reference week.
    STALE/PENDING are skipped (you cannot dodge escalation by not entering a
    number; staleness fires its own alert). Anything else breaks the streak."""
    streak = 0
    for s in states_desc:
        if s == CellState.RED:
            streak += 1
        elif s in (CellState.STALE, CellState.PENDING):
            continue
        else:
            break
    return streak


def escalation_level(red_streak: int) -> int:
    """0 none; 1 = 1-3-1 due; 2 = 15-min 1:1; 3+ = structural."""
    return min(red_streak, 3)


def month_subtotal(metric_type: str, rollup: Optional[str],
                   values: list[EntryInfo]) -> Optional[float | str]:
    """Subtotal for one month band. Missing weeks are excluded, never zeroed.
    Returns None for status metrics (averaging R/Y/G is meaningless)."""
    if metric_type == "status":
        return None
    nums = [e.value_numeric for e in values if e is not None and e.value_numeric is not None]
    if not nums:
        return None
    if metric_type == "binary":
        return f"{sum(1 for n in nums if n)}/{len(nums)}"
    if rollup == "average":
        return round(sum(nums) / len(nums), 2)
    return round(sum(nums), 2)
