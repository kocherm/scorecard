"""Slack alerts: the Tuesday week-closed summary, the Tuesday red-escalation
ladder, the Wednesday stale roll-up, and the check-in nudge DMs that ask DRIs
for their missing numbers.

Volume is a design constraint here, not a detail. #scorecard gets COUNTS - at
most two top-level messages a week, each with its per-metric detail in a
thread - and the DM gets the TO-DOS, batched one message per person. Nothing a
single named person can fix alone goes to the channel until it has been asked
for privately first, and then only inside an aggregate. Fourteen top-level
posts on a Wednesday morning is a wall nobody reads, which is an alert that
does not alert; the same fourteen facts as one line plus a thread is the same
information at a glance.

Idempotent via alerts_sent (once per metric+week+type) and slack_threads (one
parent message per week+kind); every sweep is safe to re-run.

Network calls never run inside a write transaction. A claim is committed in
its own short transaction, the message goes out with nothing held, and the
result is written back in another short transaction - a failed send releases
its claim so the next run retries it. Holding SQLite's write lock across a
10-second Slack timeout is how a logged-in page (which writes
sessions.last_seen_at, busy_timeout 5s) ends up as a "database is locked" 500.
"""
from __future__ import annotations

import functools
import json
import logging
import sqlite3
import threading
from collections import Counter
from datetime import date, datetime, time as dt_time, timedelta, timezone

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

# The first red is one person's homework and stays in their DM. Weeks 2 and 3
# are the ones the team has to arrange something about, so those reach the
# channel. Spending #scorecard's attention on the most common, least serious
# rung is how a channel gets muted before the serious rungs ever arrive.
CHANNEL_FROM_LEVEL = 2

# grid.Row.dri_name is "-" for a metric nobody owns, which reads as a missing
# value rather than a finding. In a roll-up it is the finding.
UNOWNED = "unassigned"


ICON_PATH = "/static/icon-512.png"
# Slack refuses icon_url without this; an app installed before the icon shipped
# has chat:write but not this, which is why _post_message can fall back.
CUSTOMIZE_SCOPE = "chat:write.customize"
_SCOPE_ERRORS = {"missing_scope", "invalid_scope"}


def bot_icon_url(con: sqlite3.Connection) -> str | None:
    """Absolute URL of the Scorecard mark for chat.postMessage's icon_url.

    Slack has no API for an app's own icon (that stays a one-time upload in the
    app config), so per-message icon_url is the only half of the avatar the app
    can set for itself. Absolute because Slack fetches it, not the browser -
    hence no public base URL, no icon, same setting the nudge links need."""
    base = (dbm.get_setting(con, "public_base_url") or "").rstrip("/")
    return f"{base}{ICON_PATH}" if base else None


def post_channel(webhook_url: str, text: str) -> bool:
    # No icon here on purpose: incoming webhooks post as the app and ignore
    # icon_url unless they are a legacy custom integration.
    try:
        r = httpx.post(webhook_url, json={"text": text}, timeout=10)
        return r.status_code == 200
    except httpx.HTTPError as e:
        log.warning("slack webhook failed: %s", e)
        return False


def _post_message(bot_token: str, payload: dict, what: str) -> dict | None:
    """chat.postMessage, retried once without the icon on a scope error.

    Returns Slack's response body - whose "ts" is the whole reason a thread is
    possible - or None if the message did not go out.

    The avatar is cosmetic and the message is not. An instance whose Slack app
    predates the icon has no chat:write.customize, and without this fallback
    every nudge would come back missing_scope and die in a log line - the same
    silent-Slack-failure shape CLAUDE.md already warns about, reintroduced for
    the sake of a picture."""
    try:
        r = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {bot_token}"},
            json=payload,
            timeout=10,
        )
        body = r.json()
        if r.status_code == 200 and body.get("ok"):
            return body
        if body.get("error") in _SCOPE_ERRORS and "icon_url" in payload:
            log.warning("slack %s: no %s scope, resending without the Scorecard "
                        "icon - reinstall the Slack app to fix the avatar",
                        what, CUSTOMIZE_SCOPE)
            return _post_message(
                bot_token, {k: v for k, v in payload.items() if k != "icon_url"}, what)
        log.warning("slack %s failed: %s", what, r.text[:200])
        return None
    except (httpx.HTTPError, ValueError) as e:
        log.warning("slack %s failed: %s", what, e)
        return None


