"""Typed-reply handling shared by every two-way channel (Slack DM, Telegram,
Twilio SMS/WhatsApp). Deliberately AI-free: the nudge message numbers the
user's missing metrics and pins that exact list (slack_prompts, named for the
first channel but shared by all); replies like "1: 42, 2: G" resolve indices
against the pinned list and write through entry_ops.save_value.

Transport-free: build_reply_response returns the text to send back, and each
channel module delivers it its own way. Always the REAL database - a typed
reply is a real business act, whatever the demo-data display toggle says."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta

from . import entry_ops
from . import weeks as wk

# "1: 42" / "2 = G" / "3. 1500" / "#4) yes" / "5 - 12" / "6 12".
# The index needs a separator or whitespace so a stray "142" can never
# half-match as index 14, value 2. Dot, paren and dash additionally require a
# space after them, so "1.5" stays the number 1.5 rather than item 1 value 5.
_ITEM_RE = re.compile(r"""^\s*\#?\s*(\d+)\s*
                          (?: [:=]\s*
                            | [.)\]\-–—]\s+
                            | \s+ )
                          (.+?)\s*$""", re.VERBOSE)

# Split on newlines and semicolons always, and on commas except the ones
# inside a number - "1: $1,500" is one item, not "1: $1" and a stray "500".
_SPLIT_RE = re.compile(r"[;\n]|,(?!\d{3}(?!\d))")


def parse_reply(text: str, expected: int | None = None) -> list[tuple[int, str]] | str:
    """Deterministic reply grammar - no AI, so what it accepts is exactly what
    is written here. Items split on newlines/semicolons/commas; each is
    'index<sep>value' in any of the forms above.

    If nothing carries an index and the number of values matches the number of
    metrics we asked for, they are taken in the order they were asked - "25,
    30, G" for a three-item prompt. Requiring an exact match is what keeps that
    unambiguous: a partial list would be guessing which metrics were answered.

    Returns items or a user-showable error string."""
    parts = [p for p in _SPLIT_RE.split(text) if p.strip()]
    if not parts:
        return "I could not find anything to read in that message."
    items: list[tuple[int, str]] = []
    unindexed: list[str] = []
    for p in parts:
        m = _ITEM_RE.match(p)
        if m:
            items.append((int(m.group(1)), m.group(2)))
        else:
            unindexed.append(p.strip())
    if not items and unindexed:
        if expected is not None and len(unindexed) == expected:
            return list(enumerate(unindexed, 1))
        return (f'I could not read "{unindexed[0]}".' if expected is None else
                f'I could not read "{unindexed[0]}". Number each value '
                f'("1: 12"), or send exactly {expected} values in order.')
    if unindexed:
        return (f'I could not read "{unindexed[0]}" - the rest of that message '
                "was numbered, so give this one a number too.")
    return items


def _state_word(state) -> str:
    return state.value.replace("-", " ")


def help_text(con: sqlite3.Connection, prompt: sqlite3.Row) -> str:
    metric_ids = json.loads(prompt["metric_ids"])
    week = wk.parse_week(prompt["week_start"])
    lines = [f"Open check-in for the week of {week.strftime('%b %-d')}:"]
    for i, mid in enumerate(metric_ids, 1):
        m = con.execute("SELECT * FROM metrics WHERE id = ?", (mid,)).fetchone()
        if m is not None:
            lines.append(f"{i}. {m['name']}{entry_ops.target_hint(con, m, week)}")
    lines.append('Reply like "1: 12, 2: G" - or "1. 12", "1) 12", "1 12", one '
                 "per line, whichever is easiest. Numbers for numeric metrics, "
                 "G/Y/R for client health, yes/no for binary. If you send "
                 f"exactly {len(metric_ids)} value"
                 f"{'s' if len(metric_ids) != 1 else ''} with no numbering, "
                 "I will take them in the order above.")
    return "\n".join(lines)


def build_reply_response(con: sqlite3.Connection, u: sqlite3.Row, text: str, *,
                         source: str, now: datetime) -> str:
    """Process one matched user's typed reply and describe the outcome.
    Saves happen here (source tags the channel); the caller sends the text."""
    prompt = con.execute(
        "SELECT * FROM slack_prompts WHERE user_id = ?", (u["id"],)).fetchone()
    if (prompt is None
            or wk.parse_week(prompt["week_start"])
            < wk.last_closed_week(now) - timedelta(days=7)):
        return ("I do not have an open check-in for you right now. Enter "
                "numbers on the scorecard website, or wait for the next "
                "weekly nudge.")
    if text.strip().lower() in ("help", "?"):
        return help_text(con, prompt)

    metric_ids = json.loads(prompt["metric_ids"])
    parsed = parse_reply(text, expected=len(metric_ids))
    if isinstance(parsed, str):
        return parsed + "\n\n" + help_text(con, prompt)

    week = wk.parse_week(prompt["week_start"])
    saved: list[str] = []
    problems: list[str] = []
    for idx, raw in parsed:
        if not 1 <= idx <= len(metric_ids):
            problems.append(f"{idx}: no such item (1-{len(metric_ids)})")
            continue
        m = con.execute("SELECT * FROM metrics WHERE id = ? AND archived_at IS NULL",
                        (metric_ids[idx - 1],)).fetchone()
        if m is None:
            problems.append(f"{idx}: that metric was archived")
            continue
        try:
            entry_ops.save_value(con, m, week, raw, source=source, user_id=u["id"])
        except ValueError as e:
            problems.append(f"{idx} ({m['name']}): {e}")
            continue
        state = entry_ops.state_for(con, m, week, now)
        shown = raw.strip().upper() if m["metric_type"] == "status" else raw.strip()
        saved.append(f"{m['name']} = {shown} ({_state_word(state)})")
    con.commit()

    lines = []
    if saved:
        lines.append(f"Recorded for the week of {week.strftime('%b %-d')}: "
                     + " / ".join(saved))
    if problems:
        lines.append("Could not record: " + "; ".join(problems)
                     + '. Reply "help" to see the list again.')
    return "\n".join(lines)
