from datetime import date, datetime
from zoneinfo import ZoneInfo

from app import scoring as sc
from app.scoring import CellState

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 7, 6, 12, 0, tzinfo=TZ)


def metric(**kw):
    base = dict(id=1, metric_type="numeric", direction="up",
                start_week=date(2026, 1, 5), archived_week=None)
    base.update(kw)
    return sc.MetricInfo(**base)


def entry(num=None, status=None):
    return sc.EntryInfo(value_numeric=num, value_status=status)


def test_numeric_bands():
    assert sc.score_numeric(10, 10, "up") == CellState.GREEN
    assert sc.score_numeric(7, 10, "up") == CellState.YELLOW
    assert sc.score_numeric(6.9, 10, "up") == CellState.RED
    assert sc.score_numeric(15, 10, "up") == CellState.GREEN


def test_numeric_direction_down():
    # Churn risk: target 10k, lower is better.
    assert sc.score_numeric(0, 10000, "down") == CellState.GREEN
    assert sc.score_numeric(10000, 10000, "down") == CellState.GREEN
    assert sc.score_numeric(12000, 10000, "down") == CellState.YELLOW   # ratio .83
    assert sc.score_numeric(20000, 10000, "down") == CellState.RED      # ratio .5


def test_binary_never_yellow():
    assert sc.score_binary(1) == CellState.GREEN
    assert sc.score_binary(0) == CellState.RED


def test_status_passthrough():
    assert sc.score_status("G") == CellState.GREEN
    assert sc.score_status("Y") == CellState.YELLOW
    assert sc.score_status("R") == CellState.RED


def test_target_ramp():
    t = sc.QuarterTargets(baseline=10, stretch=15)
    assert sc.target_for_week(date(2026, 7, 6), t) == 10    # Q3-W1
    assert sc.target_for_week(date(2026, 8, 10), t) == 10   # Q3-W6
    assert sc.target_for_week(date(2026, 8, 17), t) == 15   # Q3-W7
    assert sc.target_for_week(date(2024, 9, 30), t) == 15   # Q3-W14 uses stretch
    assert sc.target_for_week(date(2026, 7, 6), None) is None


def test_cell_state_precedence():
    m = metric(start_week=date(2026, 6, 1))
    # Before start week: NA even with no entry.
    assert sc.cell_state(m, date(2026, 5, 25), None, 10, NOW, TZ) == CellState.NA
    # Missing entry within grace: PENDING (week of Jun 29, stale Wed Jul 8 08:00).
    assert sc.cell_state(m, date(2026, 6, 29), None, 10, NOW, TZ) == CellState.PENDING
    # Missing entry past grace: STALE (week of Jun 22 went stale Jul 1).
    assert sc.cell_state(m, date(2026, 6, 22), None, 10, NOW, TZ) == CellState.STALE
    # Entry but no target: NO_TARGET.
    assert sc.cell_state(m, date(2026, 6, 22), entry(num=5), None, NOW, TZ) == CellState.NO_TARGET
    # Archived: NA from archive week on.
    ma = metric(archived_week=date(2026, 6, 15))
    assert sc.cell_state(ma, date(2026, 6, 15), entry(num=5), 10, NOW, TZ) == CellState.NA
    assert sc.cell_state(ma, date(2026, 6, 8), entry(num=10), 10, NOW, TZ) == CellState.GREEN


def test_red_streak_skips_stale():
    S = CellState
    assert sc.consecutive_red_weeks([S.RED, S.STALE, S.RED, S.GREEN]) == 2
    assert sc.consecutive_red_weeks([S.RED, S.RED, S.YELLOW, S.RED]) == 2
    assert sc.consecutive_red_weeks([S.GREEN, S.RED]) == 0
    assert sc.consecutive_red_weeks([S.STALE, S.RED]) == 1
    assert sc.consecutive_red_weeks([]) == 0


def test_escalation_levels():
    assert sc.escalation_level(0) == 0
    assert sc.escalation_level(1) == 1
    assert sc.escalation_level(2) == 2
    assert sc.escalation_level(7) == 3


def test_month_subtotals():
    e = lambda v: sc.EntryInfo(value_numeric=v, value_status=None)
    assert sc.month_subtotal("numeric", "sum", [e(1), e(2), None, e(3)]) == 6
    assert sc.month_subtotal("numeric", "average", [e(10), None, e(20)]) == 15
    assert sc.month_subtotal("numeric", "sum", [None, None]) is None
    assert sc.month_subtotal("binary", None, [e(1), e(0), e(1)]) == "2/3"
    assert sc.month_subtotal("status", None, [sc.EntryInfo(None, "G")]) is None


