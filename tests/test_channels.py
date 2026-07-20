"""Multi-channel nudges: per-user routing, payload shapes for each transport,
readiness skipping, and two-way vs link-only message composition."""
import json
from datetime import datetime, timezone

import httpx
import pytest

from app import alerts
from app import channels
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
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'S',0)")
        for uid, name, channel, addr in (
                (1, "Tia Telegram", "telegram", "424242"),
                (2, "Tom Teams", "teams", None),
                (3, "Wanda WhatsApp", "whatsapp", "+1 (555) 123-4567"),
                (4, "Sam SMSNoConfig", "sms", "+15550000000"),
                (5, "Gigi GChat", "gchat", None)):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role,
                                      notify_channel, notify_address)
                   VALUES (?,?,?,?,'editor',?,?)""",
                (uid, f"u{uid}@x.co", hash_password("pw-pw-pw-pw"), name, channel, addr))
        for mid, uid in ((1, 1), (2, 2), (3, 3), (4, 4), (5, 5)):
            con.execute(
                """INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                        start_week, dri_user_id)
                   VALUES (?, 1, ?, 'numeric', 'sum', '2026-01-05', ?)""",
                (mid, f"Metric {mid}", uid))
        dbm.set_setting(con, "alerts_enabled", "1")
        dbm.set_setting(con, "nudges_enabled", "1")
        dbm.set_setting(con, "public_base_url", "https://score.example.com")
        dbm.set_setting(con, "telegram_bot_token", "tg-token")
        dbm.set_setting(con, "teams_webhook_url", "https://teams.example.com/hook")
        dbm.set_setting(con, "gchat_webhook_url", "https://chat.googleapis.com/hook")
        dbm.set_setting(con, "twilio_account_sid", "AC123")
        dbm.set_setting(con, "twilio_auth_token", "twi-secret")
        dbm.set_setting(con, "twilio_from", "+15559990000")
        # Sam's channel is sms, but we break it by clearing his address below
        con.execute("UPDATE users SET notify_address = NULL WHERE id = 4")

    calls = []

    def fake_post(url, **kw):
        calls.append((url, kw))
        return httpx.Response(200, json={"ok": True},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    yield calls


def by_host(calls, frag):
    return [c for c in calls if frag in c[0]]


def test_sweep_routes_each_user_through_their_channel(env):
    calls = env
    sent = alerts.nudge_sweep("nudge1", NOW)
    assert sent == 4  # Sam has no address -> skipped

    tg = by_host(calls, "api.telegram.org")[0]
    assert tg[1]["json"]["chat_id"] == "424242"
    assert "Reply here like" in tg[1]["json"]["text"]      # two-way
    assert "https://score.example.com/checkin?t=" in tg[1]["json"]["text"]

    teams = by_host(calls, "teams.example.com")[0]
    text = teams[1]["json"]["text"]
    assert text.startswith("Tom Teams - ")                 # channel post names owner
    assert "Reply here like" not in text                   # link-only
    assert "Enter them here: https://score.example.com/checkin?t=" in text

    tw = by_host(calls, "api.twilio.com")[0]
    assert "AC123" in tw[0]
    assert tw[1]["auth"] == ("AC123", "twi-secret")
    assert tw[1]["data"]["From"] == "whatsapp:+15559990000"
    assert tw[1]["data"]["To"] == "whatsapp:+1 (555) 123-4567"
    assert "Reply here like" in tw[1]["data"]["Body"]

    gc = by_host(calls, "chat.googleapis.com")[0]
    assert gc[1]["json"]["text"].startswith("Gigi GChat - ")


def test_prompts_pinned_only_for_two_way_channels(env):
    alerts.nudge_sweep("nudge1", NOW)
    with dbm.get_db() as con:
        pinned = {r["user_id"] for r in con.execute("SELECT user_id FROM slack_prompts")}
    assert pinned == {1, 3}  # telegram + whatsapp; not teams/gchat


def test_unready_user_is_not_marked_nudged(env):
    alerts.nudge_sweep("nudge1", NOW)
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) c FROM alerts_sent WHERE metric_id=4")\
                  .fetchone()["c"] == 0
        # once his address is fixed, the same round still reaches him
        con.execute("UPDATE users SET notify_address = '+15550000000' WHERE id = 4")
    assert alerts.nudge_sweep("nudge1", NOW) == 1


def test_norm_phone():
    assert channels.norm_phone("whatsapp:+1 (555) 123-4567") == "+15551234567"
    assert channels.norm_phone("+15551234567") == "+15551234567"
    assert channels.norm_phone(None) == ""
