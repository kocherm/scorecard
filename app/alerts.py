"""Slack alerts: stale sweep (Wed 08:00 Chicago), red-escalation sweep
(Tue 08:00), and check-in nudge DMs (Mon 16:00 / Tue 09:00) that ask DRIs
for their missing numbers. Idempotent via alerts_sent; safe to re-run."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

import httpx

from . import channels
from . import db as dbm
from . import entry_ops
from . import grid as gridm
from . import scoring as sc
from . import weeks as wk
from .auth import create_magic_link

log = logging.getLogger("scorecard.alerts")

RED_ALERT_TYPES = {1: "red_week1", 2: "red_week2", 3: "red_week3"}

LADDER_TEXT = {
    1: "Week 1 red: bring a 1-3-1 (one problem, three options, one recommendation) to the weekly sync. File it on the scorecard.",
    2: "Week 2 red on the same metric: 15-minute 1:1 this week, outside the sync.",
    3: "Week 3+ red: structural conversation. Something about this number's ownership or approach needs to change.",
}


def post_channel(webhook_url: str, text: str) -> bool:
    try:
        r = httpx.post(webhook_url, json={"text": text}, timeout=10)
        return r.status_code == 200
    except httpx.HTTPError as e:
        log.warning("slack webhook failed: %s", e)
        return False


def post_channel_bot(bot_token: str, channel_id: str, text: str) -> bool:
    try:
        r = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {bot_token}"},
            json={"channel": channel_id, "text": text},
            timeout=10,
        )
        ok = r.status_code == 200 and r.json().get("ok", False)
        if not ok:
            log.warning("slack channel post failed: %s", r.text[:200])
        return ok
    except httpx.HTTPError as e:
        log.warning("slack channel post failed: %s", e)
        return False


def post_dm(bot_token: str, member_id: str, text: str, *, unfurl: bool = True) -> bool:
    try:
        r = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {bot_token}"},
            json={"channel": member_id, "text": text,
                  "unfurl_links": unfurl, "unfurl_media": unfurl},
            timeout=10,
        )
        ok = r.status_code == 200 and r.json().get("ok", False)
        if not ok:
            log.warning("slack DM failed: %s", r.text[:200])
        return ok
    except httpx.HTTPError as e:
        log.warning("slack DM failed: %s", e)
        return False


def alerts_enabled(con: sqlite3.Connection) -> bool:
    """Master switch. Ships OFF; an admin flips it in Settings when ready."""
    return dbm.get_setting(con, "alerts_enabled", "0") == "1"


def _slack_conf(con: sqlite3.Connection) -> tuple[str | None, str | None, str | None]:
    return (dbm.get_setting(con, "slack_webhook_url"),
            dbm.get_setting(con, "slack_bot_token"),
            dbm.get_setting(con, "slack_channel_id"))


def _record_and_send(con: sqlite3.Connection, metric_id: int, week_key: str,
                     alert_type: str, channel_text: str, dm_member: str | None,
                     dm_text: str | None) -> bool:
    cur = con.execute(
        "INSERT OR IGNORE INTO alerts_sent (metric_id, week_start, alert_type) VALUES (?,?,?)",
        (metric_id, week_key, alert_type),
    )
    if cur.rowcount == 0:
        return False  # already alerted
    webhook, bot, channel_id = _slack_conf(con)
    if webhook:
        post_channel(webhook, channel_text)
    elif bot and channel_id:
        post_channel_bot(bot, channel_id, channel_text)
    if bot and dm_member and dm_text:
        post_dm(bot, dm_member, dm_text)
    return True


def stale_sweep(now: datetime | None = None) -> int:
    """Flag every active metric missing last week's entry. Returns count."""
    now = now or datetime.now(timezone.utc)
    n = 0
    with dbm.get_db() as con:
        if not alerts_enabled(con):
            return 0
        week = wk.last_closed_week(now)
        if now < wk.stale_at(week):
            return 0  # grace not over yet (guard for manual runs)
        rows = con.execute(
            """SELECT m.*, u.display_name AS dri_name, u.slack_member_id
               FROM metrics m LEFT JOIN users u ON u.id = m.dri_user_id
               WHERE m.archived_at IS NULL AND m.start_week <= ?""",
            (week.isoformat(),),
        ).fetchall()
        for m in rows:
            e = con.execute(
                "SELECT 1 FROM entries WHERE metric_id=? AND week_start=?",
                (m["id"], week.isoformat()),
            ).fetchone()
            if e:
                continue
            dri = m["dri_name"] or "unassigned"
            label = wk.quarter_label(week)
            channel = (f"Scorecard: \"{m['name']}\" ({label}, due Monday EOD) has no entry. "
                       f"DRI: {dri}. The cell is gray on the TV until it's filled in.")
            dm = (f"Your scorecard metric \"{m['name']}\" is missing last week's number "
                  f"({label}). Two minutes: enter it at the scorecard and the gray goes away.")
            if _record_and_send(con, m["id"], week.isoformat(), "stale",
                                channel, m["slack_member_id"], dm):
                n += 1
    return n


# ---------------------------------------------------------------- nudges
_NUDGE_KINDS_BY_PRESET = {"mon_tue": ("nudge1", "nudge2"),
                          "mon": ("nudge1",), "tue": ("nudge2",)}


