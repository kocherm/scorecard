"""Setup readiness: does this instance actually do what it is configured to do?

Every check here exists because of a real failure during setup, and they all
had the same shape - state was storable without being verifiable, and nothing
said so at the place you would look. A bot token for an entirely different
workspace. Member IDs from that other workspace, which still resolved through
Slack Connect and so looked correct. Two independent switches, either of which
means total silence. A public base URL whose absence made the nudge sweep
return before sending anything, after a log line nobody reads.

Two tiers, deliberately separated:

- **Local checks** read settings and the DB only. No network, so they are safe
  on every page load and cheap enough to drive the nav badge.
- **Network checks** call Slack and run on demand only (the Re-check button),
  never on page load. Putting them on page load would be a regression, not a
  feature: slow admin pages, Slack rate limits, and an admin UI that goes down
  when Slack does. Results are cached in the settings table under
  `verify_<key>` with a timestamp, so the page renders instantly and states how
  old its answer is instead of pretending to be live.

Checks derive from the same helpers the rest of the app uses - channels.ready,
entry_ops.missing_due_metrics, db.get_setting. A second opinion about "is this
configured" would only be a second thing to keep in sync.

Deliberately NOT reusing alerts.post_channel_bot / post_dm for verification:
they swallow the Slack error into a log line and return a bool, and here the
reason IS the answer.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

from . import alerts
from . import channels
from . import db as dbm
from . import weeks as wk

log = logging.getLogger("scorecard.readiness")

OK, WARN, BLOCKED, UNCHECKED = "ok", "warn", "blocked", "unchecked"

# state -> .chip modifier in scorecard.css, and the word inside the chip
CHIP = {OK: "on", WARN: "warn", BLOCKED: "blocked", UNCHECKED: "off"}
BADGE = {OK: "OK", WARN: "WARN", BLOCKED: "BLOCKED", UNCHECKED: "NOT RUN"}

NET_TIMEOUT = 8.0
SLACK_ID_RE = re.compile(r"^[UVW][A-Z0-9]{2,}$")


@dataclass(frozen=True)
class Action:
    """A POST button rendered next to a check (never a link: it writes)."""
    label: str
    post: str


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    state: str
    detail: str
    fix_url: Optional[str] = None
    fix_label: Optional[str] = None
    actions: tuple[Action, ...] = ()
    items: tuple[dict, ...] = ()   # sub-rows: per-person detail under the check

    @property
    def chip(self) -> str:
        return CHIP[self.state]

    @property
    def badge(self) -> str:
        return BADGE[self.state]


# ------------------------------------------------------------------ local tier
SETTINGS_URL = "/admin/settings"


def _live_metrics(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        """SELECT m.* FROM metrics m JOIN sections s ON s.id = m.section_id
           WHERE m.archived_at IS NULL AND s.is_enabled = 1""").fetchall()


def _nudge_recipients(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Active people who own at least one live metric - the only people a nudge
    is ever addressed to, and so the honest denominator for channel coverage."""
    return con.execute(
        """SELECT DISTINCT u.* FROM users u
           JOIN metrics m ON m.dri_user_id = u.id
           JOIN sections s ON s.id = m.section_id
           WHERE u.is_active = 1 AND m.archived_at IS NULL AND s.is_enabled = 1
           ORDER BY u.display_name""").fetchall()


def _check_people(con: sqlite3.Connection) -> Check:
    n = con.execute(
        "SELECT COUNT(*) c FROM users WHERE is_active = 1 AND role <> 'viewer'"
    ).fetchone()["c"]
    if not n:
        return Check("people", "People", BLOCKED,
                     "No active accounts that can enter numbers.",
                     "/admin/users", "Add users")
    return Check("people", "People", OK,
                 f"{n} active {'account' if n == 1 else 'accounts'} can enter numbers.",
                 "/admin/users", "Users")


def _check_metrics(con: sqlite3.Connection) -> Check:
    rows = _live_metrics(con)
    if not rows:
        return Check("metrics", "Metrics", BLOCKED,
                     "No live metrics, so every surface is empty.",
                     "/admin/metrics", "Add metrics")
    return Check("metrics", "Metrics", OK,
                 f"{len(rows)} live {'metric' if len(rows) == 1 else 'metrics'}.",
                 "/admin/metrics", "Sections & metrics")