def post_channel_bot(bot_token: str, channel_id: str, text: str,
                     *, icon: str | None = None,
                     thread_ts: str | None = None) -> bool:
    payload = {"channel": channel_id, "text": text}
    if icon:
        payload["icon_url"] = icon
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return _post_message(bot_token, payload, "channel post") is not None


def post_channel_parent(bot_token: str, channel_id: str, text: str,
                        *, icon: str | None = None) -> str | None:
    """Post and hand back the message's ts - the address later replies thread
    under. Separate from post_channel_bot because only the parent of a thread
    needs its own address back; everything else just needs to know it went."""
    payload = {"channel": channel_id, "text": text}
    if icon:
        payload["icon_url"] = icon
    body = _post_message(bot_token, payload, "channel post")
    return body.get("ts") if body else None


def post_dm(bot_token: str, member_id: str, text: str, *, unfurl: bool = True,
            icon: str | None = None) -> bool:
    payload = {"channel": member_id, "text": text,
               "unfurl_links": unfurl, "unfurl_media": unfurl}
    if icon:
        payload["icon_url"] = icon
    return _post_message(bot_token, payload, "DM") is not None


def alerts_enabled(con: sqlite3.Connection) -> bool:
    """Master switch. Ships OFF; an admin flips it in Settings when ready."""
    return dbm.get_setting(con, "alerts_enabled", "0") == "1"


def _slack_conf(con: sqlite3.Connection) -> tuple[str | None, str | None, str | None]:
    return (dbm.get_setting(con, "slack_webhook_url"),
            dbm.get_setting(con, "slack_bot_token"),
            dbm.get_setting(con, "slack_channel_id"))


def _base_url(con: sqlite3.Connection) -> str:
    return (dbm.get_setting(con, "public_base_url") or "").rstrip("/")


def _record_run(con: sqlite3.Connection, kind: str, outcome: str,
                detail: str, sent: int = 0) -> int:
    """Log one sweep run - including the early returns that used to vanish.

    A sweep that stopped because a switch was off, or because the public base
    URL was empty, looks exactly like one that never fired: same silence, same
    empty Slack channel. The scheduler has nobody to tell, so it tells the DB
    and Admin > Setup & status reads it back. Returns `sent` so a skip path can
    `return _record_run(...)` and keep the "number of alerts sent" contract."""
    con.execute(
        "INSERT INTO sweep_runs (kind, outcome, detail, sent_count) VALUES (?,?,?,?)",
        (kind, outcome, detail, sent))
    return sent


def _claim(con: sqlite3.Connection, metric_id: int, week: date,
           alert_type: str) -> bool:
    """Take the once-only right to alert on this metric+week+type. False means
    somebody already has it, so this run says nothing about that metric."""
    cur = con.execute(
        "INSERT OR IGNORE INTO alerts_sent (metric_id, week_start, alert_type) "
        "VALUES (?,?,?)", (metric_id, week.isoformat(), alert_type))
    return cur.rowcount > 0


def _release(con: sqlite3.Connection, metric_id: int, week: date,
             alert_type: str) -> None:
    """Give a claim back because its message did not go out. Without this a
    Slack outage on Tuesday morning swallowed that week's alerts for good:
    the ledger said "sent" and every re-run believed it."""
    con.execute(
        "DELETE FROM alerts_sent WHERE metric_id=? AND week_start=? "
        "AND alert_type=?", (metric_id, week.isoformat(), alert_type))


def _commit(con: sqlite3.Connection) -> None:
    """End any open write transaction. Call before every network call: the
    claim must be durable before the send (so a concurrent run cannot send it
    too), and the write lock must not be held while Slack takes 10 seconds to
    time out."""
    if con.in_transaction:
        con.commit()


# ------------------------------------------------------------ channel posts
def _thread_ts(con: sqlite3.Connection, week: date, kind: str) -> str | None:
    """The parent message this week's `kind` roll-up hangs off, or None if it
    has not been posted yet. "" is a real answer meaning "posted, but on a
    channel that cannot thread" - so callers test `is not None`, never truth."""
    r = con.execute(
        "SELECT thread_ts FROM slack_threads WHERE week_start=? AND kind=?",
        (week.isoformat(), kind)).fetchone()
    return r["thread_ts"] if r else None


def _remember_thread(con: sqlite3.Connection, week: date, kind: str,
                     ts: str) -> None:
    con.execute("INSERT OR REPLACE INTO slack_threads (week_start, kind, thread_ts) "
                "VALUES (?,?,?)", (week.isoformat(), kind, ts))


