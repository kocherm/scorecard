"""Slack transport for two-way check-ins: the verified Events API endpoint.
The channel-agnostic reply handling (grammar, prompt pinning, saving,
confirmation text) lives in app/replies.py and is shared with the Telegram
and Twilio transports in app/inbound.py.

Always the REAL database: a DM reply is a real business act, whatever the
demo-data display toggle says.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from . import alerts, db as dbm
from .replies import build_reply_response, parse_reply  # noqa: F401 (re-export)

log = logging.getLogger("scorecard.slack")

router = APIRouter()

# Slack retries events it thinks failed; remember recently handled event ids
# so a retry can't double-write or double-confirm. Same in-memory pattern as
# the login throttle: fine for a single-process deployment.
_seen_events: dict[str, float] = {}
_SEEN_TTL = 3600.0


def verify_signature(secret: str, body: bytes, timestamp: str, signature: str) -> bool:
    """Slack request signing: v0 HMAC-SHA256 over 'v0:{ts}:{body}', and the
    timestamp must be within 5 minutes (replay protection)."""
    if not secret or not timestamp or not signature:
        return False
    try:
        if abs(time.time() - float(timestamp)) > 300:
            return False
    except ValueError:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/slack/events")
async def slack_events(request: Request, background: BackgroundTasks):
    body = await request.body()
    with dbm.get_db() as con:
        secret = dbm.get_setting(con, "slack_signing_secret") or ""
    if not verify_signature(secret, body,
                            request.headers.get("X-Slack-Request-Timestamp", ""),
                            request.headers.get("X-Slack-Signature", "")):
        raise HTTPException(403, "Bad Slack signature")
    payload = json.loads(body)

    if payload.get("type") == "url_verification":  # Slack's setup handshake
        return {"challenge": payload.get("challenge", "")}

    event_id = payload.get("event_id") or ""
    now_mono = time.monotonic()
    for k, t0 in list(_seen_events.items()):
        if now_mono - t0 > _SEEN_TTL:
            _seen_events.pop(k, None)
    if event_id and event_id in _seen_events:
        return {"ok": True}
    if event_id:
        _seen_events[event_id] = now_mono

    event = payload.get("event") or {}
    if (payload.get("type") != "event_callback"
            or event.get("type") != "message"
            or event.get("channel_type") != "im"
            or event.get("bot_id")        # our own confirmations echo back
            or event.get("subtype")):     # edits, joins, attachments-only
        return {"ok": True}

    # Ack within Slack's 3s window; the write + confirmation DM (10s HTTP
    # timeout worst case) run after the response.
    background.add_task(handle_dm, event.get("user", ""), event.get("text", ""))
    return {"ok": True}


def handle_dm(slack_user: str, text: str) -> None:
    """Match the Slack user, delegate to the shared reply core, DM the result."""
    if not slack_user:
        return
    with dbm.get_db() as con:
        _, bot, _ = alerts._slack_conf(con)
        if not bot:
            return
        u = con.execute(
            "SELECT * FROM users WHERE slack_member_id = ? AND is_active = 1",
            (slack_user,)).fetchone()
        if u is None:
            alerts.post_dm(bot, slack_user,
                           "This Slack account is not linked to a scorecard user. "
                           "Ask an admin to set your Slack member ID on the Users page.")
            return
        resp = build_reply_response(con, u, text, source="slack",
                                    now=datetime.now(timezone.utc))
        if resp:
            alerts.post_dm(bot, slack_user, resp)