def _check_dris(con: sqlite3.Connection) -> Check:
    rows = _live_metrics(con)
    orphans = [m for m in rows if m["dri_user_id"] is None]
    if not rows:
        return Check("dris", "Owners (DRI)", BLOCKED, "No metrics to own yet.",
                     "/admin/metrics", "Sections & metrics")
    if orphans:
        return Check("dris", "Owners (DRI)", WARN,
                     f"{len(orphans)} of {len(rows)} metrics "
                     f"{'has' if len(orphans) == 1 else 'have'} no DRI. Nobody is "
                     "nudged for them and nobody is named when they go red.",
                     "/admin/metrics", "Assign owners")
    return Check("dris", "Owners (DRI)", OK,
                 f"Every one of the {len(rows)} live metrics has a DRI.",
                 "/admin/metrics", "Sections & metrics")


def _check_slack_credentials(con: sqlite3.Connection) -> Check:
    bot = dbm.get_setting(con, "slack_bot_token") or ""
    hook = dbm.get_setting(con, "slack_webhook_url") or ""
    fix = (f"{SETTINGS_URL}#slack", "Slack settings")
    if not bot and not hook:
        return Check("slack_credentials", "Slack credentials", BLOCKED,
                     "Neither a bot token nor a webhook is set, so no Slack "
                     "message has anywhere to go.", *fix)
    if not bot:
        return Check("slack_credentials", "Slack credentials", WARN,
                     "Webhook only. Channel posts go through it; DMs and typed "
                     "replies need a bot token.", *fix)
    return Check("slack_credentials", "Slack credentials", OK,
                 "Bot token set. Re-check below to confirm which workspace it "
                 "belongs to.", *fix)


def _check_slack_channel(con: sqlite3.Connection) -> Check:
    """Channel posts prefer the webhook whenever one is set (alerts.py's
    _record_and_send), which makes that field quietly decisive - and unlike a
    bot token there is no Slack API that will tell you where a webhook points,
    so its shape is all we get to check."""
    bot = dbm.get_setting(con, "slack_bot_token") or ""
    hook = (dbm.get_setting(con, "slack_webhook_url") or "").strip()
    channel = dbm.get_setting(con, "slack_channel_id") or ""
    fix = (f"{SETTINGS_URL}#slack", "Slack settings")
    if hook and urlparse(hook).netloc != "hooks.slack.com":
        return Check("slack_channel", "Team alert channel", BLOCKED,
                     "The webhook URL is not a Slack incoming webhook (those "
                     "are on hooks.slack.com). Channel alerts are POSTed there "
                     "and go nowhere. Clear the field to fall back to the bot "
                     "and channel ID.", *fix)
    if hook and bot and channel:
        return Check("slack_channel", "Team alert channel", WARN,
                     "A webhook and a bot channel are both set. The webhook "
                     "wins, and nothing can verify where it points - clear it "
                     "to use the channel ID, which this page can check.", *fix)
    if hook:
        return Check("slack_channel", "Team alert channel", OK,
                     "Posting through the incoming webhook. Where that lands "
                     "cannot be verified from here; a bot token plus channel ID "
                     "can be.", *fix)
    if not bot:
        return Check("slack_channel", "Team alert channel", BLOCKED,
                     "No credentials to post with.", *fix)
    if not channel:
        return Check("slack_channel", "Team alert channel", WARN,
                     "No channel ID. Stale and red alerts still DM the DRI, but "
                     "nothing is posted where the team sees it.", *fix)
    return Check("slack_channel", "Team alert channel", OK,
                 f"Channel {channel}. Re-check below to confirm the bot is in it.",
                 *fix)


def _check_alerts_switch(con: sqlite3.Connection) -> Check:
    on = dbm.get_setting(con, "alerts_enabled", "0") == "1"
    return Check(
        "alerts_enabled", "Slack alerts (master switch)", OK if on else BLOCKED,
        "On. Stale and red sweeps deliver." if on else
        "Off. Every sweep returns before sending - stale, red and nudges alike.",
        f"{SETTINGS_URL}#slack", "Slack settings")


