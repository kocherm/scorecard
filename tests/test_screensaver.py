"""Night screensaver: inside the configured window the TV serves a blackout
with a drifting clock instead of the board; outside it (or when disabled) the
board renders as always. Settings round-trip through the admin endpoints."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db as dbm
from app import weeks as wk
from app.auth import hash_password

ADMIN_EMAIL = "boss@example.com"
ADMIN_PW = "correct-horse-battery"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app  # imported late so DB_PATH is already patched

    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role,
                                  must_change_password)
               VALUES (1,?,?,'Boss','admin',0)""",
            (ADMIN_EMAIL, hash_password(ADMIN_PW)))
        dbm.set_setting(con, "display_token", "tv-token")

    client = TestClient(app)
    client.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PW})
    yield client


def hhmm(offset_hours: int) -> str:
    """Business-local wall-clock time offset from now, as the settings store it."""
    local = datetime.now(timezone.utc).astimezone(wk.BUSINESS_TZ)
    return (local + timedelta(hours=offset_hours)).strftime("%H:%M")


def set_screensaver(enabled: bool, start: str, end: str):
    with dbm.get_db() as con:
        dbm.set_setting(con, "screensaver_enabled", "1" if enabled else "0")
        dbm.set_setting(con, "screensaver_start", start)
        dbm.set_setting(con, "screensaver_end", end)


def test_tv_sleeps_inside_the_window(env):
    set_screensaver(True, hhmm(-1), hhmm(+1))
    r = env.get("/display", params={"token": "tv-token"})
    assert r.status_code == 200
    assert "tv-sleep" in r.text and "tv-sleep-clock" in r.text
    assert "b-head" not in r.text  # no board underneath the blackout
    body = env.get("/display/body", params={"token": "tv-token"})
    assert "tv-sleep" in body.text and 'id="tvroot"' in body.text


def test_tv_shows_board_outside_window_or_when_disabled(env):
    set_screensaver(True, hhmm(+1), hhmm(+2))  # window entirely in the future
    assert "tv-sleep" not in env.get("/display", params={"token": "tv-token"}).text
    set_screensaver(False, hhmm(-1), hhmm(+1))  # covers now, but off
    assert "tv-sleep" not in env.get("/display/body", params={"token": "tv-token"}).text


def test_sleep_still_requires_the_display_token(env):
    set_screensaver(True, hhmm(-1), hhmm(+1))
    assert env.get("/display", params={"token": "wrong"}).status_code == 403


def test_clock_drifts_and_never_leaves_the_screen():
    """Burn-in protection is the whole point: a fresh spot every minute, and
    never a coordinate that would hang the clock off an edge."""
    from app.main import _sleep_context

    midnight = datetime.now(timezone.utc).astimezone(wk.BUSINESS_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0)
    spots = [(_sleep_context(midnight + timedelta(minutes=m))["left"],
              _sleep_context(midnight + timedelta(minutes=m))["top"])
             for m in range(24 * 60)]

    assert all(0 <= x <= 100 and 0 <= y <= 100 for x, y in spots)
    # Consecutive minutes always land somewhere new...
    assert all(a != b for a, b in zip(spots, spots[1:]))
    # ...and a full night (21:00-06:00) never reuses a spot.
    night = spots[21 * 60:] + spots[:6 * 60]
    assert len(set(night)) == len(night)
    # Spread across the panel rather than hugging one corner.
    assert max(x for x, _ in spots) > 90 and min(x for x, _ in spots) < 10
    assert max(y for _, y in spots) > 90 and min(y for _, y in spots) < 10


def test_settings_toggle_and_save(env):
    env.post("/admin/settings/screensaver-toggle")
    env.post("/admin/settings/screensaver",
             data={"screensaver_start": "22:30", "screensaver_end": "05:45"})
    with dbm.get_db() as con:
        assert dbm.get_setting(con, "screensaver_enabled") == "1"
        assert dbm.get_setting(con, "screensaver_start") == "22:30"
        assert dbm.get_setting(con, "screensaver_end") == "05:45"
    # garbage input falls back to defaults rather than wedging the TV
    env.post("/admin/settings/screensaver",
             data={"screensaver_start": "banana", "screensaver_end": "25:99"})
    with dbm.get_db() as con:
        assert dbm.get_setting(con, "screensaver_start") == "21:00"
        assert dbm.get_setting(con, "screensaver_end") == "06:00"
    env.post("/admin/settings/screensaver-toggle")  # and off again
    with dbm.get_db() as con:
        assert dbm.get_setting(con, "screensaver_enabled") == "0"
    page = env.get("/admin/settings")
    assert "Night screensaver" in page.text
