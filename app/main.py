"""Aprendio Scorecard - FastAPI app: TV display, edit grid, admin, API, scheduler."""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import alerts, db as dbm, grid as gridm, weeks as wk
from .api import router as api_router
from .auth import (SESSION_COOKIE, create_session, destroy_session, hash_password,
                   new_api_token, require_admin, require_editor, require_viewer,
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
        if dbm.get_setting(con, "display_token") is None:
            dbm.set_setting(con, "display_token", secrets.token_urlsafe(24))
    scheduler.add_job(alerts.stale_sweep, CronTrigger(
        day_of_week="wed", hour=8, minute=0, timezone="America/Chicago"),
        id="stale_sweep", replace_existing=True)
    scheduler.add_job(alerts.red_sweep, CronTrigger(
        day_of_week="tue", hour=8, minute=0, timezone="America/Chicago"),
        id="red_sweep", replace_existing=True)
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title="Aprendio Scorecard", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
app.include_router(api_router)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


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


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
          con: sqlite3.Connection = Depends(db_dep)):
    row = con.execute("SELECT * FROM users WHERE email = ? AND is_active = 1",
                      (email.strip(),)).fetchone()
    if row is None or not verify_password(password, row["password_hash"]):
        return render(request, "login.html", error="Wrong email or password.")
    token = create_session(con, row["id"])
    dest = "/account" if row["must_change_password"] else "/"
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
              con: sqlite3.Connection = Depends(db_dep)):
    vm = gridm.build_grid(con, datetime.now(timezone.utc))
    return render(request, "grid.html", user=user, vm=vm, active="grid",
                  can_edit=user["role"] in ("editor", "admin"),
                  display_token=dbm.get_setting(con, "display_token"))


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
                   con: sqlite3.Connection = Depends(db_dep)):
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
              con: sqlite3.Connection = Depends(db_dep)):
    m = _metric_or_404(con, metric_id)
    w = wk.parse_week(week)
    if w > wk.current_week(datetime.now(timezone.utc)):
        raise HTTPException(422, "Future week")
    if m["metric_type"] == "status":
        if value not in ("R", "Y", "G"):
            raise HTTPException(422)
        dbm.upsert_entry(con, metric_id, w, value_status=value,
                         source="manual", user_id=user["id"])
    else:
        if value.strip() == "":
            dbm.delete_entry(con, metric_id, w, user_id=user["id"])
        else:
            v = float(value)
            if m["metric_type"] == "binary":
                v = 1.0 if v else 0.0
            dbm.upsert_entry(con, metric_id, w, value_numeric=v,
                             source="manual", user_id=user["id"])
    con.commit()
    return _render_cell(request, con, metric_id, w)


# ---------------------------------------------------------------- 1-3-1
@app.get("/131/{metric_id}/{week}", response_class=HTMLResponse)
def one_three_one_page(metric_id: int, week: str, request: Request,
                       user=Depends(require_editor),
                       con: sqlite3.Connection = Depends(db_dep)):
    m = _metric_or_404(con, metric_id)
    w = wk.parse_week(week)
    existing = con.execute(
        """SELECT o.*, u.display_name AS author FROM one_three_ones o
           JOIN users u ON u.id = o.created_by
           WHERE o.metric_id = ? AND o.week_start = ?""",
        (metric_id, week)).fetchone()
    return render(request, "onethreeone.html", user=user, active="grid",
                  metric=m, week=week, week_date=w, existing=existing,
                  existing_options=json.loads(existing["options_json"]) if existing else [])


@app.post("/131/{metric_id}/{week}")
def one_three_one_save(metric_id: int, week: str, request: Request,
                       problem: str = Form(...), option1: str = Form(...),
                       option2: str = Form(...), option3: str = Form(...),
                       recommendation: str = Form(...),
                       user=Depends(require_editor),
                       con: sqlite3.Connection = Depends(db_dep)):
    _metric_or_404(con, metric_id)
    wk.parse_week(week)
    con.execute(
        """INSERT OR IGNORE INTO one_three_ones
           (metric_id, week_start, problem, options_json, recommendation, created_by)
           VALUES (?,?,?,?,?,?)""",
        (metric_id, week, problem, json.dumps([option1, option2, option3]),
         recommendation, user["id"]))
    return RedirectResponse("/", status_code=303)


# ---------------------------------------------------------------- TV display
def _check_display_token(con: sqlite3.Connection, token: str) -> None:
    if not token or token != dbm.get_setting(con, "display_token"):
        raise HTTPException(403, "Bad display token")


DISPLAY_VIEWS = [("hybrid", "Hybrid board"), ("wall", "Status wall"),
                 ("strip", "Now strip"), ("briefing", "Action briefing"),
                 ("hud", "Founder HUD")]


def _enabled_views(con: sqlite3.Connection) -> list[str]:
    raw = dbm.get_setting(con, "display_views", "hybrid,wall,strip,briefing,hud")
    valid = {v for v, _ in DISPLAY_VIEWS}
    views = [v.strip() for v in raw.split(",") if v.strip() in valid]
    return views or ["hybrid"]