def post_rollup(con: sqlite3.Connection, week: date, kind: str, headline: str,
                detail: list[str]) -> bool:
    """The channel's whole share of one sweep: a headline readable at a glance,
    with the per-metric detail hung underneath it.

    Detail becomes THREAD replies when the instance posts with a bot token, so
    #scorecard shows one line. An incoming webhook cannot thread - no ts comes
    back from it and no thread_ts goes out - so there the detail is folded into
    the same message instead. Either way the channel gets exactly one message,
    which is the point: the fix must not be contingent on anyone reconfiguring
    Slack first.

    The parent's ts is remembered per (week, kind), so a re-run - a restart
    mid-sweep, a metric that goes stale later the same day - appends to the
    existing thread instead of announcing the week a second time."""
    webhook, bot, channel_id = _slack_conf(con)
    can_thread = bool(bot and channel_id)
    if not (webhook or can_thread):
        return False
    _commit(con)  # nothing held across the network calls below
    icon = bot_icon_url(con)
    parent = _thread_ts(con, week, kind)
    if parent is not None:  # already announced: append, never re-announce
        if parent and can_thread and detail:
            return post_channel_bot(bot, channel_id, "\n".join(detail),
                                    icon=icon, thread_ts=parent)
        return False
    if webhook:  # webhook wins when both are set (readiness says so too)
        if not post_channel(webhook, "\n\n".join([headline, *detail])):
            return False
        _remember_thread(con, week, kind, "")
        _commit(con)
        return True
    ts = post_channel_parent(bot, channel_id, headline, icon=icon)
    if not ts:
        return False
    _remember_thread(con, week, kind, ts)
    _commit(con)
    for block in detail:
        post_channel_bot(bot, channel_id, block, icon=icon, thread_ts=ts)
    return True


def reply_under(con: sqlite3.Connection, week: date, kind: str,
                text: str) -> bool:
    """Hang a later message off an existing roll-up. Falls back to a top-level
    post when there is no thread to use: a thread is a nicety, and an
    escalation that silently went nowhere is not a tidier channel."""
    webhook, bot, channel_id = _slack_conf(con)
    _commit(con)
    # Same precedence as post_rollup, and for a sharper reason: with both set
    # the roll-up went wherever the webhook points, so replying through the bot
    # would put the escalation in a different channel from the summary it is
    # supposed to be about.
    if webhook:
        return post_channel(webhook, text)
    if bot and channel_id:
        return post_channel_bot(bot, channel_id, text, icon=bot_icon_url(con),
                                thread_ts=_thread_ts(con, week, kind) or None)
    return False


# ---------------------------------------------------------------- direct DMs
def send_direct(con: sqlite3.Connection, u: sqlite3.Row, text: str) -> bool:
    """Deliver a message over the user's chosen channel (Slack first-class,
    everything else via app.channels)."""
    if channels.user_channel(u) == "slack":
        _, bot, _ = _slack_conf(con)
        return bool(bot and u["slack_member_id"]
                    and post_dm(bot, u["slack_member_id"], text, unfurl=False,
                                icon=bot_icon_url(con)))
    return channels.send(con, u, text)


def compose_and_send_nudge(con: sqlite3.Connection, u: sqlite3.Row,
                           base: str, now: datetime,
                           *, overdue: bool = False) -> bool:
    """Message one user their missing due-week numbers over their channel.
    Returns False if nothing is missing or the send failed. The sweeps do not
    call this: they compose, commit, then send (see the module docstring)."""
    text = compose_nudge(con, u, base, now, overdue=overdue)
    return bool(text) and send_direct(con, u, text)