# --- mid-week pace -----------------------------------------------------------
# Week of Mon 2026-08-10. Target 8, due by end of Saturday, so pace runs over
# six days and each finished day owes 8/6 = 1.33.

WEEK = date(2026, 8, 10)


def at(day, hour=12):
    """Noon on `day` of the WEEK: 10=Mon, 11=Tue, ... 16=Sun."""
    return datetime(2026, 8, day, hour, 0, tzinfo=TZ)


def paced_metric(**kw):
    return metric(rollup="sum", direction="up", **kw)


def test_pace_fraction_measures_finished_days():
    assert sc.pace_fraction(WEEK, at(10)) == 0.0            # Mon: nothing due
    assert sc.pace_fraction(WEEK, at(11)) == 1 / 6          # Tue owes Monday
    assert sc.pace_fraction(WEEK, at(13)) == 3 / 6          # Thu owes Mon-Wed
    assert sc.pace_fraction(WEEK, at(15)) == 5 / 6          # Sat owes Mon-Fri
    assert sc.pace_fraction(WEEK, at(16)) == 1.0            # Sun owes the lot
    assert sc.pace_fraction(WEEK, at(17)) == 1.0            # never exceeds 1.0


def test_pace_holds_steady_across_a_day():
    """The bar must not move while someone is looking at the board."""
    assert sc.pace_fraction(WEEK, at(11, 0)) == sc.pace_fraction(WEEK, at(11, 23))


def test_monday_is_always_on_pace():
    m = paced_metric()
    assert sc.cell_state(m, WEEK, entry(num=0), 8, at(10), TZ) == CellState.GREEN
    assert sc.cell_state(m, WEEK, entry(num=1), 8, at(10), TZ) == CellState.GREEN


def test_the_reported_case_reads_on_schedule():
    """One by Monday, two by Tuesday: on schedule, must not be red."""
    m = paced_metric()
    assert sc.cell_state(m, WEEK, entry(num=1), 8, at(10), TZ) == CellState.GREEN
    assert sc.cell_state(m, WEEK, entry(num=2), 8, at(11), TZ) == CellState.GREEN


def test_pace_tightens_as_the_week_runs_out():
    m = paced_metric()
    # Thursday owes 4.0 of 8.
    assert sc.cell_state(m, WEEK, entry(num=4), 8, at(13), TZ) == CellState.GREEN
    assert sc.cell_state(m, WEEK, entry(num=3), 8, at(13), TZ) == CellState.YELLOW  # .75
    assert sc.cell_state(m, WEEK, entry(num=2), 8, at(13), TZ) == CellState.RED     # .50
    # Sunday owes the whole target - pacing is over.
    assert sc.cell_state(m, WEEK, entry(num=8), 8, at(16), TZ) == CellState.GREEN
    assert sc.cell_state(m, WEEK, entry(num=6), 8, at(16), TZ) == CellState.YELLOW
    assert sc.cell_state(m, WEEK, entry(num=3), 8, at(16), TZ) == CellState.RED


def test_closed_weeks_are_never_paced():
    """A finished week owes its whole target however early you look at it."""
    m = paced_metric()
    prior = date(2026, 8, 3)
    assert sc.cell_state(m, prior, entry(num=3), 8, at(11), TZ) == CellState.RED
    assert sc.cell_state(m, prior, entry(num=8), 8, at(11), TZ) == CellState.GREEN


def test_point_in_time_metrics_are_not_paced():
    """MRR at 5k of 25k is behind on Tuesday, not on pace."""
    mrr = metric(rollup="average", direction="up")
    assert sc.cell_state(mrr, WEEK, entry(num=5000), 25000, at(11), TZ) == CellState.RED
    # direction='down' never paces either, whatever its rollup.
    churn = metric(rollup="sum", direction="down")
    assert sc.cell_state(churn, WEEK, entry(num=20000), 10000, at(11), TZ) == CellState.RED


def test_unpaced_when_rollup_unknown():
    """Default MetricInfo keeps the old behaviour, so nothing silently shifts."""
    assert sc.is_paced(metric()) is False
    assert sc.cell_state(metric(), WEEK, entry(num=2), 8, at(11), TZ) == CellState.RED


def test_pace_leaves_missing_entries_alone():
    """Pacing colours numbers; it must not invent one where none was entered."""
    m = paced_metric()
    assert sc.cell_state(m, WEEK, None, 8, at(13), TZ) == CellState.PENDING
