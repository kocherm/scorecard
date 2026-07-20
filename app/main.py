"""Aprendio Scorecard - FastAPI app: TV display, edit grid, admin, API, scheduler."""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from functools import partial
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from migrate import add_admin_scope, slack_two_way

from . import (alerts, channels, db as dbm, demo, entry_ops, grid as gridm,
               weeks as wk)
from .api import router as api_router
from .inbound import router as inbound_router
from .slack import router as slack_router
from .auth import (SESSION_COOKIE, consume_magic_link, create_magic_link,
                   create_session, destroy_session, hash_password, new_api_token,
                   require_admin, require_editor, require_viewer, session_hash,
                   user_from_request, verify_password)
from .db import db_dep

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("scorecard")

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
templates.env.filters["qlabel"] = lambda w: wk.quarter_label(
    w if isinstance(w, date) else wk.parse_week(w))
# Cache-buster: changes whenever the stylesheet changes, so browsers never
# serve a stale scorecard.css after a deploy.
templates.env.globals["static_v"] = str(int(
    (BASE / "static" / "scorecard.css").stat().st_mtime))

scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    with dbm.get_db() as con:
        dbm.init_db(con)
        try:  # migration for DBs created before the is_key column existed
            con.execute("ALTER TABLE metrics ADD COLUMN is_key INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # migration for DBs created before api_tokens.scope allowed 'admin'
        if add_admin_scope.needs_migration(con):
            add_admin_scope.migrate(con)
            log.info("Migrated api_tokens.scope to allow 'admin'")
        # additive columns for DBs created before view-as-user / channels
        for ddl in ("ALTER TABLE sessions ADD COLUMN "
                    "impersonate_user_id INTEGER REFERENCES users(id)",
                    "ALTER TABLE users ADD COLUMN notify_channel TEXT",
                    "ALTER TABLE users ADD COLUMN notify_address TEXT"):
            try:
                con.execute(ddl)
            except sqlite3.OperationalError:
                pass
        # migrations for DBs created before two-way Slack existed
        if slack_two_way.needs_entries_migration(con):
            slack_two_way.migrate_entries(con)
            log.info("Migrated entries.source to allow 'slack'")
        if slack_two_way.needs_alerts_migration(con):
            slack_two_way.migrate_alerts(con)
            log.info("Migrated alerts_sent.alert_type to allow nudges")
        if dbm.get_setting(con, "display_token") is None:
            dbm.set_setting(con, "display_token", secrets.token_urlsafe(24))
    scheduler.add_job(alerts.stale_sweep, CronTrigger(
        day_of_week="wed", hour=8, minute=0, timezone="America/Chicago"),
        id="stale_sweep", replace_existing=True)
    scheduler.add_job(alerts.red_sweep, CronTrigger(
        day_of_week="tue", hour=8, minute=0, timezone="America/Chicago"),
        id="red_sweep", replace_existing=True)
    # Check-in nudge DMs. Always registered; enable/preset are checked inside
    # the job (same pattern as alerts_enabled) so settings changes need no
    # rescheduling. Monday 16:00 = "due tonight"; Tuesday 09:00 = last call
    # before the Wednesday 08:00 stale sweep.
    scheduler.add_job(partial(alerts.nudge_sweep, "nudge1"), CronTrigger(
        day_of_week="mon", hour=16, minute=0, timezone="America/Chicago"),
        id="nudge1", replace_existing=True)
    scheduler.add_job(partial(alerts.nudge_sweep, "nudge2"), CronTrigger(
        day_of_week="tue", hour=9, minute=0, timezone="America/Chicago"),
        id="nudge2", replace_existing=True)

    def prune_sessions():
        with dbm.get_db() as con:
            now_iso = datetime.now(timezone.utc).isoformat()
            con.execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso,))
            con.execute("DELETE FROM magic_links WHERE expires_at < ?", (now_iso,))

    scheduler.add_job(prune_sessions, CronTrigger(
        day_of_week="sun", hour=3, minute=0, timezone="America/Chicago"),
        id="prune_sessions", replace_existing=True)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Aprendio Scorecard", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
app.include_router(api_router)
app.include_router(slack_router)
app.include_router(inbound_router)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


# ---------------------------------------------------------------- demo mode
def data_db_dep(con: sqlite3.Connection = Depends(db_dep)):
    """Connection the board surfaces (grid, TV, cells, 1-3-1s) read AND write.
    Normally the real DB; the throwaway demo DB while 'Display Demo Data' is
    on. Auth, admin pages, alerts, and the JSON API always use db_dep - the
    toggle itself lives in the real DB, so real data can never be touched
    through a demo surface.

    Built ON TOP of db_dep so FastAPI's per-request dependency cache hands out
    the SAME real connection the auth guard already used. A second connection
    here would deadlock: the auth guard's last_seen_at update holds the write
    lock until its dependency teardown, which only runs after the response."""
    if dbm.get_setting(con, "display_demo_data", "0") != "1":
        yield con
        return
    months = dbm.get_setting(con, "display_months", "2")
    with demo.demo_db(datetime.now(timezone.utc), months) as dcon:
        yield dcon


