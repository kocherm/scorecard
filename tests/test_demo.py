"""Demo data mode: the generated board tells the full methodology story, the
admin toggle swaps every board surface to the demo DB, and nothing a user does
while it is on can touch real data."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db as dbm
from app import demo
from app import grid as gridm
from app import weeks as wk
from app.auth import hash_password

ADMIN_EMAIL = "boss@example.com"
ADMIN_PW = "correct-horse-battery"
REAL_METRIC = "Real Confidential Metric"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app  # imported late so DB_PATH is already patched

    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Real Section',0)")
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role,
                                  must_change_password)
               VALUES (1,?,?,'Boss','admin',0)""",
            (ADMIN_EMAIL, hash_password(ADMIN_PW)))
        con.execute(
            """INSERT INTO metrics (id, section_id, name, metric_type, rollup, start_week)
               VALUES (1, 1, ?, 'numeric', 'sum', '2026-01-05')""", (REAL_METRIC,))
        dbm.set_setting(con, "display_token", "real-tv-token")

    # Not a context manager on purpose: keeps the lifespan/scheduler off.
    client = TestClient(app)
    client.post("/login", data={"email": ADMIN_EMAIL, "password": ADMIN_PW})
    yield client


def real_entry_count():
    with dbm.get_db() as con:
        return con.execute("SELECT COUNT(*) AS n FROM entries").fetchone()["n"]


# ---------------- the generated dataset

def test_demo_board_tells_the_full_story(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    now = datetime.now(timezone.utc)
    with demo.demo_db(now, "2") as con:
        vm = gridm.build_grid(con, now)
        tv = gridm.build_tv(con, now)

    assert [s.name for s in vm.sections] == [
        "Sales Activity", "Revenue", "Client Health", "Content & Pipeline"]

    # Exactly the three scripted reds, whatever day the demo is switched on;
    # no stale/pending rows because every metric has a last-closed-week entry.
    assert vm.summary.red == 3
    assert vm.summary.stale == 0 and vm.summary.pending == 0
    red = " ".join(vm.summary.red_names)
    assert "Proposals sent" in red
    assert "New conversations from content" in red
    assert "Harborline Logistics" in red

    # The escalation ladder shows all three rungs at once.
    assert len(tv.actions) == 3 and tv.more_actions == 0
    assert {a.next_step for a in tv.actions} == {
        "15-min 1:1 this week",            # proposals, red week 2
        "file a 1-3-1 before sync",        # content convos, red week 1
        "1-3-1 filed - review in sync",    # Harborline, 1-3-1 already filed
    }

    # Goal band: current-week value, above pace, milestones on the track.
    assert tv.mrr is not None
    assert tv.mrr.pace_state == "green"
    assert tv.mrr.asof_stale is False
    assert len(tv.mrr.milestones) == 2


def test_demo_rebuilds_relative_to_any_week(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    for now in (datetime(2026, 1, 6, 12, tzinfo=timezone.utc),    # early Q1
                datetime(2026, 9, 30, 12, tzinfo=timezone.utc),   # quarter edge
                datetime(2026, 12, 29, 12, tzinfo=timezone.utc)):  # year edge
        demo.reset()
        with demo.demo_db(now, "3") as con:
            vm = gridm.build_grid(con, now)
        assert vm.summary.red == 3
        assert vm.summary.stale == 0 and vm.summary.pending == 0


# ---------------- the toggle and isolation

def test_toggle_swaps_surfaces_and_isolates_writes(env):
    client = env

    r = client.get("/")
    assert REAL_METRIC in r.text and "Acme Robotics" not in r.text

    assert client.post("/admin/settings/demo-toggle").status_code == 200
    r = client.get("/")
    assert "Acme Robotics" in r.text and "Demo data is on" in r.text
    assert REAL_METRIC not in r.text

    # Edit a demo cell: lands in the demo DB, never in the real one.
    before = real_entry_count()
    with dbm.connect(str(demo.demo_path())) as dcon:
        mid = dcon.execute("SELECT id FROM metrics WHERE name = ?",
                           ("New qualified conversations",)).fetchone()["id"]
    week = wk.current_week(datetime.now(timezone.utc)).isoformat()
    assert client.post(f"/cell/{mid}/{week}", data={"value": "999"}).status_code == 200
    assert real_entry_count() == before
    with dbm.connect(str(demo.demo_path())) as dcon:
        row = dcon.execute("SELECT value_numeric FROM entries WHERE metric_id=? AND week_start=?",
                           (mid, week)).fetchone()
    assert row["value_numeric"] == 999.0

    # Toggling off and back on discards demo edits (fresh copy every time).
    client.post("/admin/settings/demo-toggle")
    r = client.get("/")
    assert REAL_METRIC in r.text and "Acme Robotics" not in r.text
    client.post("/admin/settings/demo-toggle")
    client.get("/")  # triggers the rebuild
    with dbm.connect(str(demo.demo_path())) as dcon:
        row = dcon.execute("SELECT value_numeric FROM entries WHERE metric_id=? AND week_start=?",
                           (mid, week)).fetchone()
    assert row is None or row["value_numeric"] != 999.0


def test_display_keeps_the_real_token_while_showing_demo(env):
    client = env
    client.post("/admin/settings/demo-toggle")
    r = client.get("/display", params={"token": "real-tv-token"})
    assert r.status_code == 200
    assert "Acme Robotics" in r.text or "Harborline" in r.text
    assert client.get("/display", params={"token": "wrong"}).status_code == 403
