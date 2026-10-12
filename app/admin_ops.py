"""Structural writes shared by the admin screens and the admin API.

Adding a person, adding a metric and setting a target each have a rule that
is easy to lose in a second copy - one live row per name in a section, a
target is a baseline AND a stretch, deactivating someone ends their sessions.
Both front doors call these, so the API cannot quietly become a looser way
to do what Admin > Sections & metrics refuses."""
from __future__ import annotations

import secrets
import sqlite3
from datetime import date
from typing import Optional

from .auth import hash_password


class Conflict(ValueError):
    """The write would duplicate something that must be unique."""


def temp_password() -> str:
    return secrets.token_urlsafe(9)


def create_user(con: sqlite3.Connection, display_name: str, email: str,
                role: str, slack_member_id: Optional[str] = None) -> tuple[int, str]:
    """Insert a user with a one-time password they must change on first login.
    Returns (id, the password) - it is not stored anywhere readable, so the
    caller shows it once or it is gone."""
    pw = temp_password()
    try:
        cur = con.execute(
            """INSERT INTO users (email, password_hash, display_name, role,
                                  must_change_password, slack_member_id)
               VALUES (?,?,?,?,1,?)""",
            (email.strip(), hash_password(pw), display_name.strip(), role,
             slack_member_id or None))
    except sqlite3.IntegrityError:
        raise Conflict(f"{email.strip()} already exists")
    return cur.lastrowid, pw


def set_user_active(con: sqlite3.Connection, uid: int, active: bool) -> None:
    con.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if active else 0, uid))
    if not active:
        # A deactivated account that is still signed in somewhere is not
        # deactivated.
        con.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))


def orphaned_metrics(con: sqlite3.Connection) -> list[sqlite3.Row]:
    """Live metrics nobody active owns: no owner at all, or an owner who has
    been deactivated. Both mean the same thing in practice - the sweeps only
    nudge active people - so the screens that warn about one warn about both.
    Carries the section (and whether it is shown) and the former owner's name."""
    return con.execute(
        """SELECT m.id, m.name, s.name AS section, s.is_enabled,
                  u.display_name AS former_owner
           FROM metrics m JOIN sections s ON s.id = m.section_id
           LEFT JOIN users u ON u.id = m.dri_user_id
           WHERE m.archived_at IS NULL
             AND (m.dri_user_id IS NULL OR u.is_active IS NOT 1)
           ORDER BY s.sort_order, m.sort_order, m.id""").fetchall()


def hand_over(con: sqlite3.Connection, from_uid: int, to_uid: int) -> int:
    """Move every live metric one person owns to another; archived rows keep
    the owner they had. Returns how many moved."""
    return con.execute(
        "UPDATE metrics SET dri_user_id = ? WHERE dri_user_id = ? AND archived_at IS NULL",
        (to_uid, from_uid)).rowcount


def clean_name(name: str) -> str:
    return " ".join(name.split())


def name_taken(con: sqlite3.Connection, section_id: int, name: str,
               except_id: Optional[int] = None) -> bool:
    """Two live rows with one name are two tiles on the TV for one client."""
    return con.execute(
        "SELECT 1 FROM metrics WHERE section_id = ? AND archived_at IS NULL "
        "AND lower(name) = lower(?) AND id IS NOT ?",
        (section_id, name, except_id)).fetchone() is not None


def create_metric(con: sqlite3.Connection, section_id: int, name: str,
                  metric_type: str, rollup: Optional[str], direction: str,
                  unit: Optional[str], dri_user_id: Optional[int],
                  start_week: date) -> int:
    name = clean_name(name)
    if name_taken(con, section_id, name):
        raise Conflict(f"{name} already exists in this section")
    mx = con.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM metrics "
                     "WHERE section_id = ?", (section_id,)).fetchone()["n"]
    cur = con.execute(
        """INSERT INTO metrics (section_id, name, metric_type, rollup, direction, unit,
                                dri_user_id, start_week, sort_order)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (section_id, name, metric_type,
         (rollup or "sum") if metric_type == "numeric" else None,
         direction, unit or None, dri_user_id, start_week.isoformat(), mx))
    return cur.lastrowid


def save_target(con: sqlite3.Connection, metric_id: int, year: int, quarter: int,
                baseline: float, stretch: float) -> bool:
    """Upsert one quarter's target PAIR. Returns False when it was already
    exactly that, so a re-sent page (or a re-run automation) changes nothing."""
    cur = con.execute(
        "SELECT baseline_value, stretch_value FROM targets "
        "WHERE metric_id=? AND year=? AND quarter=?", (metric_id, year, quarter)).fetchone()
    if cur and (cur["baseline_value"], cur["stretch_value"]) == (baseline, stretch):
        return False
    con.execute(
        """INSERT INTO targets (metric_id, year, quarter, baseline_value, stretch_value)
           VALUES (?,?,?,?,?)
           ON CONFLICT(metric_id, year, quarter) DO UPDATE SET
             baseline_value = excluded.baseline_value,
             stretch_value = excluded.stretch_value""",
        (metric_id, year, quarter, baseline, stretch))
    return True