def _real_actor(request: Request, user: sqlite3.Row) -> sqlite3.Row:
    """The account actually driving the browser: the impersonating admin when
    view-as is active, else the session user. Audit rows always name them."""
    return getattr(request.state, "impersonator", None) or user


def _data_actor_id(con: sqlite3.Connection, user: sqlite3.Row) -> Optional[int]:
    """Audit attribution that also works while writes land in the demo DB,
    whose users table differs from the real one the session user lives in."""
    if con.execute("SELECT 1 FROM users WHERE id = ?", (user["id"],)).fetchone():
        return user["id"]
    row = con.execute(
        "SELECT id FROM users ORDER BY role = 'admin' DESC, id LIMIT 1").fetchone()
    return row["id"] if row else None


@app.exception_handler(HTTPException)
async def redirect_handler(request: Request, exc: HTTPException):
    if exc.status_code == 303 and exc.headers and "Location" in exc.headers:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    from fastapi.exception_handlers import http_exception_handler
    return await http_exception_handler(request, exc)


# ---------------------------------------------------------------- auth pages
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", error=None)


# Simple in-memory login throttle: 5 failures per identity per 15 minutes.
_login_failures: dict[str, list[float]] = {}
_LOCKOUT_N, _LOCKOUT_WINDOW = 5, 900.0


def _throttled(key: str) -> bool:
    import time
    now = time.monotonic()
    hits = [t for t in _login_failures.get(key, []) if now - t < _LOCKOUT_WINDOW]
    _login_failures[key] = hits
    return len(hits) >= _LOCKOUT_N


def _record_failure(key: str) -> None:
    import time
    _login_failures.setdefault(key, []).append(time.monotonic())


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          con: sqlite3.Connection = Depends(db_dep)):
    key = f"{(request.client.host if request.client else '?')}:{email.strip().lower()}"
    if _throttled(key):
        return render(request, "login.html",
                      error="Too many attempts. Wait 15 minutes and try again.")
    row = con.execute("SELECT * FROM users WHERE email = ? AND is_active = 1",
                      (email.strip(),)).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        _record_failure(key)
        return render(request, "login.html", error="Wrong email or password.")
    _login_failures.pop(key, None)
    token = create_session(con, row["id"])
    dest = "/account" if row["must_change_password"] else "/"
    # DRIs with numbers still missing land straight on the check-in page.
    # Suppressed in demo mode: /checkin would show demo data, and steering
    # someone there to "fix" real numbers would be a lie.
    if (dest == "/" and row["role"] != "viewer"
            and dbm.get_setting(con, "display_demo_data", "0") != "1"
            and entry_ops.missing_due_metrics(con, row["id"],
                                              datetime.now(timezone.utc))):
        dest = "/checkin"
    resp = RedirectResponse(dest, status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                    max_age=30 * 86400, secure=request.url.scheme == "https")
    return resp


