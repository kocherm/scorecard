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
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from datetime import date, datetime, timezone

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
        return True
    ts = post_channel_parent(bot, channel_id, headline, icon=icon)
    if not ts:
        return False
    _remember_thread(con, week, kind, ts)
    for block in detail:
        post_channel_bot(bot, channel_id, block, icon=icon, thread_ts=ts)
    return True


def reply_under(con: sqlite3.Connection, week: date, kind: str,
                text: str) -> bool:
    """Hang a later message off an existing roll-up. Falls back to a top-level
    post when there is no thread to use: a thread is a nicety, and an
    escalation that silently went nowhere is not a tidier channel."""
    webhook, bot, channel_id = _slack_conf(con)
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
    """Message one user their missing due-week numbers over their channel:
    numbered list with targets and a magic link, plus the reply format on
    two-way channels (Slack/Telegram/Twilio). Two-way sends pin the numbering
    in slack_prompts so replies can never resolve against a shifted list.
    Teams/Google Chat post to a shared channel, so the text leads with the
    owner's name and is link-only. Returns False if nothing is missing.

    `overdue` is Wednesday's wording: the same list, but the deadline has gone
    and the roll-up is about to name them. Sending the third ask in the second
    ask's words is how an escalation stops reading like one."""
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
    url = f"{base}/checkin?t={create_magic_link(con, u['id'])}"
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
    return send_direct(con, u, "\n".join(lines))


def _dm_owners(con: sqlite3.Connection, user_ids: set[int], now: datetime,
               *, overdue: bool = False) -> tuple[int, int]:
    """One DM per PERSON, never per metric. Seven missing numbers is one
    message with seven lines - the same ask, without the pile-up that made the
    ask easy to scroll past.

    Delivery reuses compose_and_send_nudge, so the message carries the magic
    link, the targets and the typed-reply shortcut, and it goes over whichever
    channel that user chose rather than over Slack alone. Returns (sent,
    unreachable)."""
    base = _base_url(con)
    sent = unreachable = 0
    for uid in sorted(user_ids):
        u = con.execute("SELECT * FROM users WHERE id=? AND is_active=1",
                        (uid,)).fetchone()
        if u is None:
            continue
        if not channels.ready(con, u) or not base:
            unreachable += 1
            continue
        sent += 1 if compose_and_send_nudge(con, u, base, now,
                                            overdue=overdue) else 0
    return sent, unreachable


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
        rows = con.execute(
            """SELECT m.*, u.display_name AS dri_name, u.slack_member_id
               FROM metrics m LEFT JOIN users u ON u.id = m.dri_user_id
               WHERE m.archived_at IS NULL AND m.start_week <= ?""",
            (week.isoformat(),),
        ).fetchall()
        missing = [m for m in rows if not con.execute(
            "SELECT 1 FROM entries WHERE metric_id=? AND week_start=?",
            (m["id"], week.isoformat())).fetchone()]
        # Claim first, post once. The ledger is still per metric, so a re-run
        # reports only what is newly late instead of repeating the list.
        fresh = [m for m in missing if _claim(con, m["id"], week, "stale")]
        n = len(fresh)
        if not n:
            detail = (f"All {len(rows)} live metrics had {label} entered."
                      if not missing else
                      f"{len(missing)} still missing for {label}, all already alerted.")
            return _record_run(con, "stale", "nothing", detail)

        sent, unreachable = _dm_owners(
            con, {m["dri_user_id"] for m in fresh if m["dri_user_id"]}, now,
            overdue=True)
        base = _base_url(con)
        cta = ("\n" + channels.link("slack", f"{base}/checkin", "Fill them in")
               if base else "")
        headline = (f"Scorecard - {label}: {len(missing)} of {len(rows)} metrics "
                    f"still have no number, past Monday's deadline."
                    f"\nOwed by: {_owed_by(missing)}{cta}")
        posted = post_rollup(
            con, week, "stale", headline,
            [f"Still missing ({label}):\n"
             + "\n".join(f"- {m['name']} ({m['dri_name'] or UNOWNED})"
                         for m in fresh)])

        run = [f"Flagged {n} of {len(missing)} metrics missing {label}.",
               f"DM'd {sent} {'owner' if sent == 1 else 'owners'}."]
        if unreachable:
            run.append(f"{unreachable} owed numbers but had no message channel "
                       "configured (or no public base URL), so they were skipped.")
        if not posted:
            run.append("Nothing was posted to the channel - none is configured.")
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
        _, bot, _ = _slack_conf(con)
        icon = bot_icon_url(con)
        dri_slack = {u["id"]: u["slack_member_id"]
                     for u in con.execute("SELECT id, slack_member_id FROM users")}
        reds = n = 0
        escalated: list[str] = []
        for section in vm.sections:
            for row in section.rows:
                if row.red_streak < 1:
                    continue
                reds += 1
                level = min(row.red_streak, 3)
                if not _claim(con, row.metric_id, week, RED_ALERT_TYPES[level]):
                    continue
                n += 1
                member = dri_slack.get(row.dri_user_id)
                if bot and member:
                    post_dm(bot, member,
                            f"\"{row.name}\" went red ({label}), week "
                            f"{row.red_streak} in a row. {LADDER_TEXT[level]}",
                            icon=icon)
                if level >= CHANNEL_FROM_LEVEL:
                    escalated.append(
                        f"- \"{row.name}\" is RED for week {row.red_streak} in a "
                        f"row. DRI: {row.dri_name}. {LADDER_TEXT[level]}")
        if escalated:
            reply_under(con, week, "summary",
                        f"Escalating in {label}:\n" + "\n".join(escalated))
        if not reds:
            detail = f"No metric was red in {label}."
        elif not n:
            detail = f"{reds} red in {label}, all already escalated."
        else:
            detail = (f"Escalated {n} of {reds} red metrics ({label}); "
                      f"{len(escalated)} reached the channel, the rest are "
                      "week-1 reds and stayed in DMs.")
        return _record_run(con, "red", "sent" if n else "nothing", detail, n)


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
        owed, unreachable = 0, 0
        for u in users:
            if not channels.ready(con, u):
                if entry_ops.missing_due_metrics(con, u["id"], now):
                    owed += 1
                    unreachable += 1
                continue
            missing = entry_ops.missing_due_metrics(con, u["id"], now)
            owed += 1 if missing else 0
            fresh = sum(1 for m in missing if _claim(con, m["id"], week, kind))
            if fresh == 0:
                continue  # everything still missing was already nudged this round
            if compose_and_send_nudge(con, u, base, now):
                n += 1
        if n:
            detail = f"Messaged {n} {'person' if n == 1 else 'people'}."
        elif not owed:
            detail = "Nobody was missing a number."
        else:
            detail = (f"{owed} still owed numbers; everyone reachable had "
                      "already been nudged this round.")
        if unreachable:
            detail += (f" {unreachable} owed numbers but had no channel "
                       "configured, so they were skipped.")
        return _record_run(con, kind, "sent" if n else "nothing", detail, n)
