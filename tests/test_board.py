"""TV board layout: worst-first sorting of status sections, widening into
sub-columns, and green-overflow folding. Pure list-in/list-out logic in app.grid, no DB needed."""
from app.grid import (BoardRow, BoardSection, COL_CAP_UNITS, MAX_SUBCOLS,
                      _layout_board, _units)


def mk(state: str, streak: int = 0, mtype: str = "status",
       name: str = "m", section: str = "S") -> BoardRow:
    return BoardRow(
        metric_id=0, name=name, metric_type=mtype, is_key=False,
        dri_name="-", dri_first="", initials="",
        latest_display="G", latest_state=state, week_note="",
        cur_state="pending", target_display="-", spark=[],
        red_streak=streak, section=section)


def col_units(col: list[BoardSection]) -> float:
    return sum(_units(g) for g in col)


def test_status_section_sorts_worst_first():
    g = BoardSection("Clients", [
        mk("green", name="g1"), mk("pending", name="p"),
        mk("red", streak=1, name="r1"), mk("stale", name="s"),
        mk("yellow", name="y"), mk("red", streak=3, name="r3"),
        # in a red streak but this week's cell not yet entered:
        mk("pending", streak=2, name="streaky"),
    ])
    _layout_board([g])
    assert [r.name for r in g.rows] == ["r3", "streaky", "r1", "y", "s", "p", "g1"]


def test_numeric_sections_keep_admin_order():
    g = BoardSection("Sales", [
        mk("green", mtype="numeric", name="a"),
        mk("red", streak=2, mtype="numeric", name="b"),
        mk("yellow", mtype="numeric", name="c"),
    ])
    _layout_board([g])
    assert [r.name for r in g.rows] == ["a", "b", "c"]


def test_no_fold_when_everything_fits():
    groups = [
        BoardSection("Sales", [mk("green", mtype="numeric") for _ in range(5)]),
        BoardSection("Clients", [mk("green") for _ in range(7)]),
    ]
    _layout_board(groups)
    assert all(not g.hidden for g in groups)


def test_overflow_folds_greens_keeps_problems_visible():
    clients = ([mk("red", streak=2, name=f"r{i}") for i in range(2)]
               + [mk("stale", name=f"s{i}") for i in range(2)]
               + [mk("green", name=f"g{i}") for i in range(36)])
    groups = [
        BoardSection("Sales", [mk("green", mtype="numeric") for _ in range(8)]),
        BoardSection("Clients", clients),
    ]
    columns, board_rows, _ = _layout_board(groups)
    g = groups[1]
    assert g.hidden, "over-capacity board must fold"
    # every hidden row is green; every red/stale is still visible
    assert all(r.latest_state == "green" for r in g.hidden)
    visible_states = [r.latest_state for r in g.rows]
    assert visible_states.count("red") == 2
    assert [r.red_streak for r in g.rows[:2]] == [2, 2]
    assert sum(1 for r in g.rows if r.latest_state == "stale") == 2
    # nothing lost: visible + hidden == total
    assert len(g.rows) + len(g.hidden) == 40
    assert g.subcols == MAX_SUBCOLS, "fold is the last resort, after widening"
    assert g.overflow_state == "green"
    assert g.overflow_label == "all green"
    # every column fits the cap after folding
    assert max(col_units(c) for c in columns) <= COL_CAP_UNITS


def test_overflow_label_reports_mixed_states():
    clients = ([mk("green", name=f"g{i}") for i in range(38)]
               + [mk("pending", name=f"p{i}") for i in range(3)])
    groups = [
        BoardSection("Sales", [mk("green", mtype="numeric") for _ in range(8)]),
        BoardSection("Clients", clients),
    ]
    _layout_board(groups)
    g = groups[1]
    hidden_states = {r.latest_state for r in g.hidden}
    if hidden_states == {"green"}:
        assert g.overflow_label == "all green"
    else:
        # worst state listed first, e.g. "3 pending · 5 green"
        assert g.overflow_label.startswith(("1 pending", "2 pending", "3 pending"))


def test_fold_never_hides_the_last_row():
    groups = [BoardSection("Clients", [mk("green", name=f"g{i}") for i in range(30)])]
    columns, _, _ = _layout_board(groups)
    shown = sum(len(g.rows) for col in columns for g in col)
    assert shown >= 1
    assert shown + len(groups[0].hidden) == 30


def test_overflow_widens_before_folding():
    # 16 clients beside 8 numeric rows: too tall for one column as a list,
    # comfortably visible as sub-columns - so nobody is hidden.
    clients = [mk("green", name=f"g{i}") for i in range(16)]
    groups = [
        BoardSection("Sales", [mk("green", mtype="numeric") for _ in range(8)]),
        BoardSection("Clients", clients),
    ]
    columns, board_rows, _ = _layout_board(groups)
    g = groups[1]
    assert not g.hidden
    assert 1 < g.subcols <= MAX_SUBCOLS
    assert max(col_units(c) for c in columns) <= COL_CAP_UNITS
    assert board_rows == max(8, -(-16 // g.subcols))


def test_numeric_sections_never_widen():
    groups = [BoardSection("Sales", [mk("green", mtype="numeric") for _ in range(12)]),
              BoardSection("More", [mk("green", mtype="numeric") for _ in range(12)])]
    _layout_board(groups)
    assert all(g.subcols == 1 and not g.hidden for g in groups)


def test_small_client_list_stays_a_list():
    groups = [
        BoardSection("Sales", [mk("green", mtype="numeric") for _ in range(5)]),
        BoardSection("Clients", [mk("green") for _ in range(7)]),
    ]
    _layout_board(groups)
    assert groups[1].subcols == 1


def test_roster_cols_grow_with_the_list():
    from app.grid import _roster_cols
    assert _roster_cols(0) == 1 and _roster_cols(1) == 1
    assert [_roster_cols(n) for n in (8, 15, 30, 60)] == [3, 4, 5, 7]