def _check_nudges_switch(con: sqlite3.Connection) -> Check:
    on = dbm.get_setting(con, "nudges_enabled", "0") == "1"
    master = dbm.get_setting(con, "alerts_enabled", "0") == "1"
    if on and not master:
        return Check("nudges_enabled", "Check-in nudges", BLOCKED,
                     "On, but the master switch above is off, so nothing is sent. "
                     "Both have to be on.", f"{SETTINGS_URL}#nudges", "Nudge settings")
    return Check("nudges_enabled", "Check-in nudges", OK if on else WARN,
                 "On. DRIs are messaged before they are late." if on else
                 "Off. People are told only after the Wednesday deadline, by the "
                 "stale sweep.", f"{SETTINGS_URL}#nudges", "Nudge settings")


def _check_public_base_url(con: sqlite3.Connection) -> Check:
    url = (dbm.get_setting(con, "public_base_url") or "").strip()
    nudges = dbm.get_setting(con, "nudges_enabled", "0") == "1"
    fix = (f"{SETTINGS_URL}#nudges", "Set the public URL")
    if url:
        return Check("public_base_url", "Public base URL", OK,
                     f"{url} - check-in links point here.", *fix)
    return Check("public_base_url", "Public base URL", BLOCKED if nudges else WARN,
                 "Not set. The nudge sweep runs on a schedule with no browser "
                 "request to infer an address from, so it stops before sending "
                 "anything." + ("" if nudges else " Nudges are off, so nothing is "
                                "lost yet."), *fix)


def _check_signing_secret(con: sqlite3.Connection) -> Check:
    secret = dbm.get_setting(con, "slack_signing_secret") or ""
    fix = (f"{SETTINGS_URL}#slack", "Slack settings")
    if secret:
        return Check("signing_secret", "Slack signing secret", OK,
                     "Set. Typed replies are verified and accepted.", *fix)
    return Check("signing_secret", "Slack signing secret", WARN,
                 "Not set, so typed replies are rejected unverified. The link in "
                 "each DM still works.", *fix)


def _check_channel_coverage(con: sqlite3.Connection) -> Check:
    people = _nudge_recipients(con)
    fix = ("/admin/users", "Users")
    can_match = bool(dbm.get_setting(con, "slack_bot_token"))
    actions = ((Action("Match Slack IDs from email", "/admin/status/match-ids"),)
               if can_match else ())
    if not people:
        return Check("channel_coverage", "Who can be reached", BLOCKED,
                     "No metric has an owner, so a nudge has nobody to go to.",
                     *fix)
    ready = {u["id"] for u in people if channels.ready(con, u)}
    items = tuple({"name": u["display_name"],
                   "state": OK if u["id"] in ready else BLOCKED,
                   "detail": (f"{channels.LABELS[channels.user_channel(u)]} - "
                              f"{_address_of(u) or 'no address set'}")}
                  for u in people)
    owners = f"{len(people)} metric {'owner' if len(people) == 1 else 'owners'}"
    if not ready:
        return Check("channel_coverage", "Who can be reached", BLOCKED,
                     f"Not one of the {owners} has a working channel. Every "
                     "nudge is skipped in silence.",
                     *fix, actions=actions, items=items)
    if len(ready) < len(people):
        return Check("channel_coverage", "Who can be reached", WARN,
                     f"{len(ready)} of the {owners} can receive a nudge. The "
                     "rest are silently skipped by the sweep.",
                     *fix, actions=actions, items=items)
    return Check("channel_coverage", "Who can be reached", OK,
                 f"All {owners} have a working channel.",
                 *fix, actions=actions, items=items)