@app.post("/logout")
def logout(request: Request, con: sqlite3.Connection = Depends(db_dep)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        destroy_session(con, token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request, user=Depends(require_viewer)):
    return render(request, "account.html", user=user, active="")


@app.post("/account/password")
def change_password(request: Request, current: str = Form(...), new: str = Form(...),
                    user=Depends(require_viewer),
                    con: sqlite3.Connection = Depends(db_dep)):
    if not verify_password(current, user["password_hash"]):
        return render(request, "account.html", user=user, active="",
                      flash="Current password is wrong.", flash_kind="err")
    if len(new) < 10:
        return render(request, "account.html", user=user, active="",
                      flash="New password must be 10+ characters.", flash_kind="err")
    con.execute("UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
                (hash_password(new), user["id"]))
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------- scorecard
@app.get("/", response_class=HTMLResponse)
def grid_page(request: Request, user=Depends(require_viewer),
              con: sqlite3.Connection = Depends(data_db_dep),
              real: sqlite3.Connection = Depends(db_dep)):
    vm = gridm.build_grid(con, datetime.now(timezone.utc))
    return render(request, "grid.html", user=user, vm=vm, active="grid",
                  can_edit=user["role"] in ("editor", "admin"),
                  demo_on=dbm.get_setting(real, "display_demo_data", "0") == "1",
                  display_token=dbm.get_setting(real, "display_token"))


def _metric_or_404(con: sqlite3.Connection, metric_id: int) -> sqlite3.Row:
    m = con.execute("SELECT * FROM metrics WHERE id = ?", (metric_id,)).fetchone()
    if m is None:
        raise HTTPException(404)
    return m


def _render_cell(request: Request, con: sqlite3.Connection, metric_id: int,
                 week: date) -> HTMLResponse:
    """Re-render a single cell after an edit (htmx swap)."""
    vm = gridm.build_grid(con, datetime.now(timezone.utc))
    for s in vm.sections:
        for r in s.rows:
            if r.metric_id == metric_id:
                for c in r.cells:
                    if c.week == week:
                        html = templates.env.from_string(
                            '{% from "_cell.html" import cell_td %}'
                            '{{ cell_td(row, cell, true, last_closed) }}'
                        ).render(row=r, cell=c, last_closed=vm.last_closed)
                        return HTMLResponse(html)
    raise HTTPException(404)


@app.get("/cell/{metric_id}/{week}/edit", response_class=HTMLResponse)
def cell_edit_form(metric_id: int, week: str, request: Request,
                   user=Depends(require_editor),
                   con: sqlite3.Connection = Depends(data_db_dep)):
    m = _metric_or_404(con, metric_id)
    w = wk.parse_week(week)
    e = con.execute("SELECT * FROM entries WHERE metric_id=? AND week_start=?",
                    (metric_id, week)).fetchone()
    current = None
    if e:
        current = e["value_status"] if m["metric_type"] == "status" else e["value_numeric"]
    return render(request, "_cell_form.html", metric=m, week=week, current=current)


@app.post("/cell/{metric_id}/{week}", response_class=HTMLResponse)
def cell_save(metric_id: int, week: str, request: Request, value: str = Form(...),
              user=Depends(require_editor),
              con: sqlite3.Connection = Depends(data_db_dep)):
    m = _metric_or_404(con, metric_id)
    w = wk.parse_week(week)
    if w > wk.current_week(datetime.now(timezone.utc)):
        raise HTTPException(422, "Future week")
    actor = _data_actor_id(con, _real_actor(request, user))
    try:
        entry_ops.save_value(con, m, w, value, source="manual", user_id=actor)
    except ValueError as e:
        raise HTTPException(422, str(e))
    con.commit()
    return _render_cell(request, con, metric_id, w)


# ---------------------------------------------------------------- my numbers
def _checkin_items(con: sqlite3.Connection, uid: Optional[int], now: datetime):
    """The (effective) user's owned metrics as check-in cards: due-week cell,
    current-week cell, missing-first ordering."""
    vm = gridm.build_grid(con, now)
    items = []
    for s in vm.sections:
        for r in s.rows:
            if r.dri_user_id != uid:
                continue
            due = next((c for c in r.cells if c.week == vm.last_closed), None)
            cur = next((c for c in r.cells if c.week == vm.current_week), None)
            items.append({
                "row": r, "section": s.name, "due": due, "cur": cur,
                "due_missing": bool(due and due.raw is None and due.editable),
            })
    items.sort(key=lambda i: not i["due_missing"])
    return vm, items


@app.get("/checkin", response_class=HTMLResponse)
def checkin_page(request: Request, t: str = "",
                 con: sqlite3.Connection = Depends(data_db_dep),
                 real: sqlite3.Connection = Depends(db_dep)):
    """One focused page: enter your own numbers. Reached from the nav, the
    post-login redirect, or a Slack magic link (?t=) that signs the DRI in."""
    user = user_from_request(request, real)
    if user is None:
        if t:
            uid = consume_magic_link(real, t)
            if uid is not None:
                token = create_session(real, uid)
                resp = RedirectResponse("/checkin", status_code=303)  # clean URL
                resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                                max_age=30 * 86400,
                                secure=request.url.scheme == "https")
                return resp
        return RedirectResponse("/login", status_code=303)
    if user["role"] == "viewer":
        raise HTTPException(403, "Viewers have no numbers to enter")
    now = datetime.now(timezone.utc)
    vm, items = _checkin_items(con, _data_actor_id(con, user), now)
    return render(request, "checkin.html", user=user, vm=vm, items=items,
                  active="checkin",
                  missing=sum(1 for i in items if i["due_missing"]),
                  demo_on=dbm.get_setting(real, "display_demo_data", "0") == "1")


@app.post("/checkin/{metric_id}/{week}", response_class=HTMLResponse)
def checkin_save(metric_id: int, week: str, request: Request, value: str = Form(""),
                 user=Depends(require_editor),
                 con: sqlite3.Connection = Depends(data_db_dep)):
    m = _metric_or_404(con, metric_id)
    w = wk.parse_week(week)
    now = datetime.now(timezone.utc)
    if w > wk.current_week(now):
        raise HTTPException(422, "Future week")
    actor = _data_actor_id(con, _real_actor(request, user))
    try:
        entry_ops.save_value(con, m, w, value, source="manual", user_id=actor)
    except ValueError as e:
        raise HTTPException(422, str(e))
    con.commit()
    vm, items = _checkin_items(con, _data_actor_id(con, user), now)
    item = next((i for i in items if i["row"].metric_id == metric_id), None)
    if item is None:
        raise HTTPException(404)
    html = templates.env.from_string(
        '{% from "_checkin_row.html" import checkin_card %}'
        '{{ checkin_card(item, vm) }}').render(item=item, vm=vm)
    return HTMLResponse(html)