def compose_nudge(con: sqlite3.Connection, u: sqlite3.Row, base: str,
                  now: datetime, *, overdue: bool = False) -> str | None:
    """The check-in message for one user, or None if nothing is missing:
    numbered list with targets and a check-in link, plus the reply format on
    two-way channels (Slack/Telegram/Twilio). Two-way sends pin the numbering
    in slack_prompts so replies can never resolve against a shifted list.
    Teams/Google Chat post to a shared channel, so the text leads with the
    owner's name and is link-only.

    The magic link (a 7-day login) goes only where channels.deliver_secret
    says the message reaches one person. A Teams/Google Chat webhook posts to
    the whole space, so there the link is plain /checkin and they sign in -
    otherwise anyone in the space could click in as the owner for a week.

    Writes (prompt pin, magic link) but does not send; callers commit before
    the network call.

    `overdue` is Wednesday's wording: the same list, but the deadline has gone
    and the roll-up is about to name them. Sending the third ask in the second
    ask's words is how an escalation stops reading like one."""
    missing = entry_ops.missing_due_metrics(con, u["id"], now)
    if not missing:
        return None
    week = wk.last_closed_week(now)
    two_way = channels.user_channel(u) in channels.TWO_WAY
    if two_way:
        con.execute(
            """INSERT INTO slack_prompts (user_id, week_start, metric_ids, sent_at)
               VALUES (?,?,?,datetime('now'))
               ON CONFLICT(user_id) DO UPDATE SET week_start = excluded.week_start,
                 metric_ids = excluded.metric_ids, sent_at = excluded.sent_at""",
            (u["id"], week.isoformat(), json.dumps([m["id"] for m in missing])))
    url = (f"{base}/checkin?t={create_magic_link(con, u['id'])}"
           if channels.deliver_secret(con, u) else f"{base}/checkin")
    cta = channels.link(channels.user_channel(u), url, "Update your numbers here")
    when = f"the week of {week.strftime('%b %-d')} ({wk.quarter_label(week)})"
    # Link first: tapping through is the path that works for everyone, on any
    # metric type, with the targets visible. The typed reply is the shortcut
    # for people who would rather not leave the thread.
    if overdue:
        lead = (f"Scorecard: your numbers for {when} are overdue - they were "
                "due Monday and the board is showing them gray."
                if two_way else
                f"{u['display_name']} - scorecard numbers for {when} are "
                "overdue (due Monday).")
    else:
        lead = (f"Scorecard check-in: your numbers for {when} are missing."
                if two_way else
                f"{u['display_name']} - scorecard numbers for {when} are missing.")
    lines = [lead, "", cta, "", "Still missing:"]
    for i, m in enumerate(missing, 1):
        lines.append(f"{i}. {m['name']}{entry_ops.target_hint(con, m, week)}")
    if two_way:
        example = ", ".join(
            f"{i}: {'G' if m['metric_type'] == 'status' else ('yes' if m['metric_type'] == 'binary' else '12')}"
            for i, m in enumerate(missing[:2], 1))
        lines += ["", f'Or, you can reply here like "{example}" '
                      "and I will record them."]
    return "\n".join(lines)


def _dm_owners(con: sqlite3.Connection, user_ids: set[int], now: datetime,
               *, overdue: bool = False) -> tuple[int, int, int]:
    """One DM per PERSON, never per metric. Seven missing numbers is one
    message with seven lines - the same ask, without the pile-up that made the
    ask easy to scroll past.

    Delivery reuses compose_nudge, so the message carries the check-in link,
    the targets and the typed-reply shortcut, and it goes over whichever
    channel that user chose rather than over Slack alone. Returns (sent,
    failed, unreachable) - sent counts only messages that went out."""
    base = _base_url(con)
    sent = failed = unreachable = 0
    for uid in sorted(user_ids):
        u = con.execute("SELECT * FROM users WHERE id=? AND is_active=1",
                        (uid,)).fetchone()
        if u is None:
            continue
        if not channels.ready(con, u) or not base:
            unreachable += 1
            continue
        text = compose_nudge(con, u, base, now, overdue=overdue)
        if not text:
            continue
        _commit(con)
        if send_direct(con, u, text):
            sent += 1
        else:
            failed += 1
    return sent, failed, unreachable


# ------------------------------------------------------- week-closed summary
def _owed_by(rows: list[sqlite3.Row]) -> str:
    """"Dana 7, Sam 3, unassigned 1" - the line that makes a roll-up scannable.
    Who owes how many is the only thing anyone reads a missing-numbers post
    for; the metric names are detail, and detail belongs in the thread."""
    counts = Counter(r["dri_name"] or UNOWNED for r in rows)
    ranked = sorted(counts.items(),
                    key=lambda kv: (-kv[1], kv[0] == UNOWNED, kv[0]))
    return ", ".join(f"{name} {n}" for name, n in ranked)


