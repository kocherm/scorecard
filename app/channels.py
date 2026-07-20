"""Optional notification channels beyond Slack: Microsoft Teams, Google Chat,
Twilio SMS/WhatsApp, and Telegram. Outbound nudges for all four; typed replies
(the same deterministic grammar as Slack) for Telegram and Twilio, whose
inbound webhooks live in app/inbound.py. Teams and Google Chat incoming
webhooks can only post to a shared channel/space - no per-user DMs without a
full bot - so they are notify-only: the message names the owner and carries
the magic link.

Configured under Settings > More channels; each user's channel and address
(phone number / Telegram chat ID) on the Users page. Slack stays first-class
in alerts.py; this module never imports alerts (alerts imports this).
"""
from __future__ import annotations

import logging
import re
import sqlite3

import httpx

from . import db as dbm

log = logging.getLogger("scorecard.channels")

CHANNELS = ("slack", "teams", "gchat", "sms", "whatsapp", "telegram")
TWO_WAY = ("slack", "sms", "whatsapp", "telegram")  # typed replies supported

LABELS = {"slack": "Slack", "teams": "Teams", "gchat": "Google Chat",
          "sms": "SMS", "whatsapp": "WhatsApp", "telegram": "Telegram"}


def user_channel(u: sqlite3.Row) -> str:
    return u["notify_channel"] or "slack"


def norm_phone(s: str | None) -> str:
    """'whatsapp:+1 (555) 123-4567' -> '+15551234567' for matching."""
    return re.sub(r"[^\d+]", "", (s or "").replace("whatsapp:", ""))


def ready(con: sqlite3.Connection, u: sqlite3.Row) -> bool:
    """Can this user's chosen channel actually deliver a nudge right now?"""
    ch = user_channel(u)
    if ch == "slack":
        return bool(dbm.get_setting(con, "slack_bot_token") and u["slack_member_id"])
    if ch == "teams":
        return bool(dbm.get_setting(con, "teams_webhook_url"))
    if ch == "gchat":
        return bool(dbm.get_setting(con, "gchat_webhook_url"))
    if ch in ("sms", "whatsapp"):
        return bool(dbm.get_setting(con, "twilio_account_sid")
                    and dbm.get_setting(con, "twilio_auth_token")
                    and dbm.get_setting(con, "twilio_from")
                    and u["notify_address"])
    if ch == "telegram":
        return bool(dbm.get_setting(con, "telegram_bot_token") and u["notify_address"])
    return False


def send_telegram(bot_token: str, chat_id: str, text: str) -> bool:
    try:
        r = httpx.post(f"https://api.telegram.org/bot{bot_token}/sendMessage",
                       json={"chat_id": chat_id, "text": text,
                             "disable_web_page_preview": True}, timeout=10)
        ok = r.status_code == 200 and r.json().get("ok", False)
        if not ok:
            log.warning("telegram send failed: %s", r.text[:200])
        return ok
    except httpx.HTTPError as e:
        log.warning("telegram send failed: %s", e)
        return False


def _send_webhook(url: str, text: str, label: str) -> bool:
    try:
        r = httpx.post(url, json={"text": text}, timeout=10)
        if r.status_code >= 300:
            log.warning("%s webhook failed: %s %s", label, r.status_code, r.text[:200])
        return r.status_code < 300
    except httpx.HTTPError as e:
        log.warning("%s webhook failed: %s", label, e)
        return False


def _send_twilio(con: sqlite3.Connection, channel: str, to: str, text: str) -> bool:
    sid = dbm.get_setting(con, "twilio_account_sid") or ""
    tok = dbm.get_setting(con, "twilio_auth_token") or ""
    frm = (dbm.get_setting(con, "twilio_from") or "").strip()
    to = to.strip()
    if channel == "whatsapp":
        if not frm.startswith("whatsapp:"):
            frm = "whatsapp:" + frm
        if not to.startswith("whatsapp:"):
            to = "whatsapp:" + to
    else:
        frm = frm.removeprefix("whatsapp:")
        to = to.removeprefix("whatsapp:")
    try:
        r = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data={"From": frm, "To": to, "Body": text}, auth=(sid, tok), timeout=10)
        if r.status_code >= 300:
            log.warning("twilio send failed: %s %s", r.status_code, r.text[:200])
        return r.status_code < 300
    except httpx.HTTPError as e:
        log.warning("twilio send failed: %s", e)
        return False


def send(con: sqlite3.Connection, u: sqlite3.Row, text: str) -> bool:
    """Deliver `text` to a user over their non-Slack channel. (Slack delivery
    stays in alerts.post_dm; alerts.send_direct dispatches between the two.)"""
    ch = user_channel(u)
    if ch == "teams":
        return _send_webhook(dbm.get_setting(con, "teams_webhook_url") or "",
                             text, "teams")
    if ch == "gchat":
        return _send_webhook(dbm.get_setting(con, "gchat_webhook_url") or "",
                             text, "google chat")
    if ch in ("sms", "whatsapp"):
        return _send_twilio(con, ch, u["notify_address"] or "", text)
    if ch == "telegram":
        return send_telegram(dbm.get_setting(con, "telegram_bot_token") or "",
                             u["notify_address"] or "", text)
    return False
