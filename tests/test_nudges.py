"""Nudge sweep: DMs list exactly the missing metrics with targets and a magic
link, dedupe per metric+week+kind, and every kill switch works."""
import json
from datetime import datetime, timezone

import pytest

from app import alerts
from app import db as dbm
from app import weeks as wk
from app.auth import hash_password

NOW = datetime.now(timezone.utc)
DUE = wk.last_closed_week(NOW)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        for uid, email, name, slack in ((2, "ed@x.co", "Eddie", "U123"),
                                        (3, "mo@x.co", "Mo", None)):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role,
                                      slack_member_id) VALUES (?,?,?,?,'editor',?)""",
                (uid, email, hash_password("pw-pw-pw-pw"), name, slack))
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                            start_week, dri_user_id)
                       VALUES (1, 1, 'Calls', 'numeric', 'sum', '2026-01-05', 2)""")
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type,
                                            start_week, dri_user_id)
                       VALUES (2, 1, 'Acme Health', 'status', '2026-01-05', 2)""")
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                            start_week, dri_user_id)
                       VALUES (3, 1, 'Mo Metric', 'numeric', 'sum', '2026-01-05', 3)""")
        y, q = wk.quarter_of(DUE)
        con.execute("""INSERT INTO targets (metric_id, year, quarter, baseline_value,
                                            stretch_value) VALUES (1,?,?,10,12)""", (y, q))
        dbm.set_setting(con, "alerts_enabled", "1")
        dbm.set_setting(con, "nudges_enabled", "1")
        dbm.set_setting(con, "slack_bot_token", "xoxb-test")
        dbm.set_setting(con, "public_base_url", "https://score.example.com")

    dms = []
    monkeypatch.setattr(alerts, "post_dm",
                        lambda bot, member, text, **kw: dms.append((member, text)) or True)
    yield dms


def test_nudge_dm_lists_missing_with_targets_and_link(env):
    dms = env
    assert alerts.nudge_sweep("nudge1", NOW) == 1  # Mo has no slack id -> only Eddie
    member, text = dms[0]
    assert member == "U123"
    assert "1. Calls (target 10)" in text or "1. Calls (target 12)" in text
    assert "2. Acme Health (G/Y/R - target G)" in text
    assert "https://score.example.com/checkin?t=" in text
    assert 'Reply here like "1: 12, 2: G"' in text
    with dbm.get_db() as con:
        rows = con.execute("SELECT metric_id FROM alerts_sent WHERE alert_type='nudge1'"
                           " ORDER BY metric_id").fetchall()
        assert [r["metric_id"] for r in rows] == [1, 2]
        prompt = con.execute("SELECT * FROM slack_prompts WHERE user_id=2").fetchone()
        assert json.loads(prompt["metric_ids"]) == [1, 2]
        assert prompt["week_start"] == DUE.isoformat()
        assert con.execute("SELECT COUNT(*) c FROM magic_links").fetchone()["c"] == 1


def test_same_round_is_idempotent_but_next_round_renudges_only_missing(env):
    dms = env
    alerts.nudge_sweep("nudge1", NOW)
    assert alerts.nudge_sweep("nudge1", NOW) == 0
    assert len(dms) == 1
    # Eddie fills one metric between rounds; nudge2 lists only the other
    with dbm.get_db() as con:
        dbm.upsert_entry(con, 1, DUE, value_numeric=11.0, source="manual", user_id=2)
    assert alerts.nudge_sweep("nudge2", NOW) == 1
    assert "Acme Health" in dms[-1][1] and "Calls" not in dms[-1][1]


def test_kill_switches(env):
    dms = env
    with dbm.get_db() as con:
        dbm.set_setting(con, "alerts_enabled", "0")
    assert alerts.nudge_sweep("nudge1", NOW) == 0
    with dbm.get_db() as con:
        dbm.set_setting(con, "alerts_enabled", "1")
        dbm.set_setting(con, "nudges_enabled", "0")
    assert alerts.nudge_sweep("nudge1", NOW) == 0
    with dbm.get_db() as con:
        dbm.set_setting(con, "nudges_enabled", "1")
        dbm.set_setting(con, "nudge_preset", "tue")
    assert alerts.nudge_sweep("nudge1", NOW) == 0   # preset excludes nudge1
    assert alerts.nudge_sweep("nudge2", NOW) == 1   # but allows nudge2
    with dbm.get_db() as con:
        dbm.set_setting(con, "public_base_url", "")
    assert alerts.nudge_sweep("nudge1", NOW) == 0   # no base URL -> no broken links
    assert dms and len(dms) == 1


def test_demo_mode_does_not_touch_nudges(env):
    dms = env
    with dbm.get_db() as con:
        dbm.set_setting(con, "display_demo_data", "1")
    assert alerts.nudge_sweep("nudge1", NOW) == 1   # real DB drives nudges
    assert "Calls" in dms[0][1]