# ---------------------------------------------------------------- 1-3-1
@app.get("/131/{metric_id}/{week}", response_class=HTMLResponse)
def one_three_one_page(metric_id: int, week: str, request: Request,
                       user=Depends(require_editor),
                       con: sqlite3.Connection = Depends(data_db_dep)):
    m = _metric_or_404(con, metric_id)
    w = wk.parse_week(week)
    dri = con.execute(
        "SELECT display_name FROM users WHERE id = ?", (m["dri_user_id"],)
    ).fetchone() if m["dri_user_id"] else None
    existing = con.execute(
        """SELECT o.*, u.display_name AS author FROM one_three_ones o
           JOIN users u ON u.id = o.created_by
           WHERE o.metric_id = ? AND o.week_start = ?""",
        (metric_id, week)).fetchone()
    return render(request, "onethreeone.html", user=user, active="grid",
                  metric=m, dri_name=dri["display_name"] if dri else None,
                  week=week, week_date=w, existing=existing,
                  existing_options=json.loads(existing["options_json"]) if existing else [])


@app.post("/131/{metric_id}/{week}")
def one_three_one_save(metric_id: int, week: str, request: Request,
                       problem: str = Form(...), option1: str = Form(...),
                       option2: str = Form(...), option3: str = Form(...),
                       recommendation: str = Form(...),
                       user=Depends(require_editor),
                       con: sqlite3.Connection = Depends(data_db_dep)):
    _metric_or_404(con, metric_id)
    wk.parse_week(week)
    con.execute(
        """INSERT OR IGNORE INTO one_three_ones
           (metric_id, week_start, problem, options_json, recommendation, created_by)
           VALUES (?,?,?,?,?,?)""",
        (metric_id, week, problem, json.dumps([option1, option2, option3]),
         recommendation, _data_actor_id(con, _real_actor(request, user))))
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------- TV display
def _check_display_token(con: sqlite3.Connection, token: str) -> None:
    if not token or token != dbm.get_setting(con, "display_token"):
        raise HTTPException(403, "Bad display token")


@app.get("/tv")
def tv_shortcut(con: sqlite3.Connection = Depends(db_dep)):
    """Typeable shortcut for a TV/kiosk browser: looks up the current display
    token server-side and 302-redirects to /display. 302 (not 301/308) so the
    redirect is never cached and keeps working after the token is rotated."""
    token = dbm.get_setting(con, "display_token") or ""
    return RedirectResponse(f"/display?token={token}", status_code=302)


def _tv_context(con: sqlite3.Connection):
    now = datetime.now(timezone.utc)
    return (gridm.build_tv(con, now),
            now.astimezone(wk.BUSINESS_TZ).strftime("%-I:%M %p"))


@app.get("/display", response_class=HTMLResponse)
def display_page(request: Request, token: str = "",
                 real: sqlite3.Connection = Depends(db_dep),
                 con: sqlite3.Connection = Depends(data_db_dep)):
    _check_display_token(real, token)
    tv, rendered_at = _tv_context(con)
    return render(request, "display.html", tv=tv, token=token,
                  rendered_at=rendered_at)


@app.get("/display/body", response_class=HTMLResponse)
def display_body(request: Request, token: str = "",
                 real: sqlite3.Connection = Depends(db_dep),
                 con: sqlite3.Connection = Depends(data_db_dep)):
    _check_display_token(real, token)
    tv, rendered_at = _tv_context(con)
    html = templates.env.get_template("_display_body.html").render(
        tv=tv, rendered_at=rendered_at)
    return HTMLResponse(f'<div class="board" id="tvroot">{html}</div>')


# ---------------------------------------------------------------- admin
@app.get("/admin", response_class=HTMLResponse)
def admin_root(user=Depends(require_admin)):
    return RedirectResponse("/admin/metrics", status_code=303)


@app.get("/admin/metrics", response_class=HTMLResponse)
def admin_metrics(request: Request, user=Depends(require_admin),
                  con: sqlite3.Connection = Depends(db_dep)):
    sections = []
    for s in con.execute("SELECT * FROM sections ORDER BY sort_order, id"):
        metrics = con.execute(
            """SELECT m.*, u.display_name AS dri_name FROM metrics m
               LEFT JOIN users u ON u.id = m.dri_user_id
               WHERE m.section_id = ? ORDER BY m.sort_order, m.id""", (s["id"],)).fetchall()
        sections.append({**dict(s), "metrics": metrics})
    users = con.execute("SELECT * FROM users WHERE is_active = 1 ORDER BY display_name").fetchall()
    return render(request, "admin_metrics.html", user=user, active="metrics",
                  sections=sections, users=users)