def send_direct(con: sqlite3.Connection, u: sqlite3.Row, text: str) -> bool:
    """Deliver a message over the user's chosen channel (Slack first-class,
    everything else via app.channels)."""
    if channels.user_channel(u) == "slack":
        _, bot, _ = _slack_conf(con)
        return bool(bot and u["slack_member_id"]
                    and post_dm(bot, u["slack_member_id"], text, unfurl=False))
    return channels.send(con, u, text)


def compose_and_send_nudge(con: sqlite3.Connection, u: sqlite3.Row,
                           base: str, now: datetime) -> bool:
    """Message one user their missing due-week numbers over their channel:
    numbered list with targets and a magic link, plus the reply format on
    two-way channels (Slack/Telegram/Twilio). Two-way sends pin the numbering
    in slack_prompts so replies can never resolve against a shifted list.
    Teams/Google Chat post to a shared channel, so the text leads with the
    owner's name and is link-only. Returns False if nothing is missing."""
    missing = entry_ops.missing_due_metrics(con, u["id"], now)
    if not missing:
        return False
    week = wk.last_closed_week(now)
    two_way = channels.user_channel(u) in channels.TWO_WAY
    if two_way:
        con.execute(
            """INSERT INTO slack_prompts (user_id, week_start, metric_ids, sent_at)
               VALUES (?,?,?,datetime('now'))
               ON CONFLICT(user_id) DO UPDATE SET week_start = excluded.week_start,
                 metric_ids = excluded.metric_ids, sent_at = excluded.sent_at""",
            (u["id"], week.isoformat(), json.dumps([m["id"] for m in missing])))
    link = f"{base}/checkin?t={create_magic_link(con, u['id'])}"
    when = f"the week of {week.strftime('%b %-d')} ({wk.quarter_label(week)})"
    lines = [f"Scorecard check-in: your numbers for {when} are missing:"
             if two_way else
             f"{u['display_name']} - scorecard numbers for {when} are missing:"]
    for i, m in enumerate(missing, 1):
        lines.append(f"{i}. {m['name']}{entry_ops.target_hint(con, m, week)}")
    if two_way:
        example = ", ".join(
            f"{i}: {'G' if m['metric_type'] == 'status' else ('yes' if m['metric_type'] == 'binary' else '12')}"
            for i, m in enumerate(missing[:2], 1))
        lines += ["", f'Reply here like "{example}" and I will record them.',
                  f"Or tap to enter them on your phone: {link}"]
    else:
        lines += ["", f"Enter them here: {link}"]
    return send_direct(con, u, "\n".join(lines))


def nudge_sweep(kind: str = "nudge1", now: datetime | None = None) -> int:
    """Message every DRI whose due-week numbers are missing, over each user's
    chosen channel; users whose channel is not configured are skipped (and not
    marked nudged). Idempotent per metric+week+kind via alerts_sent; a metric
    filled after nudge1 is not re-nudged by nudge2. Real DB always (demo mode
    is display-only)."""
    now = now or datetime.now(timezone.utc)
    n = 0
    with dbm.get_db() as con:
        if not alerts_enabled(con):
            return 0
        if dbm.get_setting(con, "nudges_enabled", "0") != "1":
            return 0
        preset = dbm.get_setting(con, "nudge_preset", "mon_tue") or "mon_tue"
        if kind not in _NUDGE_KINDS_BY_PRESET.get(preset, ("nudge1", "nudge2")):
            return 0
        base = (dbm.get_setting(con, "public_base_url") or "").rstrip("/")
        if not base:
            log.warning("nudge sweep skipped: public base URL not set in Settings")
            return 0
        week = wk.last_closed_week(now)
        users = con.execute("SELECT * FROM users WHERE is_active = 1").fetchall()
        for u in users:
            if not channels.ready(con, u):
                continue
            missing = entry_ops.missing_due_metrics(con, u["id"], now)
            fresh = 0
            for m in missing:
                cur = con.execute(
                    "INSERT OR IGNORE INTO alerts_sent (metric_id, week_start, alert_type) "
                    "VALUES (?,?,?)", (m["id"], week.isoformat(), kind))
                fresh += cur.rowcount
            if fresh == 0:
                continue  # everything still missing was already nudged this round
            if compose_and_send_nudge(con, u, base, now):
                n += 1
    return n


def red_sweep(now: datetime | None = None) -> int:
    """Escalation ladder for last week's reds. Returns alerts sent."""
    now = now or datetime.now(timezone.utc)
    n = 0
    with dbm.get_db() as con:
        if not alerts_enabled(con):
            return 0
        vm = gridm.build_grid(con, now)
        week = wk.last_closed_week(now)
        dri_slack = {u["id"]: u["slack_member_id"]
                     for u in con.execute("SELECT id, slack_member_id FROM users")}
        for section in vm.sections:
            for row in section.rows:
                if row.red_streak < 1:
                    continue
                level = min(row.red_streak, 3)
                alert_type = RED_ALERT_TYPES[level]
                label = wk.quarter_label(week)
                channel = (f"Scorecard: \"{row.name}\" is RED for week {row.red_streak} "
                           f"in a row ({label}). DRI: {row.dri_name}. {LADDER_TEXT[level]}")
                dm = (f"\"{row.name}\" went red ({label}), week {row.red_streak} in a row. "
                      f"{LADDER_TEXT[level]}")
                if _record_and_send(con, row.metric_id, week.isoformat(), alert_type,
                                    channel, dri_slack.get(row.dri_user_id), dm):
                    n += 1
    return n
