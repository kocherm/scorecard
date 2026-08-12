"""Week/time engine. Pure functions only: no DB, no clock reads.

Canonical week key: the Monday date (datetime.date) of the week, weeks run
Monday-Sunday in the business timezone (America/Chicago). A week belongs to
the month and quarter containing its Monday; that single rule drives month
bands, quarter labels, and target selection.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

BUSINESS_TZ = ZoneInfo("America/Chicago")

# Due: prior week's actuals by Monday end of day.
# Stale: Wednesday 08:00 local after that (business rule confirmed 2026-07-06).
STALE_HOUR = 8  # 08:00 on the Wednesday after the deadline Monday


def monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def current_week(now: datetime, tz: ZoneInfo = BUSINESS_TZ) -> date:
    """Monday of the week containing `now`, judged in business-local time."""
    return monday_of(now.astimezone(tz).date())


def week_key(monday: date) -> str:
    return monday.isoformat()


def parse_week(key: str) -> date:
    d = date.fromisoformat(key)
    if d.weekday() != 0:
        raise ValueError(f"week key {key!r} is not a Monday")
    return d


def quarter_of(monday: date) -> tuple[int, int]:
    return monday.year, (monday.month - 1) // 3 + 1


def first_monday_of_quarter(year: int, quarter: int) -> date:
    first = date(year, 3 * (quarter - 1) + 1, 1)
    return first + timedelta(days=(7 - first.weekday()) % 7)


def quarter_week_index(monday: date) -> int:
    """1-based week number within the calendar quarter (1..14)."""
    year, q = quarter_of(monday)
    return (monday - first_monday_of_quarter(year, q)).days // 7 + 1


def quarter_label(monday: date) -> str:
    _, q = quarter_of(monday)
    return f"Q{q}-W{quarter_week_index(monday)}"


def window_weeks(today: date, months: int = 4) -> list[date]:
    """Mondays of the rolling display window: every week whose Monday falls in
    the `months` most recent calendar months, up to and including the current
    week. No future weeks."""
    cur = monday_of(today)
    # First day of the window's oldest month.
    y, m = today.year, today.month
    m -= months - 1
    while m < 1:
        m += 12
        y -= 1
    start_month = date(y, m, 1)
    first = start_month + timedelta(days=(7 - start_month.weekday()) % 7)
    weeks = []
    w = first
    while w <= cur:
        weeks.append(w)
        w += timedelta(days=7)
    return weeks


@dataclass(frozen=True)
class MonthBand:
    year: int
    month: int
    label: str  # "APR"
    weeks: tuple[date, ...]


_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
           "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def month_bands(weeks: list[date]) -> list[MonthBand]:
    """Group consecutive Mondays into month header bands."""
    bands: list[MonthBand] = []
    group: list[date] = []
    for w in weeks:
        if group and (w.year, w.month) != (group[0].year, group[0].month):
            bands.append(MonthBand(group[0].year, group[0].month,
                                   _MONTHS[group[0].month - 1], tuple(group)))
            group = []
        group.append(w)
    if group:
        bands.append(MonthBand(group[0].year, group[0].month,
                               _MONTHS[group[0].month - 1], tuple(group)))
    return bands


def entry_deadline(week: date, tz: ZoneInfo = BUSINESS_TZ) -> datetime:
    """A week's actuals are due 23:59:59 local on the FOLLOWING Monday."""
    return datetime.combine(week + timedelta(days=7), time(23, 59, 59), tzinfo=tz)


def stale_at(week: date, tz: ZoneInfo = BUSINESS_TZ) -> datetime:
    """Missing entry turns gray (and Slack fires) Wednesday 08:00 local,
    i.e. ~32h after the Monday-EOD deadline."""
    return datetime.combine(week + timedelta(days=9), time(STALE_HOUR, 0), tzinfo=tz)


def last_closed_week(now: datetime, tz: ZoneInfo = BUSINESS_TZ) -> date:
    """The most recent fully-elapsed week (the one whose entries are due)."""
    return current_week(now, tz) - timedelta(days=7)


def in_nightly_window(now: datetime, start: str, end: str,
                      tz: ZoneInfo = BUSINESS_TZ) -> bool:
    """True when business-local wall-clock time is inside [start, end), both
    "HH:MM" strings. The window may cross midnight ("21:00"-"06:00").
    start == end, or anything unparseable, means the window never matches."""
    try:
        s, e = time.fromisoformat(start), time.fromisoformat(end)
    except (TypeError, ValueError):
        return False
    if s == e:
        return False
    t = now.astimezone(tz).time()
    return (s <= t < e) if s < e else (t >= s or t < e)


def rotation_pick(now: datetime, options: list, seconds: int):
    """Which of `options` a clock-driven rotation is showing at `now`.

    Derived from the wall clock rather than from a cursor anyone has to keep,
    which is the whole reason this is usable on the TV. The kiosk runs cog on
    DRM with no keyboard, mouse or remote, so a view you have to press a button
    to change is a view that never changes; and display.html is required to
    hold no client-side state, so a "current view" counter has nowhere honest
    to live. As a function of time it needs no storage, survives a reload or a
    power cut, and two screens in the same room agree without talking.

    seconds <= 0 means no rotation - the first option, forever. Returns None
    only when there is nothing to choose from."""
    if not options:
        return None
    if seconds <= 0:
        return options[0]
    return options[int(now.timestamp()) // seconds % len(options)]