def _check_reset_delivery(con: sqlite3.Connection) -> Check:
    """Who can reset their own password without an admin in the loop.

    Never BLOCKED: the admin temp-password path on Admin > Users always works,
    so this is about how much of the job an admin still has to do by hand. It
    counts every active user, not just metric owners - viewers sign in too -
    and only PRIVATE channels count, because a reset link in a shared Teams or
    Google Chat space is a link anyone on the team can use."""
    fix = ("/admin/users", "Users")
    people = con.execute(
        "SELECT * FROM users WHERE is_active = 1 ORDER BY display_name").fetchall()
    if not people:
        return Check("reset_delivery", "Self-serve password reset", WARN,
                     "No active users.", *fix)
    if not (dbm.get_setting(con, "public_base_url") or "").strip():
        return Check("reset_delivery", "Self-serve password reset", WARN,
                     "No public base URL, so a reset link has no address to "
                     "point at and none is sent. Everyone needs an admin.",
                     f"{SETTINGS_URL}#nudges", "Set the public URL")
    reachable = {u["id"] for u in people if channels.deliver_secret(con, u)}
    items = tuple({"name": u["display_name"],
                   "state": OK if u["id"] in reachable else WARN,
                   "detail": (f"{channels.LABELS[channels.user_channel(u)]}"
                              + ("" if u["id"] in reachable else
                                 " - shared channel, cannot carry a reset link"
                                 if channels.user_channel(u) not in channels.PRIVATE
                                 else " - not configured"))}
                  for u in people)
    n = f"{len(people)} active {'user' if len(people) == 1 else 'users'}"
    if not reachable:
        return Check("reset_delivery", "Self-serve password reset", WARN,
                     f"None of the {n} can be sent a reset link privately, so "
                     "every forgotten password needs an admin to issue a temp "
                     "one.", *fix, items=items)
    if len(reachable) < len(people):
        return Check("reset_delivery", "Self-serve password reset", WARN,
                     f"{len(reachable)} of the {n} can reset their own password. "
                     "The rest need an admin.", *fix, items=items)
    return Check("reset_delivery", "Self-serve password reset", OK,
                 f"All {n} can be sent a reset link over a private channel.",
                 *fix, items=items)


def _address_of(u: sqlite3.Row) -> Optional[str]:
    ch = channels.user_channel(u)
    if ch == "slack":
        return u["slack_member_id"]
    if ch in ("teams", "gchat"):
        return "shared webhook"
    return u["notify_address"]


def _check_display_token(con: sqlite3.Connection) -> Check:
    token = dbm.get_setting(con, "display_token")
    if not token:
        return Check("display_token", "TV display", BLOCKED,
                     "No display token, so /tv has nothing to redirect to.",
                     f"{SETTINGS_URL}#tv-display", "TV display")
    return Check("display_token", "TV display", OK,
                 "Live. The TV opens /tv and the token is resolved server-side.",
                 f"{SETTINGS_URL}#tv-display", "TV display")


def _check_api_tokens(con: sqlite3.Connection, now: datetime) -> Check:
    rows = con.execute(
        """SELECT * FROM api_tokens WHERE revoked_at IS NULL
             AND (expires_at IS NULL OR expires_at > datetime('now'))""").fetchall()
    fix = ("/admin/tokens", "API tokens")
    if not rows:
        return Check("api_tokens", "API tokens", WARN,
                     "None. The JSON API and the Claude connector both need one.",
                     *fix)
    used = [r["last_used_at"] for r in rows if r["last_used_at"]]
    if not used:
        return Check("api_tokens", "API tokens", WARN,
                     f"{len(rows)} live, none ever used. If something should be "
                     "calling, it has not connected.", *fix)
    last = max(used)
    return Check("api_tokens", "API tokens", OK,
                 f"{len(rows)} live; last call {ago(_parse_utc(last), now)}.", *fix)


def local_checks(con: sqlite3.Connection, now: datetime) -> list[Check]:
    """Ordered by dependency, so the page reads as a setup sequence while it is
    incomplete and as a health dashboard once it is not."""
    return [
        _check_people(con),
        _check_metrics(con),
        _check_dris(con),
        _check_display_token(con),
        _check_slack_credentials(con),
        _check_slack_channel(con),
        _check_alerts_switch(con),
        _check_nudges_switch(con),
        _check_public_base_url(con),
        _check_signing_secret(con),
        _check_channel_coverage(con),
        _check_reset_delivery(con),
        _check_api_tokens(con, now),
    ]


# ---------------------------------------------------------------- network tier
NETWORK_LABELS = {
    "slack_token": "Bot token and workspace",
    "slack_channel": "Bot is in the alert channel",
    "slack_members": "Member IDs resolve",
}


def _slack_call(token: str, method: str,
                params: Optional[dict] = None) -> tuple[bool, dict]:
    """One Slack Web API call that keeps the reason it failed."""
    try:
        r = httpx.post(f"https://slack.com/api/{method}",
                       headers={"Authorization": f"Bearer {token}"},
                       data=params or {}, timeout=NET_TIMEOUT)
    except httpx.HTTPError as e:
        log.warning("slack %s failed: %s", method, e)
        return False, {"error": f"could not reach Slack ({type(e).__name__})"}
    try:
        payload = r.json()
    except ValueError:
        return False, {"error": f"HTTP {r.status_code} with no JSON body"}
    # Slack reports the token's granted scopes in a header, never in the body.
    # Carried on the payload so a check can ask what this install can actually do.
    payload["_granted_scopes"] = r.headers.get("x-oauth-scopes", "")
    if not payload.get("ok"):
        log.warning("slack %s not ok: %s", method, payload.get("error"))
    return bool(payload.get("ok")), payload