def _tv_for(con: sqlite3.Connection, view: str):
    now = datetime.now(timezone.utc)
    enabled = _enabled_views(con)
    if view not in enabled:
        view = enabled[0]
    tv = gridm.build_tv(con, now)
    tv.view = view
    if len(enabled) > 1:
        i = enabled.index(view)
        tv.prev_view = enabled[(i - 1) % len(enabled)]
        tv.next_view = enabled[(i + 1) % len(enabled)]
    return tv, now


@app.get("/display", response_class=HTMLResponse)
def display_page(request: Request, token: str = "", view: str = "",
                 con: sqlite3.Connection = Depends(db_dep)):
    _check_display_token(con, token)
    tv, now = _tv_for(con, view)
    return render(request, "display.html", tv=tv, token=token,
                  rendered_at=now.astimezone(wk.BUSINESS_TZ).strftime("%-I:%M %p"))


@app.get("/display/body", response_class=HTMLResponse)
def display_body(request: Request, token: str = "", view: str = "",
                 con: sqlite3.Connection = Depends(db_dep)):
    _check_display_token(con, token)
    tv, now = _tv_for(con, view)
    html = templates.env.get_template("_display_body.html").render(
        tv=tv, rendered_at=now.astimezone(wk.BUSINESS_TZ).strftime("%-I:%M %p"))
    return HTMLResponse(f'<main class="page" id="tvroot">{html}</main>')


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
        """SELECT m.*, s.name AS section_name FROM metrics m
           JOIN sections s ON s.id = m.section_id
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


@app.post("/admin/users/{uid}/slack")
def set_slack(uid: int, slack_member_id: str = Form(""), user=Depends(require_admin),
              con: sqlite3.Connection = Depends(db_dep)):
    con.execute("UPDATE users SET slack_member_id = ? WHERE id = ?",
                (slack_member_id.strip() or None, uid))
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{uid}/toggle")
def toggle_user(uid: int, user=Depends(require_admin),
                con: sqlite3.Connection = Depends(db_dep)):
    if uid == user["id"]:
        raise HTTPException(400, "Cannot deactivate yourself")
    con.execute("UPDATE users SET is_active = 1 - is_active WHERE id = ?", (uid,))
    con.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
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
    return render(request, "admin_settings.html", user=user, active="settings",
                  display_token=dbm.get_setting(con, "display_token"),
                  slack_webhook_url=dbm.get_setting(con, "slack_webhook_url") or "",
                  slack_bot_token=dbm.get_setting(con, "slack_bot_token") or "",
                  slack_channel_id=dbm.get_setting(con, "slack_channel_id") or "",
                  alerts_enabled=dbm.get_setting(con, "alerts_enabled", "0") == "1",
                  display_months=int(dbm.get_setting(con, "display_months", "2")),
                  all_views=DISPLAY_VIEWS, enabled_views=_enabled_views(con),
                  base_url=str(request.base_url).rstrip("/"))


@app.post("/admin/settings/display-views")
def save_display_views(views: list[str] = Form([]), user=Depends(require_admin),
                       con: sqlite3.Connection = Depends(db_dep)):
    valid = {v for v, _ in DISPLAY_VIEWS}
    chosen = [v for v in views if v in valid] or ["hybrid"]
    dbm.set_setting(con, "display_views", ",".join(chosen))
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


@app.post("/admin/settings/alerts-toggle")
def alerts_toggle(user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    cur = dbm.get_setting(con, "alerts_enabled", "0")
    dbm.set_setting(con, "alerts_enabled", "0" if cur == "1" else "1")
    return RedirectResponse("/admin/settings", status_code=303)


def _save_slack_settings(con, webhook: str, bot: str, channel: str) -> None:
    dbm.set_setting(con, "slack_webhook_url", webhook.strip())
    dbm.set_setting(con, "slack_bot_token", bot.strip())
    dbm.set_setting(con, "slack_channel_id", channel.strip())


@app.post("/admin/settings/slack")
def save_slack(slack_webhook_url: str = Form(""), slack_bot_token: str = Form(""),
               slack_channel_id: str = Form(""),
               user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    _save_slack_settings(con, slack_webhook_url, slack_bot_token, slack_channel_id)
    return RedirectResponse("/admin/settings", status_code=303)


@app.post("/admin/settings/slack-test")
def slack_test(slack_webhook_url: str = Form(""), slack_bot_token: str = Form(""),
               slack_channel_id: str = Form(""),
               user=Depends(require_admin), con: sqlite3.Connection = Depends(db_dep)):
    _save_slack_settings(con, slack_webhook_url, slack_bot_token, slack_channel_id)
    con.commit()
    msg = "Aprendio Scorecard: test message. Alerts are wired up."
    if slack_webhook_url.strip():
        alerts.post_channel(slack_webhook_url.strip(), msg)
    elif slack_bot_token.strip() and slack_channel_id.strip():
        alerts.post_channel_bot(slack_bot_token.strip(), slack_channel_id.strip(), msg)
    return RedirectResponse("/admin/settings", status_code=303)
