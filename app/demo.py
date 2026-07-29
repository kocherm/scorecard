"""Demo data mode: a separate, throwaway SQLite database holding a fictional
company, so the scorecard can be shown off (screenshots, screen recordings,
lead magnets) without exposing or risking real data.

The REAL database owns the toggle (settings key `display_demo_data`). While it
is on, the board surfaces - edit grid, TV display, cell edits, 1-3-1s - read
and write the demo DB instead; login, admin pages, Slack alerts, and the JSON
API stay on the real DB. The demo DB is rebuilt from scratch whenever the
toggle is switched on, the week rolls over, or DEMO_VERSION changes, so demo
edits are temporary by design and the story always sits relative to "now".

Every name below is fictional (privacy rule: this file is tracked).

The dataset is scripted to exercise every capability on one board:
mostly-green momentum with a rising MRR band, a two-week red with a 15-min 1:1
due, a fresh red awaiting its 1-3-1, an escalated client whose 1-3-1 is filed,
a stale (gray) cell, yellows, sparklines, and enough clients to fold.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from math import ceil
from pathlib import Path
from typing import Iterator, Optional

from . import db as dbm
from . import scoring as sc
from . import weeks as wk

DEMO_VERSION = "1"  # bump after changing the generator to force a rebuild

_lock = threading.Lock()

N_WEEKS = 13  # closed weeks of history, oldest -> newest


def demo_path() -> Path:
    # Derived per call so tests that monkeypatch db.DB_PATH are honoured.
    return Path(dbm.DB_PATH).with_name("demo.db")


# ---------------------------------------------------------------- the story
_USERS = [
    {"name": "Alex Rivera", "email": "alex@demo.invalid", "role": "admin"},
    {"name": "Jordan Lee", "email": "jordan@demo.invalid", "role": "editor"},
    {"name": "Sam Patel", "email": "sam@demo.invalid", "role": "editor"},
    {"name": "Riley Chen", "email": "riley@demo.invalid", "role": "editor"},
    {"name": "Morgan Diaz", "email": "morgan@demo.invalid", "role": "editor"},
]

_DRI = {
    "sales_activity": "jordan@demo.invalid",
    "calls_held": "alex@demo.invalid",
    "revenue": "sam@demo.invalid",
    "client_health": "morgan@demo.invalid",
    "content": "riley@demo.invalid",
    "followers": "riley@demo.invalid",
}

_CLIENTS = ["Acme Robotics", "Bluepeak Media", "Cascade Outfitters",
            "Delta Wellness", "Ember & Oak", "Foundry Labs",
            "Harborline Logistics", "Juniper Skincare"]

_MRR_START, _MRR_END = 52000, 63000  # 13-week climb shown on the goal band

_TARGETS: dict[str, tuple[float, float]] = {  # key: (baseline, stretch)
    "conversations": (40, 48),
    "followups": (60, 72),
    "calls_booked": (12, 15),
    "calls_held": (8, 10),
    "proposals": (5, 6),
    "mrr": (58000, 62000),
    "new_mrr": (4000, 5000),
    "churn_risk": (3000, 2500),
    "scripts": (5, 6),
    "posts": (7, 9),
    "content_convos": (10, 12),
    "followers": (200, 240),
}

# Cell stories over the closed weeks, oldest -> newest; the LAST char is the
# last closed week. Shorter strings are left-padded with green. g/y/r set the
# scored color, '-' leaves the week empty (a gray stale cell in history).
# proposals ends "rr"  -> RED WK 2, "15-min 1:1 this week" action card.
# content_convos ends "r" -> RED WK 1, "file a 1-3-1 before sync" card.
# followers has a '-' gap and a yellow catch-up entry.
_NUMERIC_STORY = {
    "conversations": "gygggyggggg",
    "followups": "ggyggggggygg",
    "calls_booked": "gggygggggg",
    "calls_held": "ggygggggyg",
    "proposals": "gggygggggyrr",
    "new_mrr": "gygggyggggy",
    "churn_risk": "ggggygggg",
    "scripts": "ggggggyggg",
    "posts": "gygggggggg",
    "content_convos": "ggggygggyr",
    "followers": "gggggygg-y",
}

# Client health R/Y/G. Harborline ends Y,R: an active escalation whose 1-3-1
# is already filed (shows the ladder end-to-end). Bluepeak shows a recovery.
_STATUS_STORY = {
    "Acme Robotics": "G",
    "Bluepeak Media": "GGYYGG",
    "Cascade Outfitters": "G",
    "Delta Wellness": "GGYG",
    "Ember & Oak": "G",
    "Foundry Labs": "GYG",
    "Harborline Logistics": "GGYYR",
    "Juniper Skincare": "G",
}

_131 = {
    "problem": ("Harborline's health went red: onboarding stalled at the "
                "warehouse-integration step and their team missed two check-ins."),
    "options": [
        "Assign Sam as hands-on lead for a two-week onboarding sprint",
        "Cut scope to the core integration and reset the launch date",
        "Pause billing for a month while their IT clears the blocker",
    ],
    "recommendation": ("Option 1 - run the sprint with daily async updates; "
                       "revisit scope on Friday if the blocker persists."),
}


def _numeric_value(state: str, tgt: float, direction: str,
                   unit: Optional[str], i: int, n: int) -> float:
    """A value that scores `state` against `tgt`, with greens ramping up over
    time so sparklines read as momentum. Clamped after rounding so integer
    metrics with small targets cannot drift across a color boundary."""
    grow = i / max(n - 1, 1)
    if direction == "down":
        f = {"g": 0.92 - 0.12 * grow, "y": 1.2, "r": 1.6}[state]
    else:
        f = {"g": 1.02 + 0.25 * grow, "y": 0.85, "r": 0.55}[state]
    v = tgt * f
    if unit == "$":
        return float(round(v / 100) * 100)
    v = round(v)
    if direction == "up":
        if state == "g":
            v = max(v, ceil(tgt))
        elif state == "y":
            v = min(max(v, ceil(tgt * sc.YELLOW_FLOOR)), ceil(tgt) - 1)
        else:
            v = max(0, min(v, ceil(tgt * sc.YELLOW_FLOOR) - 1))
    return float(v)


def _dataset(now: datetime) -> tuple[dict, date, list[date]]:
    """Seed-shaped data dict relative to `now`, plus (current_week, closed_weeks)."""
    cw = wk.current_week(now)
    closed = [cw - timedelta(days=7 * (N_WEEKS - i)) for i in range(N_WEEKS)]

    hist_num: dict[str, list] = {}
    for key, story in _NUMERIC_STORY.items():
        b, s = _TARGETS[key]
        direction = "down" if key == "churn_risk" else "up"
        unit = "$" if key in ("mrr", "new_mrr", "churn_risk") else None
        padded = story.rjust(N_WEEKS, "g")[-N_WEEKS:]
        vals: list = []
        for i, (w, ch) in enumerate(zip(closed, padded)):
            if ch == "-":
                vals.append(None)
                continue
            tgt = sc.target_for_week(w, sc.QuarterTargets(b, s))
            vals.append(_numeric_value(ch, tgt, direction, unit, i, N_WEEKS))
        hist_num[key] = vals

    # MRR: a monotonic climb from below target to just above it - the arc the
    # goal band at the top of the TV is built to tell. Absolute dollars, not
    # target-relative, so the curve never dips at a quarter boundary.
    hist_num["mrr"] = [
        float(round((_MRR_START + (_MRR_END - _MRR_START) * i / (N_WEEKS - 1)) / 100) * 100)
        for i in range(N_WEEKS)
    ]

    hist_status = {
        client: [None if ch == "-" else ch
                 for ch in story.rjust(N_WEEKS, "G")[-N_WEEKS:]]
        for client, story in _STATUS_STORY.items()
    }

    data = {
        "users": _USERS,
        "clients": _CLIENTS,
        "dri": _DRI,
        "targets": {},  # inserted below for every quarter the window touches
        "history_weeks": [w.isoformat() for w in closed],
        "history_numeric": hist_num,
        "history_status": hist_status,
    }
    return data, cw, closed


def _build(con: sqlite3.Connection, now: datetime) -> None:
    from migrate.seed import seed_into

    dbm.init_db(con)
    data, cw, closed = _dataset(now)
    res = seed_into(con, data, start_fresh=cw)
    mid, uid = res["mid"], res["uid"]

    # Targets for every quarter the history/window can touch, so no week ever
    # renders "no target" no matter what month the demo is switched on.
    quarters = sorted({wk.quarter_of(w) for w in closed + [cw]})
    for key, (b, s) in _TARGETS.items():
        if key not in mid:
            continue
        for (y, q) in quarters:
            con.execute(
                """INSERT INTO targets (metric_id, year, quarter,
                                        baseline_value, stretch_value)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(metric_id, year, quarter) DO UPDATE SET
                     baseline_value = excluded.baseline_value,
                     stretch_value = excluded.stretch_value""",
                (mid[key], y, q, b, s))

    # A few current-week numbers so the TV mixes "this week" and "last week"
    # tiles, and the goal band reads as-of the current week.
    admin_id = uid[_USERS[0]["email"]]

    def cur_target(key: str) -> float:
        return sc.target_for_week(cw, sc.QuarterTargets(*_TARGETS[key]))

    dbm.upsert_entry(con, mid["conversations"], cw,
                     value_numeric=float(ceil(cur_target("conversations") * 1.05)),
                     source="manual", user_id=admin_id)
    dbm.upsert_entry(con, mid["calls_held"], cw,
                     value_numeric=float(ceil(cur_target("calls_held") * 1.1)),
                     source="manual", user_id=admin_id)
    # Above this week's pace (green on the band) and above last week's value,
    # whichever is higher, so the curve keeps climbing.
    cur_mrr = max(cur_target("mrr") * 1.03, _MRR_END + 400)
    dbm.upsert_entry(con, mid["mrr"], cw,
                     value_numeric=float(round(cur_mrr / 100) * 100),
                     source="manual", user_id=admin_id)

    # The escalated client's 1-3-1, filed for the week that went red.
    con.execute(
        """INSERT OR IGNORE INTO one_three_ones
           (metric_id, week_start, problem, options_json, recommendation, created_by)
           VALUES (?,?,?,?,?,?)""",
        (mid["client_Harborline Logistics"], (cw - timedelta(days=7)).isoformat(),
         _131["problem"], json.dumps(_131["options"]), _131["recommendation"],
         uid[_DRI["client_health"]]))

    dbm.set_setting(con, "mrr_milestones", "62000:$62k;80000:$80k")
    dbm.set_setting(con, "demo_built_week", cw.isoformat())
    dbm.set_setting(con, "demo_version", DEMO_VERSION)
    con.commit()


# ---------------------------------------------------------------- lifecycle
def _delete_files(path: Path) -> None:
    for p in (path, path.with_name(path.name + "-wal"),
              path.with_name(path.name + "-shm")):
        p.unlink(missing_ok=True)


def reset() -> None:
    """Drop the demo DB; the next request rebuilds it fresh."""
    with _lock:
        _delete_files(demo_path())


@contextmanager
def demo_db(now: datetime, display_months: str) -> Iterator[sqlite3.Connection]:
    """Connection to a demo DB that is guaranteed fresh for the current week
    and generator version. `display_months` mirrors the real setting so the
    demo grid honours the admin's window choice."""
    path = demo_path()
    with _lock:
        con = dbm.connect(str(path))
        try:
            try:
                current = (dbm.get_setting(con, "demo_built_week") ==
                           wk.current_week(now).isoformat()
                           and dbm.get_setting(con, "demo_version") == DEMO_VERSION)
            except sqlite3.Error:  # brand-new file: no settings table yet
                current = False
            if not current:
                con.close()
                _delete_files(path)
                con = dbm.connect(str(path))
                _build(con, now)
            dbm.set_setting(con, "display_months", display_months)
            con.commit()
        except BaseException:
            con.close()
            raise
    try:
        yield con
        con.commit()
    finally:
        con.close()
