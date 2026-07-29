"""POST /slack/events: signature gate, challenge handshake, and DM replies
becoming scorecard entries with a confirmation DM - all against the real DB."""
import hashlib
import hmac
import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db as dbm
from app import slack as slackm
from app import weeks as wk
from app.auth import hash_password

SECRET = "test-signing-secret"
DUE = wk.last_closed_week(datetime.now(timezone.utc)).isoformat()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    slackm._seen_events.clear()
    from app.main import app

    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'Sales',0)")
        con.execute(
            """INSERT INTO users (id, email, password_hash, display_name, role,
                                  slack_member_id, must_change_password)
               VALUES (2,'ed@x.co',?,'Eddie','editor','U123',0)""",
            (hash_password("pw-pw-pw-pw"),))
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                            start_week, dri_user_id)
                       VALUES (1, 1, 'Calls', 'numeric', 'sum', '2026-01-05', 2)""")
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type,
                                            start_week, dri_user_id)
                       VALUES (2, 1, 'Acme Health', 'status', '2026-01-05', 2)""")
        y, q = wk.quarter_of(wk.parse_week(DUE))
        con.execute("""INSERT INTO targets (metric_id, year, quarter, baseline_value,
                                            stretch_value) VALUES (1,?,?,10,12)""", (y, q))
        con.execute("""INSERT INTO slack_prompts (user_id, week_start, metric_ids)
                       VALUES (2, ?, ?)""", (DUE, json.dumps([1, 2])))
        dbm.set_setting(con, "slack_bot_token", "xoxb-test")
        dbm.set_setting(con, "slack_signing_secret", SECRET)

    dms = []
    monkeypatch.setattr("app.alerts.post_dm",
                        lambda bot, member, text, **kw: dms.append((member, text)) or True)
    yield TestClient(app), dms


def signed(client, payload, secret=SECRET, ts=None):
    body = json.dumps(payload).encode()
    ts = ts or str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body,
                           hashlib.sha256).hexdigest()
    return client.post("/slack/events", content=body,
                       headers={"X-Slack-Request-Timestamp": ts,
                                "X-Slack-Signature": sig,
                                "Content-Type": "application/json"})


def dm_event(text, user="U123", event_id="Ev001"):
    return {"type": "event_callback", "event_id": event_id,
            "event": {"type": "message", "channel_type": "im",
                      "user": user, "text": text, "channel": "D123"}}


def test_url_verification_challenge(env):
    client, _ = env
    r = signed(client, {"type": "url_verification", "challenge": "abc123"})
    assert r.status_code == 200 and r.json()["challenge"] == "abc123"


def test_bad_signature_and_stale_timestamp_are_403(env):
    client, _ = env
    assert signed(client, dm_event("1: 5"), secret="wrong").status_code == 403
    old = str(int(time.time()) - 600)
    assert signed(client, dm_event("1: 5"), ts=old).status_code == 403


def test_reply_writes_entries_and_confirms_with_colors(env):
    client, dms = env
    r = signed(client, dm_event("1: 12, 2: g"))
    assert r.status_code == 200
    with dbm.get_db() as con:
        e1 = con.execute("SELECT * FROM entries WHERE metric_id=1").fetchone()
        e2 = con.execute("SELECT * FROM entries WHERE metric_id=2").fetchone()
    assert (e1["value_numeric"], e1["source"], e1["entered_by_user_id"]) == (12.0, "slack", 2)
    assert e2["value_status"] == "G"
    member, text = dms[-1]
    assert member == "U123" and "Recorded" in text
    assert "Calls = 12 (green)" in text and "Acme Health = G (green)" in text


def test_partial_reply_reports_problems(env):
    client, dms = env
    signed(client, dm_event("1: twelve, 2: G, 9: 4"))
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) c FROM entries WHERE metric_id=1")\
                  .fetchone()["c"] == 0
        assert con.execute("SELECT value_status FROM entries WHERE metric_id=2")\
                  .fetchone()["value_status"] == "G"
    text = dms[-1][1]
    assert "Recorded" in text and "Could not record" in text and "no such item" in text


def test_unknown_slack_user_gets_not_linked_dm_and_no_write(env):
    client, dms = env
    signed(client, dm_event("1: 5", user="U999"))
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0
    assert "not linked" in dms[-1][1]


def test_help_and_garbage_get_the_numbered_list(env):
    client, dms = env
    signed(client, dm_event("help"))
    assert "1. Calls" in dms[-1][1] and "2. Acme Health" in dms[-1][1]
    signed(client, dm_event("no idea what to do", event_id="Ev002"))
    assert "1. Calls" in dms[-1][1]
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0


def test_bot_echo_and_subtype_events_ignored(env):
    client, dms = env
    ev = dm_event("1: 5")
    ev["event"]["bot_id"] = "B01"
    signed(client, ev)
    ev2 = dm_event("1: 5", event_id="Ev003")
    ev2["event"]["subtype"] = "message_changed"
    signed(client, ev2)
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0
    assert dms == []


def test_duplicate_event_id_is_processed_once(env):
    client, dms = env
    signed(client, dm_event("1: 12", event_id="EvDup"))
    signed(client, dm_event("1: 12", event_id="EvDup"))
    with dbm.get_db() as con:
        n = con.execute("SELECT COUNT(*) c FROM entry_audit WHERE metric_id=1")\
               .fetchone()["c"]
    assert n == 1 and len(dms) == 1


def test_stale_prompt_is_refused(env):
    client, dms = env
    with dbm.get_db() as con:
        con.execute("UPDATE slack_prompts SET week_start = '2026-01-05'")
    signed(client, dm_event("1: 12"))
    assert "open check-in" in dms[-1][1]
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0
