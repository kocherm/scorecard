"""Inbound webhooks for the non-Slack two-way channels.

- POST /telegram/webhook - Telegram bot updates, authenticated by the secret
  token we registered with setWebhook (X-Telegram-Bot-Api-Secret-Token).
- POST /twilio/webhook - Twilio incoming SMS/WhatsApp, authenticated by
  X-Twilio-Signature (HMAC-SHA1 over the public URL + sorted form params,
  keyed by the auth token). The reply rides back inline as TwiML, so no
  second API call is needed.

Both delegate to app/replies.py - the same deterministic grammar and pinned
prompt list as Slack, no AI - and always hit the REAL database.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException, Request, Response

from . import channels, db as dbm
from .replies import build_reply_response

log = logging.getLogger("scorecard.inbound")

router = APIRouter()


# ---------------------------------------------------------------- telegram
@router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    with dbm.get_db() as con:
        secret = dbm.get_setting(con, "telegram_webhook_secret") or ""
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not secret or not hmac.compare_digest(secret, header):
        raise HTTPException(403, "Bad Telegram secret token")
    payload = await request.json()
    msg = payload.get("message") or {}
    chat_id = str((msg.get("chat") or {}).get("id") or "")
    text = msg.get("text") or ""
    if not chat_id or not text or msg.get("from", {}).get("is_bot"):
        return {"ok": True}
    with dbm.get_db() as con:
        token = dbm.get_setting(con, "telegram_bot_token") or ""
        if not token:
            return {"ok": True}
        u = con.execute(
            """SELECT * FROM users WHERE notify_channel = 'telegram'
               AND notify_address = ? AND is_active = 1""", (chat_id,)).fetchone()
        if u is None:
            # Self-service linking: the bot tells people the ID to hand their
            # admin, so "message the bot once" is the whole setup for a user.
            channels.send_telegram(
                token, chat_id,
                "This chat is not linked to a scorecard user. Give your admin "
                f"this chat ID to link it: {chat_id}")
            return {"ok": True}
        resp = build_reply_response(con, u, text, source="telegram",
                                    now=datetime.now(timezone.utc))
    if resp:
        channels.send_telegram(token, chat_id, resp)
    return {"ok": True}


# ---------------------------------------------------------------- twilio
def twilio_signature_valid(auth_token: str, url: str, params: dict[str, str],
                           signature: str) -> bool:
    payload = url + "".join(k + params[k] for k in sorted(params))
    expected = base64.b64encode(
        hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    ).decode()
    return hmac.compare_digest(expected, signature)


def _twiml(text: str) -> Response:
    xml = ('<?xml version="1.0" encoding="UTF-8"?><Response>'
           f"<Message>{escape(text)}</Message></Response>")
    return Response(content=xml, media_type="text/xml")


@router.post("/twilio/webhook")
async def twilio_webhook(request: Request):
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    with dbm.get_db() as con:
        auth_token = dbm.get_setting(con, "twilio_auth_token") or ""
        base = (dbm.get_setting(con, "public_base_url") or "").rstrip("/")
    if not auth_token or not base or not twilio_signature_valid(
            auth_token, base + "/twilio/webhook", params,
            request.headers.get("X-Twilio-Signature", "")):
        raise HTTPException(403, "Bad Twilio signature")
    sender = params.get("From", "")
    text = params.get("Body", "")
    source = "whatsapp" if sender.startswith("whatsapp:") else "sms"
    with dbm.get_db() as con:
        u = None
        for row in con.execute(
                """SELECT * FROM users WHERE notify_channel IN ('sms','whatsapp')
                   AND notify_address IS NOT NULL AND is_active = 1"""):
            if channels.norm_phone(row["notify_address"]) == channels.norm_phone(sender):
                u = row
                break
        if u is None:
            return _twiml("This number is not linked to a scorecard user - "
                          "ask your admin to add it on the Users page.")
        resp = build_reply_response(con, u, text, source=source,
                                    now=datetime.now(timezone.utc))
    return _twiml(resp or "OK")