def summary_sweep(now: datetime | None = None) -> int:
    """Tuesday 08:00: the one message #scorecard gets about the closed week.

    Posted after Monday-EOD rather than Monday morning, because on Monday the
    week's numbers are not in yet - a summary that is nearly all "no number
    yet" teaches people the summary is not worth opening. Counts in the
    headline, names in the thread, and that thread is also where the morning's
    red escalations land. Returns 1 if it posted."""
    now = now or datetime.now(timezone.utc)
    with dbm.get_db() as con:
        if not alerts_enabled(con):
            return _record_run(con, "summary", "skipped",
                               "Slack alerts are off (master switch).")
        week = wk.last_closed_week(now)
        label = wk.quarter_label(week)
        if _thread_ts(con, week, "summary") is not None:
            return _record_run(con, "summary", "nothing",
                               f"{label} was already summarised in the channel.")
        vm = gridm.build_grid(con, now)
        s = vm.summary
        reds, missing = [], []
        for section in vm.sections:
            for row in section.rows:
                owner = row.dri_name if row.dri_user_id else UNOWNED
                if row.last_state == sc.CellState.RED.value:
                    streak = (f", week {row.red_streak} in a row"
                              if row.red_streak > 1 else "")
                    reds.append(f"- {row.name} ({owner}){streak}")
                elif row.last_state in (sc.CellState.STALE.value,
                                        sc.CellState.PENDING.value):
                    missing.append(f"- {row.name} ({owner})")
        counts = [f"{s.green} green", f"{s.yellow} yellow", f"{s.red} red"]
        if missing:
            counts.append(f"{len(missing)} with no number yet")
        headline = (f"Scorecard - {label} (week of {week.strftime('%b %-d')}) "
                    f"is closed.\n{', '.join(counts)}.")
        base = _base_url(con)
        if base:
            headline += "\n" + channels.link("slack", f"{base}/", "Open the board")
        detail = []
        if reds:
            detail.append(f"Red in {label}:\n" + "\n".join(reds))
        if missing:
            # No claim about DMs here: this sweep does not send them and
            # cannot see whether nudges are switched on.
            detail.append(f"No number for {label} yet:\n" + "\n".join(missing)
                          + "\nStill blank on Wednesday and it posts here again, "
                            "with names.")
        if not post_rollup(con, week, "summary", headline, detail):
            return _record_run(con, "summary", "skipped",
                               "No Slack channel configured (webhook, or bot "
                               "token plus channel ID), so nothing was posted.")
        return _record_run(con, "summary", "sent",
                           f"Posted the {label} summary: {', '.join(counts)}.", 1)


# ------------------------------------------------------------- stale roll-up
def stale_sweep(now: datetime | None = None) -> int:
    """Wednesday 08:00: one channel roll-up naming who owes what, and one
    batched DM per person. Returns the number of metrics newly flagged.

    Both halves used to be per-metric - a channel post AND a DM for every
    missing number - so a bad week put fourteen messages in #scorecard and
    seven in one person's DMs. The information is identical; the wall was the
    bug."""
    now = now or datetime.now(timezone.utc)
    with dbm.get_db() as con:
        if not alerts_enabled(con):
            return _record_run(con, "stale", "skipped",
                               "Slack alerts are off (master switch).")
        week = wk.last_closed_week(now)
        label = wk.quarter_label(week)
        if now < wk.stale_at(week):
            return _record_run(con, "stale", "skipped",
                               f"{label} is not late yet - the grace period runs "
                               "to Wednesday 8:00 AM.")
        # Hidden sections are off the board, /checkin and the nudges
        # (entry_ops.missing_due_metrics), so they are off the chase too:
        # naming someone in the channel for a number nobody ever asked them
        # for privately breaks "private, private, then public".
        rows = con.execute(
            """SELECT m.*, u.display_name AS dri_name, u.slack_member_id
               FROM metrics m
               JOIN sections s ON s.id = m.section_id
               LEFT JOIN users u ON u.id = m.dri_user_id
               WHERE m.archived_at IS NULL AND s.is_enabled = 1
                 AND m.start_week <= ?""",
            (week.isoformat(),),
        ).fetchall()
        missing = [m for m in rows if not con.execute(
            "SELECT 1 FROM entries WHERE metric_id=? AND week_start=?",
            (m["id"], week.isoformat())).fetchone()]
        # Claim first, post once. The ledger is still per metric, so a re-run
        # reports only what is newly late instead of repeating the list.
        fresh = [m for m in missing if _claim(con, m["id"], week, "stale")]
        _commit(con)
        n = len(fresh)
        if not n:
            detail = (f"All {len(rows)} live metrics had {label} entered."
                      if not missing else
                      f"{len(missing)} still missing for {label}, all already alerted.")
            return _record_run(con, "stale", "nothing", detail)

        sent, failed, unreachable = _dm_owners(
            con, {m["dri_user_id"] for m in fresh if m["dri_user_id"]}, now,
            overdue=True)
        base = _base_url(con)
        cta = ("\n" + channels.link("slack", f"{base}/checkin", "Fill them in")
               if base else "")
        headline = (f"Scorecard - {label}: {len(missing)} of {len(rows)} metrics "
                    f"still have no number, past Monday's deadline."
                    f"\nOwed by: {_owed_by(missing)}{cta}")
        announced = _thread_ts(con, week, "stale") is not None
        posted = post_rollup(
            con, week, "stale", headline,
            [f"Still missing ({label}):\n"
             + "\n".join(f"- {m['name']} ({m['dri_name'] or UNOWNED})"
                         for m in fresh)])

        run = [f"Flagged {n} of {len(missing)} metrics missing {label}.",
               f"DM'd {sent} {'owner' if sent == 1 else 'owners'}."]
        if failed:
            run.append(f"{failed} {'DM' if failed == 1 else 'DMs'} failed to "
                       "send (see the server log).")
        if unreachable:
            run.append(f"{unreachable} owed numbers but had no message channel "
                       "configured (or no public base URL), so they were skipped.")
        if not posted:
            webhook, bot, channel_id = _slack_conf(con)
            if not (webhook or (bot and channel_id)):
                run.append("Nothing was posted to the channel - none is configured.")
            elif announced:
                run.append("The channel already had this week's roll-up and "
                           "cannot thread an addition to it.")
            else:
                run.append("The channel post failed (see the server log).")
        return _record_run(con, "stale", "sent", " ".join(run), n)


