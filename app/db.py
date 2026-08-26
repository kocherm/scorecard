"""SQLite data layer. One connection per request via FastAPI dependency."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator, Optional

DB_PATH = os.environ.get("SCORECARD_DB", str(Path(__file__).parent.parent / "data" / "scorecard.db"))
SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False because a sync generator dependency does not get
    # to choose its threads: Starlette runs the `yield` and the teardown as two
    # separate to_thread calls, so under any real concurrency the connection is
    # CLOSED on a different worker than opened it and sqlite3 raises
    # "SQLite objects created in a thread can only be used in that same
    # thread" - from inside the teardown, where it surfaces as a bare 500 with
    # the real request already half-served. With one client (the TV, polling
    # alone) the pool hands back the same thread and it never shows; two
    # clients at once and /display fails ~99% of the time.
    #
    # This is safe here and is not a licence to share connections: db_dep opens
    # one per REQUEST and hands it to exactly one request, so the two threads
    # touch it strictly one after the other, never at the same time. Anything
    # that wants a connection on its own schedule - the sweeps, the migrations
    # - still calls connect() and gets its own.
    con = sqlite3.connect(p, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA busy_timeout = 5000")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA_PATH.read_text())
    con.commit()


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def db_dep() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency."""
    with get_db() as con:
        yield con


def get_setting(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def upsert_entry(
    con: sqlite3.Connection,
    metric_id: int,
    week_start: date,
    *,
    value_numeric: Optional[float] = None,
    value_status: Optional[str] = None,
    source: str,
    user_id: Optional[int] = None,
    token_id: Optional[int] = None,
) -> None:
    """One number per metric per week; every write is audited."""
    wk = week_start.isoformat()
    old = con.execute(
        "SELECT value_numeric, value_status FROM entries WHERE metric_id=? AND week_start=?",
        (metric_id, wk),
    ).fetchone()
    con.execute(
        """INSERT INTO entries (metric_id, week_start, value_numeric, value_status,
                                source, entered_by_user_id, entered_by_token_id)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(metric_id, week_start) DO UPDATE SET
             value_numeric = excluded.value_numeric,
             value_status = excluded.value_status,
             source = excluded.source,
             entered_by_user_id = excluded.entered_by_user_id,
             entered_by_token_id = excluded.entered_by_token_id,
             updated_at = datetime('now')""",
        (metric_id, wk, value_numeric, value_status, source, user_id, token_id),
    )
    con.execute(
        """INSERT INTO entry_audit (metric_id, week_start, old_numeric, old_status,
                                    new_numeric, new_status, source, actor_user_id, actor_token_id)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (metric_id, wk,
         old["value_numeric"] if old else None, old["value_status"] if old else None,
         value_numeric, value_status, source, user_id, token_id),
    )


def delete_entry(
    con: sqlite3.Connection,
    metric_id: int,
    week_start: date,
    *,
    user_id: Optional[int] = None,
) -> bool:
    wk = week_start.isoformat()
    old = con.execute(
        "SELECT value_numeric, value_status FROM entries WHERE metric_id=? AND week_start=?",
        (metric_id, wk),
    ).fetchone()
    if not old:
        return False
    con.execute("DELETE FROM entries WHERE metric_id=? AND week_start=?", (metric_id, wk))
    con.execute(
        """INSERT INTO entry_audit (metric_id, week_start, old_numeric, old_status,
                                    new_numeric, new_status, source, actor_user_id)
           VALUES (?,?,?,?,NULL,NULL,'manual',?)""",
        (metric_id, wk, old["value_numeric"], old["value_status"], user_id),
    )
    return True
