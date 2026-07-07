from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app import weeks as wk

TZ = ZoneInfo("America/Chicago")


def test_monday_of():
    assert wk.monday_of(date(2026, 7, 6)) == date(2026, 7, 6)   # a Monday
    assert wk.monday_of(date(2026, 7, 12)) == date(2026, 7, 6)  # Sunday same week
    assert wk.monday_of(date(2026, 7, 13)) == date(2026, 7, 13)


def test_current_week_respects_timezone():
    # Sunday 23:30 Chicago = Monday 04:30 UTC; must still be the ending week.
    utc_monday = datetime(2026, 7, 13, 4, 30, tzinfo=ZoneInfo("UTC"))
    assert wk.current_week(utc_monday, TZ) == date(2026, 7, 6)


def test_week_key_roundtrip():
    m = date(2026, 7, 6)
    assert wk.parse_week(wk.week_key(m)) == m
    try:
        wk.parse_week("2026-07-07")
        assert False, "non-Monday accepted"
    except ValueError:
        pass


def test_quarter_labels():
    # Q3 2026 starts Jul 1 (Wed); first Monday is Jul 6.
    assert wk.first_monday_of_quarter(2026, 3) == date(2026, 7, 6)
    assert wk.quarter_label(date(2026, 7, 6)) == "Q3-W1"
    assert wk.quarter_label(date(2026, 9, 28)) == "Q3-W13"
    assert wk.quarter_label(date(2026, 10, 5)) == "Q4-W1"


def test_fourteen_monday_quarter():
    # Q3 2024: Jul 1 was a Monday; Sep 30 was also a Monday -> 14 Mondays.
    assert wk.first_monday_of_quarter(2024, 3) == date(2024, 7, 1)
    assert wk.quarter_week_index(date(2024, 9, 30)) == 14
    assert wk.quarter_label(date(2024, 9, 30)) == "Q3-W14"


def test_week_belongs_to_month_of_its_monday():
    # Monday Sep 28 2026 spans into October but is a September week.
    bands = wk.month_bands([date(2026, 9, 28), date(2026, 10, 5)])
    assert [b.label for b in bands] == ["SEP", "OCT"]


def test_window_weeks_four_months():
    weeks = wk.window_weeks(date(2026, 7, 6))
    # April..July 2026; first Monday in April is Apr 6, last is current week Jul 6.
    assert weeks[0] == date(2026, 4, 6)
    assert weeks[-1] == date(2026, 7, 6)
    assert all(b - a == timedelta(days=7) for a, b in zip(weeks, weeks[1:]))
    bands = wk.month_bands(weeks)
    assert [b.label for b in bands] == ["APR", "MAY", "JUN", "JUL"]
    # No future weeks.
    assert all(w <= date(2026, 7, 6) for w in weeks)


def test_window_weeks_january_wraps_year():
    weeks = wk.window_weeks(date(2026, 1, 12))
    bands = wk.month_bands(weeks)
    assert [b.label for b in bands] == ["OCT", "NOV", "DEC", "JAN"]
    assert weeks[0].year == 2025


def test_deadline_and_stale():
    week = date(2026, 7, 6)  # week Jul 6-12
    dl = wk.entry_deadline(week, TZ)
    assert dl == datetime(2026, 7, 13, 23, 59, 59, tzinfo=TZ)
    st = wk.stale_at(week, TZ)
    assert st == datetime(2026, 7, 15, 8, 0, tzinfo=TZ)  # Wednesday 08:00


def test_stale_across_dst_fall_back():
    # Week of Oct 26 2026; DST ends Nov 1. Deadline Mon Nov 2, stale Wed Nov 4 08:00 CST.
    week = date(2026, 10, 26)
    st = wk.stale_at(week, TZ)
    assert st.hour == 8 and st.date() == date(2026, 11, 4)
    assert st.utcoffset().total_seconds() == -6 * 3600  # CST after fall back