# --------------------------------------------------------- escalation ladder
def red_sweep(now: datetime | None = None) -> int:
    """Tuesday 08:05: the escalation ladder for last week's reds.

    Week 1 is a DM and nothing else. A first red is one person's homework -
    bring a 1-3-1 - and announcing every one of those in #scorecard spends the
    channel's whole attention budget on the most common and least serious rung,
    so by the time a week-3 red arrives the channel is muted. Weeks 2 and 3
    need something arranged between people, so those post, threaded under that
    morning's week-closed summary. Returns alerts sent."""
    now = now or datetime.now(timezone.utc)
    with dbm.get_db() as con:
        if not alerts_enabled(con):
            return _record_run(con, "red", "skipped",
                               "Slack alerts are off (master switch).")
        vm = gridm.build_grid(con, now)
        week = wk.last_closed_week(now)
        label = wk.quarter_label(week)
        base = _base_url(con)
        # Only a week that is itself RED escalates. The streak skips STALE and
        # PENDING weeks (you cannot dodge the ladder by not entering), which
        # is right for the count on the board but wrong as a trigger: on a
        # Tuesday when last week has no number yet, the reds before it would
        # re-claim a rung for THIS week and re-post an escalation the channel
        # already had - then skip the real rung once the number arrived.
        red_rows = [row for section in vm.sections for row in section.rows
                    if row.last_state == sc.CellState.RED.value
                    and row.red_streak >= 1]
        reds = len(red_rows)

        # 1. Claim, in one short transaction, committed before any send.
        fresh = []
        for row in red_rows:
            level = min(row.red_streak, 3)
            if _claim(con, row.metric_id, week, RED_ALERT_TYPES[level]):
                fresh.append((row, level))
        _commit(con)
        if not fresh:
            detail = (f"No metric was red in {label}." if not reds else
                      f"{reds} red in {label}, all already escalated.")
            return _record_run(con, "red", "nothing", detail)

        # 2. Send, with no transaction open. One DM per PERSON listing every
        # red they own, over their own channel - but only a private one: week
        # 1 is DM-only by design, and a Teams/Google Chat webhook is a shared
        # space, so posting there would be the public naming the ladder
        # saves for week 2.
        delivered: set[int] = set()
        by_owner: dict[int, list] = {}
        for row, level in fresh:
            if row.dri_user_id:
                by_owner.setdefault(row.dri_user_id, []).append((row, level))
        dm_sent = dm_failed = unreachable = 0
        for uid in sorted(by_owner):
            u = con.execute("SELECT * FROM users WHERE id=? AND is_active=1",
                            (uid,)).fetchone()
            if u is None or not channels.deliver_secret(con, u):
                unreachable += 1
                continue
            if send_direct(con, u, _red_dm_text(u, by_owner[uid], week, label,
                                                base)):
                dm_sent += 1
                delivered.update(row.metric_id for row, _ in by_owner[uid])
            else:
                dm_failed += 1
        escalated = [(row, level) for row, level in fresh
                     if level >= CHANNEL_FROM_LEVEL]
        channel_ok = None
        if escalated:
            channel_ok = reply_under(
                con, week, "summary", f"Escalating in {label}:\n" + "\n".join(
                    f"- \"{row.name}\" is RED for week {row.red_streak} in a "
                    f"row. DRI: {row.dri_name if row.dri_user_id else UNOWNED}. "
                    f"{LADDER_TEXT[level]}" for row, level in escalated))
            if channel_ok:
                delivered.update(row.metric_id for row, _ in escalated)

        # 3. Record: a claim nothing delivered is handed back, so the next run
        # retries it instead of the ledger swearing it was sent.
        undelivered = [(row, level) for row, level in fresh
                       if row.metric_id not in delivered]
        for row, level in undelivered:
            _release(con, row.metric_id, week, RED_ALERT_TYPES[level])
        n = len(fresh) - len(undelivered)
        run = [f"Escalated {n} of {reds} red metrics ({label}).",
               f"DM'd {dm_sent} {'owner' if dm_sent == 1 else 'owners'}."]
        webhook, bot, channel_id = _slack_conf(con)
        channel_failed = (channel_ok is False
                          and bool(webhook or (bot and channel_id)))
        if channel_ok:
            run.append(f"{len(escalated)} at week 2+ reached the channel.")
        elif channel_failed:
            run.append(f"The channel reply for {len(escalated)} week-2+ "
                       "reds failed to post (see the server log).")
        elif channel_ok is False:
            run.append(f"{len(escalated)} week-2+ reds had no channel to go "
                       "to - none is configured.")
        if dm_failed:
            run.append(f"{dm_failed} {'DM' if dm_failed == 1 else 'DMs'} "
                       "failed to send (see the server log).")
        if unreachable:
            run.append(f"{unreachable} {'owner has' if unreachable == 1 else 'owners have'}"
                       " no private message channel configured.")
        if undelivered:
            run.append(f"{len(undelivered)} not delivered anywhere; "
                       "they will be retried on the next run.")
        # Nothing delivered is never "nothing to do": it either failed (error,
        # which the boot catch-up retries) or had nowhere to go (skipped).
        outcome = ("sent" if n else
                   "error" if dm_failed or channel_failed else "skipped")
        return _record_run(con, "red", outcome, " ".join(run), n)


