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
