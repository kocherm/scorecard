"""Switchable TV views, rebuilt server-side.

The five client-switched views were removed in 71944be because they carried
prev/next buttons - state in the page, and a control the kiosk has no input
device to press. These are the same idea done as a function of the clock: the
server decides on every poll, the TV stores nothing, and a unit running cog on
DRM rotates by itself.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db as dbm
from app import weeks as wk
from app.auth import hash_password

PW = "a-fine-password-123"
VIEWS = ["board", "act", "key"]


# ------------------------------------------------------- the pure function
def at(seconds: int) -> datetime:
    return datetime(2026, 8, 12, tzinfo=timezone.utc) + timedelta(seconds=seconds)


def test_rotation_advances_with_the_clock():
    seen = [wk.rotation_pick(at(s), VIEWS, 30) for s in (0, 30, 60, 90)]
    assert seen == [seen[0], seen[1], seen[2], seen[0]]
    assert len(set(seen)) == 3          # every view gets a turn


def test_rotation_holds_a_view_for_its_full_period():
    assert wk.rotation_pick(at(0), VIEWS, 30) == wk.rotation_pick(at(29), VIEWS, 30)
    assert wk.rotation_pick(at(0), VIEWS, 30) != wk.rotation_pick(at(30), VIEWS, 30)


def test_two_screens_asking_at_the_same_moment_agree():
    """The reason this is a function of time and not a stored cursor: no
    handshake, no drift, and a reload or power cut changes nothing."""
    assert wk.rotation_pick(at(77), VIEWS, 20) == wk.rotation_pick(at(77), VIEWS, 20)


def test_zero_seconds_means_no_rotation():
    assert all(wk.rotation_pick(at(s), VIEWS, 0) == "board" for s in (0, 5_000))


def test_nothing_to_choose_from_is_none_not_a_crash():
    assert wk.rotation_pick(at(0), [], 30) is None


def test_a_single_view_never_changes():
    assert all(wk.rotation_pick(at(s), ["act"], 30) == "act" for s in (0, 31, 999))


# ------------------------------------------------------------- the display
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app

    with dbm.get_db() as con:
        dbm.init_db(con)
        dbm.set_setting(con, "display_token", "tok")
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Ops',0)")
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role)
               VALUES (1,'a@x.co',?,'Ada','admin')""", (hash_password(PW),))
        con.execute(
            """INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                    is_key, start_week)
               VALUES (1,1,'Revenue','numeric','sum',1,'2026-01-05')""")
    yield app


def body(client, **params):
    params.setdefault("token", "tok")
    return client.get("/display/body", params=params).text


def test_default_is_the_full_board_alone(env):
    """Nothing changes for an instance that never opens the new panel."""
    with dbm.get_db() as con:
        assert dbm.get_setting(con, "display_views") is None
    assert "Company Scorecard" in body(TestClient(env))


def test_enabled_views_are_what_rotates(env):
    with dbm.get_db() as con:
        dbm.set_setting(con, "display_views", "key")
        dbm.set_setting(con, "display_rotate_seconds", "30")
    assert "Key metrics" in body(TestClient(env))


def test_a_view_with_nothing_to_show_is_skipped(env):
    """'Act on this' is empty exactly when the company is doing well. Rotating
    onto a blank screen would make the best case look like a fault.

    The entry matters: a metric with no number goes stale once the week's
    Wednesday 08:00 deadline passes, which is an action item. Without it this
    test passed on a Tuesday and failed on a Wednesday - it was asserting the
    day of the week, not the behaviour."""
    with dbm.get_db() as con:
        dbm.set_setting(con, "display_views", "act")
        dbm.set_setting(con, "display_rotate_seconds", "30")
        con.execute(
            """INSERT INTO entries (metric_id, week_start, value_numeric,
                                    source, entered_by_user_id)
               VALUES (1, ?, 10, 'manual', 1)""",
            (wk.last_closed_week(datetime.now(timezone.utc)).isoformat(),))
    assert "Company Scorecard" in body(TestClient(env))   # fell back to the board


def test_a_pinned_view_overrides_the_rotation_and_rides_into_the_poll(env):
    with dbm.get_db() as con:
        dbm.set_setting(con, "display_views", "board,key")
        dbm.set_setting(con, "display_rotate_seconds", "30")
    c = TestClient(env)
    assert "Key metrics" in body(c, view="key")
    page = c.get("/display", params={"token": "tok", "view": "key"}).text
    assert "view=key" in page          # the poll keeps the pin
    assert "view=" not in c.get("/display", params={"token": "tok"}).text


def test_the_tv_page_carries_no_view_state_of_its_own(env):
    """The invariant the old prev/next buttons broke: display.html must hold no
    client-side state, so the poll is the only thing that changes the view."""
    with dbm.get_db() as con:
        dbm.set_setting(con, "display_views", "board,key")
        dbm.set_setting(con, "display_rotate_seconds", "30")
    page = TestClient(env).get("/display", params={"token": "tok"}).text
    for banned in ("localStorage", "sessionStorage", "setInterval(rotate",
                   "nextView", "prev_view"):
        assert banned not in page


def test_screensaver_still_wins_over_any_view(env):
    with dbm.get_db() as con:
        dbm.set_setting(con, "display_views", "key")
        dbm.set_setting(con, "screensaver_enabled", "1")
        dbm.set_setting(con, "screensaver_start", "00:00")
        dbm.set_setting(con, "screensaver_end", "23:59")
    assert "Key metrics" not in body(TestClient(env))


def test_a_bad_view_name_falls_back_rather_than_500s(env):
    assert TestClient(env).get(
        "/display/body", params={"token": "tok", "view": "../etc"}).status_code == 200


# -------------------------------------------------------------- the settings
def admin(env):
    c = TestClient(env)
    c.post("/login", data={"email": "a@x.co", "password": PW})
    return c


def test_saving_views_and_period(env):
    admin(env).post("/admin/settings/tv-views",
                    data={"views": ["board", "key"], "rotate_seconds": "60"})
    with dbm.get_db() as con:
        assert dbm.get_setting(con, "display_views") == "board,key"
        assert dbm.get_setting(con, "display_rotate_seconds") == "60"


def test_unticking_everything_falls_back_to_the_board(env):
    """The TV cannot be fixed from the TV, so an empty rotation must not be
    savable."""
    admin(env).post("/admin/settings/tv-views",
                    data={"views": [], "rotate_seconds": "30"})
    with dbm.get_db() as con:
        assert dbm.get_setting(con, "display_views") == "board"


def test_a_period_below_the_poll_is_rounded_up(env):
    from app.main import TV_ROTATE_MIN
    admin(env).post("/admin/settings/tv-views",
                    data={"views": ["board"], "rotate_seconds": "3"})
    with dbm.get_db() as con:
        assert dbm.get_setting(con, "display_rotate_seconds") == str(TV_ROTATE_MIN)


def test_zero_is_kept_as_off_not_rounded_up(env):
    admin(env).post("/admin/settings/tv-views",
                    data={"views": ["board"], "rotate_seconds": "0"})
    with dbm.get_db() as con:
        assert dbm.get_setting(con, "display_rotate_seconds") == "0"
