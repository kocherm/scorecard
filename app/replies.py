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

# "1: 42" / "2 = G" / "3 1500" - the index needs a separator or whitespace so
# a stray "142" can never half-match as index 14, value 2.
_ITEM_RE = re.compile(r"^\s*(\d+)\s*(?:[:=]\s*|\s+)(.+?)\s*$")


def parse_reply(text: str) -> list[tuple[int, str]] | str:
    """Deterministic reply grammar. Items split on newlines/commas/semicolons;
    each is 'index[:= ]value'. Returns items or an error string. No AI."""
    parts = [p for chunk in text.replace(";", ",").splitlines()
             for p in chunk.split(",") if p.strip()]
    if not parts:
        return "I could not find anything to read in that message."
    items: list[tuple[int, str]] = []
    for p in parts:
        m = _ITEM_RE.match(p)
        if not m:
            return f'I could not read "{p.strip()}".'
        items.append((int(m.group(1)), m.group(2)))
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
    lines.append('Reply like "1: 12, 2: G" (numbers for numeric metrics, '
                 "G/Y/R for client health, yes/no for binary).")
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

    parsed = parse_reply(text)
    if isinstance(parsed, str):
        return parsed + "\n\n" + help_text(con, prompt)

    metric_ids = json.loads(prompt["metric_ids"])
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