@app.post("/admin/sections")
def add_section(name: str = Form(...), icon: str = Form("chart"),
                user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    mx = con.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM sections").fetchone()["n"]
    con.execute("INSERT INTO sections (name, icon, sort_order) VALUES (?,?,?)", (name, icon, mx))
    return RedirectResponse("/admin/metrics", status_code=303)


@app.post("/admin/sections/{section_id}/toggle")
def toggle_section(section_id: int, user=Depends(require_admin),
                   con: sqlite3.Connection = Depends(db_dep)):
    con.execute("UPDATE sections SET is_enabled = 1 - is_enabled WHERE id = ?", (section_id,))
    return RedirectResponse("/admin/metrics", status_code=303)


@app.post("/admin/metrics")
def add_metric(section_id: int = Form(...), name: str = Form(...),
               metric_type: str = Form(...), rollup: str = Form("sum"),
               unit: str = Form(""), direction: str = Form("up"),
               dri_user_id: str = Form(""),
               user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    start = wk.current_week(datetime.now(timezone.utc))
    mx = con.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM metrics WHERE section_id = ?",
                     (section_id,)).fetchone()["n"]
    con.execute(
        """INSERT INTO metrics (section_id, name, metric_type, rollup, direction, unit,
                                dri_user_id, start_week, sort_order)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (section_id, name, metric_type,
         rollup if metric_type == "numeric" else None,
         direction, unit or None,
         int(dri_user_id) if dri_user_id else None, start.isoformat(), mx))
    return RedirectResponse("/admin/metrics", status_code=303)


@app.get("/admin/metrics/{metric_id}", response_class=HTMLResponse)
def edit_metric_page(metric_id: int, request: Request, user=Depends(require_admin),
                     con: sqlite3.Connection = Depends(db_dep)):
    m = _metric_or_404(con, metric_id)
    section = con.execute("SELECT * FROM sections WHERE id = ?", (m["section_id"],)).fetchone()
    sections = con.execute("SELECT * FROM sections ORDER BY sort_order").fetchall()
    users = con.execute("SELECT * FROM users WHERE is_active = 1 ORDER BY display_name").fetchall()
    return render(request, "admin_metric_edit.html", user=user, active="metrics",
                  m=m, section=section, sections=sections, users=users)


@app.post("/admin/metrics/{metric_id}")
def edit_metric(metric_id: int, section_id: int = Form(...), name: str = Form(...),
                metric_type: str = Form(...), rollup: str = Form("sum"),
                unit: str = Form(""), direction: str = Form("up"),
                dri_user_id: str = Form(""), start_week: str = Form(...),
                sort_order: int = Form(0), is_key: str = Form(""),
                user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    _metric_or_404(con, metric_id)
    start = wk.monday_of(date.fromisoformat(start_week)).isoformat()
    con.execute(
        """UPDATE metrics SET section_id=?, name=?, metric_type=?, rollup=?, direction=?,
                              unit=?, dri_user_id=?, start_week=?, sort_order=?, is_key=?
           WHERE id=?""",
        (section_id, name, metric_type,
         rollup if metric_type == "numeric" else None,
         direction, unit or None,
         int(dri_user_id) if dri_user_id else None, start, sort_order,
         1 if is_key else 0, metric_id))
    return RedirectResponse("/admin/metrics", status_code=303)


@app.post("/admin/metrics/{metric_id}/archive")
def archive_metric(metric_id: int, user=Depends(require_admin),
                   con: sqlite3.Connection = Depends(db_dep)):
    con.execute("UPDATE metrics SET archived_at = datetime('now') WHERE id = ?", (metric_id,))
    return RedirectResponse("/admin/metrics", status_code=303)


@app.post("/admin/metrics/{metric_id}/unarchive")
def unarchive_metric(metric_id: int, user=Depends(require_admin),
                     con: sqlite3.Connection = Depends(db_dep)):
    con.execute("UPDATE metrics SET archived_at = NULL WHERE id = ?", (metric_id,))
    return RedirectResponse("/admin/metrics", status_code=303)


# ---------------- targets
@app.get("/admin/targets", response_class=HTMLResponse)
def admin_targets(request: Request, year: Optional[int] = None, quarter: Optional[int] = None,
                  user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    now_w = wk.current_week(datetime.now(timezone.utc))
    y, q = wk.quarter_of(now_w)
    year = year or y
    quarter = quarter or q
    metrics = con.execute(
        """SELECT m.*, s.name AS section_name, u.display_name AS dri_name
           FROM metrics m
           JOIN sections s ON s.id = m.section_id
           LEFT JOIN users u ON u.id = m.dri_user_id
           WHERE m.archived_at IS NULL AND m.metric_type = 'numeric'
           ORDER BY s.sort_order, m.sort_order""").fetchall()
    rows = []
    missing = 0
    for m in metrics:
        t = con.execute("SELECT * FROM targets WHERE metric_id=? AND year=? AND quarter=?",
                        (m["id"], year, quarter)).fetchone()
        if t is None:
            missing += 1
        rows.append(type("R", (), {"m": m, "t": t})())
    return render(request, "admin_targets.html", user=user, active="targets",
                  rows=rows, year=year, quarter=quarter,
                  missing=missing if missing else None)


@app.post("/admin/targets/{metric_id}")
def save_target(metric_id: int, year: int, quarter: int,
                baseline: float = Form(...), stretch: float = Form(...),
                user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    con.execute(
        """INSERT INTO targets (metric_id, year, quarter, baseline_value, stretch_value)
           VALUES (?,?,?,?,?)
           ON CONFLICT(metric_id, year, quarter) DO UPDATE SET
             baseline_value = excluded.baseline_value,
             stretch_value = excluded.stretch_value""",
        (metric_id, year, quarter, baseline, stretch))
    return RedirectResponse(f"/admin/targets?year={year}&quarter={quarter}", status_code=303)


# ---------------- users
@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(request: Request, user=Depends(require_admin),
                con: sqlite3.Connection = Depends(db_dep)):
    users = con.execute("SELECT * FROM users ORDER BY display_name").fetchall()
    return render(request, "admin_users.html", user=user, active="users",
                  users=users, temp_password=None, temp_user=None)


def _temp_password() -> str:
    return secrets.token_urlsafe(9)


@app.post("/admin/users", response_class=HTMLResponse)
def add_user(request: Request, display_name: str = Form(...), email: str = Form(...),
             role: str = Form("editor"),
             user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    pw = _temp_password()
    try:
        con.execute(
            """INSERT INTO users (email, password_hash, display_name, role, must_change_password)
               VALUES (?,?,?,?,1)""",
            (email.strip(), hash_password(pw), display_name.strip(), role))
    except sqlite3.IntegrityError:
        users = con.execute("SELECT * FROM users ORDER BY display_name").fetchall()
        return render(request, "admin_users.html", user=user, active="users", users=users,
                      temp_password=None, temp_user=None,
                      flash=f"{email} already exists.", flash_kind="err")
    users = con.execute("SELECT * FROM users ORDER BY display_name").fetchall()
    return render(request, "admin_users.html", user=user, active="users",
                  users=users, temp_password=pw, temp_user=display_name)


@app.post("/admin/users/{uid}/role")
def set_role(uid: int, role: str = Form(...), user=Depends(require_admin),
             con: sqlite3.Connection = Depends(db_dep)):
    if uid == user["id"]:
        raise HTTPException(400, "Cannot change your own role")
    con.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{uid}/notify")
def set_notify(uid: int, notify_channel: str = Form("slack"), address: str = Form(""),
               user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    """One control per user: which channel nudges use and its address.
    Slack keeps its dedicated column; the rest share notify_address
    (Teams/Google Chat post to a shared webhook, so no address needed)."""
    if notify_channel not in channels.CHANNELS:
        raise HTTPException(422, "Unknown channel")
    addr = address.strip() or None
    if notify_channel == "slack":
        con.execute("UPDATE users SET notify_channel = 'slack', slack_member_id = ? "
                    "WHERE id = ?", (addr, uid))
    else:
        con.execute("UPDATE users SET notify_channel = ?, notify_address = ? "
                    "WHERE id = ?", (notify_channel, addr, uid))
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{uid}/toggle")
def toggle_user(uid: int, user=Depends(require_admin),
                con: sqlite3.Connection = Depends(db_dep)):
    if uid == user["id"]:
        raise HTTPException(400, "Cannot deactivate yourself")
    con.execute("UPDATE users SET is_active = 1 - is_active WHERE id = ?", (uid,))
    con.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{uid}/impersonate")
def impersonate_start(uid: int, request: Request, user=Depends(require_admin),
                      con: sqlite3.Connection = Depends(db_dep)):
    """View as user: this session renders as the target until exited. The
    session row keeps the admin as user_id; audit stays on the real admin."""
    target = con.execute(
        "SELECT id FROM users WHERE id = ? AND is_active = 1", (uid,)).fetchone()
    if target is None:
        raise HTTPException(404)
    con.execute("UPDATE sessions SET impersonate_user_id = ? WHERE token_hash = ?",
                (uid, session_hash(request)))
    return RedirectResponse("/", status_code=303)


@app.post("/impersonate/stop")
def impersonate_stop(request: Request, con: sqlite3.Connection = Depends(db_dep)):
    # No role guard: the effective user may be a viewer; the real admin must
    # always be able to exit. Clearing on a non-impersonating session is a no-op.
    con.execute("UPDATE sessions SET impersonate_user_id = NULL WHERE token_hash = ?",
                (session_hash(request),))
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{uid}/reset", response_class=HTMLResponse)
def reset_password(uid: int, request: Request, user=Depends(require_admin),
                   con: sqlite3.Connection = Depends(db_dep)):
    target = con.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if target is None:
        raise HTTPException(404)
    pw = _temp_password()
    con.execute("UPDATE users SET password_hash = ?, must_change_password = 1 WHERE id = ?",
                (hash_password(pw), uid))
    con.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
    users = con.execute("SELECT * FROM users ORDER BY display_name").fetchall()
    return render(request, "admin_users.html", user=user, active="users",
                  users=users, temp_password=pw, temp_user=target["display_name"])


# ---------------- API tokens
@app.get("/admin/tokens", response_class=HTMLResponse)
def admin_tokens(request: Request, user=Depends(require_admin),
                 con: sqlite3.Connection = Depends(db_dep)):
    tokens = con.execute("SELECT * FROM api_tokens ORDER BY created_at DESC").fetchall()
    return render(request, "admin_tokens.html", user=user, active="tokens",
                  tokens=tokens, new_token=None, new_name=None)


@app.post("/admin/tokens", response_class=HTMLResponse)
def create_token(request: Request, name: str = Form(...), scope: str = Form("read_write"),
                 user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    raw = new_api_token(con, name.strip(), scope, user["id"])
    tokens = con.execute("SELECT * FROM api_tokens ORDER BY created_at DESC").fetchall()
    return render(request, "admin_tokens.html", user=user, active="tokens",
                  tokens=tokens, new_token=raw, new_name=name)


@app.post("/admin/tokens/{tid}/revoke")
def revoke_token(tid: int, user=Depends(require_admin),
                 con: sqlite3.Connection = Depends(db_dep)):
    con.execute("UPDATE api_tokens SET revoked_at = datetime('now') WHERE id = ?", (tid,))
    return RedirectResponse("/admin/tokens", status_code=303)


# ---------------- settings
@app.get("/admin/settings", response_class=HTMLResponse)
def admin_settings(request: Request, user=Depends(require_admin),
                   con: sqlite3.Connection = Depends(db_dep)):
    goal_metrics = con.execute(
        """SELECT m.id, m.name, s.name AS section FROM metrics m
           JOIN sections s ON s.id = m.section_id
           WHERE m.archived_at IS NULL AND m.metric_type = 'numeric'
           ORDER BY s.sort_order, m.sort_order""").fetchall()
    return render(request, "admin_settings.html", user=user, active="settings",
                  display_token=dbm.get_setting(con, "display_token"),
                  slack_webhook_url=dbm.get_setting(con, "slack_webhook_url") or "",
                  slack_bot_token=dbm.get_setting(con, "slack_bot_token") or "",
                  slack_channel_id=dbm.get_setting(con, "slack_channel_id") or "",
                  alerts_enabled=dbm.get_setting(con, "alerts_enabled", "0") == "1",
                  demo_enabled=dbm.get_setting(con, "display_demo_data", "0") == "1",
                  display_months=int(dbm.get_setting(con, "display_months", "2")),
                  slack_signing_secret=dbm.get_setting(con, "slack_signing_secret") or "",
                  nudges_enabled=dbm.get_setting(con, "nudges_enabled", "0") == "1",
                  nudge_preset=dbm.get_setting(con, "nudge_preset", "mon_tue"),
                  public_base_url=dbm.get_setting(con, "public_base_url") or "",
                  channel_settings={k: dbm.get_setting(con, k) or ""
                                    for k in _CHANNEL_SETTING_KEYS},
                  goal_metrics=goal_metrics,
                  hud_mrr_metric_id=dbm.get_setting(con, "hud_mrr_metric_id") or "",
                  mrr_goal=dbm.get_setting(con, "mrr_goal") or "",
                  mrr_milestones=dbm.get_setting(con, "mrr_milestones") or "",
                  base_url=str(request.base_url).rstrip("/"))


@app.post("/admin/settings/goal-band")
def save_goal_band(hud_mrr_metric_id: str = Form(""), mrr_goal: str = Form(""),
                   mrr_milestones: str = Form(""), user=Depends(require_admin),
                   con: sqlite3.Connection = Depends(db_dep)):
    dbm.set_setting(con, "hud_mrr_metric_id", hud_mrr_metric_id.strip())
    dbm.set_setting(con, "mrr_goal", mrr_goal.strip())
    dbm.set_setting(con, "mrr_milestones", mrr_milestones.strip())
    return RedirectResponse("/admin/settings", status_code=303)


@app.post("/admin/settings/display-months")
def save_display_months(display_months: int = Form(...), user=Depends(require_admin),
                        con: sqlite3.Connection = Depends(db_dep)):
    dbm.set_setting(con, "display_months", str(max(1, min(4, display_months))))
    return RedirectResponse("/admin/settings", status_code=303)


@app.post("/admin/settings/rotate-display-token")
def rotate_display_token(user=Depends(require_admin),
                         con: sqlite3.Connection = Depends(db_dep)):
    dbm.set_setting(con, "display_token", secrets.token_urlsafe(24))
    return RedirectResponse("/admin/settings", status_code=303)


@app.post("/admin/settings/demo-toggle")
def demo_toggle(user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    turning_on = dbm.get_setting(con, "display_demo_data", "0") != "1"
    dbm.set_setting(con, "display_demo_data", "1" if turning_on else "0")
    if turning_on:
        demo.reset()  # fresh fictional data every time it is switched on
    return RedirectResponse("/admin/settings", status_code=303)


@app.post("/admin/settings/alerts-toggle")
def alerts_toggle(user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    cur = dbm.get_setting(con, "alerts_enabled", "0")
    dbm.set_setting(con, "alerts_enabled", "0" if cur == "1" else "1")
    return RedirectResponse("/admin/settings", status_code=303)


def _save_slack_settings(con, webhook: str, bot: str, channel: str,
                         signing: str) -> None:
    dbm.set_setting(con, "slack_webhook_url", webhook.strip())
    dbm.set_setting(con, "slack_bot_token", bot.strip())
    dbm.set_setting(con, "slack_channel_id", channel.strip())
    dbm.set_setting(con, "slack_signing_secret", signing.strip())


@app.post("/admin/settings/slack")
def save_slack(slack_webhook_url: str = Form(""), slack_bot_token: str = Form(""),
               slack_channel_id: str = Form(""), slack_signing_secret: str = Form(""),
               user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    _save_slack_settings(con, slack_webhook_url, slack_bot_token, slack_channel_id,
                         slack_signing_secret)
    return RedirectResponse("/admin/settings", status_code=303)


@app.post("/admin/settings/nudges")
def save_nudges(public_base_url: str = Form(""), nudge_preset: str = Form("mon_tue"),
                user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    dbm.set_setting(con, "public_base_url", public_base_url.strip().rstrip("/"))
    if nudge_preset in ("mon_tue", "mon", "tue"):
        dbm.set_setting(con, "nudge_preset", nudge_preset)
    return RedirectResponse("/admin/settings", status_code=303)


_CHANNEL_SETTING_KEYS = ("teams_webhook_url", "gchat_webhook_url",
                         "twilio_account_sid", "twilio_auth_token",
                         "twilio_from", "telegram_bot_token")


def _save_channel_settings(con: sqlite3.Connection, form: dict[str, str]) -> None:
    for key in _CHANNEL_SETTING_KEYS:
        dbm.set_setting(con, key, (form.get(key) or "").strip())


@app.post("/admin/settings/channels")
async def save_channels(request: Request, user=Depends(require_admin),
                        con: sqlite3.Connection = Depends(db_dep)):
    form = {k: str(v) for k, v in (await request.form()).items()}
    _save_channel_settings(con, form)
    return RedirectResponse("/admin/settings", status_code=303)


@app.post("/admin/settings/telegram-register")
async def telegram_register(request: Request, user=Depends(require_admin),
                            con: sqlite3.Connection = Depends(db_dep)):
    """Save the channel settings, then point the Telegram bot's webhook at
    this server (with a generated secret token) so typed replies work."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    _save_channel_settings(con, form)
    token = (form.get("telegram_bot_token") or "").strip()
    base = (dbm.get_setting(con, "public_base_url")
            or str(request.base_url).rstrip("/"))
    secret = dbm.get_setting(con, "telegram_webhook_secret")
    if not secret:
        secret = secrets.token_urlsafe(24)
        dbm.set_setting(con, "telegram_webhook_secret", secret)
    con.commit()
    if token:
        try:
            import httpx
            r = httpx.post(f"https://api.telegram.org/bot{token}/setWebhook",
                           json={"url": f"{base}/telegram/webhook",
                                 "secret_token": secret,
                                 "allowed_updates": ["message"]}, timeout=10)
            log.info("telegram setWebhook: %s %s", r.status_code, r.text[:200])
        except Exception as e:  # network failure must not 500 the settings page
            log.warning("telegram setWebhook failed: %s", e)
    return RedirectResponse("/admin/settings", status_code=303)


@app.post("/admin/settings/nudges-toggle")
def nudges_toggle(user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    cur = dbm.get_setting(con, "nudges_enabled", "0")
    dbm.set_setting(con, "nudges_enabled", "0" if cur == "1" else "1")
    return RedirectResponse("/admin/settings", status_code=303)


@app.post("/admin/settings/nudge-test")
def nudge_test(request: Request, user=Depends(require_admin),
               con: sqlite3.Connection = Depends(db_dep)):
    """Message the current admin their own nudge, over their chosen channel,
    exactly as DRIs will get it. No alerts_sent rows, so it can be re-sent
    any number of times."""
    base = (dbm.get_setting(con, "public_base_url")
            or str(request.base_url).rstrip("/"))
    real = _real_actor(request, user)
    if channels.ready(con, real):
        now = datetime.now(timezone.utc)
        if not alerts.compose_and_send_nudge(con, real, base, now):
            alerts.send_direct(con, real,
                               "Test nudge: all your numbers are entered - "
                               "nothing due right now.")
    return RedirectResponse("/admin/settings", status_code=303)


@app.post("/admin/settings/slack-test")
def slack_test(slack_webhook_url: str = Form(""), slack_bot_token: str = Form(""),
               slack_channel_id: str = Form(""), slack_signing_secret: str = Form(""),
               user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    _save_slack_settings(con, slack_webhook_url, slack_bot_token, slack_channel_id,
                         slack_signing_secret)
    con.commit()
    msg = "Aprendio Scorecard: test message. Alerts are wired up."
    if slack_webhook_url.strip():
        alerts.post_channel(slack_webhook_url.strip(), msg)
    elif slack_bot_token.strip() and slack_channel_id.strip():
        alerts.post_channel_bot(slack_bot_token.strip(), slack_channel_id.strip(), msg)
    return RedirectResponse("/admin/settings", status_code=303)