def _red_dm_text(u: sqlite3.Row, items: list, week: date, label: str,
                 base: str) -> str:
    """Every red one person owns, in one message: each metric with its streak,
    its rung of the ladder, and a link to file the 1-3-1. Links are absolute
    and built only from public_base_url (never a request's Host); with no base
    URL they are left out rather than sent broken."""
    ch = channels.user_channel(u)
    when = f"{label} (week of {week.strftime('%b %-d')})"
    lines = [f"Scorecard: a metric you own was red in {when}."
             if len(items) == 1 else
             f"Scorecard: {len(items)} metrics you own were red in {when}."]
    for row, level in items:
        lines += ["", f"\"{row.name}\" - week {row.red_streak} in a row. "
                      f"{LADDER_TEXT[level]}"]
        if base:
            lines.append(channels.link(
                ch, f"{base}/131/{row.metric_id}/{week.isoformat()}",
                "File the 1-3-1"))
    return "\n".join(lines)


# ---------------------------------------------------------------- nudges
_NUDGE_KINDS_BY_PRESET = {"mon_tue": ("nudge1", "nudge2"),
                          "mon": ("nudge1",), "tue": ("nudge2",)}


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
            return _record_run(con, kind, "skipped",
                               "Slack alerts are off (master switch).")
        if dbm.get_setting(con, "nudges_enabled", "0") != "1":
            return _record_run(con, kind, "skipped", "Check-in nudges are off.")
        preset = dbm.get_setting(con, "nudge_preset", "mon_tue") or "mon_tue"
        if kind not in _NUDGE_KINDS_BY_PRESET.get(preset, ("nudge1", "nudge2")):
            return _record_run(con, kind, "skipped",
                               f"Not in the schedule (set to {preset}).")
        base = _base_url(con)
        if not base:
            log.warning("nudge sweep skipped: public base URL not set in Settings")
            return _record_run(con, kind, "skipped",
                               "No public base URL set, so every check-in link "
                               "would be broken. Nothing was sent.")
        week = wk.last_closed_week(now)
        users = con.execute("SELECT * FROM users WHERE is_active = 1").fetchall()
        owed, unreachable, failed = 0, 0, 0
        for u in users:
            if not channels.ready(con, u):
                if entry_ops.missing_due_metrics(con, u["id"], now):
                    owed += 1
                    unreachable += 1
                continue
            missing = entry_ops.missing_due_metrics(con, u["id"], now)
            owed += 1 if missing else 0
            fresh = [m for m in missing if _claim(con, m["id"], week, kind)]
            if not fresh:
                continue  # everything still missing was already nudged this round
            text = compose_nudge(con, u, base, now)
            _commit(con)  # claims durable, write lock free, THEN the network
            if text and send_direct(con, u, text):
                n += 1
                continue
            # Not sent: hand the claims back so the next run still nudges.
            for m in fresh:
                _release(con, m["id"], week, kind)
            _commit(con)
            failed += 1
        if n:
            detail = f"Messaged {n} {'person' if n == 1 else 'people'}."
        elif not owed:
            detail = "Nobody was missing a number."
        elif failed:
            detail = "Nobody was messaged."
        else:
            detail = (f"{owed} still owed numbers; everyone reachable had "
                      "already been nudged this round.")
        if failed:
            detail += (f" {failed} {'message' if failed == 1 else 'messages'} "
                       "failed to send (see the server log) and will be "
                       "retried on the next run.")
        if unreachable:
            detail += (f" {unreachable} owed numbers but had no channel "
                       "configured, so they were skipped.")
        outcome = "sent" if n else ("error" if failed else "nothing")
        return _record_run(con, kind, outcome, detail, n)


