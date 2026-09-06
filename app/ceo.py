"""The "7 metrics every CEO should track" template: Revenue, Expenses, Leads,
Conversions, CAC, Retention, Profit - and the slot mapping behind the TV view
that arranges them as a story (growth engine, unit economics, bottom line)
instead of a list.

Two halves. install_template creates the seven as ORDINARY metrics in their
own section, so they are entered, scored, paced, alerted and audited like
every other row - nothing here is a second kind of metric. The view finds its
seven rows through a SLOT MAPPING in settings (ceo_slot_<slot> -> metric id),
so a company already tracking "MRR" points the Revenue slot at it instead of
starting a duplicate; an unset slot falls back to a name match, the way the
goal band detects a metric named "MRR".

The mapping lives in whichever database holds the metrics (the DATA db, read
through the same connection build_tv uses), never the real db by fiat: it is a
map of metric ids, which mean nothing across databases, so demo mode gets its
own mapping (or the name fallback) just as it gets its own goal band.

Rollups are chosen so the in-progress week paces correctly (CLAUDE.md): the
five flows are sums, and CAC and Retention are averages because they are
point-in-time ratios - a half-week's CAC is already whole, and scaling its
target would call an on-budget week red on a Tuesday.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from . import db as dbm
from . import weeks as wk

SECTION_NAME = "CEO metrics"
SECTION_ICON = "dollar"
VIEW_KEY = "ceo"


@dataclass(frozen=True)
class Slot:
    key: str
    label: str
    unit: Optional[str]
    rollup: str
    direction: str
    keywords: tuple      # lower-case fragments a metric name can match on
    hint: str            # one line for the settings page


SLOTS: tuple[Slot, ...] = (
    Slot("revenue", "Revenue", "$", "sum", "up",
         ("revenue", "mrr", "sales"), "Money in for the week."),
    Slot("expenses", "Expenses", "$", "sum", "down",
         ("expense", "cost", "spend", "burn"), "Money out. Lower is better."),
    Slot("leads", "Leads", None, "sum", "up",
         ("lead",), "New people who raised a hand."),
    Slot("conversions", "Conversions", None, "sum", "up",
         ("conversion", "new client", "new customer", "closed", "signup"),
         "Leads that became customers."),
    Slot("cac", "CAC", "$", "average", "down",
         ("cac", "acquisition cost"), "Cost to acquire one customer. Lower is better."),
    Slot("retention", "Retention", "%", "average", "up",
         ("retention",), "Share of customers who stayed."),
    Slot("profit", "Profit", "$", "sum", "up",
         ("profit", "net income"), "What is left after expenses."),
)
SLOT_BY_KEY = {s.key: s for s in SLOTS}


def setting_key(slot: str) -> str:
    return f"ceo_slot_{slot}"


def _live_numeric(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        """SELECT m.id, m.name FROM metrics m JOIN sections s ON s.id = m.section_id
           WHERE m.archived_at IS NULL AND m.metric_type = 'numeric'
           ORDER BY s.sort_order, m.sort_order, m.id""").fetchall()


def explicit_slots(con: sqlite3.Connection) -> dict[str, str]:
    """The raw settings, '' where unset - what the settings form shows."""
    return {s.key: (dbm.get_setting(con, setting_key(s.key)) or "").strip()
            for s in SLOTS}


def resolve_slots(con: sqlite3.Connection) -> dict[str, Optional[int]]:
    """slot key -> metric id (or None). An explicit setting wins when it still
    points at a live numeric metric; otherwise the first live metric whose
    name matches the slot - exact label first, then keyword - that no other
    slot has already claimed. "New MRR" never fills Revenue: a metric named
    with 'new' is a flow into the thing, not the thing."""
    live = _live_numeric(con)
    by_id = {m["id"]: m for m in live}
    out: dict[str, Optional[int]] = {}
    taken: set[int] = set()
    raw = explicit_slots(con)
    for s in SLOTS:
        try:
            mid = int(raw[s.key]) if raw[s.key] else None
        except ValueError:
            mid = None
        if mid in by_id and mid not in taken:
            out[s.key] = mid
            taken.add(mid)
        else:
            out[s.key] = None
    for s in SLOTS:
        if out[s.key] is not None:
            continue
        found = None
        for m in live:
            name = m["name"].lower()
            if m["id"] in taken or "new" in name.split():
                continue
            if name == s.label.lower():
                found = m["id"]
                break
        if found is None:
            for m in live:
                name = m["name"].lower()
                if m["id"] in taken or "new" in name.split():
                    continue
                if any(k in name for k in s.keywords):
                    found = m["id"]
                    break
        if found is not None:
            out[s.key] = found
            taken.add(found)
    return out


def save_slots(con: sqlite3.Connection, chosen: dict[str, str]) -> None:
    """Persist the form: '' means auto-detect, anything else must be a live
    numeric metric id or it is stored as auto rather than as a dangling id."""
    live_ids = {m["id"] for m in _live_numeric(con)}
    for s in SLOTS:
        v = (chosen.get(s.key) or "").strip()
        try:
            ok = v != "" and int(v) in live_ids
        except ValueError:
            ok = False
        dbm.set_setting(con, setting_key(s.key), v if ok else "")


@dataclass
class InstallResult:
    section_id: int
    created: list          # metric names created
    skipped: list          # slots already mapped to a live metric


def install_template(con: sqlite3.Connection, now: datetime,
                     dri_user_id: Optional[int] = None) -> InstallResult:
    """Create the seven as ordinary metrics in a "CEO metrics" section and map
    the slots to them. Idempotent in the way an admin expects: a slot that
    already resolves to a live metric (mapped, or matched by name) is left
    alone rather than duplicated, so running it on a board that already has
    "Revenue" adds the six it is missing. The section is reused if it exists.
    The caller enables the TV view; that setting lives in the real db and
    this function may be handed either."""
    sec = con.execute("SELECT id FROM sections WHERE name = ? ORDER BY id LIMIT 1",
                      (SECTION_NAME,)).fetchone()
    if sec:
        section_id = sec["id"]
    else:
        mx = con.execute(
            "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM sections").fetchone()["n"]
        cur = con.execute("INSERT INTO sections (name, icon, sort_order) VALUES (?,?,?)",
                          (SECTION_NAME, SECTION_ICON, mx))
        section_id = cur.lastrowid
    start = wk.current_week(now).isoformat()
    existing = resolve_slots(con)
    created, skipped = [], []
    for i, s in enumerate(SLOTS):
        if existing.get(s.key) is not None:
            # Pin the match so a later rename cannot silently unmap it.
            dbm.set_setting(con, setting_key(s.key), str(existing[s.key]))
            skipped.append(s.key)
            continue
        cur = con.execute(
            """INSERT INTO metrics (section_id, name, metric_type, rollup, direction,
                                    unit, dri_user_id, start_week, sort_order)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (section_id, s.label, "numeric", s.rollup, s.direction, s.unit,
             dri_user_id, start, i))
        dbm.set_setting(con, setting_key(s.key), str(cur.lastrowid))
        created.append(s.label)
    return InstallResult(section_id=section_id, created=created, skipped=skipped)
