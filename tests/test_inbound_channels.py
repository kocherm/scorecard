"""Telegram and Twilio inbound webhooks: auth gates, user matching, typed
replies writing entries with the channel's source tag, and self-service
linking hints for unknown senders."""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import channels
from app import db as dbm
from app import weeks as wk
from app.auth import hash_password
from app.inbound import twilio_signature_valid

DUE = wk.last_closed_week(datetime.now(timezone.utc)).isoformat()
TG_SECRET = "tg-webhook-secret"
TWILIO_TOKEN = "twi-secret"
BASE = "https://score.example.com"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(dbm, "DB_PATH", str(tmp_path / "t.db"))
    from app.main import app

    with dbm.get_db() as con:
        dbm.init_db(con)
        con.execute("INSERT INTO sections (id, name, sort_order) VALUES (1,'S',0)")
        for uid, name, channel, addr in ((1, "Tia", "telegram", "424242"),
                                         (2, "Wanda", "whatsapp", "+15551234567"),
                                         (3, "Stan", "sms", "+15557654321")):
            con.execute(
                """INSERT INTO users (id, email, password_hash, display_name, role,
                                      notify_channel, notify_address)
                   VALUES (?,?,?,?,'editor',?,?)""",
                (uid, f"u{uid}@x.co", hash_password("pw-pw-pw-pw"), name, channel, addr))
        con.execute("""INSERT INTO metrics (id, section_id, name, metric_type, rollup,
                                            start_week, dri_user_id)
                       VALUES (1, 1, 'Calls', 'numeric', 'sum', '2026-01-05', 1)""")
        y, q = wk.quarter_of(wk.parse_week(DUE))
        con.execute("""INSERT INTO targets (metric_id, year, quarter, baseline_value,
                                            stretch_value) VALUES (1,?,?,10,12)""", (y, q))
        for uid in (1, 2, 3):
            con.execute("""INSERT INTO slack_prompts (user_id, week_start, metric_ids)
                           VALUES (?,?,?)""", (uid, DUE, json.dumps([1])))
        dbm.set_setting(con, "telegram_bot_token", "tg-token")
        dbm.set_setting(con, "telegram_webhook_secret", TG_SECRET)
        dbm.set_setting(con, "twilio_auth_token", TWILIO_TOKEN)
        dbm.set_setting(con, "public_base_url", BASE)

    tg_sent = []
    monkeypatch.setattr(channels, "send_telegram",
                        lambda token, chat, text: tg_sent.append((chat, text)) or True)
    yield TestClient(app), tg_sent


def tg_post(client, chat_id, text, secret=TG_SECRET):
    return client.post("/telegram/webhook",
                       json={"message": {"chat": {"id": chat_id}, "text": text,
                                         "from": {"is_bot": False}}},
                       headers={"X-Telegram-Bot-Api-Secret-Token": secret})


def twilio_post(client, params, token=TWILIO_TOKEN):
    payload = BASE + "/twilio/webhook" + "".join(k + params[k] for k in sorted(params))
    sig = base64.b64encode(
        hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()).decode()
    return client.post("/twilio/webhook", data=params,
                       headers={"X-Twilio-Signature": sig})


# ---------------- telegram

def test_telegram_reply_writes_entry_and_confirms(env):
    client, tg_sent = env
    r = tg_post(client, 424242, "1: 12")
    assert r.status_code == 200
    with dbm.get_db() as con:
        e = con.execute("SELECT * FROM entries WHERE metric_id=1").fetchone()
    assert (e["value_numeric"], e["source"], e["entered_by_user_id"]) == (12.0, "telegram", 1)
    chat, text = tg_sent[-1]
    assert chat == "424242" and "Calls = 12 (green)" in text


def test_telegram_bad_secret_403(env):
    client, _ = env
    assert tg_post(client, 424242, "1: 12", secret="wrong").status_code == 403


def test_telegram_unknown_chat_gets_linking_hint(env):
    client, tg_sent = env
    tg_post(client, 777777, "hello")
    chat, text = tg_sent[-1]
    assert chat == "777777" and "chat ID" in text and "777777" in text
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0


# ---------------- twilio

def test_whatsapp_reply_writes_entry_and_answers_in_twiml(env):
    client, _ = env
    r = twilio_post(client, {"From": "whatsapp:+15551234567", "Body": "1: 11"})
    assert r.status_code == 200
    assert "Recorded" in r.text and "<Message>" in r.text
    with dbm.get_db() as con:
        e = con.execute("SELECT * FROM entries WHERE metric_id=1").fetchone()
    assert (e["value_numeric"], e["source"], e["entered_by_user_id"]) == (11.0, "whatsapp", 2)


def test_sms_reply_matches_by_normalized_phone(env):
    client, _ = env
    r = twilio_post(client, {"From": "+1 555 765 4321", "Body": "1: 13"})
    assert r.status_code == 200
    with dbm.get_db() as con:
        e = con.execute("SELECT * FROM entries WHERE metric_id=1").fetchone()
    assert (e["source"], e["entered_by_user_id"]) == ("sms", 3)


def test_twilio_bad_signature_403(env):
    client, _ = env
    r = client.post("/twilio/webhook", data={"From": "+15551234567", "Body": "1: 9"},
                    headers={"X-Twilio-Signature": "nope"})
    assert r.status_code == 403
    with dbm.get_db() as con:
        assert con.execute("SELECT COUNT(*) c FROM entries").fetchone()["c"] == 0


def test_twilio_unknown_number_gets_polite_twiml(env):
    client, _ = env
    r = twilio_post(client, {"From": "+19998887777", "Body": "1: 9"})
    assert r.status_code == 200 and "not linked" in r.text


def test_twilio_signature_helper_roundtrip():
    params = {"From": "+15551234567", "Body": "1: 9"}
    url = BASE + "/twilio/webhook"
    payload = url + "".join(k + params[k] for k in sorted(params))
    sig = base64.b64encode(
        hmac.new(b"tok", payload.encode(), hashlib.sha1).digest()).decode()
    assert twilio_signature_valid("tok", url, params, sig)
    assert not twilio_signature_valid("tok", url, params, "bad")