def _store(con: sqlite3.Connection, key: str, state: str, detail: str,
           now: datetime, **extra) -> dict:
    """Cache one network result. The timestamp is the point: a stale answer
    that says how stale it is beats a fresh-looking one that is not."""
    result = {"state": state, "detail": detail, "at": now.isoformat(), **extra}
    dbm.set_setting(con, f"verify_{key}", json.dumps(result))
    return result


def cached(con: sqlite3.Connection, key: str) -> Optional[dict]:
    raw = dbm.get_setting(con, f"verify_{key}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def verify_token(con: sqlite3.Connection, now: datetime) -> dict:
    """auth.test - the five-second question that would have caught a token for
    an entirely different workspace. Returns the workspace it actually belongs
    to, which is the only way to tell a good token from a valid one."""
    token = dbm.get_setting(con, "slack_bot_token") or ""
    if not token:
        # Not "blocked": there is nothing to reject. Whether a missing token
        # matters is the local slack_credentials check's question, not this one.
        return _store(con, "slack_token", UNCHECKED, "No bot token set.", now)
    ok, payload = _slack_call(token, "auth.test")
    if not ok:
        return _store(con, "slack_token", BLOCKED,
                      f"Slack rejected the token: {payload.get('error', 'unknown')}.",
                      now)
    team, bot = payload.get("team", "?"), payload.get("user", "?")
    detail = f"Workspace {team}, bot @{bot}."
    # Still OK either way: a default avatar is cosmetic, and messages go out
    # regardless (alerts._post_message drops the icon rather than the message).
    # Silence would be worse than the wrong picture - say which one you have.
    scopes = [s.strip() for s in (payload.get("_granted_scopes") or "").split(",")]
    if alerts.CUSTOMIZE_SCOPE in scopes:
        detail += " Messages carry the Scorecard icon."
    elif any(scopes):
        detail += (f" Messages use Slack's default avatar - add the "
                   f"{alerts.CUSTOMIZE_SCOPE} scope and reinstall for the icon.")
    return _store(con, "slack_token", OK, detail, now,
                  team=team, team_id=payload.get("team_id"), bot=bot)


def verify_channel(con: sqlite3.Connection, now: datetime) -> dict:
    token = dbm.get_setting(con, "slack_bot_token") or ""
    channel = (dbm.get_setting(con, "slack_channel_id") or "").strip()
    if not token or not channel:
        return _store(con, "slack_channel", UNCHECKED,
                      "Needs a bot token and a channel ID.", now)
    ok, payload = _slack_call(token, "conversations.info", {"channel": channel})
    if not ok:
        return _store(con, "slack_channel", BLOCKED,
                      f"Slack: {payload.get('error', 'unknown')}.", now)
    ch = payload.get("channel") or {}
    name = ch.get("name") or channel
    if not ch.get("is_member"):
        return _store(con, "slack_channel", BLOCKED,
                      f"#{name} exists but the bot is not a member - invite it "
                      "with /invite in the channel. Posts fail silently until "
                      "you do.", now)
    return _store(con, "slack_channel", OK, f"#{name}, bot is a member.", now)


def verify_members(con: sqlite3.Connection, now: datetime,
                   team_id: Optional[str] = None) -> dict:
    """users.info per stored member ID, compared against OUR workspace.

    The trap this exists for: an ID from another workspace resolves perfectly
    through Slack Connect, so "it came back ok" proves nothing. The team_id is
    what separates a colleague from a stranger who happens to share a channel."""
    token = dbm.get_setting(con, "slack_bot_token") or ""
    if not token:
        return _store(con, "slack_members", UNCHECKED, "Needs a bot token.", now)
    people = con.execute(
        """SELECT * FROM users WHERE is_active = 1
             AND slack_member_id IS NOT NULL AND TRIM(slack_member_id) <> ''
           ORDER BY display_name""").fetchall()
    if not people:
        return _store(con, "slack_members", UNCHECKED,
                      "No Slack member IDs stored yet.", now)
    items, bad, foreign = [], 0, 0
    for u in people:
        mid = u["slack_member_id"].strip()
        ok, payload = _slack_call(token, "users.info", {"user": mid})
        if not ok:
            bad += 1
            items.append({"name": u["display_name"], "state": BLOCKED,
                          "detail": f"{mid} - Slack: {payload.get('error', 'unknown')}"})
            continue
        member = payload.get("user") or {}
        their_team = member.get("team_id")
        who = member.get("real_name") or member.get("name") or mid
        if team_id and their_team and their_team != team_id:
            foreign += 1
            items.append({"name": u["display_name"], "state": WARN,
                          "detail": f"{mid} resolves to {who}, but in another "
                                    "workspace - a Slack Connect account, not a "
                                    "colleague. DMs will not arrive."})
        elif member.get("deleted"):
            bad += 1
            items.append({"name": u["display_name"], "state": BLOCKED,
                          "detail": f"{mid} is a deactivated Slack account."})
        else:
            items.append({"name": u["display_name"], "state": OK,
                          "detail": f"{mid} - {who}"})
    ids = f"{len(people)} member {'ID' if len(people) == 1 else 'IDs'}"
    if bad:
        state, detail = BLOCKED, f"{bad} of the {ids} do not resolve at all."
    elif foreign:
        state, detail = WARN, (f"{foreign} of the {ids} belong to a different "
                               "workspace, so their DMs go nowhere.")
    elif not team_id:
        # Resolving is the easy half. Without our own team_id there is nothing
        # to compare against, and "it resolved" is exactly the false green that
        # let another workspace's IDs sit here looking correct.
        state, detail = WARN, (f"All {ids} resolve, but our own workspace could "
                               "not be confirmed, so nothing was compared "
                               "against it. Fix the token check and re-check.")
    else:
        state, detail = OK, f"All {ids} are accounts in this workspace."
    return _store(con, "slack_members", state, detail, now, items=items)


def run_network_checks(con: sqlite3.Connection, now: datetime) -> None:
    """The Re-check button. Token first: its team_id is what makes the member
    check able to tell 'resolved' from 'ours'."""
    token = verify_token(con, now)
    verify_channel(con, now)
    verify_members(con, now, team_id=token.get("team_id"))


def network_checks(con: sqlite3.Connection, now: datetime) -> list[Check]:
    """Render the cached results, each stating its own age. Never calls out."""
    out = []
    for key, label in NETWORK_LABELS.items():
        c = cached(con, key)
        if not c:
            out.append(Check(key, label, UNCHECKED, "Not checked yet."))
            continue
        at = _parse_utc(c.get("at"))
        detail = c.get("detail", "")
        if at:
            detail = f"{detail} Checked {ago(at, now)}."
        out.append(Check(key, label, c.get("state", UNCHECKED), detail,
                         items=tuple(c.get("items") or ())))
    return out


def blocked_count(con: sqlite3.Connection, now: datetime) -> int:
    """Nav badge. Local checks plus whatever the last re-check already found -
    reading the cache is a settings lookup, so this stays network-free."""
    checks = local_checks(con, now) + network_checks(con, now)
    return sum(1 for c in checks if c.state == BLOCKED)


# ------------------------------------------------------- match IDs from email
def propose_member_ids(con: sqlite3.Connection) -> tuple[list[dict], Optional[str]]:
    """Join our users to Slack accounts on email and return a PROPOSAL.

    Email is the only field both sides agree on, and it is the join that worked
    when this was done by hand. It never writes: an admin confirms the diff,
    because the failure mode being fixed here is exactly "a plausible ID that
    was never actually checked"."""
    token = dbm.get_setting(con, "slack_bot_token") or ""
    if not token:
        return [], "No bot token set, so there is nobody to ask."
    by_email: dict[str, dict] = {}
    cursor = ""
    for _ in range(10):  # paginate, but never unbounded
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        ok, payload = _slack_call(token, "users.list", params)
        if not ok:
            err = payload.get("error", "unknown")
            hint = (" The bot needs the users:read and users:read.email scopes."
                    if err == "missing_scope" else "")
            return [], f"Slack: {err}.{hint}"
        for m in payload.get("members") or []:
            email = ((m.get("profile") or {}).get("email") or "").strip().lower()
            if email and not m.get("deleted") and not m.get("is_bot"):
                by_email[email] = m
        cursor = (payload.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    rows = []
    for u in con.execute(
            "SELECT * FROM users WHERE is_active = 1 ORDER BY display_name"):
        m = by_email.get((u["email"] or "").strip().lower())
        current = (u["slack_member_id"] or "").strip()
        proposed = (m or {}).get("id") or ""
        if not m:
            state, note = "none", "No Slack account with this email."
        elif proposed == current:
            state, note = "match", "Already correct."
        elif current:
            state, note = "change", f"Currently {current} - would be replaced."
        else:
            state, note = "new", "Not set yet."
        rows.append({"user_id": u["id"], "name": u["display_name"],
                     "email": u["email"], "current": current, "proposed": proposed,
                     "slack_name": (m or {}).get("real_name")
                                   or (m or {}).get("name") or "",
                     "state": state, "note": note})
    return rows, None


def apply_member_ids(con: sqlite3.Connection, pairs: list[str]) -> int:
    """Write the confirmed subset. Each pair is "<user_id>:<slack_id>"; both
    sides are validated rather than trusted, since they came back off a form."""
    n = 0
    for pair in pairs:
        uid, _, mid = pair.partition(":")
        if not uid.isdigit() or not SLACK_ID_RE.match(mid):
            continue
        cur = con.execute(
            "UPDATE users SET slack_member_id = ?, notify_channel = 'slack' "
            "WHERE id = ? AND is_active = 1", (mid, int(uid)))
        n += cur.rowcount
    return n


# ----------------------------------------------------------------- sweep runs
SWEEP_LABELS = {
    "nudge1": "Check-in nudge - Monday 4:00 PM",
    "nudge2": "Check-in nudge - Tuesday 9:00 AM",
    "red": "Red escalation - Tuesday 8:00 AM",
    "stale": "Stale sweep - Wednesday 8:00 AM",
}
SWEEP_STATE = {"sent": OK, "nothing": OK, "skipped": WARN}


def sweep_runs(con: sqlite3.Connection, now: datetime) -> list[dict]:
    """Latest run per sweep. A skip that reads 'no public base URL' is the
    whole point: silence with a reason attached."""
    rows = {r["kind"]: r for r in con.execute(
        """SELECT * FROM sweep_runs WHERE id IN
           (SELECT MAX(id) FROM sweep_runs GROUP BY kind)""")}
    out = []
    for kind, label in SWEEP_LABELS.items():
        r = rows.get(kind)
        if r is None:
            out.append({"kind": kind, "label": label, "state": UNCHECKED,
                        "chip": CHIP[UNCHECKED], "badge": BADGE[UNCHECKED],
                        "when": "not run yet", "outcome": "never", "sent": 0,
                        "detail": "No run recorded."})
            continue
        at = _parse_utc(r["ran_at"])
        state = SWEEP_STATE.get(r["outcome"], WARN)
        out.append({"kind": kind, "label": label, "state": state,
                    "chip": CHIP[state], "badge": BADGE[state],
                    "when": _stamp(at) if at else r["ran_at"],
                    "outcome": r["outcome"], "detail": r["detail"],
                    "sent": r["sent_count"]})
    return out


def prune_sweep_runs(con: sqlite3.Connection, keep_days: int = 90) -> None:
    con.execute("DELETE FROM sweep_runs WHERE ran_at < datetime('now', ?)",
                (f"-{int(keep_days)} days",))


# --------------------------------------------------------------------- timing
def _parse_utc(ts: Optional[str]) -> Optional[datetime]:
    """SQLite writes 'YYYY-MM-DD HH:MM:SS' in UTC; our own caches write ISO."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _stamp(at: datetime) -> str:
    return at.astimezone(wk.BUSINESS_TZ).strftime("%a %b %-d, %-I:%M %p")


def ago(at: Optional[datetime], now: datetime) -> str:
    if at is None:
        return "at an unknown time"
    secs = (now - at).total_seconds()
    if secs < 90:
        return "just now"
    for unit, size, limit in (("minute", 60, 3600), ("hour", 3600, 86400),
                              ("day", 86400, 8 * 86400)):
        if secs < limit:
            n = int(secs // size)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return f"on {_stamp(at)}"