# ------------------------------------------------------------- scheduling
# Each sweep's slot in the business week: (weekday Mon=0, hour, minute). These
# mirror the CronTriggers in main.py and exist so a restart can tell whether
# this week's run was missed.
SCHEDULE = {"summary": (1, 8, 0), "red": (1, 8, 5), "stale": (2, 8, 0)}
# Summary before red: red replies into the summary's thread.
CATCH_UP_ORDER = ("summary", "red", "stale")

# One sweep at a time in this process. APScheduler's pool would otherwise let
# the boot catch-up and a cron job (or two cron jobs) interleave - and the
# summary's "already posted?" check is a read followed, seconds later, by a
# write.
_SWEEP_LOCK = threading.Lock()


def guarded(kind: str, fn):
    """Wrap a sweep for the scheduler: never two at once, and a crash leaves a
    row in sweep_runs (outcome 'error') written on its OWN connection - the
    sweep's connection rolls back with the exception and would take a record
    written through it along. Only the exception's type is stored: the message
    can carry a webhook URL, and the status page is not the place for it."""
    @functools.wraps(fn)
    def run(*args, **kwargs):
        with _SWEEP_LOCK:
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001 - logged and recorded
                log.exception("%s sweep crashed", kind)
                try:
                    with dbm.get_db() as con:
                        _record_run(con, kind, "error",
                                    f"Crashed with {type(e).__name__} - see "
                                    "the server log.")
                except Exception:  # noqa: BLE001
                    log.exception("could not record the %s sweep crash", kind)
                return 0
    return run


def scheduled_at(kind: str, now: datetime) -> datetime:
    """When `kind` is scheduled in the business week containing `now`."""
    local = now.astimezone(wk.BUSINESS_TZ)
    monday = local.date() - timedelta(days=local.weekday())
    day, hour, minute = SCHEDULE[kind]
    return datetime.combine(monday + timedelta(days=day), dt_time(hour, minute),
                            tzinfo=wk.BUSINESS_TZ)


def _ran_since(con: sqlite3.Connection, kind: str, since: datetime) -> bool:
    """Did `kind` record a run at or after `since`? A crash ('error') does not
    count - that is exactly the run a restart should make up for."""
    stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return con.execute(
        "SELECT 1 FROM sweep_runs WHERE kind=? AND outcome != 'error' "
        "AND ran_at >= ?", (kind, stamp)).fetchone() is not None


def missed_sweeps(now: datetime) -> list[str]:
    """The sweeps whose slot this week has passed with no run recorded, in the
    order they must run."""
    with dbm.get_db() as con:
        return [kind for kind in CATCH_UP_ORDER
                if now >= scheduled_at(kind, now)
                and not _ran_since(con, kind, scheduled_at(kind, now))]


def catch_up_sweeps(now: datetime | None = None) -> list[str]:
    """Boot-time make-up for sweeps a restart skipped. APScheduler computes a
    fresh job's next run from the moment it is added, so a container that was
    down over Tuesday 08:00 never runs that week's summary at all - misfire
    grace only covers a scheduler that was up but late. The sweeps are
    idempotent (alerts_sent, slack_threads), so running one that did in fact
    go out costs nothing. Returns the kinds it ran."""
    sweeps = {"summary": summary_sweep, "red": red_sweep, "stale": stale_sweep}
    due = missed_sweeps(now or datetime.now(timezone.utc))
    for kind in due:
        log.info("catching up the %s sweep missed while the app was down", kind)
        guarded(kind, sweeps[kind])(now)
    return due
